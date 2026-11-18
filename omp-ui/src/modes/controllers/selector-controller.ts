/**
 * The overlay selectors the Hermes gateway shell keeps.
 *
 * Every row comes from the gateway session or the shell's own settings: the model picker
 * reads the model registry (`session.modelRegistry`/`scopedModels`) and mutates the session
 * through `session.setModel`; the resume picker reads `session.listSessions`; the copy
 * picker walks the projected transcript; the settings panel edits the shell's `Settings`.
 * There is no local session store, provider catalog, role registry or plugin backend here.
 */
import { ThinkingLevel } from "../../vendor/agent/src/index.ts";
import type { Component, OverlayHandle } from "@oh-my-pi/pi-tui";
import { type TranscriptEntryLike } from "@oh-my-pi/pi-tui/chat/transcript-entry";
import { CopySelectorComponent } from "@oh-my-pi/pi-tui/overlays/copy-selector";
import {
	ModelHubComponent,
	type ModelHubCallbacks,
	type ModelHubRegistry,
} from "@oh-my-pi/pi-tui/overlays/model-hub";
import { ModelPickerComponent, type ModelPickerRegistry } from "@oh-my-pi/pi-tui/overlays/model-picker";
import type { PluginSettingsHost } from "@oh-my-pi/pi-tui/overlays/plugin-settings";
import { SessionSelectorComponent, type SessionSelectorEntry } from "@oh-my-pi/pi-tui/overlays/session-selector";
import type { SettingsDisplayEntry, SettingsHost } from "@oh-my-pi/pi-tui/overlays/settings-defs";
import {
	SettingsSelectorComponent,
	type StatusLinePreviewSettings,
} from "@oh-my-pi/pi-tui/overlays/settings-selector";
import { ThinkingSelectorComponent } from "@oh-my-pi/pi-tui/overlays/thinking-selector";
import type { StatusLineSettings } from "@oh-my-pi/pi-tui/status-line/types";
import { getAvailableThemes, previewTheme } from "@oh-my-pi/pi-tui/theme";
import { AUTO_THINKING, type ConfiguredThinkingLevel, parseConfiguredThinkingLevel } from "@oh-my-pi/pi-tui/thinking";
import { all, lookup, type AnySetting } from "../../config/registry";
import type { Settings } from "../../config/settings";
import type { InteractiveModeContext, SelectOnlyModelSelectorOptions } from "../types";
import type { Model } from "../../vendor/ai/src/index.ts";
import type { SessionListRow, SessionTranscriptFile } from "../rpc/gateway-contract.generated";
import type { GatewayOmpSession } from "../rpc/gateway-session";
import { createHermesModelHubSource } from "../model-hub-source";
import { createModelBrowserSource } from "../model-browser-source";
import type { ModelRoleStore, RolesThinkingLevel } from "../rpc/gateway-roles";
import { copyToClipboard } from "../../utils/clipboard";
import { openPath } from "../../utils/open";

/** `defaultThinkingLevel` (the settings-panel row that also switches the live session). */
const DEFAULT_THINKING_LEVEL_SETTING = "defaultThinkingLevel";
/** Archived rows whose bundle preview is hydrated per picker open (ADR-16 P4). */
const MAX_PREVIEW_HYDRATIONS = 20;
/** Messages read for that preview — the first user turn is enough to identify the row. */
const PREVIEW_PAGE_MESSAGES = 20;

/**
 * The shell has no plugin backend: the vendored settings panel always shows a Plugins tab,
 * and an empty installed list is the truth for this client. Plugin installation is the
 * profile's business, not the terminal shell's.
 */
const NO_PLUGINS: PluginSettingsHost = {
	manager: {
		list: async () => [],
		getPlugin: async () => undefined,
		getPluginSettings: async () => ({}),
		setEnabled: async () => {},
		getEnabledFeatures: async () => null,
		setEnabledFeatures: async () => {},
		setPluginSetting: async () => {},
	},
	createMarketplaceManager: async () => ({
		listInstalledPlugins: async () => [],
		setPluginEnabled: async () => {},
	}),
	parsePluginId: () => null,
};

/** Per-provider request limits floored to whole numbers ≥ 1; malformed entries are dropped. */
function normalizeProviderLimits(value: unknown): Record<string, number> {
	const limits: Record<string, number> = {};
	if (typeof value !== "object" || value === null || Array.isArray(value)) return limits;
	for (const provider in value) {
		const limit = (value as Record<string, unknown>)[provider];
		if (typeof limit === "number" && Number.isFinite(limit) && limit > 0) {
			limits[provider] = Math.max(1, Math.floor(limit));
		}
	}
	return limits;
}

