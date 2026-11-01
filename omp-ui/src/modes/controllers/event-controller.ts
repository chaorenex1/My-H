/**
 * Renders the Hermes gateway's session event stream into the interactive transcript.
 *
 * The authority owns the turn: `AgentSession` forwards its projections here (`message.*`,
 * `tool.*`, `turn.*`, `notice`, model/reasoning and queue snapshots) and this controller is the
 * only place that turns them into pi-tui components — one streaming assistant block, tool cards
 * (collapsed into a read group when the call is a compact read), the working row and the status
 * line repaints. Nothing is executed, persisted or replayed locally: `message_end` finalizes the
 * components the gateway already streamed.
 */

import type { AssistantMessage, ImageContent } from "../../vendor/ai/src/index.ts";
import { isRecord } from "../../vendor/utils/src/index.ts";
import { INTENT_FIELD } from "../../vendor/wire/src/index.ts";
import type { AssistantMessageComponent } from "@oh-my-pi/pi-tui/chat/assistant-message";
import { detectCacheInvalidation } from "@oh-my-pi/pi-tui/chat/cache-invalidation-marker";
import { ReadToolGroupComponent, readArgsCollapseIntoGroup } from "@oh-my-pi/pi-tui/chat/read-tool-group";
import { ToolExecutionComponent } from "@oh-my-pi/pi-tui/chat/tool-execution";
import { assistantUsageIsBilled } from "@oh-my-pi/pi-tui/chat/transcript-render-helpers";
import { readMediaPath } from "@oh-my-pi/pi-tui/native/blobs";
import { createUsageRowBlock, TurnUsageTally, turnElapsedMs } from "@oh-my-pi/pi-tui/overlays/usage-row";
import {
	assistantMessageLinkTargets,
	createAssistantMessageComponent,
	refreshAssistantMessageLinkTargets,
} from "@oh-my-pi/pi-tui/prompt/interactive-context-helpers";
import type { AgentSessionEvent } from "../../session/agent-session-events";
import { type RunStatus, setRunStatus } from "../../utils/run-status";
import {
	cfgDisplayCacheMissMarker,
	cfgDisplayShowTokenUsage,
	cfgDisplayShowTurnTime,
	cfgTerminalShowImages,
} from "../settings";
import type { InteractiveModeContext } from "../types";

/** Working-line text for a tool intent, or `undefined` when unusable. */
function normalizeIntent(intent: unknown): string | undefined {
	if (typeof intent !== "string") return undefined;
	return (
		intent
			.trim()
			.replace(/\s*\.+$/, "")
			.trim() || undefined
	);
}

/** Render args for a live card: the gateway sends the intent beside the args, renderers read it as `i`. */
function renderArgsWithIntent(args: unknown, intent: string | undefined): unknown {
	if (intent === undefined || !isRecord(args) || INTENT_FIELD in args) return args;
	return { ...args, [INTENT_FIELD]: intent };
}

/** Image blocks a read result carries for inline display under the reply that asked for them. */
function readResultImages(result: unknown): ImageContent[] {
	if (!isRecord(result) || !Array.isArray(result.content)) return [];
	return result.content.filter(
		(content): content is ImageContent =>
			isRecord(content) &&
			content.type === "image" &&
			typeof content.data === "string" &&
			typeof content.mimeType === "string",
	);
}

export class EventController {
	static readonly #MESSAGE_UPDATE_COALESCE_MS = 33;

	#unsubscribe: (() => void) | undefined;
	/** Latest cumulative `message_update` in the current coalescing window; see {@link #enqueueMessageUpdate}. */
	#pendingMessageUpdate: Extract<AgentSessionEvent, { type: "message_update" }> | undefined;
	#messageUpdateTimer: NodeJS.Timeout | undefined;
	#lastReadGroup: ReadToolGroupComponent | undefined;
	/** Assistant block a read's inline images belong under, keyed by call id. */
	#readToolCallAssistant = new Map<string, AssistantMessageComponent>();
	#lastAssistantComponent: AssistantMessageComponent | undefined;
	/** Assistant block whose turn-ending error is mirrored in the pinned banner above the editor. */
	#pinnedErrorComponent: AssistantMessageComponent | undefined;
	/** Timestamp of the current turn's user prompt; drives the usage row's prompt→yield delta. */
	#turnStartedAt: number | undefined;
	/** The running turn's token and cost totals, shown under its last answer. */
	readonly #turnUsage = new TurnUsageTally();
	#lastIntent: string | undefined;

