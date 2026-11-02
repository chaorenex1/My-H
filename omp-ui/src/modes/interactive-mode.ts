/**
 * Interactive mode for the coding agent.
 * Handles TUI rendering and user interaction, delegating business logic to AgentSession.
 */
import * as fs from "node:fs/promises";
import * as path from "node:path";
import { type Agent, type AgentMessage, agentPauseGate, ThinkingLevel } from "../vendor/agent/src/index.ts";
import type { AssistantMessage, ImageContent, Model, Usage, UsageReport } from "../vendor/ai/src/index.ts";
import { execReplace } from "@oh-my-pi/pi-natives";
import type { Component, EditorTheme, KeyId, LoaderMessageColorFn, OverlayHandle, SlashCommand } from "@oh-my-pi/pi-tui";
import { Container, clearRenderCache, getComposerStyle, getPaddingX, getWidthConfigEpoch, Loader, Spacer, setTerminalTextSizing, setTuiTight, TERMINAL, Text, type TUI, visibleWidth, wrapTextWithAnsi } from "@oh-my-pi/pi-tui";
import type { TerminalAppearanceRequestToken } from "@oh-my-pi/pi-tui/terminal";
import type { DescribeContext, NativeChild, NativeNode, NativeUiEvent } from "@oh-my-pi/pi-tui/native/node";
import { col, compact, keyed, node, row, span, text } from "@oh-my-pi/pi-tui/native/describe";
import { describeJobWidgetMiniSpark, describeJobWidgetsHovercard, type JobWidgetEntry, jobWidgetGlance, jobWidgetGlanceSpans, jobWidgetSummary, latestJobWidget } from "@oh-my-pi/pi-tui/tools/job-widget";
import { sameItems } from "@oh-my-pi/pi-tui/native/memo";
import { describeSegmentTrack, renderSegmentTrack, type TrackSegment } from "@oh-my-pi/pi-tui/chrome/segment-track";
import type { WorkingRowSpec } from "@oh-my-pi/pi-tui/components/loader";
import { formatDoubleTap } from "@oh-my-pi/pi-tui/key-hint-format";
import { thinkingLevelWord } from "@oh-my-pi/pi-tui/status-line/segments";
import type { TspChecklistItem, TspChecklistPhase, TspSpan, TspTreeNode } from "../vendor/wire/src/index.ts";
import { isInsideTerminalMultiplexer } from "@oh-my-pi/pi-tui/terminal-capabilities";
import { $env, adjustHsv, formatDuration, formatNumber, getActiveProfile, getProjectDir, logger, postmortem, prompt, sanitizeText } from "../vendor/utils/src/index.ts";
import chalk from "../vendor/utils/src/chalk.ts";
import { KeybindingsManager } from "@oh-my-pi/pi-tui/app-keybindings";
import { appKey } from "@oh-my-pi/pi-tui/chrome/keybinding-hints";
import { isSettingsInitialized, Settings, settings } from "../config/settings";
import { resolveLocalRoot } from "../internal-urls";
import type { AgentHubRegistry } from "@oh-my-pi/pi-tui/overlays/agent-hub-types";
import type { AgentCostSnapshot } from "@oh-my-pi/pi-tui/overlays/agent-hub-projection";
import { type AgentSession, type DroppedPrompt } from "../session/agent-session";
import { setWordPredictionHost } from "@oh-my-pi/pi-tui/prompt/word-completion";
import { resolveMarkdownLinkHrefs } from "../internal-urls/hyperlink-targets";
import type { ResolveContext } from "../internal-urls/index";
import { modelMentionDisplayName } from "@oh-my-pi/pi-tui/prompt/model-mention-syntax";
import { modelMentionChipLabel, shiftImageMarkers } from "@oh-my-pi/pi-tui/prompt/composer-attachments";
import type { SessionManager } from "../session/session-manager";
import type { ContextUsage } from "@oh-my-pi/pi-tui/status-line/types";
import type { StatusLineHostSession } from "./status-line-host";
import { agentTypeBadge, formatTaskId } from "@oh-my-pi/pi-tui/tools/task";
import type { ConfiguredThinkingLevel } from "@oh-my-pi/pi-tui/thinking";
import { FEED_MODEL_BADGE_WIDTH, formatFeedModelBadge, formatMoreItems, isFeedModelBadgeEnabled, previewLine, replaceTabs, shortenEmbeddedPaths, shortenToolArgumentPaths, TRUNCATE_LENGTHS, truncateToWidth } from "@oh-my-pi/pi-tui/render/render-utils";
import { formatPhaseDisplayName, isClosedTodo, selectCollapsedTodos, setActiveTodoDescriptionsProvider, todoMatchesAnyDescription } from "@oh-my-pi/pi-tui/tools/todo";
import { applyFileLinkSetting, applyHyperlinkSetting, fileHyperlink } from "@oh-my-pi/pi-tui/render/hyperlink";
import { extensionLinkClicksAvailable, setExtensionLinkPicking, setExtensionLinks } from "@oh-my-pi/pi-tui/render/extension-links";
import { onNativeRenderingChange } from "@oh-my-pi/pi-tui/native/state";
import { renderTreeList } from "@oh-my-pi/pi-tui/render/tree-list";
import { copyToClipboard } from "../utils/clipboard";
import type { EventBus } from "../utils/event-bus";
import { openPath } from "../utils/open";
import { resumeCommand } from "../utils/resume-command";
import { getSessionAccentAnsi, getSessionAccentHex } from "@oh-my-pi/pi-tui/theme/session-color";
import { messageHasDisplayableThinking } from "@oh-my-pi/pi-tui/chat/thinking-display";
import type { TokenRateMeter } from "../utils/token-rate";
import { disposeProgramStatus, initProgramStatus, resendProgramStatus, setProgramStatusEnabled } from "../utils/run-status";
import { AssistantMessageComponent } from "@oh-my-pi/pi-tui/chat/assistant-message";
import { setSvgFigureRendering } from "@oh-my-pi/pi-tui/chat/svg-figure";
import { setTranscriptActionHandler } from "@oh-my-pi/pi-tui/chat/transcript-actions";
import { ReadToolGroupComponent } from "@oh-my-pi/pi-tui/chat/read-tool-group";
import { ToolExecutionComponent } from "@oh-my-pi/pi-tui/chat/tool-execution";
import { AttachmentChipsBand } from "@oh-my-pi/pi-tui/prompt/attachment-chips";
import { ChatBlock, type ChatBlockHost } from "@oh-my-pi/pi-tui/chrome/chat-block";
import { CodexResetFireworksController } from "@oh-my-pi/pi-tui/overlays/codex-reset-fireworks";
import { type ComposerNativeState, CustomEditor } from "@oh-my-pi/pi-tui/prompt/custom-editor";
import { EditorTopGap } from "@oh-my-pi/pi-tui/prompt/editor-top-gap";
import { ErrorBannerComponent } from "@oh-my-pi/pi-tui/overlays/error-banner";
import { type PlanReviewAnnotationState } from "@oh-my-pi/pi-tui/overlays/plan-review-overlay";
import { ServedModelTracker } from "@oh-my-pi/pi-tui/chat/served-model-marker";
import { SessionInfoOverlay } from "@oh-my-pi/pi-tui/overlays/session-info-overlay";
import { StatusLineComponent } from "@oh-my-pi/pi-tui/status-line";
import { statusLineHost } from "./status-line-host";
import { stopSharedSpinnerTicker, type ToolExecutionHandle } from "@oh-my-pi/pi-tui/chat/tool-execution";
import { TranscriptContainer } from "@oh-my-pi/pi-tui/chrome/transcript-container";
import { Composer, type ComposerPreferences, type ComposerStatusCache } from "@oh-my-pi/pi-tui/prompt/composer";
import { setMagicKeywords } from "@oh-my-pi/pi-tui/prompt/magic-keywords";
import { sharedComposerCache } from "@oh-my-pi/pi-tui/prompt/composer-cache";
import { CommandController } from "./controllers/command-controller";
import { EventController } from "./controllers/event-controller";
import { InputController } from "./controllers/input-controller";
import { SelectorController } from "./controllers/selector-controller";
import { SessionFocusController } from "./controllers/session-focus-controller";
import { TodoCommandController } from "./controllers/todo-command-controller";
import { imageReferenceHyperlink, setImageBlobWriter } from "@oh-my-pi/pi-tui/prompt/image-references";
import { resolveComposerHint } from "@oh-my-pi/pi-tui/prompt/composer-hints";
import { hintUsage } from "../utils/usage-counter";
import { type ObservableSession, SessionObserverRegistry } from "@oh-my-pi/pi-tui/overlays/session-observer-registry";
import { createSessionTeardown, type SessionTeardown } from "./session-teardown";
import { sanitizeStatusText } from "@oh-my-pi/pi-tui/chrome/shared";
import { clearMermaidCache } from "@oh-my-pi/pi-tui/theme/mermaid-cache";
import { type ShimmerPalette, shimmerEnabled, shimmerText } from "@oh-my-pi/pi-tui/theme/shimmer";
import type { Theme } from "@oh-my-pi/pi-tui/theme";
import { onTerminalAppearanceChange, onThemeChange, setMarkdownMermaidRendering, setSymbolPreset, startMacOSAppearanceReprobeFallback, theme, warmHighlighter } from "@oh-my-pi/pi-tui/theme";
import type { InteractiveModeContext, InteractiveModeInitOptions, RenderSessionContextOptions, ResumeSessionOptions, SessionPickHandler, ShowStatusOptions, SelectOnlyModelSelectorOptions, SubmittedUserInput } from "./types";
import type { TodoItem, TodoPhase } from "@oh-my-pi/pi-tui/tools/todo";
import { materializeImageChipLinks, UiHelpers } from "./utils/ui-helpers";

import { askTimeoutMs, cfgAutocompleteMaxVisible, cfgComposerPredictions, cfgComposerShape, cfgComposerTokenRate, cfgDisplayAdvisorCollapsedLines, cfgDisplayCacheMissMarker, cfgDisplayCollapseCompacted, cfgDisplayHideToolActivity, cfgDisplayPinnedAgents, cfgDisplayShowTokenUsage, cfgDisplayShowTurnTime, cfgDisplaySubagentLivePreview, cfgGitEnabled, cfgRecapEnabled, cfgRecapIdleSeconds, cfgShowHardwareCursor, cfgSpellingAutocomplete, cfgSpellingAutocorrect, cfgSpellingTypoDetection, cfgStartupQuiet, cfgStatusLineCompactThinkingLevel, cfgStatusLineContextLine, cfgStatusLineLeftSegments, cfgStatusLinePreset, cfgStatusLineRightSegments, cfgStatusLineSegmentOptions, cfgStatusLineSeparator, cfgStatusLineSessionAccent, cfgStatusLineShowHookStatus, cfgStatusLineTransparent, cfgSymbolPreset, cfgTerminalProgramStatus, cfgTerminalShowImages, cfgTuiHyperlinks, cfgTuiImeSafeCursor, cfgTuiMaxInlineImages, cfgTuiAutoGraph, cfgTuiMouse, cfgTuiRenderMermaid, cfgTuiRenderSvg, cfgTuiResizeScrollback, cfgTuiCompleteScrollback, cfgTuiTextSizing, cfgTuiTight, cfgTuiTitleSpinner, cfgTuiTitleState, cfgTuiVimMode, cfgTuiVimModeDisplay } from "./settings";
import { cfgExpandThinkingBlocks, cfgProseOnlyThinking } from "../session/settings";
import { cfgHideThinkingBlock } from "../session/settings";
import { cfgModelRoles } from "../config/model-settings";
import { combine, type SettingValueOf } from "../config/registry";
import { cfgTierAdvisor } from "../session/settings";

/**
 * Settings with live interactive-UI side effects, keyed by id. One coalesced listener applies
 * them (`InteractiveMode.#applyUiSettingChanges`) so a bulk reload rebuilds the transcript once.
 */
const cfgLiveUiSettings = combine({
	showHardwareCursor: cfgShowHardwareCursor,
	"tui.maxInlineImages": cfgTuiMaxInlineImages,
	"tui.resizeScrollback": cfgTuiResizeScrollback,
	"tui.completeScrollback": cfgTuiCompleteScrollback,
	"tui.imeSafeCursor": cfgTuiImeSafeCursor,
	autocompleteMaxVisible: cfgAutocompleteMaxVisible,
	"spelling.typoDetection": cfgSpellingTypoDetection,
	"spelling.autocomplete": cfgSpellingAutocomplete,
	"spelling.autocorrect": cfgSpellingAutocorrect,
	"composer.shape": cfgComposerShape,
	"tui.vimMode": cfgTuiVimMode,
	"tui.vimModeDisplay": cfgTuiVimModeDisplay,
	"composer.predictions": cfgComposerPredictions,
	"display.hideToolActivity": cfgDisplayHideToolActivity,
	"terminal.showImages": cfgTerminalShowImages,
	"terminal.programStatus": cfgTerminalProgramStatus,
	hideThinkingBlock: cfgHideThinkingBlock,
	proseOnlyThinking: cfgProseOnlyThinking,
	expandThinkingBlocks: cfgExpandThinkingBlocks,
	"display.cacheMissMarker": cfgDisplayCacheMissMarker,
	"display.collapseCompacted": cfgDisplayCollapseCompacted,
	"display.showTokenUsage": cfgDisplayShowTokenUsage,
	"display.showTurnTime": cfgDisplayShowTurnTime,
	"tui.renderMermaid": cfgTuiRenderMermaid,
	"tui.renderSvg": cfgTuiRenderSvg,
	"tui.autoGraph": cfgTuiAutoGraph,
	"tui.textSizing": cfgTuiTextSizing,
	"tui.tight": cfgTuiTight,
	"tui.hyperlinks": cfgTuiHyperlinks,
	"tui.titleState": cfgTuiTitleState,
	"tui.titleSpinner": cfgTuiTitleSpinner,
	"statusLine.preset": cfgStatusLinePreset,
	"statusLine.leftSegments": cfgStatusLineLeftSegments,
	"statusLine.rightSegments": cfgStatusLineRightSegments,
	"statusLine.separator": cfgStatusLineSeparator,
	"statusLine.showHookStatus": cfgStatusLineShowHookStatus,
	"statusLine.sessionAccent": cfgStatusLineSessionAccent,
	"statusLine.transparent": cfgStatusLineTransparent,
	"statusLine.segmentOptions": cfgStatusLineSegmentOptions,
	"statusLine.compactThinkingLevel": cfgStatusLineCompactThinkingLevel,
	"statusLine.contextLine": cfgStatusLineContextLine,
});
type LiveUiSettings = SettingValueOf<typeof cfgLiveUiSettings>;