/** {@link normalizeProviderLimits}, but a non-positive entry fails the write instead of being dropped. */
function validateProviderLimits(value: unknown): Record<string, number> {
	if (typeof value === "object" && value !== null && !Array.isArray(value)) {
		const invalid = Object.keys(value).filter(provider => {
			const limit = (value as Record<string, unknown>)[provider];
			return !(typeof limit === "number" && Number.isFinite(limit) && limit > 0);
		});
		if (invalid.length > 0) {
			throw new Error(`Provider request limits must be positive numbers: ${invalid.join(", ")}`);
		}
	}
	return normalizeProviderLimits(value);
}

/**
 * Adapt the shell's `Settings` to the settings panel: the registry is the single source of
 * paths, defaults and display metadata, so the panel lists exactly the settings this build
 * registers (the Appearance tab is written through to the gateway by `gateway-appearance`).
 */
function createShellSettingsHost(settings: Settings): SettingsHost {
	const entries: SettingsDisplayEntry[] = [];
	for (const setting of all()) {
		const ui = setting.ui;
		// Config-file-only settings declare no panel row.
		if (!ui) continue;
		entries.push({
			path: setting.id,
			type: setting.type,
			defaultValue: setting.default,
			ui,
			enumValues: setting.enumValues,
			credential: setting.isCredential,
		});
	}
	const resolve = (path: string): AnySetting => {
		const setting = lookup(path);
		if (!setting) throw new Error(`Unknown setting: ${path}`);
		return setting;
	};
	return {
		entries,
		get: path => lookup(path)?.layered(settings),
		set: (path, value) => resolve(path).set(settings, value),
		unset: path => resolve(path).unset(settings),
		normalizeProviderLimits,
		validateProviderLimits,
	};
}

/** The status line's whole settings object, with a preview's overrides applied. */
function statusLineSettings(host: SettingsHost, overrides?: StatusLinePreviewSettings): StatusLineSettings {
	const current = <T>(path: string): T | undefined => host.get(path) as T | undefined;
	return {
		preset: current<StatusLineSettings["preset"]>("statusLine.preset"),
		leftSegments: current<StatusLineSettings["leftSegments"]>("statusLine.leftSegments"),
		rightSegments: current<StatusLineSettings["rightSegments"]>("statusLine.rightSegments"),
		separator: current<StatusLineSettings["separator"]>("statusLine.separator"),
		showHookStatus: current<boolean>("statusLine.showHookStatus"),
		sessionAccent: current<boolean>("statusLine.sessionAccent"),
		transparent: current<boolean>("statusLine.transparent"),
		compactThinkingLevel: current<boolean>("statusLine.compactThinkingLevel"),
		contextLine: current<StatusLineSettings["contextLine"]>("statusLine.contextLine"),
		segmentOptions: current<StatusLineSettings["segmentOptions"]>("statusLine.segmentOptions"),
		...overrides,
	};
}

/** Where a row's transcript actually lives (ADR-16 P4). */
export interface ResumeStorageState {
	/** The profile's mirror manifest lists a bundle for this session. */
	mirrored: boolean;
	/** The message rows were pruned; resuming restores them from the bundle first. */
	archived: boolean;
	/** The bundle's file metadata, when the transcript RPC knew it. */
	transcript?: SessionTranscriptFile;
	/** Preview hydrated from the bundle (an archived row has no DB preview). */
	preview?: string;
}

/** Seconds since the epoch, or milliseconds (some stored rows already carry ms). */
function toDate(value: number | undefined): Date {
	const started = typeof value === "number" ? value : 0;
	return new Date(started > 1_000_000_000_000 ? started : started * 1000);
}

/**
 * One `session.list` row as the resume picker's entry (its `path`/`id` is the stored session id).
 *
 * `state` is absent when the transcript RPC is unavailable: the picker then claims nothing about
 * mirroring. Otherwise the storage state rides the entry's `note` (the pinned pi-tui patch renders
 * it beside date/size in both the terminal and native forms), leaving the preview real content.
 */