	constructor(private ctx: InteractiveModeContext) {}

	/** Subscribe to the attached gateway session; calling again replaces the old subscription. */
	subscribeToAgent(): void {
		this.#unsubscribe?.();
		this.#unsubscribe = this.ctx.session.subscribe(event => this.#dispatch(event));
	}

	dispose(): void {
		if (this.#messageUpdateTimer) {
			clearTimeout(this.#messageUpdateTimer);
			this.#messageUpdateTimer = undefined;
		}
		this.#pendingMessageUpdate = undefined;
		this.#unsubscribe?.();
		this.#unsubscribe = undefined;
	}

	/**
	 * Entry point shared by the live subscription and a replayed transcript.
	 *
	 * `message_update` carries the cumulative assistant message (every delta re-lists all blocks),
	 * so a burst of deltas only needs the latest snapshot rendered. Only the most recent one is
	 * kept and any other event flushes it first, preserving event order.
	 */
	handleEvent(event: AgentSessionEvent): void {
		switch (event.type) {
			case "agent_start":
				this.#handleAgentStart();
				return;
			case "agent_end":
				this.#handleAgentEnd(event);
				return;
			case "turn_start":
				this.#handleTurnStart();
				return;
			case "turn_end":
				this.#handleTurnEnd();
				return;
			case "message_start":
				this.#handleMessageStart(event.message);
				return;
			case "message_update":
				this.#handleMessageUpdate(event);
				return;
			case "message_end":
				this.#handleMessageEnd(event.message);
				return;
			case "tool_execution_start":
				this.#handleToolExecutionStart(event);
				return;
			case "tool_execution_update":
				this.#handleToolExecutionUpdate(event);
				return;
			case "tool_execution_end":
				this.#handleToolExecutionEnd(event);
				return;
			case "notice":
				this.#handleNotice(event);
				return;
			case "model_changed":
			case "thinking_level_changed":
				this.ctx.statusLine.invalidate();
				this.ctx.ui.requestRender();
				return;
			case "queue_update":
				this.ctx.updatePendingMessagesDisplay();
				return;
			default:
				// The gateway reports nothing this shell renders for the remaining event kinds
				// (config_warnings_changed and the local-only members of the session vocabulary).
				return;
		}
	}

	#dispatch(event: AgentSessionEvent): void {
		if (event.type === "message_update") {
			this.#enqueueMessageUpdate(event);
			return;
		}
		this.#flushPendingMessageUpdate();
		try {
			this.handleEvent(event);
		} catch (error) {
			this.ctx.session.emitNotice(
				"error",
				error instanceof Error ? error.message : String(error),
				"event-controller",
			);
		}
	}