const STILL_CLOSING_DELAY_MS = 3_000;
/** Startup emits several status-bar changes within a second; persist only the settled one. */
const COMPOSER_STATUS_PERSIST_DELAY_MS = 1_000;
/** Status-line slot of the link-hint mode prompt; the `omp:` prefix keeps it apart from extension keys. */
const DEFAULT_WORKING_MESSAGE = "Working…";
/** Retire a fully-closed todo HUD after this delay (client-side display state). */
const TODO_HUD_AUTO_CLEAR_SECONDS = 5;
/** Native working-message shimmer under a session accent: the hue itself is the terminal's `accent`. */
const NATIVE_ACCENT_SHIMMER: ShimmerPalette = { low: "dim", mid: "accent", high: "accent", bold: true };

interface WorkingMessageAccent {
	main: string;
	dim: string;
	/**
	 * Interned shimmer palette for this accent so `compile()` inside
	 * `shimmerText` sees a stable palette object between animation ticks.
	 * A fresh palette literal every frame guaranteed a cache miss on the
	 * Symbol-keyed compiled-ANSI slot and forced `resolveTierAnsi` to walk
	 * every tier open/close for the ~30fps loader redraw (issue #4377).
	 */
	palette?: ShimmerPalette;
}

interface WorkingMessageAccentCacheKey {
	sessionName: string | undefined;
	accentSurfaceLuminance: number | undefined;
	sessionAccentEnabled: boolean;

}
function renderWorkingMessage(message: string, accent?: WorkingMessageAccent): string {
	if (!accent) return shimmerText(message, theme);
	accent.palette ??= {
		low: "dim",
		mid: { ansi: accent.main },
		high: { ansi: accent.main },
		bold: true,
	};
	return shimmerText(message, theme, accent.palette);
}

const EDITOR_MAX_HEIGHT_MIN = 6;
const EDITOR_MAX_HEIGHT_MAX = 18;
const EDITOR_RESERVED_ROWS = 12;
const EDITOR_FALLBACK_ROWS = 24;
const EDITOR_MIN_CHROME_ROWS = 4; // rows reserved for transcript + status on small terms
const EDITOR_MIN_RENDERED_ROWS = 3; // bordered editor floor: top+bottom border + 1 content row

/**
 * Editor max-height cap for a terminal of `terminalRows` rows.
 *
 * Roomy terminals get the comfortable [6, 18] band. Small terminals shrink the
 * cap so the editor leaves at least EDITOR_MIN_CHROME_ROWS rows for the
 * transcript + status line. The editor is bordered, so it never renders fewer
 * than EDITOR_MIN_RENDERED_ROWS rows; once the terminal is too small for both
 * (terminalRows < EDITOR_MIN_RENDERED_ROWS + EDITOR_MIN_CHROME_ROWS) the cap is
 * pinned to that floor — returning a smaller number would not shrink the editor
 * any further, it would only misreport the rows it actually occupies.
 */
export function computeEditorMaxHeight(terminalRows: number): number {
	const rows = Number.isFinite(terminalRows) && terminalRows > 0 ? terminalRows : EDITOR_FALLBACK_ROWS;
	const comfortable = Math.max(EDITOR_MAX_HEIGHT_MIN, Math.min(EDITOR_MAX_HEIGHT_MAX, rows - EDITOR_RESERVED_ROWS));
	return Math.max(EDITOR_MIN_RENDERED_ROWS, Math.min(comfortable, rows - EDITOR_MIN_CHROME_ROWS));
}

const HUD_NOTE_SUP_DIGITS: Record<string, string> = {
	"0": "\u2070",
	"1": "\u00b9",
	"2": "\u00b2",
	"3": "\u00b3",
	"4": "\u2074",
	"5": "\u2075",
	"6": "\u2076",
	"7": "\u2077",
	"8": "\u2078",
	"9": "\u2079",
};

function formatHudNoteMarker(count: number): string {
	if (count <= 0) return "";
	const sub = String(count)
		.split("")
		.map(d => HUD_NOTE_SUP_DIGITS[d] ?? d)
		.join("");
	return theme.fg("dim", chalk.italic(` \u207a${sub}`));
}

function formatContextTokenCount(value: number): string {
	return formatNumber(Math.max(0, Math.round(value))).toLowerCase();
}

function hasAssistantToolCall(message: AgentMessage): boolean {
	return message.role === "assistant" && message.content.some(block => block.type === "toolCall");
}

/**
 * Reads a tool-name snapshot out of a persisted `mode_change` payload. Session
 * files are user-editable and survive across versions, so anything that is not
 * a plain array of strings is treated as absent rather than trusted.
 */
function readPersistedToolNames(value: unknown): string[] | undefined {
	if (!Array.isArray(value)) return undefined;
	if (!value.every(name => typeof name === "string")) return undefined;
	return value as string[];
}

export interface InteractiveModeOptions {
	/** Providers that were migrated during startup */
	migratedProviders?: string[];
	/** Warning message if model fallback occurred */
	modelFallbackMessage?: string;
	/** Initial message to send */
	initialMessage?: string;
	/** Initial images to include with the message */
	initialImages?: ImageContent[];
	/** Additional initial messages to queue */
	initialMessages?: string[];
}

export const TODO_COMPACT_TERMINAL_ROWS_THRESHOLD = 18;

/** Holds mutable HUD and editor-adjacent chrome outside transcript history. */
class AnchoredLiveContainer extends Container {}

/** An empty HUD slot: the terminal owns spacing, so nothing is described. */
const EMPTY_HUD: NativeNode = col([]);

/** Renders through an ANSI component and describes as a node built alongside it. */
class DescribedComponent implements Component {
	constructor(
		readonly inner: Component,
		readonly native: NativeNode,
	) {}

	render(width: number): readonly string[] {
		return this.inner.render(width);
	}

	describe(): NativeNode {
		return this.native;
	}

	invalidate(): void {
		this.inner.invalidate?.();
	}
}

class TodoHudContainer extends AnchoredLiveContainer {
	constructor(private readonly mode: InteractiveMode) {
		super();
	}

	override render(width: number): readonly string[] {
		if (this.mode.isCompactTodoMode()) {
			return [];
		}
		return super.render(width);
	}
}

/**
 * Native-only dock row (§8.1), right-aligned above the activity line, holding
 * the running-work counter: one pill with an agents segment and a
 * background-jobs segment. ANSI renders the pinned agent list in its own
 * container and the agent and job counts in the status line, so this row
 * renders nothing.
 */
class StatusHudContainer extends AnchoredLiveContainer {
	constructor(private readonly mode: InteractiveMode) {
		super();
	}

	override describe(cx: DescribeContext): NativeNode {
		return this.mode.describeStatusHud(this.children, cx);
	}

	override render(width: number): readonly string[] {
		const lines = this.#renderLines(width);
		this.mode.statusRowOccupied = lines.length > 0;
		return lines;
	}

	#renderLines(width: number): readonly string[] {
		const childLines = super.render(width);
		if (!this.mode.isCompactTodoMode()) {
			if (childLines.length === 0) {
				const idle = this.mode.renderIdleStatusHud(width);
				if (idle) return idle;
			}
			return childLines;
		}
		return this.mode.renderCompactStatusLine(width, childLines);
	}
}

/**
 * Preview of the command panels queued while the agent streams, rendered above
 * the editor so `/usage` and friends answer immediately mid-turn.
 *
 * Capped in height: the panels are shown in full in the transcript at the next
 * settle, so the preview only has to answer the question, not reproduce the
 * whole report. Rendering is delegated to the real panels at the real width, so
 * the preview cannot drift from what eventually lands in the transcript.
 */
class DeferredCommandPreview implements Component {
	#native: NativeNode | undefined;

	constructor(
		private readonly items: readonly Component[],
		private readonly maxRows: number,
		private readonly commandCount: number,
	) {}

	/** The real panels, clamped to the preview height, then the queued-output note. */
	describe(): NativeNode {
		const queued = this.commandCount === 1 ? "1 command output" : `${this.commandCount} command outputs`;
		this.#native ??= col(
			[
				col(this.items, { max: { h: `${this.maxRows}lines` } }),
				text([span(`${queued} — shown in full in the transcript when the agent pauses`, "dim")]),
			],
			{ role: "omp.hud.deferred" },
		);
		return this.#native;
	}

	render(width: number): readonly string[] {
		const rows: string[] = [];
		for (const item of this.items) rows.push(...item.render(width));
		const queued = this.commandCount === 1 ? "1 command output" : `${this.commandCount} command outputs`;
		if (rows.length <= this.maxRows) {
			rows.push(theme.fg("dim", `${queued} — repeated in the transcript when the agent pauses`));
			return rows;
		}
		const shown = rows.slice(0, Math.max(1, this.maxRows - 1));
		const hidden = rows.length - shown.length;
		shown.push(theme.fg("dim", `… ${hidden} more rows — ${queued} shown in full when the agent pauses`));
		return shown;
	}
}

/** Never shrink the queued-output preview below this, even on a short terminal. */
const DEFERRED_PREVIEW_MIN_ROWS = 6;
/** Ceiling for the preview as a share of the viewport, so the prompt stays visible. */
const DEFERRED_PREVIEW_VIEWPORT_FRACTION = 0.4;

/** How long the ctrl+p model-role cycle chip track lingers above the editor
 *  before it auto-clears, mirroring the todo HUD's auto-clear timer. */
const MODEL_CYCLE_TRACK_CLEAR_MS = 4000;

/** Active subagent sessions the anchored HUD jump-lists, sync or detached. Slots follow registry order. */
const CTRL_L_APPEARANCE_RESPONSE_DEADLINE_MS = 2000;

/** Repaint cadence of the open jobs sheet: output tails, pids and list ages are polled, not pushed. */
const JOBS_SHEET_REFRESH_MS = 250;


/** Project the gateway session onto the status line's display contract. */
function statusLineSessionFor(session: AgentSession): StatusLineHostSession {
	const manager = session.sessionManager;
	const usage = manager.getUsageStatistics();
	const stats = session.getSessionStats();
	return {
		state: { model: session.model, thinkingLevel: session.thinkingLevel, messages: session.messages },
		model: session.model,
		messages: session.messages,
		sessionFile: undefined,
		isStreaming: session.isStreaming,
		isAutoThinking: false,
		systemPrompt: [],
		agent: { state: { tools: [] } },
		skills: [],
		compactionSpeculation: "idle",
		sessionManager: {
			getSessionName: () => manager.getSessionName(),
			getSessionId: () => manager.getSessionId(),
			getUsageStatistics: () => ({
				input: usage.input,
				output: usage.output,
				cacheRead: usage.cacheRead,
				cacheWrite: usage.cacheWrite,
				totalTokens: usage.input + usage.output + usage.cacheRead + usage.cacheWrite,
				orchestrationInput: 0,
				orchestrationOutput: 0,
				orchestrationCacheRead: 0,
				premiumRequests: stats.premiumRequests,
				cost: stats.cost,
			}),
		},
		modelRegistry: { isUsingOAuth: () => false },
		getContextUsage: () => session.getContextUsage(),
		autoResolvedThinkingLevel: () => undefined,
		isFastModeActive: () => false,
		getAsyncJobSnapshot: () => null,
		getGoalModeState: () => undefined,
	};
}

export class InteractiveMode {
	#ownsStartedUi: boolean;
	session: AgentSession;
	sessionManager: SessionManager;
	settings: Settings;
	keybindings: KeybindingsManager;
	agent: Agent;

	/** Canonical composer shared by cold prepaint and the session-aware runtime. */
	readonly composer: Composer;
	ui: TUI;
	chatContainer: TranscriptContainer;
	pendingMessagesContainer: Container;
	statusContainer: Container;
	/** Whether {@link statusContainer} rendered lines in the latest frame. */
	statusRowOccupied = false;
	todoContainer: Container;
	errorBannerContainer: Container;
	modelCycleContainer: Container;
	deferredCommandContainer: Container;
	reportContainer: Container;
	editor: CustomEditor;
	editorContainer: Container;
	/** Composer attachment band (chip cards) rendered directly above the prompt box. */
	attachmentChipsContainer: Container;
	statusLine: StatusLineComponent;

	isInitialized = false;
	toolOutputExpanded = false;
	hideToolActivity = false;
	todoExpanded = false;

	#todoAutoClearTimer: NodeJS.Timeout | undefined;
	#todoAutoClearGeneration = 0;
	#modelCycleClearTimer: NodeJS.Timeout | undefined;
	#composerStatusPersistTimer: NodeJS.Timeout | undefined;
	readonly #initialComposerModel: Model | undefined;
	#nextAppearanceRequestToken = 1;
	#appearanceRefreshRequest: { token: TerminalAppearanceRequestToken; deadline: number } | undefined;
	todoPhases: TodoPhase[] = [];
	#todoPhasesOwner?: AgentSession;
	#todoHudHidden = false;
	hideThinkingBlock = false;
	#sessionsWithDisplayableThinkingContent = new WeakSet<AgentSession>();
	pendingTools = new Map<string, ToolExecutionHandle>();
	transcriptMessageComponents = new WeakMap<AgentMessage, Component>();
	servedModelTracker = new ServedModelTracker();
	#workingMessageAccentCacheKey?: WorkingMessageAccentCacheKey;
	#workingMessageAccentCacheValue?: WorkingMessageAccent;
	#workingMessageAccentCacheHasValue = false;
	#sessionInfoOverlayHandle: OverlayHandle | undefined;
	unsubscribe?: () => void;
	onInputCallback?: (input: SubmittedUserInput) => void;
	#shutdownPromise: Promise<void> | undefined;
	#lastSigintTime = 0;
	#lastEscapeTime = 0;
	#lastLeftTapTime = 0;
	#dialogLifetime = new AbortController();
	#resizeHandler: (() => void) | undefined;
	#cleanupUnsubscribe: (() => void) | undefined;