export function toResumeEntry(row: SessionListRow, state?: ResumeStorageState): SessionSelectorEntry {
	const id = row.resolved_id ?? row.id;
	const date = toDate(row.started_at);
	const transcript = state?.transcript;
	const preview = state?.preview ?? row.preview ?? "";
	const note = state === undefined ? "" : state.archived ? "archived" : state.mirrored ? "" : "not mirrored";
	return {
		path: id,
		id,
		cwd: "",
		title: row.title || undefined,
		created: date,
		modified: transcript?.modified_at ? toDate(transcript.modified_at) : date,
		size: transcript?.size_bytes ?? 0,
		firstMessage: preview,
		allMessagesText: preview,
		...(note ? { note } : {}),
	};
}

/** The picker's preview text from one bundle page: its first user turn, else the first message. */
export function previewFromTranscript(messages: readonly Record<string, unknown>[]): string {
	const textOf = (message: Record<string, unknown>): string => {
		const content = message.content;
		if (typeof content === "string") return content;
		if (!Array.isArray(content)) return "";
		return content
			.map(part =>
				part && typeof part === "object" && "text" in part && typeof part.text === "string" ? part.text : ""
			)
			.join(" ");
	};
	const user = messages.find(message => message.role === "user" && textOf(message).trim().length > 0);
	const chosen = user ?? messages.find(message => textOf(message).trim().length > 0);
	return chosen ? textOf(chosen).replace(/\s+/g, " ").trim() : "";
}

/** Sources the human picker never lists: delegate runs (ADR-16 P4; `sessions.show_subagents` is
 * a session-browser setting, not a reason to put a child run in the resume picker). */
const PICKER_HIDDEN_SOURCES = new Set(["subagent"]);

/** The picker's rows: the stored sessions minus non-human sources. */
export function pickerRows(rows: readonly SessionListRow[]): SessionListRow[] {
	return rows.filter(row => !PICKER_HIDDEN_SOURCES.has((row.source ?? "").toLowerCase()));
}

/** omp's configured level as the wire's effort override ("auto"/inherit mean no override). */
function toRolesThinking(level: ConfiguredThinkingLevel | undefined): RolesThinkingLevel | null {
	if (level === undefined || level === AUTO_THINKING || level === ThinkingLevel.Inherit) return "inherit";
	if (level === ThinkingLevel.Off) return "none";
	return level;
}

/** What the hub's assignment callbacks need from the shell (a fake satisfies it in tests). */
export interface ModelHubCallbackHost {
	roles: ModelRoleStore;
	session: { readonly model: Model | undefined; setModel(model: Model): Promise<void> };
	showStatus(message: string): void;
	showError(message: string): void;
	invalidateStatusLine(): void;
	requestRender(): void;
	/** Re-read roles after an asynchronous write (the hub's own refresh runs too early for it). */
	refreshHub(): void;
	close(): void;
}

/**
 * The hub's Roles callbacks over `model.roles.*`. Hermes keeps the main model on the session and
 * the `default` fallback chain under `fallback_providers`, so those two stay outside the role table.
 * Failures are surfaced with `showError` and never thrown into the overlay.
 */
