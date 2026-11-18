/**
 * The model catalog the shell renders.
 *
 * In the gateway path Hermes owns credentials and routing, so this registry is a **display catalog**
 * only: bundled catalog rows plus the rows a provider installs through {@link ModelRegistry.registerProvider}
 * (see `modes/rpc/gateway-models.ts`). Nothing here discovers endpoints, reads `models.yml`, or sends a
 * model request — an installed provider is the authority's inventory, and its `apiKey` marker is what
 * makes it selectable.
 */

import type { AuthApiKeyOptions } from "../vendor/ai/src/auth-storage.ts";
import type { AuthStorage } from "../vendor/ai/src/auth-storage.ts";
import { OAuthRefreshUnavailableError } from "../vendor/ai/src/error/index.ts";
import type { Api, Model } from "../vendor/ai/src/types.ts";
import { buildModel } from "../vendor/catalog/src/build.ts";
import { getBundledModels, getBundledProviders } from "../vendor/catalog/src/models.ts";
import { modelKind, type ModelKind, type ModelSpec, type ThinkingConfig } from "../vendor/catalog/src/types.ts";
import { resolveProviderModelReference } from "./model-resolver";
import { cfgDisabledProviders } from "./model-settings";
import { type Settings, settings } from "./settings";

/** One published catalog row of a provider registration. */
export interface ProviderModelConfig {
	id: string;
	name: string;
	api?: Api;
	kind?: ModelKind;
	baseUrl?: string;
	reasoning: boolean;
	thinking?: ThinkingConfig;
	input: ("text" | "image")[];
	cost: { input: number; output: number; cacheRead: number; cacheWrite: number };
	contextWindow: number;
	maxTokens: number;
	headers?: Record<string, string>;
	compat?: ModelSpec<Api>["compat"];
}

/**
 * A provider's display rows and transport identity, as installed by
 * {@link ModelRegistry.registerProvider}.
 */
export interface ProviderConfigInput {
	baseUrl?: string;
	api?: Api;
	/**
	 * Credential marker. In the gateway path this is never sent anywhere: it only tells the pickers
	 * that the authority can run the provider (`hermes-gateway` in `modes/rpc/gateway-models.ts`).
	 */
	apiKey?: string;
	headers?: Record<string, string>;
	compat?: ModelSpec<Api>["compat"];
	/** Replaces every bundled row of the provider; omitted or empty keeps the bundled rows. */
	models?: ProviderModelConfig[];
}

export interface ModelRegistryOptions {
	/** Settings source for provider availability (`disabledProviders`). */
	settings?: Settings;
}

/** Rows of a provider the shell must not offer, per `disabledProviders`. */
function disabledProviderIds(settingsInstance?: Settings): ReadonlySet<string> {
	const configured = cfgDisabledProviders.get(settingsInstance ?? settings);
	return new Set(configured.map(provider => provider.trim().toLowerCase()).filter(Boolean));
}

function modelFromRow(provider: string, providerConfig: ProviderConfigInput, row: ProviderModelConfig): Model<Api> {
	const api = row.api ?? providerConfig.api;
	if (!api) throw new Error(`Provider ${provider}, model ${row.id}: no "api" specified.`);
	const headers = row.headers ?? providerConfig.headers;
	const compat = row.compat ?? providerConfig.compat;
	const spec: ModelSpec<Api> = {
		provider,
		id: row.id,
		name: row.name,
		api,
		baseUrl: row.baseUrl ?? providerConfig.baseUrl ?? "",
		reasoning: row.reasoning,
		input: row.input,
		cost: row.cost,
		contextWindow: row.contextWindow,
		maxTokens: row.maxTokens,
		...(row.kind ? { kind: row.kind } : {}),
		...(row.thinking ? { thinking: row.thinking } : {}),
		...(headers ? { headers } : {}),
		...(compat ? { compat } : {}),
	};
	return buildModel(spec);
}

/**
 * Model registry — the published catalog of one session: bundled rows, with every registered
 * provider's rows replacing its bundled ones.
 */
export class ModelRegistry {
	readonly authStorage: AuthStorage;
	#settings: Settings | undefined;
	#models: Model<Api>[] = [];
	#kindSnapshots = new Map<ModelKind, Model<Api>[]>();
	/** Provider name (as installed) → its registration. */
	#overlays = new Map<string, ProviderConfigInput>();
	/** Provider name → lowercase provider id, for case-insensitive lookups. */
	#providerKeys = new Map<string, string>();
	/** Providers installed with a credential marker. */
	#providerApiKeys = new Map<string, string>();
	/** Registration source id → the providers it installed, so one source can be dropped whole. */
	#sourceProviders = new Map<string, Set<string>>();
	#providerSources = new Map<string, string>();