	streamingComponent: AssistantMessageComponent | undefined = undefined;
	streamingMessage: AssistantMessage | undefined = undefined;
	lastAssistantUsage: Usage | undefined = undefined;
	loadingAnimation: Loader | undefined = undefined;
	proseOnlyThinking = true;
	expandThinkingBlocks = false;
	#pendingWorkingMessage: string | (() => string) | undefined;
	#workingMessage: string | (() => string) = DEFAULT_WORKING_MESSAGE;
	#workingStartedAt = 0;
	#statusHudNative: { children: readonly Component[]; todo: NativeNode | undefined; node: NativeNode } | undefined;
	#todoHudNative: { checklist: NativeNode; fallback: NativeNode } | undefined;
	#teardownFailed = false;
	#isShuttingDown = false;
	#version: string;
	#mouseCapture = false;
	#eventBusUnsubscribers: Array<() => void> = [];
	#pendingCommandOutput: Component[] = [];
	#pendingCommandOutputSessionId: string | undefined;
	#pendingCommandOutputCommands = 0;
	#pendingSubmittedInput: SubmittedUserInput | undefined;
	#pendingSubmissionDispose: (() => void) | undefined;
	#pendingSubmissionPreservesDraft = false;
	#optimisticUserMessageComponents: Component[] = [];
	optimisticUserMessageSignature: string | undefined = undefined;
	locallySubmittedUserSignatures: Set<string> = new Set();
	shutdownRequested = false;
	#disposeImageBlobWriter: (() => void) | undefined;
	readonly #chatHost: ChatBlockHost = {
		requestRender: () => this.ui.requestRender(),
	};
	readonly #eventController: EventController;
	readonly #commandController: CommandController;
	readonly #todoCommandController: TodoCommandController;
	readonly #selectorController: SelectorController;
	readonly #inputController: InputController;
	readonly #uiHelpers: UiHelpers;

	get hasDisplayableThinkingContent(): boolean {
		return this.#sessionsWithDisplayableThinkingContent.has(this.viewSession);
	}

	noteDisplayableThinkingContent(message: AgentMessage): boolean {
		if (this.hasDisplayableThinkingContent || !messageHasDisplayableThinking(message, this.proseOnlyThinking)) {
			return false;
		}
		this.#sessionsWithDisplayableThinkingContent.add(this.viewSession);
		return true;
	}

	get effectiveHideThinkingBlock(): boolean {
		const thinkingOff = (this.viewSession?.thinkingLevel ?? ThinkingLevel.Off) === ThinkingLevel.Off;
		return this.hideThinkingBlock || (thinkingOff && !this.hasDisplayableThinkingContent);
	}

	#workingTitleTrailer(): string | undefined {
		if (cfgComposerShape.get(settings) !== "band") return undefined;
		const name = this.sessionManager.getSessionName();
		if (!name) return undefined;
		return `\x1b[2;3m${sanitizeStatusText(name)}\x1b[23;22m`;
	}

	#workingRowTrailer(): string | undefined {
		return this.#workingTitleTrailer();
	}

	renderIdleStatusHud(width: number): readonly string[] | undefined {
		const trailer = this.#workingRowTrailer();
		if (!trailer) return undefined;
		return ["", " ".repeat(Math.max(0, width - visibleWidth(trailer))) + trailer];
	}

	maintenanceInterruptKey(): KeyId | undefined {
		return this.keybindings.getKeys("app.interrupt")[0] ?? "escape";
	}

	#workingRowSpec(): WorkingRowSpec {
		const accent = this.#getWorkingMessageAccent() !== undefined;
		return {
			label: typeof this.#workingMessage === "function" ? this.#workingMessage() : this.#workingMessage,
			startedAt: this.#workingStartedAt,
			palette: accent ? NATIVE_ACCENT_SHIMMER : undefined,
			interruptKey: this.keybindings.getKeys("app.interrupt")[0] ?? "escape",
		};
	}

	interruptFromPointer(): void {
		this.editor.onEscape?.();
	}

	describeStatusHud(children: readonly Component[], cx: DescribeContext): NativeNode {
		const hud = this.#todoHudNative;
		const todo = hud && (cx.supports("checklist") ? hud.checklist : hud.fallback);
		const memo = this.#statusHudNative;
		if (memo && memo.todo === todo && sameItems(memo.children, children)) return memo.node;
		const parts: NativeChild[] = [];
		if (children.length > 0) parts.push(node("col", { role: "omp.hud.status" }, children.slice(), "status"));
		if (todo) parts.push(todo);
		const described =
			parts.length === 0 ? EMPTY_HUD : row(parts, { role: "omp.hud.activity", align: "center", gap: "sm" });
		this.#statusHudNative = { children: children.slice(), todo, node: described };
		return described;
	}

	get teardownFailed(): boolean {
		return this.#teardownFailed;
	}

	get isShuttingDown(): boolean {
		return this.#isShuttingDown;
	}

	get eventController(): EventController {
		return this.#eventController;
	}

	/** Dispatch one submitted slash command; `true` when it was consumed (no prompt follows). */
	async executeCommandText(text: string): Promise<boolean> {
		return this.#commandController.dispatch(text);
	}

	get viewSession(): AgentSession {
		return this.session;
	}

	get assistantImagesVisible(): boolean {
		return cfgTerminalShowImages.get(this.settings);
	}

	get tableChartsVisible(): boolean {
		return true;
	}

	resolveAssistantMessageLinkHrefs(hrefs: readonly string[]): Promise<ReadonlyMap<string, string>> {
		return resolveMarkdownLinkHrefs(hrefs, this.#linkResolveContext());
	}

	#linkResolveContext(): ResolveContext {
		const session = this.viewSession;
		return {
			cwd: session.sessionManager.getCwd(),
			sessionFile: session.sessionFile,
			settings: session.settings,
			localProtocolOptions: {
				getArtifactsDir: () => session.sessionManager.getArtifactsDir(),
				getSessionId: () => session.sessionManager.getSessionId(),
			},
		};
	}

	get sessionName(): string | undefined {
		return this.session.sessionName;
	}

	resolveViewportClickCandidates(index: number): string[] {
		return this.composer.viewportClickCandidates(index);
	}

	setClickHoverId(id: string | undefined): void {
		this.composer.setHoveredClickId(id);
	}

	clearTransientSessionUi(options?: { viewOnly?: boolean }): void {
		this.#hideSessionInfo();
		if (this.loadingAnimation) {
			this.loadingAnimation.stop();
			this.loadingAnimation = undefined;
		}
		this.statusContainer.disposeChildren();
		this.pendingMessagesContainer.disposeChildren();
		this.#cancelModelCycleClearTimer();
		this.modelCycleContainer.disposeChildren();
		this.deferredCommandContainer.disposeChildren();
		this.#pendingCommandOutput = [];
		this.#pendingCommandOutputSessionId = undefined;
		this.#pendingCommandOutputCommands = 0;
		if (!options?.viewOnly) this.#inputController.restoreCompactionQueueToEditor();
		this.streamingComponent = undefined;
		this.streamingMessage = undefined;
		this.lastAssistantUsage = undefined;
		this.servedModelTracker = new ServedModelTracker();
		this.pendingTools.clear();
	}

	constructor(session: AgentSession, version: string, composer?: Composer) {
		this.session = session;
		this.sessionManager = session.sessionManager;
		this.settings = session.settings;
		this.agent = session.agent;
		this.#version = version;
		this.#initialComposerModel = session.model;
		const preferences = {
			quiet: cfgStartupQuiet.get(session.settings),
			composerShape: cfgComposerShape.get(session.settings),
			...this.#liveComposerPreferences(),
		};
		const wasStarted = composer?.started ?? false;
		this.composer = composer ?? new Composer({ preferences, welcome: { version } });
		this.composer.setPreferences(preferences);
		this.ui = this.composer.ui;
		this.editor = this.composer.editor;
		this.editor.placeholder = () => this.#composerHint();
		this.editor.composerState = () => this.#composerNativeState();
		this.editor.fileHyperlink = (filePath, text) => fileHyperlink(filePath, text, { line: 1 });
		this.#ownsStartedUi = wasStarted;
		this.keybindings = KeybindingsManager.inMemory();

		setTuiTight(cfgTuiTight.get(session.settings));
		setMarkdownMermaidRendering(cfgTuiRenderMermaid.get(session.settings));
		setSvgFigureRendering(cfgTuiRenderSvg.get(session.settings));
		applyFileLinkSetting();
		applyHyperlinkSetting();

		// The TUI polls the provider every frame: keep one field in sync instead of reading settings in render.
		this.#mouseCapture = cfgTuiMouse.get(session.settings);
		this.#eventBusUnsubscribers.push(
			cfgTuiMouse.listen(session.settings, on => {
				this.#mouseCapture = on;
				if (!on) this.#inputController?.clearHoverHighlight();
				this.ui.requestRender();
			}),
		);
		this.ui.setInlineMouseTrackingProvider(() => this.#mouseCapture);

		this.chatContainer = new TranscriptContainer();
		this.pendingMessagesContainer = new AnchoredLiveContainer();
		this.statusContainer = new StatusHudContainer(this);
		this.todoContainer = new TodoHudContainer(this);
		this.errorBannerContainer = new AnchoredLiveContainer();
		this.modelCycleContainer = new AnchoredLiveContainer();
		this.deferredCommandContainer = new AnchoredLiveContainer();
		this.reportContainer = new AnchoredLiveContainer();

		this.#applyVimMode(this.editor);
		this.editor.viewportRowsProvider = () => this.ui.terminal.rows;
		this.editor.onAutocompleteCancel = () => {
			this.ui.requestRender(true);
		};
		this.editor.onAutocompleteUpdate = () => {
			this.ui.requestRender();
		};
		this.editor.setShimmerRepaintHandler(() => this.ui.requestComponentRender(this.editor));
		this.#syncEditorMaxHeight();
		this.#resizeHandler = () => this.#syncEditorMaxHeight();
		process.stdout.on("resize", this.#resizeHandler);

		this.attachmentChipsContainer = new Container();
		const attachmentChips = new AttachmentChipsBand(this.editor, this.ui.imageBudget, () => this.ui.requestRender());
		this.attachmentChipsContainer.addChild(attachmentChips);
		this.editor.attachmentChips = attachmentChips;
		this.editorContainer = new Container();
		this.editorContainer.addChild(this.editor);
		this.statusLine = new StatusLineComponent(statusLineSessionFor(session), statusLineHost);
		this.statusLine.onNativeAction = action => {
			switch (action) {
				case "status.model":
					this.showModelSelector({ temporaryOnly: true });
					return;
				case "status.context":
					this.handleContextCommand();
					return;
				case "status.cost":
					void this.handleUsageCommand();
					return;
				default:
					return;
			}
		};

		this.#eventController = new EventController(this);
		this.#commandController = new CommandController(this);
		this.#todoCommandController = new TodoCommandController(this);
		this.#selectorController = new SelectorController(this);
		this.#inputController = new InputController(this);
		this.#uiHelpers = new UiHelpers(this);
		this.#subscribeToAgent();
	}

	async init(options: InteractiveModeInitOptions = {}): Promise<void> {
		if (this.isInitialized) return;
		this.isInitialized = true;
		this.#dialogLifetime.abort();
		this.#dialogLifetime = new AbortController();
		this.keybindings = KeybindingsManager.create();
		await hintUsage.load();

		// One teardown for signals and Ctrl+C: a real SIGINT/SIGTERM goes through the same close
		// path as the keypress, so the gateway session is closed exactly once.
		const onSignal = (reason: string) => {
			void this.#teardown().finally(() => {
				process.exitCode = 130;
			});
		};
		const signums: NodeJS.Signals[] = ["SIGINT", "SIGTERM", "SIGHUP"];
		const handlers = signums.map(signal => {
			const handler = () => onSignal(signal);
			process.on(signal, handler);
			return () => process.off(signal, handler);
		});
		this.#cleanupUnsubscribe = () => {
			for (const off of handlers) off();
		};

		const startupQuiet = cfgStartupQuiet.get(this.settings);
		this.composer.setPreferences({ quiet: startupQuiet });
		this.composer.updateWelcome({ version: this.#version });
		this.statusLine.watchBranch(() => this.ui.requestRender());
		this.composer.setStatusComponent(this.statusLine);
		this.composer.setRuntimeChildren(
			[
				this.chatContainer,
				this.pendingMessagesContainer,
				this.todoContainer,
				this.reportContainer,
				this.errorBannerContainer,
				this.modelCycleContainer,
				this.deferredCommandContainer,
				this.statusContainer,
				this.attachmentChipsContainer,
				this.editorContainer,
			],
			{
				// Roots that collapse without new transcript output must not raise the post-run chrome floor.
				transientChrome: [
					this.editorContainer,
					this.attachmentChipsContainer,
					this.reportContainer,
					this.pendingMessagesContainer,
					this.errorBannerContainer,
				],
				selfClearingChrome: [this.todoContainer, this.modelCycleContainer],
				nativeDock: [
					this.errorBannerContainer,
					this.modelCycleContainer,
					this.deferredCommandContainer,
					this.statusContainer,
					this.pendingMessagesContainer,
					this.editorContainer,
				],
			},
		);
		this.ui.setFocus(this.editor);
		this.syncComposerShape();

		this.#inputController.setupKeyHandlers();
		this.#inputController.setupEditorSubmitHandler();
		setTranscriptActionHandler(action => {
			switch (action.act) {
				case "retry":
					void this.session.retry();
					return;
				case "switch-model":
					this.showModelSelector({ temporaryOnly: true });
					return;
				case "copy":
					copyToClipboard(action.text).then(
						() => this.showStatus("Copied to clipboard"),
						(error: unknown) => this.showWarning(`Copy failed: ${error instanceof Error ? error.message : String(error)}`),
					);
					return;
				default:
					return;
			}
		});

		await logger.time("InteractiveMode.init:todos", () => this.#loadTodoList());

		if (!this.#ownsStartedUi) {
			this.composer.start({
				clearScrollback: options.clearInitialTerminalHistory === true,
				playWelcomeIntro: !options.suppressWelcomeIntro,
			});
			this.#ownsStartedUi = true;
		}
		this.#syncVimStatus(this.editor);
		this.#eventBusUnsubscribers.push(
			this.sessionManager.onSessionNameChanged(() => {
				this.#handleSessionAccentInputsChanged();
				this.ui.requestRender();
			}),
		);
		this.#applyUiSettingChanges(cfgLiveUiSettings.get(this.settings), cfgLiveUiSettings.get(this.settings));
		this.#subscribeToAgent();
	}
	async getUserInput(): Promise<SubmittedUserInput> {
		const { promise, resolve } = Promise.withResolvers<SubmittedUserInput>();
		this.onInputCallback = input => {
			this.onInputCallback = undefined;
			resolve(input);
		};
		return await promise;
	}

	recordLocalSubmission(text: string, imageCount = 0): () => void {
		if (this.isKnownSlashCommand(text)) {
			return () => {};
		}
		const signature = `${text}\u0000${imageCount}`;
		this.locallySubmittedUserSignatures.add(signature);
		let disposed = false;
		return () => {
			if (disposed) return;
			disposed = true;
			this.locallySubmittedUserSignatures.delete(signature);
		};
	}

	async withLocalSubmission<T>(text: string, fn: () => Promise<T>, options?: { imageCount?: number }): Promise<T> {
		const dispose = this.recordLocalSubmission(text, options?.imageCount ?? 0);
		try {
			return await fn();
		} catch (err) {
			dispose();
			throw err;
		}
	}
	#captureAddedChatComponents(render: () => void): Component[] {
		const start = this.chatContainer.children.length;
		render();
		return this.chatContainer.children.slice(start);
	}

	clearOptimisticUserMessage(): void {
		this.optimisticUserMessageSignature = undefined;
		this.#pendingSubmissionDispose?.();
		this.#pendingSubmissionDispose = undefined;
		this.#optimisticUserMessageComponents = [];
	}

	replaceOptimisticUserMessage(
		message: AgentMessage,
		options?: { imageLinks?: readonly (string | undefined)[] },
	): void {
		this.optimisticUserMessageSignature = undefined;
		this.#pendingSubmissionDispose?.();
		this.#pendingSubmissionDispose = undefined;
		for (const component of this.#optimisticUserMessageComponents) {
			this.chatContainer.removeChild(component);
		}
		this.#optimisticUserMessageComponents = [];
		this.addMessageToChat(message, options);
	}

	/**
	 * Optimistically render a user-invoked `/skill:` row before its awaited
	 * dispatch so a slow preflight (memory recall, `before_agent_start` hooks,
	 * auto-thinking classification, pre-prompt compaction) does not leave the
	 * submission invisible — normal prompts paint their row via
	 * {@link startPendingSubmission} the same way (issue #8895). The canonical
	 * skill `message_start` swaps this row in place via
	 * {@link reconcileOptimisticSkillMessage}; a failed or bailed dispatch drops
	 * it via {@link clearOptimisticSkillMessage}.
	 */
	startPendingSubmission(
		input: {
			text: string;
			images?: ImageContent[];
			imageLinks?: (string | undefined)[];
			customType?: string;
			display?: boolean;
			streamingBehavior?: "steer" | "followUp";
		},
		options?: { preserveDraft?: boolean; clearEditor?: boolean },
	): SubmittedUserInput {
		const submission: SubmittedUserInput = {
			text: input.text,
			images: input.images,
			imageLinks: input.imageLinks,
			customType: input.customType,
			display: input.display,
			streamingBehavior: input.streamingBehavior,
			cancelled: false,
			started: false,
		};
		this.#pendingSubmittedInput = submission;
		this.#pendingSubmissionPreservesDraft = options?.preserveDraft === true;
		// `submitInteractiveInput` dispatches known `/skill:` text as a custom
		// message, and EventController appends that canonical row on its own. An
		// ordinary optimistic user row would survive as a duplicate, so mirror the
		// dispatch condition here.
		if (!submission.customType) {
			const imageCount = submission.images?.length ?? 0;
			this.optimisticUserMessageSignature = `${submission.text}\u0000${imageCount}`;
			this.#pendingSubmissionDispose = this.recordLocalSubmission(submission.text, imageCount);
			this.#optimisticUserMessageComponents = this.#captureAddedChatComponents(() => {
				this.addMessageToChat(
					{
						role: "user",
						content: [{ type: "text", text: submission.text }, ...(submission.images ?? [])],
						attribution: "user",
						timestamp: Date.now(),
					},
					{ imageLinks: input.imageLinks },
				);
			});
		} else {
			this.clearOptimisticUserMessage();
		}
		if (!options?.preserveDraft && options?.clearEditor !== false) {
			this.editor.setText("");
			this.editor.imageLinks = undefined;
		}
		this.ensureLoadingAnimation();
		if (this.composer.started) this.ui.renderNow();
		else this.ui.requestRender(true);
		return submission;
	}

	cancelPendingSubmission(): boolean {
		const submission = this.#pendingSubmittedInput;
		if (!submission || submission.started) {
			return false;
		}
		const preserveDraft = this.#pendingSubmissionPreservesDraft;

		submission.cancelled = true;
		this.#pendingSubmittedInput = undefined;
		this.#pendingSubmissionPreservesDraft = false;
		this.clearOptimisticUserMessage();
		this.#pendingWorkingMessage = undefined;
		if (this.loadingAnimation) {
			this.#stopLoadingAnimation(true);
		}
		if (!submission.customType && !preserveDraft) {
			// Enter clears the submitted draft before this cancellation can run.
			// Keep anything typed or attached since then, after the recovered input.
			const laterText = this.editor.getExpandedText();
			const submittedImages = submission.images ?? [];
			const laterImages = this.editor.pendingImages;
			const recoveredText = laterText
				? `${submission.text}\n${shiftImageMarkers(laterText, submittedImages.length)}`
				: submission.text;
			this.editor.pendingImages = [...submittedImages, ...laterImages];
			this.editor.pendingImageLinks = [
				...(submission.imageLinks ?? submittedImages.map(() => undefined)),
				...this.editor.pendingImageLinks,
			];
			this.editor.imageLinks = this.editor.pendingImageLinks;
			this.rebuildChatFromMessages();
			this.editor.setCollapsedText(recoveredText);
		}
		this.updateEditorBorderColor();
		this.ui.requestRender();
		return true;
	}

	/**
	 * Hands back a prompt the session dropped before dispatch (an Esc abort,
	 * usage preflight denial or the cost cap raced turn setup). The message was
	 * never persisted, so the tree/branch selectors cannot offer it — remove the
	 * optimistic transcript row and put the typed text back in the editor for editing.
	 */
	#restoreDroppedPrompt(prompt: DroppedPrompt): void {
		this.clearOptimisticUserMessage();
		this.#pendingWorkingMessage = undefined;
		if (this.loadingAnimation) {
			this.#stopLoadingAnimation(true);
		}
		this.rebuildChatFromMessages();
		// The rebuild drops transient notices, including the one that refused this prompt.
		if (prompt.reason) this.showError(prompt.reason);
		// The drop arrives asynchronously (after the abort settles); never clobber
		// a draft the user has already started typing in the meantime.
		if (!this.editor.getText().trim()) {
			this.editor.pendingImages = prompt.images ? [...prompt.images] : [];
			this.editor.pendingImageLinks = prompt.images ? prompt.images.map(() => undefined) : [];
			this.editor.imageLinks = this.editor.pendingImageLinks;
			this.editor.setText(prompt.text);
		}
		this.ui.requestRender();
	}

	markPendingSubmissionStarted(input: SubmittedUserInput): boolean {
		if (input.cancelled) return false;
		input.started = true;
		if (this.#pendingSubmittedInput === input) this.#pendingSubmissionPreservesDraft = false;
		return true;
	}

	finishPendingSubmission(input: SubmittedUserInput): void {
		const wasPendingSubmission = this.#pendingSubmittedInput === input;
		const pendingSubmissionDispose = this.#pendingSubmissionDispose;
		if (wasPendingSubmission) {
			this.#pendingSubmittedInput = undefined;
			this.#pendingSubmissionDispose = undefined;
			this.#pendingSubmissionPreservesDraft = false;
		}

		if (wasPendingSubmission && !this.session.isStreaming && !this.streamingComponent) {
			this.optimisticUserMessageSignature = undefined;
			pendingSubmissionDispose?.();
			this.#optimisticUserMessageComponents = [];
			this.#pendingWorkingMessage = undefined;
			// A run that only waits on background jobs keeps its working row, which
			// names the wait; the settle watch or the next run tears it down.
			if (this.loadingAnimation) this.#stopLoadingAnimation(true);
		}
	}

	/** Drop one pending goal-continuation reservation, so idle maintenance stops waiting for its turn. */
	#computeEditorMaxHeight(): number {
		return computeEditorMaxHeight(this.ui.terminal.rows);
	}

	#syncEditorMaxHeight(): void {
		this.editor.setMaxHeight(this.#computeEditorMaxHeight());
	}

	/** Live-setting composer preferences; `quiet` is startup-only and `composerShape` flows through {@link syncComposerShape}. */
	#liveComposerPreferences(): Omit<ComposerPreferences, "quiet" | "composerShape"> {
		return {
			showHardwareCursor: cfgShowHardwareCursor.get(this.settings),
			maxInlineImages: cfgTuiMaxInlineImages.get(this.settings),
			resizeScrollback: cfgTuiResizeScrollback.get(this.settings),
			completeScrollback: cfgTuiCompleteScrollback.get(this.settings),
			imeSafeCursor: cfgTuiImeSafeCursor.get(this.settings),
			autocompleteMaxVisible: cfgAutocompleteMaxVisible.get(this.settings),
			spellingTypoDetection: cfgSpellingTypoDetection.get(this.settings),
			spellingAutocomplete: cfgSpellingAutocomplete.get(this.settings),
			spellingAutocorrect: cfgSpellingAutocorrect.get(this.settings),
		};
	}

	/**
	 * OSC 66 text-sizing is Kitty-only; resolve the setting against the terminal's
	 * capability (`TERMINAL.supportsTextSizing` defaults on for Kitty) so it stays off
	 * unless the user opts in, and never emits raw escapes on other terminals.
	 */
	#applyTextSizingSetting(): void {
		setTerminalTextSizing(cfgTuiTextSizing.get(this.settings) && TERMINAL.supportsTextSizing);
	}

	/**
	 * Charts under assistant tables per `tui.autoGraph`: `smart` picks charts through the session's judge,
	 * `auto` only while the judge role resolves to a native backend (checked per table, so a login or
	 * role change mid-session applies), else the local guess.
	 */
	#applyUiSettingChanges(next: LiveUiSettings, previous: LiveUiSettings): void {
		const any = (...ids: (keyof LiveUiSettings)[]) => ids.some(id => !Bun.deepEquals(next[id], previous[id]));
		let rebuildChat = false;
		let resetDisplay = false;

		if (
			any(
				"showHardwareCursor",
				"tui.maxInlineImages",
				"tui.resizeScrollback",
				"tui.completeScrollback",
				"tui.imeSafeCursor",
				"autocompleteMaxVisible",
				"spelling.typoDetection",
				"spelling.autocomplete",
				"spelling.autocorrect",
			)
		) {
			this.composer.setPreferences(this.#liveComposerPreferences());
			// setPreferences re-themes the editor, which resets its mode-tinted border.
			this.updateEditorBorderColor();
		}
		if (any("composer.shape")) this.syncComposerShape();
		if (any("tui.vimMode", "tui.vimModeDisplay")) this.#applyVimModeSetting();

		// Field comparisons skip effects the keybinding toggles already applied.
		const hideToolActivity = cfgDisplayHideToolActivity.get(this.settings);
		if (any("display.hideToolActivity") && hideToolActivity !== this.hideToolActivity) {
			this.hideToolActivity = hideToolActivity;
			if (!hideToolActivity) this.toolOutputExpanded = false;
			for (const child of this.chatContainer.children) {
				if (
					!hideToolActivity &&
					(child instanceof ToolExecutionComponent || child instanceof ReadToolGroupComponent)
				) {
					child.setExpanded(false);
				} else if (child instanceof AssistantMessageComponent) {
					child.setToolResultImagesVisible(!hideToolActivity);
				}
			}
			this.chatContainer.setToolActivityVisible(!hideToolActivity);
			if (hideToolActivity) this.ui.clearInlineImages();
			// Visibility changes must rebuild retired terminal history.
			resetDisplay = true;
		}
		if (any("terminal.showImages")) {
			const visible = cfgTerminalShowImages.get(this.settings);
			for (const child of this.chatContainer.children) {
				if (child instanceof ToolExecutionComponent) {
					child.setShowImages(visible);
				} else if (child instanceof AssistantMessageComponent) {
					child.setImagesVisible(visible);
				}
			}
			if (!visible) this.ui.clearInlineImages();
			this.ui.requestRender(true);
		}
		const hideThinkingBlock = cfgHideThinkingBlock.get(this.settings);
		if (any("hideThinkingBlock") && hideThinkingBlock !== this.hideThinkingBlock) {
			this.hideThinkingBlock = hideThinkingBlock;
			for (const child of this.chatContainer.children) {
				if (child instanceof AssistantMessageComponent) child.setHideThinkingBlock(this.effectiveHideThinkingBlock);
			}
			this.ui.requestRender(true);
		}
		const proseOnlyThinking = cfgProseOnlyThinking.get(this.settings);
		if (any("proseOnlyThinking") && proseOnlyThinking !== this.proseOnlyThinking) {
			this.proseOnlyThinking = proseOnlyThinking;
			for (const child of this.chatContainer.children) {
				if (child instanceof AssistantMessageComponent) child.setProseOnlyThinking(proseOnlyThinking);
			}
			this.ui.requestRender(true);
		}
		const expandThinkingBlocks = cfgExpandThinkingBlocks.get(this.settings);
		if (any("expandThinkingBlocks") && expandThinkingBlocks !== this.expandThinkingBlocks) {
			this.expandThinkingBlocks = expandThinkingBlocks;
			for (const child of this.chatContainer.children) {
				if (child instanceof AssistantMessageComponent) child.setExpandThinkingBlocks(expandThinkingBlocks);
			}
			this.ui.requestRender(true);
		}

		// Usage-row detection, compacted-history collapse, and markdown render options are baked in at
		// build time: rebuild, then retire rows already committed to native scrollback.
		if (
			any(
				"display.cacheMissMarker",
				"display.collapseCompacted",
				"display.showTokenUsage",
				"display.showTurnTime",
			)
		) {
			rebuildChat = true;
		}
		if (any("tui.renderMermaid")) {
			setMarkdownMermaidRendering(cfgTuiRenderMermaid.get(this.settings));
			rebuildChat = true;
		}
		if (any("tui.renderSvg")) {
			setSvgFigureRendering(cfgTuiRenderSvg.get(this.settings));
			rebuildChat = true;
		}
		if (any("tui.textSizing")) {
			this.#applyTextSizingSetting();
			this.ui.invalidate();
			resetDisplay = true;
		}
		if (any("tui.tight")) {
			setTuiTight(cfgTuiTight.get(this.settings));
			this.ui.invalidate();
			this.ui.requestRender();
		}
		if (any("tui.hyperlinks")) {
			// The tui.hyperlinks effect already re-applied the flag; repaint cached rows.
			this.statusLine.invalidate();
			this.ui.invalidate();
			this.ui.requestRender();
		}
		if (any("terminal.programStatus")) setProgramStatusEnabled(cfgTerminalProgramStatus.get(this.settings));

		if (
			any(
				"statusLine.preset",
				"statusLine.leftSegments",
				"statusLine.rightSegments",
				"statusLine.separator",
				"statusLine.sessionAccent",
				"statusLine.transparent",
				"statusLine.segmentOptions",
				"statusLine.compactThinkingLevel",
				"statusLine.contextLine",
			)
		) {
			this.#syncStatusLineSettings();
			this.ui.requestRender();
		}
		if (any("statusLine.sessionAccent")) this.#handleSessionAccentInputsChanged();

		if (rebuildChat) this.rebuildChatFromMessages();
		if (rebuildChat || resetDisplay) this.ui.resetDisplay();
	}


	#syncStatusLineSettings(): void {
		this.statusLine.updateSettings({
			preset: cfgStatusLinePreset.get(settings),
			leftSegments: cfgStatusLineLeftSegments.get(settings),
			rightSegments: cfgStatusLineRightSegments.get(settings),
			separator: cfgStatusLineSeparator.get(settings),
			showHookStatus: cfgStatusLineShowHookStatus.get(settings),
			sessionAccent: cfgStatusLineSessionAccent.get(settings),
			transparent: cfgStatusLineTransparent.get(settings),
			segmentOptions: cfgStatusLineSegmentOptions.get(settings),
			compactThinkingLevel: cfgStatusLineCompactThinkingLevel.get(settings),
			contextLine: cfgStatusLineContextLine.get(settings),
		});
	}
	syncComposerShape(): void {
		const shape = cfgComposerShape.get(settings);
		const style = getComposerStyle(shape);
		this.composer.setPreferences({ composerShape: shape });
		this.statusLine.attachToEditor(this.editor, style);
		this.updateEditorBorderColor();
		this.#scheduleComposerStatusPersist();
		this.ui.requestRender();
	}

	/**
	 * Push the viewed session's extension link schemes to the markdown renderer; returns whether that
	 * changed. A focused subagent's runner draws with what the main runner leaves active.
	 */
	#scheduleComposerStatusPersist(): void {
		if (this.#composerStatusPersistTimer) return;
		this.#composerStatusPersistTimer = setTimeout(() => {
			this.#composerStatusPersistTimer = undefined;
			this.#persistComposerStatus();
		}, COMPOSER_STATUS_PERSIST_DELAY_MS);
		this.#composerStatusPersistTimer.unref();
	}

	/**
	 * Cache the inputs of a fresh session's status bar (settings, model, thinking
	 * state) so the next launch renders it at first paint; see `createStartupStatusLine`.
	 */
	#persistComposerStatus(): void {
		if (!this.sessionManager.getSessionFile()) return;
		// Role changes affect the next fresh launch; session-only switches do not.
		const model = this.#initialComposerModel ?? this.session.model;
		// Recover the border's ANSI wrapper by coloring a sentinel and splitting around it.
		const marker = "\0";
		const colored = this.editor.borderColor(marker);
		const markerIndex = colored.indexOf(marker);
		const status: ComposerStatusCache = {
			borderColor:
				markerIndex < 0
					? undefined
					: {
							prefix: colored.slice(0, markerIndex),
							suffix: colored.slice(markerIndex + marker.length),
						},
			statusLine: {
				settings: statusLineHost.getSettings(),
				gitEnabled: statusLineHost.gitEnabled(),
				model,
				thinkingLevel: this.session.thinkingLevel,
				autoThinking: false,
				fastMode: false,
				usingSubscription: model ? this.session.modelRegistry.isUsingOAuth(model) : false,
				autoCompactEnabled: false,
				compactionBoundaries: null,
			},
		};
		sharedComposerCache()?.writeStatus(this.sessionManager.getCwd(), status);
	}

	#handleSessionAccentInputsChanged(): void {
		this.#clearWorkingMessageAccentCache();
		this.statusLine.invalidate();
		this.updateEditorBorderColor();
	}

	updateEditorBorderColor(): void {
		// `vimMode` reads "insert" when modal editing is off, so every Vim branch below must gate on
		// `vimEnabled` — otherwise non-Vim users would lose the session-accent/thinking border.
		const vimMode = this.editor.vimEnabled ? this.editor.vimMode : undefined;
		if (vimMode === "visual" || vimMode === "visual-line") {
			this.editor.borderColor = (str: string) => theme.fg("warning", str);
		} else if (vimMode === "normal" || vimMode === "replace") {
			this.editor.borderColor = (str: string) => theme.fg("accent", str);
		} else if (vimMode === "insert") {
			// Insert gets its own colour rather than falling through to the session accent: with Normal
			// and Visual both coloured, an uncoloured Insert made the border unreadable as a mode.
			// Matches the `vim` status-line segment, which uses the same three colours.
			this.editor.borderColor = (str: string) => theme.fg("success", str);
		} else {
			const accentEnabled = !isSettingsInitialized() || cfgStatusLineSessionAccent.get(settings) !== false;
			const sessionName = accentEnabled ? this.sessionManager.getSessionName() : undefined;
			const hex = sessionName ? getSessionAccentHex(sessionName, theme.sessionAccentInputs) : undefined;
			const ansi = getSessionAccentAnsi(hex);
			if (ansi) {
				this.editor.borderColor = (str: string) => `${ansi}${str}\x1b[39m`;
			} else {
				const level = this.viewSession.thinkingLevel ?? ThinkingLevel.Off;
				this.editor.borderColor = theme.getThinkingBorderColor(level);
			}
		}
		this.ui.requestRender();
	}

	/**
	 * Refresh the running-subagents status badge from the active local or collab
	 * registry, and the cost segment's subagent-tree spend (local sessions only:
	 * a collab guest's registry mirrors host transcripts outside this root).
	 */
	#composerNativeState(): ComposerNativeState {
		return {
			thinking: thinkingLevelWord({
				state: { model: this.session.model, thinkingLevel: this.session.thinkingLevel, messages: this.session.messages },
				isAutoThinking: false,
				autoResolvedThinkingLevel: () => undefined,
			}),
			thinkingInModel: cfgStatusLineCompactThinkingLevel.get(settings),
			running: this.loadingAnimation !== undefined || this.session.isStreaming,
			title: this.sessionManager.getSessionName(),
		} as ComposerNativeState;
	}

	/** The focused subagent and its live ancestors below main, outermost first; undefined on the main session. */
	#composerHint(): string | undefined {
		return resolveComposerHint({
			conversationStarted: this.viewSession.messages.length > 0,
			runningAgents: 0,
			focusedOnAgent: false,
			keybindings: this.keybindings,
			uses: id => hintUsage.get(id),
		});
	}

	rebuildChatFromMessages(options: { reuseSettledComponents?: boolean } = {}): void {
		// Mid-stream rebuilds (e.g. `/shake`, theme/setting changes that touch the
		// transcript) replay only committed `state.messages`. The agent's in-flight
		// `streamMessage` and its still-pending tool calls live OUTSIDE
		// `state.messages` until `message_end`, so a plain clear+replay detaches
		// their UI components while keeping the `streamingComponent` / `pendingTools`
		// references — subsequent `message_update`/`message_end` events would then
		// update orphaned components that never re-render and the live LLM output
		// vanishes from the chat (#3656). Snapshot the in-flight components,
		// clear+replay, then re-append them in their original chat-container order
		// and restore the `pendingTools` map so streaming routes back into them.
		const liveComponents: Component[] = [];
		const livePendingTools = new Map<string, ToolExecutionHandle>();
		if (this.viewSession?.isStreaming) {
			const liveSet = new Set<Component>();
			if (this.streamingComponent) liveSet.add(this.streamingComponent);
			for (const [id, component] of this.pendingTools) {
				livePendingTools.set(id, component);
				liveSet.add(component as unknown as Component);
			}
			if (liveSet.size > 0) {
				for (const child of this.chatContainer.children) {
					if (liveSet.has(child)) liveComponents.push(child);
				}
			}
		}
		this.chatContainer.clear();
		// Live display collapses to the compacted transcript tail unless the
		// user opted into the full inline history; export/resume callers choose
		// their own mode.
		const context = { messages: this.viewSession.messages };
		const preservedLiveToolCallIds = new Set<string>();
		// A preserved pending-tool component whose result has already landed in
		// the replayed transcript is re-rendered by `renderSessionContext` itself
		// (the toolResult message reconstructs the block with its output). Keeping
		// it in the live set too re-appends a second identical block below the
		// replayed one — the tool call renders twice (#6516). The preservation
		// above assumes every pending-tool component is still dangling (its result
		// lives outside `state.messages`), which stops holding the instant the
		// result is persisted while the component lingers in `pendingTools` (a
		// rebuild racing tool-completion, a background/displaceable snapshot).
		// Drop the already-resolved ones and let the replay own them; only
		// genuinely in-flight (dangling, replay-stripped) calls still need
		// preserving.
		for (const message of context.messages) {
			if (message.role !== "toolResult") continue;
			const resolved = livePendingTools.get(message.toolCallId);
			if (!resolved) continue;
			// A background job call's initial `async.state === "running"` result is
			// persisted while `EventController#handleToolExecutionEnd` deliberately
			// keeps its component in `pendingTools` so a later
			// `tool_execution_update`/`_end` settles it. Such a handle is still
			// live — dropping it would strand those updates on the running snapshot
			// — so keep it and let the live component retain ownership; only
			// terminal results are owned by the replay. (Cast mirrors the async
			// detail reads in tool-execution.ts / event-controller.ts.)
			const details = message.details as { async?: { state?: string } } | undefined;
			if (details?.async?.state === "running") {
				preservedLiveToolCallIds.add(message.toolCallId);
				continue;
			}
			livePendingTools.delete(message.toolCallId);
			// A `ReadToolGroupComponent` is shared by every read id it renders
			// (ui-helpers sets the same group for each collapsed read call). While a
			// sibling read id still points at it the component must stay on screen
			// and preserved — splicing it here would detach the pending read's
			// display and strand its future result on an off-screen component.
			// Splice only once no remaining pending id shares it.
			let stillShared = false;
			for (const other of livePendingTools.values()) {
				if (other === resolved) {
					stillShared = true;
					break;
				}
			}
			if (stillShared) {
				// The shared component still owns this completed member as well as
				// its pending sibling. Suppress the replay copy so the group remains
				// a single on-screen block while future results keep routing to it.
				preservedLiveToolCallIds.add(message.toolCallId);
				continue;
			}
			const index = liveComponents.indexOf(resolved as unknown as Component);
			if (index >= 0) liveComponents.splice(index, 1);
		}
		// Prune the settled-component cache to the messages this rebuild will
		// actually render. Message objects stay strongly reachable through
		// session entries for the whole session, so entries for compacted-away
		// history would otherwise pin their components' rendered layout caches
		// forever — exactly the memory a collapsed compaction used to release.
		const retained = new WeakMap<AgentMessage, Component>();
		for (const message of context.messages) {
			const component = this.transcriptMessageComponents.get(message);
			if (component) retained.set(message, component);
		}
		this.transcriptMessageComponents = retained;
		this.renderSessionContext({
			reuseSettledComponents: options.reuseSettledComponents,
			preservedLiveToolCallIds,
		});
		for (const child of liveComponents) {
			this.chatContainer.addChild(child);
		}
		// `renderSessionContext` clears `pendingTools` at start AND end so the
		// reconstructed historical tool components don't leak into live tracking.
		// Restore the in-flight entries afterwards so the next streamed tool-call
		// delta is routed into the preserved component instead of stacking a
		// duplicate ToolExecutionComponent below it.
		for (const [id, component] of livePendingTools) {
			this.pendingTools.set(id, component);
		}
		// During the pre-streaming window — after `startPendingSubmission` has
		// optimistically rendered the user's message but before the user
		// `message_start` event lands it in `session` entries — any rebuild
		// (e.g. Ctrl+T toggleThinkingBlockVisibility, theme selector) would
		// otherwise erase the user's just-submitted message until the first
		// assistant token arrived (#2372). Once `message_start` fires the
		// signature is cleared by `EventController`, so this replay is a no-op
		// post-streaming and cannot duplicate.
		this.#replayOptimisticUserMessage();
	}

	#replayOptimisticUserMessage(): void {
		if (!this.optimisticUserMessageSignature) return;
		const submission = this.#pendingSubmittedInput;
		if (!submission || submission.cancelled || submission.customType) return;
		this.#optimisticUserMessageComponents = this.#captureAddedChatComponents(() => {
			this.addMessageToChat(
				{
					role: "user",
					content: [{ type: "text", text: submission.text }, ...(submission.images ?? [])],
					attribution: "user",
					timestamp: Date.now(),
				},
				{ imageLinks: submission.imageLinks },
			);
		});
	}

	#formatTodoLine(todo: TodoItem, prefix: string, matched: boolean): string {
		const checkbox = theme.checkbox;
		const marker = formatHudNoteMarker(todo.notes?.length ?? 0);
		switch (todo.status) {
			case "completed":
				return theme.fg("success", `${prefix}${checkbox.checked} ${chalk.strikethrough(todo.content)}`) + marker;
			case "in_progress":
				return theme.fg("accent", `${prefix}${checkbox.unchecked} ${todo.content}`) + marker;
			case "abandoned":
				return theme.fg("error", `${prefix}${checkbox.unchecked} ${chalk.strikethrough(todo.content)}`) + marker;
			case "blocked":
				return theme.fg("warning", `${prefix}${checkbox.unchecked} ${todo.content} (blocked)`) + marker;
			default:
				if (matched) return theme.fg("accent", `${prefix}${checkbox.unchecked} ${todo.content}`) + marker;
				return theme.fg("dim", `${prefix}${checkbox.unchecked} ${todo.content}`) + marker;
		}
	}

	#cancelTodoAutoClearTimer(): void {
		this.#todoAutoClearGeneration++;
		if (this.#todoAutoClearTimer) {
			clearTimeout(this.#todoAutoClearTimer);
			this.#todoAutoClearTimer = undefined;
		}
	}

	#syncTodoHudState(owner: AgentSession): void {
		this.#cancelTodoAutoClearTimer();
		const phases = this.todoPhases;
		this.#todoHudHidden = false;
		if (phases.length === 0) return;
		const tasks = phases.flatMap(phase => phase.tasks);
		if (tasks.length === 0 || tasks.some(task => !isClosedTodo(task))) return;
		// Everything is closed: retire the HUD after the display delay. This is client state only —
		// the authority owns the todo rows and their lifecycle.
		const generation = this.#todoAutoClearGeneration;
		const snapshotKey = JSON.stringify(phases);
		this.#todoAutoClearTimer = setTimeout(() => {
			this.#todoAutoClearTimer = undefined;
			if (generation !== this.#todoAutoClearGeneration || JSON.stringify(this.todoPhases) !== snapshotKey) return;
			this.#todoHudHidden = true;
			this.#renderTodoList();
			this.ui.requestRender();
		}, TODO_HUD_AUTO_CLEAR_SECONDS * 1000);
		this.#todoAutoClearTimer.unref?.();
	}

	/**
	 * Render the ctrl+p model-role cycle chip track into its own anchored
	 * container (just above the editor), mirroring the todo HUD: the container is
	 * cleared and rebuilt in place on every cycle, so rapid presses or concurrent
	 * chat activity can never stack duplicate tracks into the scrollback.
	 */
	showModelCycleTrack(segments: readonly TrackSegment[], activeIndex: number): void {
		this.#renderModelCycleTrack({ segments, activeIndex });
		this.#syncModelCycleClearTimer();
		this.ui.requestRender();
	}

	#renderModelCycleTrack(track: { segments: readonly TrackSegment[]; activeIndex: number } | null): void {
		this.modelCycleContainer.clear();
		if (!track) return;
		this.modelCycleContainer.addChild(new Spacer(1));
		this.modelCycleContainer.addChild(
			new DescribedComponent(
				new Text(renderSegmentTrack([...track.segments], track.activeIndex), 1, 0),
				describeSegmentTrack(track.segments, track.activeIndex),
			),
		);
	}

	#cancelModelCycleClearTimer(): void {
		if (!this.#modelCycleClearTimer) return;
		clearTimeout(this.#modelCycleClearTimer);
		this.#modelCycleClearTimer = undefined;
	}

	#syncModelCycleClearTimer(): void {
		this.#cancelModelCycleClearTimer();
		this.#modelCycleClearTimer = setTimeout(() => {
			this.#modelCycleClearTimer = undefined;
			this.#renderModelCycleTrack(null);
			this.ui.requestRender();
		}, MODEL_CYCLE_TRACK_CLEAR_MS);
		this.#modelCycleClearTimer.unref?.();
	}

	#getActivePhase(phases: TodoPhase[]): TodoPhase | undefined {
		const nonEmpty = phases.filter(phase => phase.tasks.length > 0);
		const active = nonEmpty.find(phase =>
			phase.tasks.some(task => task.status === "pending" || task.status === "in_progress"),
		);
		return active ?? nonEmpty[nonEmpty.length - 1];
	}

	#renderTodoList(): void {
		this.todoContainer.clear();
		this.#todoHudNative = undefined;
		if (this.#todoHudHidden) return;
		const phases = this.todoPhases.filter(phase => phase.tasks.length > 0);
		if (phases.length === 0) return;
		const expanded = this.todoExpanded;
		const multiPhase = phases.length > 1;
		const activeIdx = phases.indexOf(this.#getActivePhase(phases) ?? phases[0]);
		// Fixed budgets keep the HUD bounded regardless of plan size / progress.
		const subsequentStageCap = 4; // stages shown after the active one (a trailing summary row covers the rest)
		const activeTaskCap = 5; // open tasks previewed for the active stage

		const isMatched = (_todo: TodoItem): boolean => false;

		// Task subtree for a phase. Collapsed runs the shared walking-viewport
		// policy (completed/abandoned omitted, active work pulled to the head,
		// then following pending tasks) so the HUD and the transient tool result
		// can never disagree about the current work (#5873). Expanded lists all.
		const renderTasks = (phase: TodoPhase): string[] => {
			if (expanded) {
				return renderTreeList(
					{
						items: phase.tasks,
						expanded: true,
						renderItem: todo => this.#formatTodoLine(todo, "", isMatched(todo)),
					},
					theme,
				);
			}
			const selection = selectCollapsedTodos(phase.tasks, isMatched, activeTaskCap);
			return renderTreeList(
				{
					items: selection.items,
					itemType: "task",
					trailingSummary: selection.summary,
					renderItem: todo => this.#formatTodoLine(todo, "", isMatched(todo)),
				},
				theme,
			);
		};

		// One phase node. The active stage is highlighted with normal-brightness task
		// progress; other stages render their whole row (name + progress) in the
		// brighter muted gray. Overall progress lives in the tree spine (below).
		const renderPhase = (phase: TodoPhase, oneBased: number, isActive: boolean): string | string[] => {
			const label = multiPhase ? formatPhaseDisplayName(phase.name, oneBased) : phase.name;
			// Closed, not just completed: the collapsed task window hides abandoned
			// tasks too, so counting only completions leaves the phase reading stuck.
			const done = phase.tasks.filter(isClosedTodo).length;
			const progress = ` · ${done}/${phase.tasks.length}`;
			if (!isActive) {
				const header = theme.fg("muted", label) + theme.fg("dim", progress);
				return expanded ? [header, ...renderTasks(phase)] : header;
			}
			const header = theme.bold(theme.fg("accent", label)) + theme.fg("dim", progress);
			return [header, ...renderTasks(phase)];
		};

		// Collapsed: active stage + a bounded number of following stages, with a
		// "… n more stages" row for anything past the cap. Expanded: every stage
		// from the top. Roman numerals stay tied to the real phase index.
		const baseIdx = expanded ? 0 : activeIdx;
		const phaseSlice = expanded ? phases.slice(baseIdx) : phases.slice(baseIdx, baseIdx + 1 + subsequentStageCap);
		const hiddenStages = phases.length - baseIdx - phaseSlice.length;

		// Flatten the stage tree into content rows plus a per-row top-level spine
		// glyph (`├─` for stage rows, `│` for continuations). The spine never
		// closes downward — a short elbow tail (`└────`) ends the block instead,
		// so spine + bend + tail form one continuous progress path.
		const spineGlyphs: string[] = [];
		const contentLines: string[] = [];
		const pushBlock = (block: string | string[]): void => {
			const rows = Array.isArray(block) ? block : [block];
			if (rows.length === 0) return;
			spineGlyphs.push(`${theme.tree.branch} `);
			contentLines.push(replaceTabs(rows[0]!));
			for (let i = 1; i < rows.length; i++) {
				spineGlyphs.push(`${theme.tree.vertical}  `);
				contentLines.push(replaceTabs(rows[i]!));
			}
		};
		for (let i = 0; i < phaseSlice.length; i++) {
			pushBlock(renderPhase(phaseSlice[i], baseIdx + i + 1, baseIdx + i === activeIdx));
		}
		if (hiddenStages > 0) {
			pushBlock(theme.fg("muted", formatMoreItems(hiddenStages, "stage")));
		}

		// Closing tail: hook + a few horizontals. Every tail cell is 1 column in
		// both glyph sets, so string slicing below splits it by visible cells.
		const tailLen = 6;
		const tail = theme.tree.hook + theme.tree.horizontal.repeat(Math.max(0, tailLen - visibleWidth(theme.tree.hook)));

		// Overall progress (summed across every stage) fills the path in reading
		// order: down the spine, around the bend, out along the tail.
		// Clamp so partial progress lights at least one cell; a closed plan fills
		// the entire path until the configured auto-clear removes the HUD.
		const totalTasks = phases.reduce((sum, phase) => sum + phase.tasks.length, 0);
		const closedTasks = phases.reduce((sum, phase) => sum + phase.tasks.filter(isClosedTodo).length, 0);
		const pathLen = contentLines.length + tailLen;
		let filled = Math.round((closedTasks / totalTasks) * pathLen);
		if (closedTasks > 0) filled = Math.max(filled, 1);
		if (closedTasks < totalTasks) filled = Math.min(filled, pathLen - 1);

		const lines = ["", theme.bold(theme.fg("accent", "TODO"))];
		for (let i = 0; i < contentLines.length; i++) {
			lines.push(` ${theme.fg(i < filled ? "accent" : "dim", spineGlyphs[i]!)}${contentLines[i]}`);
		}
		const tailFilled = Math.max(0, Math.min(filled - contentLines.length, tail.length));
		lines.push(` ${theme.fg("accent", tail.slice(0, tailFilled))}${theme.fg("dim", tail.slice(tailFilled))}`);
		this.todoContainer.addChild(new Text(lines.join("\n"), 1, 0));

		// Native: the same stage window as a tree, overall progress as a bar.
		const describeTask = (todo: TodoItem, id: string): TspTreeNode => {
			const done = todo.status === "completed";
			const token =
				todo.status === "completed"
					? "success del"
					: todo.status === "abandoned"
						? "error del"
						: todo.status === "blocked"
							? "warning"
							: todo.status === "in_progress" || isMatched(todo)
								? "accent"
								: "dim";
			const label: TspSpan[] = [span(todo.status === "blocked" ? `${todo.content} (blocked)` : todo.content, token)];
			const notes = todo.notes?.length ?? 0;
			if (notes > 0) label.push(span(` +${notes}`, "dim em"));
			return { id, label, icon: done ? "check" : "circle" };
		};
		const describeTasks = (phase: TodoPhase, phaseIndex: number): TspTreeNode[] => {
			const index = (todo: TodoItem): string => `${phaseIndex}.${phase.tasks.indexOf(todo)}`;
			if (expanded) return phase.tasks.map(todo => describeTask(todo, index(todo)));
			const selection = selectCollapsedTodos(phase.tasks, isMatched, activeTaskCap);
			const nodes = selection.items.map(todo => describeTask(todo, index(todo)));
			if (selection.summary) nodes.push({ id: `${phaseIndex}.more`, label: [span(selection.summary, "muted")] });
			return nodes;
		};
		const phaseNodes: TspTreeNode[] = phaseSlice.map((phase, offset) => {
			const phaseIndex = baseIdx + offset;
			const isActive = phaseIndex === activeIdx;
			const name = multiPhase ? formatPhaseDisplayName(phase.name, phaseIndex + 1) : phase.name;
			const done = phase.tasks.filter(isClosedTodo).length;
			const open = isActive || expanded;
			return {
				id: `${phaseIndex}`,
				label: [span(name, isActive ? "accent strong" : "muted"), span(` · ${done}/${phase.tasks.length}`, "dim")],
				open,
				children: open ? describeTasks(phase, phaseIndex) : undefined,
			};
		});
		if (hiddenStages > 0) {
			phaseNodes.push({ id: "more", label: [span(formatMoreItems(hiddenStages, "stage"), "muted")] });
		}
		const fallback = node(
			"col",
			{ role: "omp.hud.todo" },
			[
				row(
					[
						text([span("TODO", "accent strong")]),
						node("progress", { value: closedTasks / totalTasks, label: `${closedTasks}/${totalTasks}` }),
					],
					{ gap: "sm", align: "center" },
				),
				node("tree", { nodes: phaseNodes }),
			],
			"todo",
		);
		// A `checklist` HUD is a pill with the whole plan as its popover.
		const checklistPhases: TspChecklistPhase[] = phases.map((phase, phaseIndex) => ({
			id: `${phaseIndex}`,
			title: multiPhase ? formatPhaseDisplayName(phase.name, phaseIndex + 1) : phase.name,
			collapsed: phase.tasks.every(isClosedTodo) || undefined,
			items: phase.tasks.map((todo, taskIndex): TspChecklistItem => {
				const note = todo.blocker ?? todo.notes?.at(-1);
				return {
					id: `${phaseIndex}.${taskIndex}`,
					text: todo.content,
					status:
						todo.status === "completed"
							? "done"
							: todo.status === "abandoned"
								? "dropped"
								: todo.status === "blocked"
									? "blocked"
									: todo.status === "in_progress" || isMatched(todo)
										? "active"
										: "pending",
					note,
				};
			}),
		}));
		this.#todoHudNative = {
			checklist: node(
				"checklist",
				{ phases: checklistPhases, mode: "hud", role: "omp.hud.todo" },
				undefined,
				"todo",
			),
			fallback,
		};
	}

	isCompactTodoMode(): boolean {
		const rows = this.ui?.terminal?.rows ?? process.stdout.rows ?? 24;
		return rows < TODO_COMPACT_TERMINAL_ROWS_THRESHOLD;
	}

	renderCompactStatusLine(width: number, childLines: readonly string[]): readonly string[] {
		const phases = this.todoPhases.filter(phase => phase.tasks.length > 0);
		if (phases.length === 0) return childLines;

		const isMatched = (_todo: TodoItem): boolean => false;
		const totalTasks = phases.reduce((sum, phase) => sum + phase.tasks.length, 0);
		const closedTasks = phases.reduce((sum, phase) => sum + phase.tasks.filter(isClosedTodo).length, 0);
		const activeTask = phases.flatMap(phase => phase.tasks).find(task => !isClosedTodo(task));

		const header = `${theme.bold(theme.fg("accent", "TODO"))} ${theme.fg("dim", `${closedTasks}/${totalTasks}`)}`;
		const taskStr = activeTask
			? this.#formatTodoLine(activeTask, "", isMatched(activeTask))
			: theme.fg("success", `${theme.checkbox.checked} done`);
		const rightLine = `${header} ${theme.fg("dim", "·")} ${taskStr}`;

		const rightPad = " ";
		const rightWidth = visibleWidth(rightLine) + 1;

		let leftLine = "";
		if (childLines.length > 0) {
			leftLine = childLines[childLines.length - 1] ?? "";
		}

		const leftWidth = visibleWidth(leftLine);
		const minGap = 2;

		let combinedLine: string;
		if (leftWidth === 0) {
			if (rightWidth <= width) {
				const gap = Math.max(0, width - rightWidth);
				combinedLine = " ".repeat(gap) + rightLine + rightPad;
			} else {
				const maxRight = Math.max(4, width - 1);
				const truncatedRight = truncateToWidth(rightLine, maxRight);
				const gap = Math.max(0, width - visibleWidth(truncatedRight) - 1);
				combinedLine = " ".repeat(gap) + truncatedRight + rightPad;
			}
		} else {
			if (leftWidth + minGap + rightWidth <= width) {
				const gap = width - leftWidth - rightWidth;
				combinedLine = leftLine + " ".repeat(gap) + rightLine + rightPad;
			} else {
				const maxRight = Math.min(rightWidth, Math.max(10, Math.floor(width * 0.45)));
				const truncatedRight = truncateToWidth(rightLine, maxRight);
				const truncatedRightWidth = visibleWidth(truncatedRight) + 1;
				const availableLeft = Math.max(4, width - truncatedRightWidth - minGap);
				const truncatedLeft = truncateToWidth(leftLine, availableLeft);
				const gap = Math.max(minGap, width - visibleWidth(truncatedLeft) - truncatedRightWidth);
				combinedLine = truncatedLeft + " ".repeat(gap) + truncatedRight + rightPad;
			}
		}

		const leadingLines = childLines.length > 1 ? childLines.slice(0, -1) : [""];
		return [...leadingLines, combinedLine];
	}

	async #loadTodoList(source: AgentSession = this.session): Promise<void> {
		this.todoPhases = source.getTodoPhases();
		this.#todoPhasesOwner = source;
		this.#syncTodoHudState(source);
		this.#renderTodoList();
	}

	#applyVimMode(editor: CustomEditor): void {
		editor.setVimMode(cfgTuiVimMode.get(settings));
		editor.onYank = text => {
			void this.#copyYankToClipboard(text);
		};
		// Recolor the prompt border on every mode switch: in a modal editor the mode has to be
		// visible at a glance, and the border is where bash/python mode already signal themselves.
		editor.onVimModeChange = () => this.#syncVimStatus(editor);
	}

	/**
	 * Re-apply `tui.vimMode` to the live editor. `setVimMode` is idempotent and always lands in
	 * Insert, so toggling the setting mid-session can never strand the editor in a mode where
	 * ordinary typing does nothing.
	 */
	#applyVimModeSetting(): void {
		this.#applyVimMode(this.editor);
		this.#syncVimStatus(this.editor);
		this.ui.requestRender();
	}

	/**
	 * Push an editor's modal state into the three places that surface it: the prompt border, the
	 * status-line `vim` segment, and the hardware cursor shape. Called on every mode/pending change,
	 * so it stays cheap — the terminal dedupes an unchanged DECSCUSR shape. Takes the editor rather
	 * than reading `this.editor`: `setEditorComponent` configures its replacement before swapping it in.
	 */
	#syncVimStatus(editor: CustomEditor): void {
		this.statusLine.setVimStatus(
			editor.vimEnabled
				? {
						mode: editor.vimMode,
						pending: editor.vimPending,
						selectedLines: editor.vimSelectedLines,
						display: cfgTuiVimModeDisplay.get(settings),
					}
				: undefined,
		);
		// Insert gets the bar every non-modal editor uses; Normal/Visual rest *on* a grapheme, which
		// is a block. Sent unconditionally: the software cursor carries the same distinction itself
		// (Editor#cursorCell), and when the hardware cursor is hidden this only reshapes something
		// invisible. ProcessTerminal dedupes, so an unchanged shape costs nothing per frame.
		this.ui.terminal.setCursorShape?.(
			editor.vimEnabled && editor.vimMode !== "insert" ? "block" : editor.vimEnabled ? "bar" : "default",
		);
		this.updateEditorBorderColor();
	}

	async #copyYankToClipboard(content: string): Promise<void> {
		try {
			await copyToClipboard(content);
		} catch (error) {
			// Best-effort: the yank already landed in the internal register, so `p` still puts it
			// back. A per-yank warning would spam headless/SSH sessions on every `yy`.
			logger.debug("Vim yank clipboard copy failed", {
				error: error instanceof Error ? error.message : String(error),
			});
		}
	}

	#resolveLocalRoot(): string {
		return path.resolve(
			resolveLocalRoot({
				getArtifactsDir: () => this.sessionManager.getArtifactsDir(),
				getSessionId: () => this.sessionManager.getSessionId(),
			}),
		);
	}

	stop(): void {
		this.#appearanceRefreshRequest = undefined;
		this.#dialogLifetime.abort();
		clearTimeout(this.#composerStatusPersistTimer);
		this.#composerStatusPersistTimer = undefined;
		this.#persistComposerStatus();
		if (this.loadingAnimation) this.#stopLoadingAnimation(false);
		stopSharedSpinnerTicker();
		this.#cancelTodoAutoClearTimer();
		for (const unsubscribe of this.#eventBusUnsubscribers) unsubscribe();
		this.#eventBusUnsubscribers = [];
		this.#eventController.dispose();
		this.statusLine.dispose();
		if (this.#resizeHandler) {
			process.stdout.removeListener("resize", this.#resizeHandler);
			this.#resizeHandler = undefined;
		}
		if (this.unsubscribe) this.unsubscribe();
		if (this.#cleanupUnsubscribe) this.#cleanupUnsubscribe();
		this.#disposeImageBlobWriter?.();
		this.#disposeImageBlobWriter = undefined;
		this.#hideSessionInfo();
		if (this.#ownsStartedUi) {
			this.ui.stop();
			this.#ownsStartedUi = false;
		}
		this.isInitialized = false;
	}

	async #quitHostOwned(code: number): Promise<void> {
		try {
			await postmortem.quit(code);
		} catch {
			postmortem.exitProcess(code);
		}
	}

	async shutdown(): Promise<void> {
		if (this.#isShuttingDown) return;
		if (this.#teardownFailed) return;
		this.#beginClose();
		this.showStatus("Closing session…");
		try {
			await this.#teardown();
		} catch (error) {
			this.#handleTeardownError("close", error);
			return;
		}
		await this.#quitHostOwned(0);
	}

	#handleTeardownError(action: "close" | "restart", error: unknown): void {
		this.#isShuttingDown = false;
		const detail = error instanceof Error ? error.message : String(error);
		// Arm the escape hatch only once dispose() has begun: its promise is
		// memoized, so a retry can only re-fail. A failure BEFORE dispose (a
		// transient BTW/live-command flush) leaves the session undisposed and
		// the teardown genuinely retryable, so it must not force-quit.
		this.#teardownFailed = this.session.isDisposed;
		this.showError(
			this.#teardownFailed
				? `Could not ${action} session: ${detail}\nPress ${appKey(this.keybindings, "app.clear")} again to exit without saving the session log.`
				: `Could not ${action} session: ${detail}`,
		);
	}



	/**
	 * Claim the one-shot `shutdown()`/`restart()` close and acknowledge it before
	 * any await: worktree exit planning, live commands, and BTW history writes can
	 * all stall, and the user must see a reason for the pause.
	 */
	#beginClose(): void {
		this.#isShuttingDown = true;
		this.showStatus("Closing session…");
	}

	/** Shared `shutdown()` teardown: dispose the gateway session and hand the terminal back. */
	async #teardown(): Promise<void> {
		// An in-flight close must not stack: `#beginClose()` already latched the state.
		const stillClosingTimer = setTimeout(() => {
			this.showStatus("Still closing…");
		}, STILL_CLOSING_DELAY_MS);
		try {
			await this.session.dispose();
		} finally {
			clearTimeout(stillClosingTimer);
		}
		// Stop from the last committed frame so the parent shell gets the cursor immediately after
		// visible content; no forced final render on torn-down state.
		this.ui.closeNative();
		await this.ui.terminal.drainInput(1000);
		this.stop();
	}


	requestShutdown(): void {
		this.shutdownRequested = true;
		// Background extensions do not submit terminal input. Start the same
		// settled-boundary check without waiting for another user keystroke.
		void this.checkShutdownRequested().catch(error => {
			this.showError(`Shutdown failed: ${error instanceof Error ? error.message : String(error)}`);
		});
	}

	hasPendingSubmission(): boolean {
		return this.#pendingSubmittedInput !== undefined;
	}

	async checkShutdownRequested(): Promise<void> {
		if (!this.shutdownRequested || this.isShuttingDown) return;
		await this.session.waitForIdle();
		if (
			this.isShuttingDown ||
			this.hasPendingSubmission() ||
			this.session.isStreaming ||
			this.session.queuedMessageCount > 0
		) {
			return;
		}
		await this.shutdown();
	}

	present(content: Component | readonly Component[]): void {
		if (Array.isArray(content)) {
			for (const item of content) this.#mountChatChild(item);
		} else {
			this.#mountChatChild(content as Component);
		}
		this.ui.requestRender();
	}

	presentCommandOutput(content: Component | readonly Component[]): void {
		if (!this.session.isStreaming || this.ui.nativeRendering) {
			this.present(content);
			return;
		}
		const sessionId = this.sessionManager.getSessionId();
		if (this.#pendingCommandOutput.length > 0 && this.#pendingCommandOutputSessionId !== sessionId) {
			this.#pendingCommandOutput = [];
			this.#pendingCommandOutputCommands = 0;
		}
		this.#pendingCommandOutputSessionId = sessionId;
		const items = Array.isArray(content) ? content : [content as Component];
		this.#pendingCommandOutput.push(...items);
		this.#pendingCommandOutputCommands += 1;
		this.#renderDeferredCommandNotice();
		this.ui.requestRender();
	}

	showSessionInfo(info: string, context?: ContextUsage): void {
		this.#hideSessionInfo();
		const overlay = new SessionInfoOverlay(this.ui, info, () => this.#hideSessionInfo(), context);
		this.#sessionInfoOverlayHandle = this.ui.showOverlay(overlay, {
			anchor: "bottom-center",
			width: "100%",
			maxHeight: "100%",
			margin: 0,
		});
		this.ui.setFocus(overlay);
		this.ui.requestRender();
	}

	#hideSessionInfo(): void {
		const handle = this.#sessionInfoOverlayHandle;
		this.#sessionInfoOverlayHandle = undefined;
		if (!handle) return;
		handle.hide();
		// Focus the visible editor-slot owner, not this.editor: an extension ask
		// or hook may have swapped into editorContainer while the panel was open,
		// and keys must reach the visible prompt (same stale-focus class as #3349).
		this.ui.setFocus(this.editor);
		this.ui.requestRender();
	}

	#renderDeferredCommandNotice(): void {
		this.deferredCommandContainer.clear();
		if (this.#pendingCommandOutput.length === 0) return;
		const maxRows = Math.max(
			DEFERRED_PREVIEW_MIN_ROWS,
			Math.floor(this.ui.terminal.rows * DEFERRED_PREVIEW_VIEWPORT_FRACTION),
		);
		this.deferredCommandContainer.addChild(new Spacer(1));
		this.deferredCommandContainer.addChild(
			new DeferredCommandPreview([...this.#pendingCommandOutput], maxRows, this.#pendingCommandOutputCommands),
		);
	}

	flushPendingCommandOutput(): void {
		if (this.#pendingCommandOutput.length === 0) return;
		const pending = this.#pendingCommandOutput;
		const pendingSessionId = this.#pendingCommandOutputSessionId;
		this.#pendingCommandOutput = [];
		this.#pendingCommandOutputSessionId = undefined;
		this.#pendingCommandOutputCommands = 0;
		this.#renderDeferredCommandNotice();
		if (pendingSessionId !== this.sessionManager.getSessionId()) return;
		this.present(pending);
	}

	#mountChatChild(item: Component): void {
		this.chatContainer.addChild(item);
		if (item instanceof ChatBlock) item.mount(this.#chatHost);
	}

	resetTranscript(): void {
		this.transcriptMessageComponents = new WeakMap<AgentMessage, Component>();
		this.chatContainer.dispose();
		this.chatContainer.clear();
	}

	showStatus(message: string, options?: ShowStatusOptions): void {
		this.#uiHelpers.showStatus(message, options);
	}

	showError(message: string): void {
		this.#pendingSubmittedInput = undefined;
		this.#pendingSubmissionPreservesDraft = false;
		this.clearOptimisticUserMessage();
		this.#pendingWorkingMessage = undefined;
		if (this.loadingAnimation) {
			this.#stopLoadingAnimation(true);
		}
		this.#uiHelpers.showError(message);
	}

	showPinnedError(message: string): void {
		this.errorBannerContainer.clear();
		this.errorBannerContainer.addChild(new ErrorBannerComponent(message, () => this.clearPinnedError()));
		this.ui.requestRender();
	}

	clearPinnedError(): void {
		if (this.errorBannerContainer.children.length === 0) return;
		this.errorBannerContainer.clear();
		this.ui.requestRender();
	}

	showWarning(message: string, options?: { hideWithToolActivity?: boolean }): void {
		this.#uiHelpers.showWarning(message, options);
	}

	#clearWorkingMessageAccentCache(): void {
		this.#workingMessageAccentCacheKey = undefined;
		this.#workingMessageAccentCacheValue = undefined;
		this.#workingMessageAccentCacheHasValue = false;
	}

	#buildWorkingMessageAccentCacheKey(): WorkingMessageAccentCacheKey {
		const sessionAccentEnabled = !isSettingsInitialized() || cfgStatusLineSessionAccent.get(settings) !== false;
		return {
			sessionAccentEnabled,
			sessionName: sessionAccentEnabled ? this.sessionManager.getSessionName() : undefined,
			accentSurfaceLuminance: theme.accentSurfaceLuminance,
		};
	}

	#workingMessageAccentCacheKeyEquals(a: WorkingMessageAccentCacheKey, b: WorkingMessageAccentCacheKey): boolean {
		return (
			a.sessionName === b.sessionName &&
			a.accentSurfaceLuminance === b.accentSurfaceLuminance &&
			a.sessionAccentEnabled === b.sessionAccentEnabled
		);
	}

	#cacheWorkingMessageAccent(
		key: WorkingMessageAccentCacheKey,
		value: WorkingMessageAccent | undefined,
	): WorkingMessageAccent | undefined {
		this.#workingMessageAccentCacheKey = key;
		this.#workingMessageAccentCacheValue = value;
		this.#workingMessageAccentCacheHasValue = true;
		return value;
	}

	#getWorkingMessageAccent(): WorkingMessageAccent | undefined {
		const key = this.#buildWorkingMessageAccentCacheKey();
		if (
			this.#workingMessageAccentCacheHasValue &&
			this.#workingMessageAccentCacheKey &&
			this.#workingMessageAccentCacheKeyEquals(key, this.#workingMessageAccentCacheKey)
		) {
			return this.#workingMessageAccentCacheValue;
		}
		if (!key.sessionAccentEnabled || !key.sessionName) {
			return this.#cacheWorkingMessageAccent(key, undefined);
		}
		const hex = getSessionAccentHex(key.sessionName, theme.sessionAccentInputs);
		const main = getSessionAccentAnsi(hex);
		const dim = getSessionAccentAnsi(adjustHsv(hex, { s: 0.55, v: 0.65 }));
		return this.#cacheWorkingMessageAccent(key, main && dim ? { main, dim } : undefined);
	}

	ensureLoadingAnimation(): void {
		if (!this.loadingAnimation) {
			this.#clearWorkingMessageAccentCache();
			this.statusContainer.disposeChildren();
			const messageColorFn = ((message: string) =>
				renderWorkingMessage(message, this.#getWorkingMessageAccent())) as LoaderMessageColorFn & {
				animated?: true;
			};
			// Shimmer drives the 30fps redraw; when it is disabled the working
			// message is static, so leave `animated` unset and let the loader use
			// the spinner-only ~12.5fps cadence instead of repainting a frozen line.
			if (shimmerEnabled()) messageColorFn.animated = true;
			this.loadingAnimation = new Loader(
				this.ui,
				spinner => {
					const accent = this.#getWorkingMessageAccent();
					return accent ? `${accent.dim}${spinner}\x1b[39m` : theme.fg("muted", spinner);
				},
				messageColorFn,
				DEFAULT_WORKING_MESSAGE,
				// The brand spinner lives in the status line while working; this row
				// leads with the interrupt affordance instead of a second spinner.
				// The leading space nudges the row one column right of the flush-left
				// status rows so the interrupt glyph reads as indented.
				[` ${appKey(this.keybindings, "app.interrupt")}`],
			);
			this.loadingAnimation.setTrailer(() => this.#workingRowTrailer());
			this.loadingAnimation.setWorkingRow(
				() => this.#workingRowSpec(),
				() => this.interruptFromPointer(),
			);
			this.#workingMessage = DEFAULT_WORKING_MESSAGE;
			this.#workingStartedAt = this.viewSession.runStartedAt ?? Date.now();
			this.statusContainer.addChild(this.loadingAnimation);
		} else if (!this.statusContainer.children.includes(this.loadingAnimation)) {
			this.statusContainer.disposeChildren();
			this.loadingAnimation.start();
			this.statusContainer.addChild(this.loadingAnimation);
			this.ui.requestRender();
		}
		this.applyPendingWorkingMessage();
	}

	#stopLoadingAnimation(clearStatusContainer: boolean): void {
		if (!this.loadingAnimation) return;
		this.loadingAnimation.stop();
		this.loadingAnimation = undefined;
		this.#clearWorkingMessageAccentCache();
		if (clearStatusContainer) {
			this.statusContainer.disposeChildren();
		}
	}

	setWorkingMessage(message?: string | (() => string)): void {
		if (message === undefined) {
			this.#pendingWorkingMessage = undefined;
			if (this.loadingAnimation) {
				this.loadingAnimation.setMessage(DEFAULT_WORKING_MESSAGE);
				this.#workingMessage = DEFAULT_WORKING_MESSAGE;
			}
			return;
		}

		if (this.loadingAnimation) {
			const label = typeof message === "function" ? this.#oneRowWorkingLabel(message) : message;
			this.loadingAnimation.setMessage(label);
			this.#workingMessage = label;
			return;
		}

		this.#pendingWorkingMessage = message;
	}

	#oneRowWorkingLabel(message: () => string): () => string {
		return () => {
			const lead = visibleWidth(` ${appKey(this.keybindings, "app.interrupt")}`) + 1;
			return truncateToWidth(message(), Math.max(1, this.ui.terminal.columns - lead - 2));
		};
	}

	applyPendingWorkingMessage(): void {
		if (this.#pendingWorkingMessage === undefined) {
			return;
		}

		const message = this.#pendingWorkingMessage;
		this.#pendingWorkingMessage = undefined;
		this.setWorkingMessage(message);
	}

	clearEditor(): void {
		this.#uiHelpers.clearEditor();
	}

	updatePendingMessagesDisplay(): void {
		this.#uiHelpers.updatePendingMessagesDisplay();
	}

	isKnownSlashCommand(text: string): boolean {
		return this.#uiHelpers.isKnownSlashCommand(text);
	}

	addMessageToChat(
		message: AgentMessage,
		options?: {
			imageLinks?: readonly (string | undefined)[];
			reuseSettledComponent?: boolean;
		},
	): Component[] {
		return this.#uiHelpers.addMessageToChat(message, options);
	}

	renderSessionContext(options?: RenderSessionContextOptions): void {
		for (const message of this.viewSession.messages) this.noteDisplayableThinkingContent(message);
		this.#uiHelpers.renderSessionContext(options);
	}

	async renderInitialMessages(options?: {
		preserveExistingChat?: boolean;
		clearTerminalHistory?: boolean;
	}): Promise<void> {
		await this.#uiHelpers.renderInitialMessages(options);
	}

	findLastAssistantMessage(): AssistantMessage | undefined {
		return this.#uiHelpers.findLastAssistantMessage();
	}

	extractAssistantText(message: AssistantMessage): string {
		return this.#uiHelpers.extractAssistantText(message);
	}

	handleTodoCommand(args: string): Promise<void> {
		return this.#todoCommandController.handleTodoCommand(args);
	}

	handleSessionCommand(): Promise<void> {
		return this.#commandController.handleSessionCommand();
	}

	handleUsageCommand(): Promise<void> {
		return this.#commandController.handleUsageCommand();
	}

	handleHotkeysCommand(): void {
		this.#commandController.handleHotkeysCommand();
	}

	handleToolsCommand(): void {
		this.#commandController.handleToolsCommand();
	}

	handleContextCommand(): void {
		this.#commandController.handleContextCommand();
	}

	handleClearCommand(): Promise<void> {
		return this.#commandController.handleClearCommand();
	}

	handleRenameCommand(title: string): Promise<void> {
		return this.#commandController.handleRenameCommand(title);
	}

	openInBrowser(urlOrPath: string): void {
		this.#commandController.openInBrowser(urlOrPath);
	}

	showSettingsSelector(): void {
		this.#selectorController.showSettingsSelector();
	}

	showModelSelector(options?: { temporaryOnly?: boolean; selectOnly?: SelectOnlyModelSelectorOptions }): void {
		this.#selectorController.showModelSelector(options);
	}

	showModelHub(): void {
		this.#selectorController.showModelHub();
	}

	async switchSessionModel(model: Model, thinkingLevel?: ConfiguredThinkingLevel): Promise<void> {
		await this.session.setModel(model);
		if (thinkingLevel !== undefined) this.session.setThinkingLevel(thinkingLevel);
		this.ui.requestRender();
	}

	showCopySelector(): void {
		this.#selectorController.showCopySelector();
	}

	showThinkingSelector(): void {
		this.#selectorController.showThinkingSelector();
	}

	showSessionSelector(): void {
		void this.#selectorController.showSessionSelector();
	}

	/** Resume a session from the gateway list: pair a connection on it and re-seed the transcript. */
	async handleResumeSession(sessionId: string, _options?: ResumeSessionOptions): Promise<void> {
		const switched = await this.session.switchGatewaySession(sessionId);
		if (!switched) return;
		this.resetTranscript();
		this.sessionManager.reset(this.session.gateway?.initialMessages() ?? []);
		await this.renderInitialMessages({ preserveExistingChat: true });
		await this.reloadTodos();
		this.ui.requestRender(true);
	}

	handleCtrlC(): void {
		this.#inputController.handleCtrlC();
	}

	handleCtrlD(): void {
		this.#inputController.handleCtrlD();
	}

	handleCtrlZ(): void {
		this.#inputController.handleCtrlZ();
	}

	resetDisplayAfterAppearanceRefresh(): void {
		const refreshAppearance = this.ui.terminal.refreshAppearance;
		if (refreshAppearance) {
			const token = this.#nextAppearanceRequestToken++;
			const request = {
				token,
				deadline: Date.now() + CTRL_L_APPEARANCE_RESPONSE_DEADLINE_MS,
			};
			this.#appearanceRefreshRequest = request;
			const acceptedToken = refreshAppearance.call(this.ui.terminal, token);
			if (acceptedToken !== token && this.#appearanceRefreshRequest === request) {
				this.#appearanceRefreshRequest = undefined;
			}
		} else {
			this.#appearanceRefreshRequest = undefined;
		}
		// Preserve Ctrl+L's immediate full replay when the probe is unsupported,
		// receives no response, or reports an unchanged appearance.
		this.ui.resetDisplay();
	}

	handleDequeue(): void {
		this.#inputController.handleDequeue();
	}

	restoreCompactionQueueToEditor(status?: string): void {
		this.#inputController.restoreCompactionQueueToEditor(status);
	}

	waitForSubmissionDispatches(): Promise<void> {
		return this.#inputController.waitForSubmissionDispatches();
	}

	handleImagePaste(): Promise<boolean> {
		return this.#inputController.handleImagePaste();
	}

	handleImagePathPaste(path: string): Promise<void> {
		return this.#inputController.handleImagePathPaste(path);
	}

	handleQueueCommand(
		message: string,
		detached?: Pick<SubmittedUserInput, "text" | "images" | "imageLinks">,
	): Promise<void> {
		return this.#inputController.handleQueueCommand(message, detached);
	}

	cycleThinkingLevel(): void {
		this.#inputController.cycleThinkingLevel();
	}

	toggleToolOutputExpansion(): void {
		this.#inputController.toggleToolOutputExpansion();
	}

	setToolsExpanded(expanded: boolean): void {
		this.#inputController.setToolsExpanded(expanded);
	}

	toggleThinkingBlockVisibility(): void {
		this.#inputController.toggleThinkingBlockVisibility();
	}

	toggleTodoExpansion(): void {
		this.setTodoExpanded(!this.todoExpanded);
	}

	setTodoExpanded(expanded: boolean): void {
		this.todoExpanded = expanded;
		if (expanded) {
			this.#cancelTodoAutoClearTimer();
			this.#todoHudHidden = false;
		}
		this.#renderTodoList();
		this.ui.requestRender();
	}

	setTodos(todos: TodoItem[] | TodoPhase[]): void {
		if (todos.length > 0 && "tasks" in todos[0]) {
			this.todoPhases = todos as TodoPhase[];
		} else {
			this.todoPhases = [
				{
					name: "Todos",
					tasks: todos as TodoItem[],
				},
			];
		}
		this.#todoPhasesOwner = this.viewSession;
		this.#syncTodoHudState(this.viewSession);
		this.#renderTodoList();
		this.ui.requestRender();
	}

	async reloadTodos(source: AgentSession = this.session): Promise<void> {
		await this.#loadTodoList(source);
		this.ui.requestRender();
	}

	openExternalEditor(): void {
		this.#inputController.openExternalEditor();
	}

	#subscribeToAgent(): void {
		this.#eventController.subscribeToAgent();
	}
}