export function createModelHubCallbacks(host: ModelHubCallbackHost): ModelHubCallbacks {
	const surface = (run: () => Promise<void>): void => {
		void run().catch((error: unknown) => host.showError(error instanceof Error ? error.message : String(error)));
	};
	return {
		onAssign: async (model, role, thinkingLevel) => {
			if (role === "default") {
				try {
					await host.session.setModel(model);
					host.invalidateStatusLine();
					host.showStatus(`Model: ${model.provider}/${model.id}`);
					host.requestRender();
				} catch (error) {
					host.showError(error instanceof Error ? error.message : String(error));
				}
				return;
			}
			try {
				await host.roles.assign({
					role,
					provider: model.provider,
					model: model.id,
					reasoning_effort: toRolesThinking(thinkingLevel),
				});
				host.refreshHub();
				host.showStatus(`${role}: ${model.provider}/${model.id}`);
			} catch (error) {
				host.showError(error instanceof Error ? error.message : String(error));
			}
		},
		onUnassign: (role) => {
			if (role === "default") {
				host.showStatus("The main model is switched with /model");
				return;
			}
			surface(async () => {
				await host.roles.clear(role);
				host.refreshHub();
				host.showStatus(`${role}: automatic`);
			});
		},
		onFallbackChainChange: (role, chain) => {
			// `fallback_providers` owns the default chain; `hermes fallback` edits it.
			if (role === "default") return;
			const entries = chain.flatMap((selector) => {
				const slash = selector.indexOf("/");
				if (slash <= 0 || slash === selector.length - 1) return [];
				return [{ provider: selector.slice(0, slash), model: selector.slice(slash + 1) }];
			});
			surface(async () => {
				// Role ids and model-keyed chains (`provider/model`, `provider/*`) are edited by the
				// same hub row; only the latter go through the chain-key branch of `model.roles.set`.
				const isRole = host.roles.byId(role) !== undefined;
				if (isRole) await host.roles.setFallbackChain(role, entries);
				else await host.roles.setModelChain(role, entries);
				host.refreshHub();
				host.showStatus(`${role}: ${entries.length === 0 ? "no fallbacks" : `${entries.length} fallback(s)`}`);
			});
		},
		onSavePreset: (name) => {
			surface(async () => {
				const saved = await host.roles.savePreset(name);
				// The save answer carries only the name; re-read the summary so the strip finds it.
				await host.roles.refresh();
				host.refreshHub();
				host.showStatus(`Preset saved: ${saved}`);
			});
		},
		onSwitchPreset: async (name) => {
			try {
				const rows = await host.roles.applyPreset(name);
				host.refreshHub();
				host.showStatus(`Preset applied: ${name} (${rows.length} role${rows.length === 1 ? "" : "s"})`);
			} catch (error) {
				host.showError(error instanceof Error ? error.message : String(error));
			}
		},
		onCycleOrderChange: (order) => {
			surface(async () => {
				const saved = await host.roles.setCycleOrder(order);
				host.refreshHub();
				host.showStatus(saved.length === 0 ? "Model cycle: off" : `Model cycle: ${saved.join(" → ")}`);
			});
		},
		onCancel: host.close,
	};
}

/** Menus that open at most once: a repeat request focuses the open one. */
type MenuKind = "settings" | "model-picker" | "model-picker-select" | "model-hub";

/** An open menu: registered when the request runs, filled once its component mounts. */
interface OpenMenu {
	component?: Component;
	handle?: OverlayHandle;
	/** Shows `component` as a new overlay and focuses it; also raises a covered menu. */
	mount?: () => OverlayHandle;
}

export class SelectorController {
	/**
	 * Each single-instance menu from the moment it is requested until it closes,
	 * so a repeat request (typed or clicked while the first is still loading)
	 * focuses it instead of stacking another menu on top.
	 */
	#openMenus = new Map<MenuKind, OpenMenu>();

	constructor(private ctx: InteractiveModeContext) {}

