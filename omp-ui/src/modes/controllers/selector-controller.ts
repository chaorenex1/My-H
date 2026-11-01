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
import { type ConfiguredThinkingLevel, parseConfiguredThinkingLevel } from "@oh-my-pi/pi-tui/thinking";
import { all, lookup, type AnySetting } from "../../config/registry";
import type { Settings } from "../../config/settings";
import type { InteractiveModeContext, SelectOnlyModelSelectorOptions } from "../types";
import { createModelBrowserSource } from "../model-browser-source";
import { copyToClipboard } from "../../utils/clipboard";
import { openPath } from "../../utils/open";

/** `defaultThinkingLevel` (the settings-panel row that also switches the live session). */
const DEFAULT_THINKING_LEVEL_SETTING = "defaultThinkingLevel";

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

/** One `session.list` row as the resume picker's entry (its `path`/`id` is the stored session id). */
function toResumeEntry(row: {
	id: string;
	resolved_id?: string | null;
	title?: string;
	preview?: string;
	started_at?: number;
}): SessionSelectorEntry {
	const id = row.resolved_id ?? row.id;
	const started = typeof row.started_at === "number" ? row.started_at : 0;
	const date = new Date(started > 1_000_000_000_000 ? started : started * 1000);
	const preview = row.preview ?? "";
	return {
		path: id,
		id,
		cwd: "",
		title: row.title || undefined,
		created: date,
		modified: date,
		size: 0,
		firstMessage: preview,
		allMessagesText: preview,
	};
}

/** Menus that open at most once: a repeat request focuses the open one. */
type MenuKind = "settings" | "model-picker" | "model-picker-select";

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
	 * The gateway's resume list (`session.list`). Selecting a row hands its stored session id
	 * to the shell, which resumes it over a fresh connection; the shell keeps no local table.
	 */
	async showSessionSelector(): Promise<void> {
		const gateway = this.ctx.session.gateway;
		if (!gateway) {
			this.ctx.showError("This session is not connected to the Hermes gateway.");
			return;
		}
		let rows;
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

		const sessions = rows.map(toResumeEntry);
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
