/**
 * Project Hermes `model.options` into the catalog the omp shell already renders.
 *
 * The shell's pickers, status line and `/model` hub read one `ModelRegistry`; in the gateway path the
 * authority owns credentials and routing, so the registry is only a **display catalog**. This module
 * replaces the bundled rows of every provider the gateway advertises with the gateway's own rows and
 * installs an in-memory availability marker, so the pickers list what Hermes can actually run while
 * nothing is written to the omp agent database (`docs/omp-ui-panels-gateway-plan.md` §0.1: the UI stays
 * the display layer; Hermes keeps the credentials and routing).
 *
 * The wire shapes are `ModelOptionsResult` (`docs/appendix-omp-ui-business-rpc.md` B.6). Fields the
 * gateway leaves `null` (context window, max tokens, cost) fall back to display-only defaults; the
 * session's own `session.info.context_length` remains the authority for the live status line.
 */

import { ModelRegistry } from "../../config/model-registry"
import type { Api, Model } from "../../vendor/ai/src/index.ts"
import type { ModelKind } from "../../vendor/catalog/src/types.ts"
import { Effort } from "../../vendor/catalog/src/effort.ts"
import type { ModelOptionsResult, ModelInfoEntry } from "./gateway-contract.generated"

/** The provider's models are executed by the authority, never by this process. */
const DISPLAY_API: Api = "openai-completions"
/** An unroutable placeholder: nothing in the gateway path sends a model request from here. */
const DISPLAY_BASE_URL = "http://127.0.0.1:9/v1"
/** In-memory config key; `AuthStorage` never persists it because the shell opens `:memory:`. */
const GATEWAY_KEY = "hermes-gateway"
const DEFAULT_CONTEXT_WINDOW = 200_000
const DEFAULT_MAX_TOKENS = 32_768
const EFFORT_LEVELS: Readonly<Record<string, Effort>> = {
	minimal: Effort.Minimal,
	low: Effort.Low,
	medium: Effort.Medium,
	high: Effort.High,
	xhigh: Effort.XHigh,
	max: Effort.Max
}

/** One provider's replacement rows, ready for `ModelRegistry.registerProvider`. */
export interface GatewayProviderRegistration {
	provider: string
	authenticated: boolean
	baseUrl: string
	models: Array<{
		id: string
		name: string
		api: Api
		reasoning: boolean
		input: ("text" | "image")[]
		cost: { input: number; output: number; cacheRead: number; cacheWrite: number }
		contextWindow: number
		maxTokens: number
		thinking?: { mode: "effort"; efforts: Effort[] }
	}>
}

const positive = (value: number | null | undefined, fallback: number): number =>
	typeof value === "number" && Number.isFinite(value) && value > 0 ? value : fallback

const modelInputs = (entry: ModelInfoEntry): ("text" | "image")[] => {
	const inputs = (entry.input ?? []).filter((value): value is "text" | "image" => value === "text" || value === "image")
	return inputs.length > 0 ? [...new Set(inputs)] : ["text"]
}

const thinkingOf = (entry: ModelInfoEntry): GatewayProviderRegistration["models"][number]["thinking"] => {
	const efforts = (entry.thinking?.levels ?? []).flatMap((level) => {
		const effort = EFFORT_LEVELS[level]
		return effort ? [effort] : []
	})
	return efforts.length > 0 ? { mode: "effort", efforts } : undefined
}

/**
 * Group `model.options.models` by provider and attach the provider's authentication verdict.
 *
 * Providers with no model rows are skipped: the picker can only offer rows it can render, and an
 * unauthenticated provider without models carries nothing to select (Hermes `/login` owns adding it).
 */
export function planGatewayProviders(result: ModelOptionsResult): GatewayProviderRegistration[] {
	const byProvider = new Map<string, ModelInfoEntry[]>()
	for (const entry of result.models ?? []) {
		const provider = entry.provider?.trim()
		if (!provider) continue
		const rows = byProvider.get(provider) ?? []
		rows.push(entry)
		byProvider.set(provider, rows)
	}
	const registrations: GatewayProviderRegistration[] = []
	for (const provider of result.providers ?? []) {
		const rows = byProvider.get(provider.slug)
		if (!rows || rows.length === 0) continue
		const thinking = new Map(rows.map((row) => [row.id, thinkingOf(row)]))
		registrations.push({
			provider: provider.slug,
			authenticated: provider.authenticated === true,
			baseUrl: provider.api_url?.trim() || DISPLAY_BASE_URL,
			models: rows.map((entry) => ({
				id: entry.id,
				name: entry.name?.trim() || entry.id,
				api: DISPLAY_API,
				reasoning: entry.reasoning === true,
				input: modelInputs(entry),
				cost: {
					input: entry.cost?.input ?? 0,
					output: entry.cost?.output ?? 0,
					cacheRead: entry.cost?.cache_read ?? 0,
					cacheWrite: entry.cost?.cache_write ?? 0
				},
				contextWindow: positive(entry.context_window, DEFAULT_CONTEXT_WINDOW),
				maxTokens: positive(entry.max_tokens, DEFAULT_MAX_TOKENS),
				...(thinking.get(entry.id) ? { thinking: thinking.get(entry.id) } : {})
			}))
		})
	}
	return registrations
}