	/**
	 * Mount a primary fullscreen menu (the `/settings` idiom): the overlay takes the
	 * alternate screen and enables mouse tracking for its lifetime, leaving the
	 * transcript untouched underneath.
	 */
	#showFullscreenMenu(component: Component): OverlayHandle {
		const handle = this.ctx.ui.showOverlay(component, {
			anchor: "bottom-center",
			width: "100%",
			maxHeight: "100%",
			margin: 0,
			fullscreen: true,
		});
		this.ctx.ui.setFocus(component);
		this.ctx.ui.requestRender();
		return handle;
	}

	/**
	 * Bring the open `kind` menu forward; true when one was open and the request
	 * is handled. Overlays have no raise API, so a menu another overlay covers
	 * is remounted on top rather than handed keys while hidden.
	 */
	#focusOpenMenu(kind: MenuKind): boolean {
		const open = this.#openMenus.get(kind);
		if (!open) return false;
		if (open.component && open.mount && this.#isCovered(open.component)) {
			open.handle?.hide();
			open.handle = open.mount();
		} else if (open.component) {
			this.ctx.ui.setFocus(open.component);
		}
		this.ctx.ui.requestRender();
		return true;
	}

	/** Whether a visible overlay sits above `component`'s overlay. */
	#isCovered(component: Component): boolean {
		const stack = this.ctx.ui.overlayStack;
		const at = stack.findIndex(entry => entry.component === component);
		return at >= 0 && stack.slice(at + 1).some(entry => !entry.hidden);
	}

	/** Mount `component` as `menu`'s overlay through `mount`, which also raises it later. */
	#mountMenu(menu: OpenMenu, component: Component, mount: () => OverlayHandle): void {
		menu.component = component;
		menu.mount = mount;
		menu.handle = mount();
	}

	/** Register a new `kind` menu; pass it to {@link #releaseMenu} when it closes or fails to open. */
	#claimMenu(kind: MenuKind): OpenMenu {
		const menu: OpenMenu = {};
		this.#openMenus.set(kind, menu);
		return menu;
	}

	#releaseMenu(kind: MenuKind, menu: OpenMenu): void {
		if (this.#openMenus.get(kind) === menu) this.#openMenus.delete(kind);
	}

	/**
	 * Restore keyboard focus to whatever currently owns the editor slot. The
	 * slot can hold the editor itself or a selector pushed in by another
	 * controller; `overlayHandle.hide()` restores focus to the component
	 * focused when the overlay opened, which is stale once the slot was
	 * swapped out. Call this after the overlay hides to re-target focus at the
	 * visible slot owner.
	 */
	focusActiveEditorArea(): void {
		const visible = this.ctx.editorContainer.children[0] ?? this.ctx.editor;
		this.ctx.ui.setFocus(visible);
	}

	/**
	 * Temporarily replaces the editor slot with a selector, restoring the prior
	 * slot contents and focus when the selector finishes.
	 */
	showSelector(create: (done: () => void) => { component: Component; focus: Component }): void {
		const previousChildren = [...this.ctx.editorContainer.children];
		const previousFocus = this.ctx.ui.getFocused();
		const done = () => {
			this.ctx.editorContainer.clear();
			for (const child of previousChildren) this.ctx.editorContainer.addChild(child);
			const focus =
				previousFocus && previousChildren.includes(previousFocus)
					? previousFocus
					: (previousChildren[0] ?? this.ctx.editor);
			this.ctx.ui.setFocus(focus);
		};
		const { component, focus } = create(done);
		this.ctx.editorContainer.clear();
		this.ctx.editorContainer.addChild(component);
		this.ctx.ui.setFocus(focus);
		this.ctx.ui.requestRender();
	}

	showSettingsSelector(): void {
		if (this.#focusOpenMenu("settings")) return;
		const menu = this.#claimMenu("settings");
		const host = createShellSettingsHost(this.ctx.settings);
		getAvailableThemes()
			.then(availableThemes => {
				const done = () => {
					menu.handle?.hide();
					this.#releaseMenu("settings", menu);
					this.focusActiveEditorArea();
					this.ctx.ui.requestRender();
				};
				const selector = new SettingsSelectorComponent(
					{
						settings: host,
						plugins: NO_PLUGINS,
						availableThinkingLevels: [...this.ctx.session.getAvailableThinkingLevels()],
						thinkingLevel: this.ctx.session.thinkingLevel,
						availableThemes,
						providers: [...new Set(this.ctx.session.getAvailableModels().map(model => model.provider))].sort(
							(a, b) => a.localeCompare(b),
						),
						model: this.ctx.session.model,
						imageBudget: this.ctx.ui.imageBudget,
						requestRender: () => this.ctx.ui.requestRender(),
						composerPreviewStatus: this.ctx.statusLine,
					},
					{
						onChange: (id, value) => this.handleSettingChange(id, value),
						onThemePreview: async themeName => {
							const result = await previewTheme(themeName);
							if (result.success) {
								this.ctx.statusLine.invalidate();
								this.ctx.ui.invalidate();
								this.ctx.ui.requestRender();
							}
						},
						onStatusLinePreview: previewSettings => {
							this.ctx.statusLine.updateSettings(statusLineSettings(host, previewSettings));
							this.ctx.ui.requestRender();
						},
						getStatusLinePreview: () => {
							// The bar exactly as the active composer shape renders it (box top
							// border, claude rule + chip, or the plain standalone bottom bar).
							const availableWidth = this.ctx.editor.getTopBorderAvailableWidth(this.ctx.ui.terminal.columns);
							return this.ctx.statusLine.getPreviewLines(availableWidth).join("\n");
						},
						describeStatusLinePreview: () => this.ctx.statusLine.describePreview(),
						onCancel: () => {
							done();
							// Previews never wrote to `Settings`; re-read the stored values.
							this.ctx.statusLine.updateSettings(statusLineSettings(host));
							this.ctx.ui.requestRender();
						},
					},
				);
				this.#mountMenu(menu, selector, () => this.#showFullscreenMenu(selector));
			})
			.catch((error: unknown) => {
				// A menu that never opened must not block the next `/settings`.
				this.#releaseMenu("settings", menu);
				throw error;
			});
	}

	/**
	 * Apply the side effects of a setting change the user made in this process
	 * (settings selector, approved `cfg://` writes); the value is already stored.
	 * Only `defaultThinkingLevel` needs this: it also switches the live session,
	 * which config reloads and parent-session writes must not do. Every other
	 * setting applies through handle listeners owned by the session and InteractiveMode.
	 */
	handleSettingChange(id: string, value: unknown): void {
		if (id !== DEFAULT_THINKING_LEVEL_SETTING || typeof value !== "string") return;
		const level = parseConfiguredThinkingLevel(value);
		if (level === undefined || level === this.ctx.session.configuredThinkingLevel()) return;
		this.ctx.session.setThinkingLevel(level);
	}

	/**
	 * The model picker: the registry's catalog (or the `--models` scope), the session's
	 * current model highlighted. Picking mutates the gateway session through
	 * `session.setModel`, which is a session-scoped choice — the shell owns no role defaults.
	 * `temporaryOnly` is accepted for call-site compatibility and selects the same path.
	 */
	showModelSelector(options?: { temporaryOnly?: boolean; selectOnly?: SelectOnlyModelSelectorOptions }): void {
		const selectOnly = options?.selectOnly;
		const kind: MenuKind = selectOnly ? "model-picker-select" : "model-picker";
		// A selection-only picker replaces an open session picker (and vice versa) instead of
		// stacking two lists the caller cannot tell apart.
		const conflicting: MenuKind = selectOnly ? "model-picker" : "model-picker-select";
		const open = this.#openMenus.get(conflicting);
		if (open) {
			open.handle?.hide();
			this.#releaseMenu(conflicting, open);
		}
		if (this.#focusOpenMenu(kind)) return;

		const menu = this.#claimMenu(kind);
		const session = this.ctx.session;
		const registry: ModelPickerRegistry = {
			getAvailable: kind => session.modelRegistry.getAvailable(kind),
			getAll: kind => session.modelRegistry.getAll(kind),
			// The authority owns the catalog: there is no local refresh to schedule, and no
			// discovery error of the shell's own to report.
			getError: () => undefined,
			refreshIfStale: async () => false,
		};
		const done = () => {
			if (this.#openMenus.get(kind) !== menu) return;
			menu.handle?.hide();
			this.#releaseMenu(kind, menu);
			this.focusActiveEditorArea();
			this.ctx.ui.requestRender();
		};
		const picker = new ModelPickerComponent(
			this.ctx.ui,
			createModelBrowserSource(this.ctx.settings),
			registry,
			session.scopedModels,
			{
				onPick: async (model, selector) => {
					done();
					try {
						if (selectOnly) {
							await selectOnly.onSelect(selector);
							return;
						}
						await session.setModel(model);
						this.ctx.statusLine.invalidate();
						this.ctx.showStatus(`Model: ${selector}`);
					} catch (error) {
						this.ctx.showError(error instanceof Error ? error.message : String(error));
					}
				},
				onCancel: done,
			},
			{
				currentSelector: session.model ? `${session.model.provider}/${session.model.id}` : undefined,
				currentContextTokens: session.getContextUsage()?.tokens,
				selectOnly: selectOnly !== undefined,
			},
		);
		this.#mountMenu(menu, picker, () => {
			const handle = this.ctx.ui.showOverlay(picker, {
				anchor: "bottom-center",
				width: "100%",
				maxHeight: "100%",
				margin: 0,
			});
			this.ctx.ui.setFocus(picker);
			return handle;
		});
		this.ctx.ui.requestRender();
	}

	/**
	 * The `/models` hub: omp's full-screen model browser with a Roles view backed by Hermes'
	 * `auxiliary.<task>` table. `/model` remains the session picker; only role writes leave the shell.
	 */
	showModelHub(): void {
		const kind: MenuKind = "model-hub";
		if (this.#focusOpenMenu(kind)) return;
		const gateway = this.ctx.session.gateway;
		if (!gateway) {
			this.ctx.showError("This session is not connected to the Hermes gateway.");
			return;
		}
		const menu = this.#claimMenu(kind);
		const session = this.ctx.session;
		let hub: ModelHubComponent | undefined;
		const done = () => {
			if (this.#openMenus.get(kind) !== menu) return;
			menu.handle?.hide();
			hub?.dispose();
			this.#releaseMenu(kind, menu);
			this.focusActiveEditorArea();
			this.ctx.ui.requestRender();
		};
		void (async () => {
			try {
				await gateway.roles.refresh();
			} catch (error) {
				this.#releaseMenu(kind, menu);
				this.ctx.showError(`Failed to load model roles: ${error instanceof Error ? error.message : String(error)}`);
				return;
			}
			if (this.#openMenus.get(kind) !== menu) return;
			const source = createHermesModelHubSource(gateway.roles, this.ctx.settings, session);
			const registry: ModelHubRegistry = {
				getAvailable: (entryKind) => session.modelRegistry.getAvailable(entryKind),
				getAll: (entryKind) => session.modelRegistry.getAll(entryKind),
				// The authority owns the catalog: no local discovery error or refresh exists here.
				getError: () => undefined,
				find: (provider, id) => session.modelRegistry.find(provider, id),
				authStorage: { keys: { source: () => undefined } },
				getDiscoverableProviders: () => [
					...new Set(session.modelRegistry.getAll("all").map((model) => model.provider)),
				],
				getProviderDiscoveryState: () => undefined,
				refresh: async () => {},
				refreshProvider: async () => {},
			};
			const component = new ModelHubComponent(
				this.ctx.ui,
				source,
				registry,
				session.scopedModels,
				createModelHubCallbacks({
					roles: gateway.roles,
					session: {
						get model() {
							return session.model;
						},
						setModel: (model) => session.setModel(model),
					},
					showStatus: (message) => this.ctx.showStatus(message),
					showError: (message) => this.ctx.showError(message),
					invalidateStatusLine: () => this.ctx.statusLine.invalidate(),
					requestRender: () => this.ctx.ui.requestRender(),
					refreshHub: () => hub?.refreshAfterExternalMutation(),
					close: done,
				}),
				{
					currentSelector: session.model ? `${session.model.provider}/${session.model.id}` : undefined,
				},
			);
			hub = component;
			this.#mountMenu(menu, component, () => this.#showFullscreenMenu(component));
			this.ctx.ui.requestRender();
		})();
	}

	/**
	 * Copy picker over the transcript the shell renders. Entries are the session's projected
	 * messages, chained in transcript order so the picker groups turns the way they are shown.
	 */
	showCopySelector(): void {
		const entries = this.#transcriptEntries();
		if (entries.length === 0) {
			this.ctx.showStatus("Nothing to copy yet.");
			return;
		}

		const done = () => {
			overlayHandle?.hide();
			selector?.dispose();
			this.focusActiveEditorArea();
			this.ctx.ui.requestRender();
		};
		const selector = new CopySelectorComponent(entries, {
			ui: this.ctx.ui,
			cwd: this.ctx.sessionManager.getCwd(),
			hideThinkingBlock: () => this.ctx.hideThinkingBlock,
			proseOnlyThinking: () => this.ctx.proseOnlyThinking,
			expandThinkingBlocks: () => this.ctx.expandThinkingBlocks,
			requestRender: () => this.ctx.ui.requestRender(),
			onPick: (content, label) => {
				done();
				if (!content.trim()) {
					this.ctx.showStatus("Nothing to copy in that item");
					return;
				}
				void copyToClipboard(content);
				this.ctx.showStatus(`Copied ${label} to clipboard`);
			},
			onOpen: (href, label) => {
				done();
				openPath(href);
				this.ctx.showStatus(`Opening ${label}: ${href}`);
			},
			onCancel: done,
		});
		if (selector.targetCount === 0) {
			selector.dispose();
			this.ctx.showStatus("Nothing to copy yet.");
			return;
		}
		const overlayHandle = this.ctx.ui.showOverlay(selector, {
			anchor: "bottom-center",
			width: "100%",
			maxHeight: "100%",
			margin: 0,
			fullscreen: true,
		});
		this.ctx.ui.setFocus(selector);
		this.ctx.ui.requestRender();
	}

	/** The session's messages as transcript entries (a linear chain: the shell keeps no tree). */
	#transcriptEntries(): TranscriptEntryLike[] {
		return this.ctx.sessionManager.getMessages().map((message, index) => ({
			type: "message" as const,
			id: String(index),
			parentId: index === 0 ? null : String(index - 1),
			timestamp: new Date(message.timestamp ?? Date.now()).toISOString(),
			message,
		}));
	}

	/**
	 * Bundle previews for `archived` rows (ADR-16 P4): their message rows were pruned, so the DB
	 * preview is empty and the picker would show a bare id. One bounded page from
	 * `session.transcript.read` gives the row something to identify itself by; an unreadable
	 * bundle keeps the row (the picker still marks it `archived`) and resume reports the reason.
	 */
	async #archivedPreviews(
		gateway: GatewayOmpSession,
		states: readonly { row: SessionListRow; transcript?: SessionTranscriptFile }[],
	): Promise<Map<string, string>> {
		const previews = new Map<string, string>();
		const pending = states
			.filter(({ row, transcript }) => row.restore_required && transcript && !row.preview)
			.slice(0, MAX_PREVIEW_HYDRATIONS);
		await Promise.all(
			pending.map(async ({ row }) => {
				const id = row.resolved_id ?? row.id;
				try {
					const page = await gateway.readTranscript(id, 0, PREVIEW_PAGE_MESSAGES);
					const preview = previewFromTranscript(page.messages);
					if (preview) previews.set(id, preview);
				} catch {
					// The bundle is missing or unreadable: resume reports it; the row stays listed.
				}
			})
		);
		return previews;
	}

	/**
	 * The gateway's resume list (`session.list`), annotated with the profile's cold mirror
	 * (`session.transcript.list`, ADR-16 P4). Selecting a row hands its stored session id to the
	 * shell, which resumes it over a fresh connection; an `archived` row's resume restores its
	 * message rows from the bundle first and its failure (missing / checksum-mismatched file)
	 * surfaces here. The shell keeps no local table and reads no host path.
	 */
	async showSessionSelector(): Promise<void> {
		const gateway = this.ctx.session.gateway;
		if (!gateway) {
			this.ctx.showError("This session is not connected to the Hermes gateway.");
			return;
		}
		let rows: SessionListRow[];
		try {
			rows = await gateway.listSessions();
		} catch (error) {
			this.ctx.showError(`Failed to list Hermes sessions: ${error instanceof Error ? error.message : String(error)}`);
			return;
		}
		if (rows.length === 0) {
			this.ctx.showWarning("No Hermes sessions found");
			return;
		}

		const transcripts = await gateway.listTranscripts().catch(() => null);
		const byId = new Map<string, SessionTranscriptFile>();
		for (const file of transcripts ?? []) byId.set(file.session_id, file);
		const states = pickerRows(rows).map(row => {
			const transcript = byId.get(row.resolved_id ?? row.id) ?? byId.get(row.id);
			return { row, transcript } as const;
		});
		const previews = await this.#archivedPreviews(gateway, states);
		const sessions = states.map(({ row, transcript }) =>
			toResumeEntry(row, transcripts === null ? undefined : {
				mirrored: transcript !== undefined,
				archived: Boolean(row.restore_required),
				transcript,
				preview: previews.get(row.resolved_id ?? row.id) ?? previews.get(row.id),
			})
		);
		// Keep the fullscreen picker on the alternate buffer while the selected session is
		// resumed and its transcript is rebuilt.
		const done = () => {
			overlayHandle?.hide();
			this.focusActiveEditorArea();
			this.ctx.ui.requestRender();
		};
		const selector = new SessionSelectorComponent(
			sessions,
			async session => {
				selector.lockInput();
				done();
				try {
					await this.ctx.handleResumeSession(session.id);
				} catch (error) {
					this.ctx.showError(error instanceof Error ? error.message : String(error));
				}
			},
			done,
			done,
			{
				title: "Resume Hermes Session",
				scopeLabel: false,
				showCwd: false,
				getTerminalRows: () => this.ctx.ui.terminal.rows,
				fillHeight: true,
			},
		);
		selector.setOnRequestRender(() => this.ctx.ui.requestRender());
		const overlayHandle = this.ctx.ui.showOverlay(selector, {
			anchor: "top-left",
			width: "100%",
			maxHeight: "100%",
			margin: 0,
			fullscreen: true,
		});
		this.ctx.ui.setFocus(selector);
		this.ctx.ui.requestRender();
	}

	/** Open the thinking-level picker for the session's model. */
	showThinkingSelector(): void {
		const session = this.ctx.session;
		const configured = session.configuredThinkingLevel();
		this.showSelector(done => {
			const selector = new ThinkingSelectorComponent(
				configured === ThinkingLevel.Inherit ? ThinkingLevel.Off : configured,
				session.getAvailableEffortSelectors(),
				(level: ConfiguredThinkingLevel) => {
					done();
					// thinking_level_changed refreshes the status line and editor border.
					session.setThinkingLevel(level);
					this.ctx.ui.requestRender();
				},
				() => {
					done();
					this.ctx.ui.requestRender();
				},
			);
			return { component: selector, focus: selector.getSelectList() };
		});
	}
}