	#enqueueMessageUpdate(event: Extract<AgentSessionEvent, { type: "message_update" }>): void {
		this.#pendingMessageUpdate = event;
		if (this.#messageUpdateTimer) return;
		this.#messageUpdateTimer = setTimeout(() => {
			this.#messageUpdateTimer = undefined;
			try {
				this.#flushPendingMessageUpdate();
			} catch (error) {
				this.ctx.session.emitNotice(
					"error",
					error instanceof Error ? error.message : String(error),
					"event-controller",
				);
			}
		}, EventController.#MESSAGE_UPDATE_COALESCE_MS);
	}

	/** Render the latest queued streaming snapshot; superseded intermediates are dropped. */
	#flushPendingMessageUpdate(): void {
		if (this.#messageUpdateTimer) {
			clearTimeout(this.#messageUpdateTimer);
			this.#messageUpdateTimer = undefined;
		}
		const event = this.#pendingMessageUpdate;
		if (!event) return;
		this.#pendingMessageUpdate = undefined;
		this.handleEvent(event);
	}

	#handleAgentStart(): void {
		this.#sealPendingToolCards();
		this.#resetReadGroup();
		this.#readToolCallAssistant.clear();
		this.#lastAssistantComponent = undefined;
		this.#lastIntent = undefined;
		this.#pinnedErrorComponent?.setErrorPinned(false);
		this.#pinnedErrorComponent = undefined;
		this.ctx.clearPinnedError();
		this.ctx.setWorkingMessage();
		this.ctx.statusLine.markActivityStart();
		this.ctx.ensureLoadingAnimation();
		setRunStatus({ state: "working" });
		this.ctx.ui.requestRender();
	}

	#handleTurnStart(): void {
		this.ctx.statusLine.markActivityStart();
		this.ctx.ensureLoadingAnimation();
		setRunStatus({ state: "working" });
		this.ctx.ui.requestRender();
	}

	#handleTurnEnd(): void {
		this.#resetReadGroup();
		this.ctx.ui.requestRender();
	}

	#handleMessageStart(message: Extract<AgentSessionEvent, { type: "message_start" }>["message"]): void {
		if (message.role === "assistant") {
			// A streaming block left over from a start that never saw its `message_end` must not stay
			// live: one unfinalized block at the transcript frontier blocks history retirement.
			const abandoned = this.ctx.streamingComponent;
			if (abandoned) {
				this.ctx.chatContainer.removeChild(abandoned);
				abandoned.markTranscriptBlockFinalized();
			}
			const component = createAssistantMessageComponent(this.ctx);
			component.updateContent(message, { transient: true });
			component.pickReactionTarget(this.ctx.chatContainer.children);
			this.ctx.chatContainer.addChild(component);
			this.ctx.streamingComponent = component;
			this.ctx.streamingMessage = message;
			this.ctx.ensureLoadingAnimation();
			this.ctx.ui.requestRender();
			return;
		}
		if (message.role === "user") {
			// Only genuinely user-attributed prompts anchor the turn; a mid-run agent-authored user
			// message (a steering echo) must not.
			if (message.attribution !== "agent") {
				this.#turnStartedAt = message.timestamp;
				this.#turnUsage.reset();
			}
			this.#resetReadGroup();
			this.ctx.addMessageToChat(message);
			this.ctx.updatePendingMessagesDisplay();
			this.ctx.ui.requestRender(true);
			return;
		}
		this.#resetReadGroup();
		this.ctx.addMessageToChat(message);
		this.ctx.ui.requestRender();
	}

	#handleMessageUpdate(event: Extract<AgentSessionEvent, { type: "message_update" }>): void {
		if (event.message.role !== "assistant") return;
		const component = this.ctx.streamingComponent;
		if (!component) return;
		if (this.ctx.noteDisplayableThinkingContent(event.message)) {
			component.setHideThinkingBlock(this.ctx.effectiveHideThinkingBlock);
		}
		this.ctx.streamingMessage = event.message;
		component.updateContent(event.message, { transient: true });
		this.ctx.ui.requestRender();
	}

	#handleMessageEnd(message: Extract<AgentSessionEvent, { type: "message_end" }>["message"]): void {
		if (message.role === "user") {
			// The queue snapshot lags the row: drop the delivered message's pending chip now.
			this.ctx.updatePendingMessagesDisplay();
			return;
		}
		if (message.role !== "assistant") return;

		const live = this.ctx.streamingComponent;
		const component = live ?? createAssistantMessageComponent(this.ctx, message);
		if (live) {
			live.updateContent(message);
		} else {
			// No `message_start` reached the transcript (a resumed mid-turn snapshot): render the
			// finished row instead of dropping it.
			this.ctx.chatContainer.addChild(component);
		}
		if (this.ctx.noteDisplayableThinkingContent(message)) {
			component.setHideThinkingBlock(this.ctx.effectiveHideThinkingBlock);
		}

		const usage = message.usage;
		if (usage.cacheRead + usage.cacheWrite + usage.input > 0) {
			if (cfgDisplayCacheMissMarker.get(this.ctx.settings)) {
				const invalidation = detectCacheInvalidation(this.ctx.lastAssistantUsage, usage);
				if (invalidation) component.setCacheInvalidation(invalidation);
			}
			this.ctx.lastAssistantUsage = usage;
		}
		component.setServedModelMismatch(this.ctx.servedModelTracker.check(message));
		component.markTranscriptBlockFinalized();
		this.#lastAssistantComponent = component;

		const turnUsage = this.#turnUsage.add(message, this.#turnStartedAt);
		if (turnUsage) component.setTurnUsage(turnUsage);
		if (cfgDisplayShowTokenUsage.get(this.ctx.settings) && assistantUsageIsBilled(usage)) {
			const turnElapsed = cfgDisplayShowTurnTime.get(this.ctx.settings)
				? turnElapsedMs(this.#turnStartedAt, message)
				: undefined;
			this.#resetReadGroup();
			this.ctx.chatContainer.addChild(
				createUsageRowBlock(usage, message.duration, message.ttft, message.timestamp, turnElapsed),
			);
		}

		// Pin a turn-ending provider error above the editor so it survives transcript scroll.
		if (message.stopReason === "error" && message.errorMessage) {
			component.setErrorPinned(true);
			this.#pinnedErrorComponent = component;
			this.ctx.showPinnedError(message.errorMessage);
		}

		void refreshAssistantMessageLinkTargets(this.ctx, [message])
			.then(targets => component.setLinkTargets(assistantMessageLinkTargets(message, targets)))
			.catch(() => {
				// Unresolved prose links fall back to the destinations already cached.
			});

		this.ctx.transcriptMessageComponents.set(message, component);
		this.ctx.streamingComponent = undefined;
		this.ctx.streamingMessage = undefined;
		this.ctx.statusLine.invalidate();
		this.ctx.ui.requestRender();
	}

	#handleToolExecutionStart(event: Extract<AgentSessionEvent, { type: "tool_execution_start" }>): void {
		this.#updateWorkingMessageFromIntent(event.intent);
		const args = renderArgsWithIntent(event.args, event.intent);

		const pending = this.ctx.pendingTools.get(event.toolCallId);
		if (pending) {
			// The arguments are final now; the card created by an earlier streamed preview keeps its
			// identity and picks up the authoritative args.
			pending.updateArgs(args, event.toolCallId);
			pending.setArgsComplete(event.toolCallId);
			pending.setExecutionStarted(event.toolCallId);
			this.ctx.ui.requestRender();
			return;
		}

		if (event.toolName === "read" && readArgsCollapseIntoGroup(event.args)) {
			// Consecutive reads collapse into one compact group instead of a stack of cards.
			const assistant = this.ctx.streamingComponent ?? this.#lastAssistantComponent;
			if (assistant) this.#readToolCallAssistant.set(event.toolCallId, assistant);
			const group = this.#getReadGroup();
			group.updateArgs(event.args, event.toolCallId);
			group.setArgsComplete(event.toolCallId);
			group.setExecutionStarted(event.toolCallId);
			this.ctx.pendingTools.set(event.toolCallId, group);
			this.ctx.ui.requestRender();
			return;
		}

		this.#resetReadGroup();
		const component = new ToolExecutionComponent(
			event.toolName,
			args,
			{
				useBuiltInRenderer: this.ctx.session.hasBuiltInTool(event.toolName),
				showImages: cfgTerminalShowImages.get(this.ctx.settings),
			},
			undefined,
			this.ctx.ui,
			this.ctx.sessionManager.getCwd(),
			event.toolCallId,
		);
		component.setArgsComplete(event.toolCallId);
		component.setExecutionStarted(event.toolCallId);
		component.setExpanded(this.ctx.toolOutputExpanded);
		this.ctx.chatContainer.addChild(component);
		this.ctx.pendingTools.set(event.toolCallId, component);
		this.ctx.ui.requestRender();
	}

	#handleToolExecutionUpdate(event: Extract<AgentSessionEvent, { type: "tool_execution_update" }>): void {
		const component = this.ctx.pendingTools.get(event.toolCallId);
		if (!component) return;
		const partial = event.partialResult;
		component.updateResult({ ...partial, isError: partial?.isError === true }, true, event.toolCallId);
		this.ctx.ui.requestRender();
	}

	#handleToolExecutionEnd(event: Extract<AgentSessionEvent, { type: "tool_execution_end" }>): void {
		const component = this.ctx.pendingTools.get(event.toolCallId);
		if (component) {
			component.updateResult({ ...event.result, isError: event.isError === true }, false, event.toolCallId);
			this.ctx.pendingTools.delete(event.toolCallId);
		}
		if (event.toolName === "read") this.#inlineReadToolImages(event.toolCallId, event.result);
		this.#readToolCallAssistant.delete(event.toolCallId);

		// The todo tool's completion carries the authoritative phase list (`details.phases`, built
		// from the gateway's rows); the HUD then re-reads the session's todo projection.
		const details = isRecord(event.result) ? event.result.details : undefined;
		if (!event.isError && isRecord(details) && Array.isArray(details.phases)) {
			void this.ctx.reloadTodos().catch(error => {
				this.ctx.session.emitNotice(
					"warning",
					error instanceof Error ? error.message : String(error),
					"todo",
				);
			});
		}
		this.ctx.ui.requestRender();
	}

	#handleNotice(event: Extract<AgentSessionEvent, { type: "notice" }>): void {
		const message = event.source ? `${event.source}: ${event.message}` : event.message;
		if (event.level === "error") this.ctx.showError(message);
		else if (event.level === "warning") this.ctx.showWarning(message);
		else this.ctx.showStatus(message);
	}

	#handleAgentEnd(event: Extract<AgentSessionEvent, { type: "agent_end" }>): void {
		// A superseded end (another viewer already started a turn): the live run owns the chrome and
		// finalizes it at its own `agent_end`.
		if (this.ctx.session.isStreaming) return;
		this.#flushPendingMessageUpdate();
		this.#stopWorkingLoader();
		this.#sealPendingToolCards();
		this.#resetReadGroup();
		this.#readToolCallAssistant.clear();
		this.#lastAssistantComponent = undefined;
		this.#lastIntent = undefined;
		const abandoned = this.ctx.streamingComponent;
		if (abandoned) {
			abandoned.markTranscriptBlockFinalized();
			this.ctx.streamingComponent = undefined;
			this.ctx.streamingMessage = undefined;
		}
		// The usage row already consumed the prompt→yield anchor at `message_end`; a settled turn must
		// not leave it (or a partial tally) to be inherited by a later run with no user prompt.
		this.#turnStartedAt = undefined;
		this.#turnUsage.reset();
		this.ctx.statusLine.markActivityEnd();
		setRunStatus(this.#settledRunStatus(event));
		this.ctx.ui.requestRender();
	}

	/** The state a settled turn leaves the status line in, read from the turn's own messages. */
	#settledRunStatus(event: Extract<AgentSessionEvent, { type: "agent_end" }>): RunStatus {
		const last = event.messages.findLast((message): message is AssistantMessage => message.role === "assistant");
		if (last?.stopReason === "error") return { state: "error", msg: last.errorMessage };
		if (!last || last.stopReason === "aborted") return { state: "idle" };
		return { state: "done" };
	}

	#stopWorkingLoader(): void {
		if (!this.ctx.loadingAnimation) return;
		this.ctx.loadingAnimation.stop();
		this.ctx.loadingAnimation = undefined;
		this.ctx.statusContainer.disposeChildren();
	}

	/** Freeze tool cards a dropped `agent_end` left live so they cannot pin transcript retirement. */
	#sealPendingToolCards(): void {
		for (const component of this.ctx.pendingTools.values()) component.seal();
		this.ctx.pendingTools.clear();
	}

	#getReadGroup(): ReadToolGroupComponent {
		if (!this.#lastReadGroup) {
			const group = new ReadToolGroupComponent();
			group.setExpanded(this.ctx.toolOutputExpanded);
			this.ctx.chatContainer.addChild(group);
			this.#lastReadGroup = group;
		}
		return this.#lastReadGroup;
	}

	#resetReadGroup(): void {
		this.#lastReadGroup?.finalize();
		this.#lastReadGroup = undefined;
	}

	/** Show a read result's images under the reply that requested it. */
	#inlineReadToolImages(toolCallId: string, result: unknown): void {
		const component = this.#readToolCallAssistant.get(toolCallId);
		if (!component) return;
		const details = isRecord(result) ? result.details : undefined;
		const images = readResultImages(result);
		const mediaPath = readMediaPath(details);
		if (images.length === 0 && !mediaPath) return;
		component.setToolResultImages(toolCallId, images, mediaPath);
	}

	/** Surface a streamed tool intent as the working row's text. */
	#updateWorkingMessageFromIntent(intent: unknown): void {
		if (this.ctx.session.isAborting) return;
		const trimmed = normalizeIntent(intent);
		if (!trimmed || trimmed === this.#lastIntent) return;
		this.#lastIntent = trimmed;
		this.ctx.setWorkingMessage(trimmed);
	}
}