	constructor(authStorage: AuthStorage, options: ModelRegistryOptions = {}) {
		this.authStorage = authStorage;
		this.#settings = options.settings;
		this.#models = getBundledProviders().flatMap(provider => getBundledModels(provider));
	}

	#rebuild(): void {
		const replaced = new Set(
			[...this.#overlays]
				.filter(([, config]) => config.models?.length)
				.map(([provider]) => provider.toLowerCase()),
		);
		const models: Model<Api>[] = [];
		for (const model of getBundledProviders().flatMap(provider => getBundledModels(provider))) {
			if (!replaced.has(model.provider.toLowerCase())) models.push(model);
		}
		for (const [provider, config] of this.#overlays) {
			for (const row of config.models ?? []) models.push(modelFromRow(provider, config, row));
		}
		this.#models = models;
		this.#kindSnapshots.clear();
	}

	/** The installed name of `provider` (case-insensitive), or undefined when it was never installed. */
	#installedName(provider: string): string | undefined {
		const key = provider.trim().toLowerCase();
		if (!key) return undefined;
		return this.#providerKeys.get(key);
	}

	/** Whether settings disable `provider` (`disabledProviders`). */
	#isProviderDisabled(provider: string): boolean {
		return disabledProviderIds(this.#settings).has(provider.trim().toLowerCase());
	}

	/**
	 * Install (or replace) one provider's rows. Repeating a registration is idempotent, so a refresh
	 * of the authority's inventory can be replayed without accumulating rows.
	 */
	registerProvider(providerName: string, config: ProviderConfigInput, sourceId?: string): void {
		if (!config.models?.length && config.apiKey === undefined && config.baseUrl === undefined) {
			throw new Error(`Provider ${providerName}: a registration needs models, an apiKey, or a baseUrl.`);
		}
		const key = providerName.trim().toLowerCase();
		const previous = this.#providerKeys.get(key);
		if (previous !== undefined && previous !== providerName) this.#overlays.delete(previous);
		this.#providerKeys.set(key, providerName);
		this.#overlays.set(providerName, config);

		if (config.apiKey !== undefined) {
			this.#providerApiKeys.set(providerName, config.apiKey);
			this.authStorage.keys.setConfig(providerName, config.apiKey);
		} else {
			this.#providerApiKeys.delete(providerName);
			this.authStorage.keys.removeConfig(providerName);
		}

		if (sourceId !== undefined) {
			const previousSource = this.#providerSources.get(providerName);
			if (previousSource !== undefined && previousSource !== sourceId) {
				this.#dropFromSource(previousSource, providerName);
			}
			const providers = this.#sourceProviders.get(sourceId) ?? new Set<string>();
			providers.add(providerName);
			this.#sourceProviders.set(sourceId, providers);
			this.#providerSources.set(providerName, sourceId);
		}
		this.#rebuild();
	}

	#dropFromSource(sourceId: string, providerName: string): void {
		const providers = this.#sourceProviders.get(sourceId);
		providers?.delete(providerName);
		if (providers && providers.size === 0) this.#sourceProviders.delete(sourceId);
		this.#providerSources.delete(providerName);
	}

	/** Remove one installed provider, restoring its bundled rows. */
	unregisterProvider(providerName: string): void {
		const installed = this.#installedName(providerName);
		if (installed === undefined) return;
		const sourceId = this.#providerSources.get(installed);
		if (sourceId !== undefined) this.#dropFromSource(sourceId, installed);
		this.#overlays.delete(installed);
		this.#providerKeys.delete(installed.toLowerCase());
		this.#providerApiKeys.delete(installed);
		this.authStorage.keys.removeConfig(installed);
		this.#rebuild();
	}

	/** Remove every provider one registration source installed. */
	clearSourceRegistrations(sourceIds: string | Iterable<string>): void {
		for (const sourceId of typeof sourceIds === "string" ? [sourceIds] : sourceIds) {
			for (const providerName of [...(this.#sourceProviders.get(sourceId) ?? [])]) {
				this.unregisterProvider(providerName);
			}
			this.#sourceProviders.delete(sourceId);
		}
	}

	/** All published rows of one catalog kind. The default excludes role-specific runners. */
	getAll(kind: ModelKind | "all" = "chat"): Model<Api>[] {
		if (kind === "all") return this.#models;
		let cached = this.#kindSnapshots.get(kind);
		if (!cached) {
			cached = this.#models.filter(model => modelKind(model) === kind);
			this.#kindSnapshots.set(kind, cached);
		}
		return cached;
	}

	/**
	 * Availability predicate with per-provider memoization: credential lookups resolve once per
	 * provider instead of once per row.
	 */
	#createProviderAvailabilityCheck(): (provider: string) => boolean {
		const disabled = disabledProviderIds(this.#settings);
		const byProvider = new Map<string, boolean>();
		return provider => {
			let available = byProvider.get(provider);
			if (available === undefined) {
				available = !disabled.has(provider.toLowerCase()) && this.authStorage.keys.source(provider) !== undefined;
				byProvider.set(provider, available);
			}
			return available;
		};
	}

	/** Available rows for an explicit provider set. */
	getAvailableForProviders(providers: ReadonlySet<string>, kind: ModelKind | "all" = "chat"): Model<Api>[] {
		const requested = new Set([...providers].map(provider => provider.trim().toLowerCase()).filter(Boolean));
		const isProviderAvailable = this.#createProviderAvailabilityCheck();
		return this.#models.filter(
			model =>
				requested.has(model.provider.toLowerCase()) &&
				isProviderAvailable(model.provider) &&
				(kind === "all" || modelKind(model) === kind),
		);
	}

	/** Available rows of one catalog kind: providers the authority installed with a credential. */
	getAvailable(kind: ModelKind | "all" = "chat"): Model<Api>[] {
		return this.getAvailableForProviders(new Set(this.#models.map(model => model.provider)), kind);
	}

	/** Whether a provider the authority installed can run the model. */
	hasConfiguredAuth(model: Model<Api>): boolean {
		return (
			this.#providerApiKeys.has(model.provider) ||
			this.authStorage.keys.source(model.provider, { env: "aliases" }) !== undefined
		);
	}

	/**
	 * Whether `provider` has a *concrete* credential (an installed marker or a stored login), as
	 * opposed to a sentinel that only signals an ambient credential source. Default-model selection
	 * prefers these so an ambiently-available provider never displaces the installed one.
	 */
	hasConcreteAuth(provider: string): boolean {
		return this.#providerApiKeys.has(provider) || this.authStorage.keys.source(provider)?.concrete === true;
	}

	/** Whether `providerId` has published rows or an installed registration. */
	hasProvider(providerId: string): boolean {
		const installed = this.#installedName(providerId);
		if (installed !== undefined) return true;
		const key = providerId.trim().toLowerCase();
		return this.#models.some(model => model.provider.toLowerCase() === key);
	}

	/** Find a model by provider and id; a disabled provider has no models to find. */
	find(provider: string, modelId: string): Model<Api> | undefined {
		if (this.#isProviderDisabled(provider)) return undefined;
		return resolveProviderModelReference(provider, modelId, this.getProviderModels(provider));
	}

	/** One provider's published rows, every kind. */
	getProviderModels(provider: string): Model<Api>[] {
		const key = provider.trim().toLowerCase();
		if (!key) return [];
		return this.#models.filter(model => model.provider.toLowerCase() === key);
	}

	/** Provider base URL: the installed registration first, then any published row. */
	getProviderBaseUrl(provider: string): string | undefined {
		const installed = this.#installedName(provider) ?? provider;
		const registered = this.#overlays.get(installed)?.baseUrl;
		return registered ?? this.getProviderModels(provider).find(model => model.baseUrl)?.baseUrl;
	}

	/** Resolve a model's request credential; a disabled provider never gets one. */
	async getApiKey(
		model: Model<Api>,
		sessionId?: string,
		options?: { signal?: AbortSignal },
	): Promise<string | undefined> {
		if (this.#isProviderDisabled(model.provider)) return undefined;
		return this.authStorage.keys.get(model.provider, sessionId, {
			baseUrl: model.baseUrl,
			modelId: model.id,
			accountIds: model.accountAccess && Object.keys(model.accountAccess),
			signal: options?.signal,
		});
	}

	/**
	 * Resolve a provider's credential for availability checks. A transient OAuth refresh failure
	 * resolves `undefined`, as `authStorage.keys.get` does.
	 */
	async getApiKeyForProvider(
		provider: string,
		sessionId?: string,
		options?: AuthApiKeyOptions,
	): Promise<string | undefined> {
		if (this.#isProviderDisabled(provider)) return undefined;
		const accountAccess = options?.modelId ? this.find(provider, options.modelId)?.accountAccess : undefined;
		try {
			const credential = await this.authStorage.keys.getWithCredential(provider, sessionId, {
				...options,
				accountIds: accountAccess && Object.keys(accountAccess),
			});
			return credential?.apiKey;
		} catch (error) {
			if (error instanceof OAuthRefreshUnavailableError) return undefined;
			throw error;
		}
	}

	/** Whether a model's provider is authenticated through an OAuth subscription. */
	isUsingOAuth(model: Model<Api>): boolean {
		return this.authStorage.credentials.hasOAuth(model.provider);
	}
}