/**
 * Replace the bundled rows of every gateway provider with the authority's rows.
 *
 * `registerProvider` installs (or replaces) one provider's overlay for the lifetime of the registry,
 * so repeated calls after a refresh stay idempotent. Only a provider the authority reports as
 * authenticated gets the in-memory availability marker — an unauthenticated provider stays visible
 * in the hub with its "sign in" affordance instead of pretending it can run.
 *
 * A {@link GatewayModelRegistry} is also told which providers are exclusive, so its published lists
 * cannot keep offering bundled/keyless rows the authority does not serve.
 */
export function installGatewayModelCatalog(
	registry: GatewayCatalogRegistry,
	result: ModelOptionsResult,
	sourceId = "hermes-gateway"
): GatewayProviderRegistration[] {
	const registrations = planGatewayProviders(result)
	for (const registration of registrations) {
		registry.registerProvider(
			registration.provider,
			{
				baseUrl: registration.baseUrl,
				api: DISPLAY_API,
				...(registration.authenticated ? { apiKey: GATEWAY_KEY } : {}),
				models: registration.models
			},
			sourceId
		)
	}
	registry.setGatewayProviders?.(registrations.map((registration) => registration.provider))
	return registrations
}

/** The catalog surface `installGatewayModelCatalog` needs; a plain registry still works. */
export interface GatewayCatalogRegistry extends Pick<ModelRegistry, "registerProvider"> {
	/** Present on {@link GatewayModelRegistry}; makes the published catalog authority-exclusive. */
	setGatewayProviders?(providers: Iterable<string>): void
}

/** Keep only rows whose provider the authority advertises (case-insensitive slug match). */
export function filterToGatewayProviders<T extends { provider: string }>(
	models: readonly T[],
	providers: ReadonlySet<string>
): T[] {
	return models.filter((model) => providers.has(model.provider.toLowerCase()))
}

/**
 * `ModelRegistry` whose published lists are the authority's catalog.
 *
 * The shell still uses the registry for rendering, pricing and context accounting, but the gateway
 * is the only backend: bundled rows of providers the gateway did not advertise (and keyless
 * `local`/`web` rows) must not reach pickers or model cycling, or selecting one would reach
 * `session.mutate` with a model the authority refuses. Before the first catalog install the
 * provider set is empty and every list behaves as before, so boot-time resolution is unaffected.
 */
export class GatewayModelRegistry extends ModelRegistry {
	#gatewayProviders: ReadonlySet<string> | undefined

	setGatewayProviders(providers: Iterable<string>): void {
		this.#gatewayProviders = new Set([...providers].map((provider) => provider.toLowerCase()))
	}

	#admitProvider(provider: string): boolean {
		const providers = this.#gatewayProviders
		if (providers === undefined) return true
		return providers.has(provider.toLowerCase())
	}

	#admit(models: readonly Model<Api>[]): Model<Api>[] {
		return this.#gatewayProviders === undefined
			? [...models]
			: filterToGatewayProviders(models, this.#gatewayProviders)
	}

	override getAll(kind: ModelKind | "all" = "chat"): Model<Api>[] {
		return this.#admit(super.getAll(kind))
	}

	override getAvailable(kind: ModelKind | "all" = "chat"): Model<Api>[] {
		return this.#admit(super.getAvailable(kind))
	}

	override getAvailableForProviders(
		providers: ReadonlySet<string>,
		kind: ModelKind | "all" = "chat"
	): Model<Api>[] {
		const gateways = this.#gatewayProviders
		if (gateways === undefined || gateways.size === 0) return super.getAvailableForProviders(providers, kind)
		const allowed = new Set([...providers].filter((provider) => gateways.has(provider.toLowerCase())))
		return allowed.size === 0 ? [] : super.getAvailableForProviders(allowed, kind)
	}

	override find(provider: string, modelId: string): Model<Api> | undefined {
		return this.#admitProvider(provider) ? super.find(provider, modelId) : undefined
	}

	override hasConfiguredAuth(model: Model<Api>): boolean {
		return this.#admitProvider(model.provider) && super.hasConfiguredAuth(model)
	}
}
