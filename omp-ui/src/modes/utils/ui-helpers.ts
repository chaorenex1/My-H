import type { AgentMessage } from "../../vendor/agent/src/index.ts";
import type { CustomMessage } from "../../vendor/agent/src/compaction/messages.ts";
import type { AssistantMessage, ImageContent } from "../../vendor/ai/src/index.ts";
import type { Component } from "@oh-my-pi/pi-tui";
import { createAdvisorMessageCard } from "@oh-my-pi/pi-tui/chat/advisor-message";
import { AssistantMessageComponent } from "@oh-my-pi/pi-tui/chat/assistant-message";
import { BashExecutionComponent } from "@oh-my-pi/pi-tui/chat/bash-execution";
import { detectCacheInvalidation } from "@oh-my-pi/pi-tui/chat/cache-invalidation-marker";
import { BranchSummaryMessageComponent, CompactionSummaryMessageComponent } from "@oh-my-pi/pi-tui/chat/compaction-summary-message";
import { CustomMessageComponent } from "@oh-my-pi/pi-tui/chat/custom-message";
import { EvalExecutionComponent } from "@oh-my-pi/pi-tui/chat/eval-execution";
import type { AdvisorMessageDetails, SkillPromptDetails } from "@oh-my-pi/pi-tui/chat/messages";
import { SkillMessageComponent } from "@oh-my-pi/pi-tui/chat/skill-message";
import { ServedModelTracker } from "@oh-my-pi/pi-tui/chat/served-model-marker";
import { TodoReminderComponent } from "@oh-my-pi/pi-tui/chat/todo-reminder";
import { ToolExecutionComponent, toolRenderName } from "@oh-my-pi/pi-tui/chat/tool-execution";
import { assistantUsageIsBilled } from "@oh-my-pi/pi-tui/chat/transcript-render-helpers";
import { buildFileMentionBlock } from "@oh-my-pi/pi-tui/chat/transcript-render-helpers";
import { imageContent, textContent } from "@oh-my-pi/pi-tui/chat/transcript-entry";
import type { TodoItem } from "@oh-my-pi/pi-tui/tools/todo";
import { StatusNotice } from "@oh-my-pi/pi-tui/chrome/status-notice";
import { ToolActivityContainer } from "@oh-my-pi/pi-tui/chrome/tool-activity";
import { TranscriptNotice } from "@oh-my-pi/pi-tui/chrome/transcript-notice";
import { createUsageRowBlock, turnElapsedMs } from "@oh-my-pi/pi-tui/overlays/usage-row";
import { imageAttachmentSource } from "@oh-my-pi/pi-tui/prompt/image-source";
import { QueuedMessagesBand } from "@oh-my-pi/pi-tui/prompt/queued-messages";
import { EMPTY_LINK_TARGETS } from "@oh-my-pi/pi-tui/render/render-utils";
import { theme } from "@oh-my-pi/pi-tui/theme";
import { UserMessageComponent } from "@oh-my-pi/pi-tui/chat/user-message";
import { InternalUrlRouter } from "../../internal-urls";
import type { InteractiveModeContext, RenderSessionContextOptions, ShowStatusOptions } from "../../modes/types";
import { isRecord } from "../../vendor/utils/src/index.ts";
import {
	cfgComposerRecallClearedDrafts,
	cfgDisplayCacheMissMarker,
	cfgDisplayShowTokenUsage,
	cfgDisplayShowTurnTime,
	cfgTerminalShowImages,
} from "../settings";

/** What `addMessageToChat` needs beyond the message itself. */
export type AddMessageOptions = {
	/** Destinations for the message's image chips, aligned with the message's images. */
	imageLinks?: readonly (string | undefined)[];
	/** Re-attach the component a previous render built for this message instead of a fresh one. */
	reuseSettledComponent?: boolean;
	/** Tool calls whose live component remains the sole render owner across a rebuild (#6516). */
	preservedLiveToolCallIds?: ReadonlySet<string>;
};

/** What a transcript rebuild needs; the message list itself comes from the session ledger. */
export interface RenderInitialMessagesOptions {
	/** Keep what is already on screen and append the replayed transcript after it. */
	preserveExistingChat?: boolean;
	/** Retire the rows already sent to native scrollback. */
	clearTerminalHistory?: boolean;
}

/**
 * Where an image chip points: the local file the image was pasted or dropped from.
 * A `local://` source stays on the image for the model but resolves against the
 * session root, so a chip cannot address it — those render unlinked.
 */
function imageChipLink(image: ImageContent): string | undefined {
	const sourcePath = imageAttachmentSource(image)?.path;
	if (!sourcePath) return undefined;
	return InternalUrlRouter.instance().canHandle(sourcePath) ? undefined : sourcePath;
}

/** Chip targets for the images attached to one message. */
function imageLinksForMessage(images: readonly ImageContent[]): (string | undefined)[] | undefined {
	if (images.length === 0) return undefined;
	return images.map(imageChipLink);
}

/**
 * Chip targets for images the composer is about to submit, so the user turn the
 * shell renders optimistically carries the same links its replay will.
 */
export function materializeImageChipLinks(images: readonly ImageContent[]): (string | undefined)[] {
	return images.map(imageChipLink);
}

/**
 * Transcript helpers for the gateway shell.
 *
 * Everything here renders what the session authority sends: user turns, assistant
 * prose plus its per-turn usage, and the shell's own notices. Tool cards are the
 * event controller's (they follow live `tool.*` notifications, which never enter
 * the message ledger), so a rebuild only has to reproduce messages.
 */
export class UiHelpers {
	/** The last status line this helper appended, for in-place coalescing of back-to-back statuses. */
	#lastStatus: StatusNotice | undefined;

	constructor(private ctx: InteractiveModeContext) {}

	/**
	 * Show a status message in the chat.
	 *
	 * If multiple status messages are emitted back-to-back (without anything else being added to the chat),
	 * we update the previous status line instead of appending new ones to avoid log spam. A repeat of the
	 * exact same text replaces the line with a fresh notice: an in-place update with identical props sends
	 * nothing to a native terminal, so its expired toast would never show again.
	 */
	showStatus(message: string, options?: ShowStatusOptions): void {
		const children = this.ctx.chatContainer.children;
		const last = children.length > 0 ? children[children.length - 1] : undefined;
		const useDim = options?.dim ?? true;
		// Resolve the dim color lazily so a later theme change re-shapes the line
		// instead of leaving the palette that was active when it was presented.
		const styleFn = useDim ? (t: string) => theme.fg("dim", t) : undefined;
		const notice = { styleFn, toast: options?.toast };

		if (last && last === this.#lastStatus) {
			if (this.#lastStatus.message !== message) {
				this.#lastStatus.setMessage(message, notice);
				this.ctx.ui.requestRender();
				return;
			}
			this.ctx.chatContainer.removeChild(last);
		}

		const status = new StatusNotice(message, notice);
		this.ctx.present([status]);
		this.#lastStatus = status;
	}

	showError(errorMessage: string): void {
		this.ctx.present(TranscriptNotice.line("error", errorMessage));
	}

	showWarning(warningMessage: string, options?: { hideWithToolActivity?: boolean }): void {
		const notice = TranscriptNotice.line("warning", warningMessage);
		this.ctx.present(options?.hideWithToolActivity ? new ToolActivityContainer(notice) : notice);
	}

	/** Clear the composer and its recall draft, leaving nothing for a later `Alt+Up` to restore. */
	clearEditor(): void {
		if (cfgComposerRecallClearedDrafts.get(this.ctx.settings)) this.ctx.editor.clearDraftForRecall();
		else this.ctx.editor.clearDraft();
		this.ctx.ui.requestRender();
	}

	/** Append one message's transcript block; returns the components it added. */
	addMessageToChat(message: AgentMessage, options?: AddMessageOptions): Component[] {
		switch (message.role) {
			case "hookMessage":
			case "custom":
				return this.#customBlock(message);
			case "compactionSummary": {
				const component = new CompactionSummaryMessageComponent(message);
				component.setExpanded(this.ctx.toolOutputExpanded);
				return [this.#child(component)];
			}
			case "branchSummary": {
				const component = new BranchSummaryMessageComponent(message);
				component.setExpanded(this.ctx.toolOutputExpanded);
				return [this.#child(component)];
			}
			case "toolResult":
				return this.#toolBlock(message, options);
			case "user":
			case "developer":
				return this.#userBlock(message, options);
			case "bashExecution": {
				const component = new BashExecutionComponent(
					message.command,
					this.ctx.ui,
					message.excludeFromContext ?? false
				);
				component.appendOutput(message.output);
				component.setComplete(message.exitCode, message.cancelled);
				return [this.#child(component)];
			}
			case "pythonExecution": {
				const component = new EvalExecutionComponent(
					message.code,
					this.ctx.ui,
					message.excludeFromContext ?? false
				);
				component.appendOutput(message.output);
				component.setComplete(message.exitCode, message.cancelled);
				return [this.#child(component)];
			}
			case "fileMention": {
				const block = buildFileMentionBlock(message.files, 1);
				return block.children.length === 0 ? [] : [this.#child(block)];
			}
			case "assistant":
				return this.#assistantBlock(message, options);
			default:
				// ADR-10 D10: every AgentMessage role is handled above; a new one fails typecheck.
				return message satisfies never;
		}
	}

	#child<T extends Component>(component: T): T {
		this.ctx.chatContainer.addChild(component);
		return component;
	}

	/** ADR-10 D8.2: custom/hook blocks select their omp renderer from `customType` + `details`. */
	#customBlock(message: CustomMessage<unknown> | Extract<AgentMessage, { role: "hookMessage" }>): Component[] {
		if (!message.display) return [];
		const custom = message as CustomMessage<unknown>;
		const generic = (): Component[] => {
			const component = new CustomMessageComponent(custom);
			component.setExpanded(this.ctx.toolOutputExpanded);
			return [this.#child(component)];
		};
		switch (custom.customType) {
			case "advisor": {
				const details = custom.details as AdvisorMessageDetails | undefined;
				if (!details || !Array.isArray(details.notes)) return generic();
				return [
					this.#child(
						createAdvisorMessageCard(details, () => this.ctx.toolOutputExpanded, theme)
					)
				];
			}
			case "skill-prompt": {
				if (!custom.details) return generic();
				const skill = new SkillMessageComponent(custom as CustomMessage<SkillPromptDetails>);
				skill.setExpanded(this.ctx.toolOutputExpanded);
				return [this.#child(skill)];
			}
			case "todo-reminder": {
				const details = custom.details as
					| { todos: TodoItem[]; attempt: number; maxAttempts: number }
					| undefined;
				if (!details || !Array.isArray(details.todos)) return generic();
				const reminder = new TodoReminderComponent(details.todos, details.attempt, details.maxAttempts);
				reminder.setToolActivityVisible(!this.ctx.hideToolActivity);
				return [this.#child(reminder)];
			}
			case "gateway-notice":
			case "model-switch":
			case "personality-switch":
			case "process-complete":
			case "async-delegation-complete":
			case "internal-notification":
				return [this.#child(this.#noticeBlock(custom))];
			default:
				return generic();
		}
	}

	#noticeBlock(custom: CustomMessage<unknown>): Component {
		const details = custom.details as { level?: string; source?: string } | undefined;
		const text = textContent(custom.content);
		return details?.level === "error"
			? TranscriptNotice.line("error", text)
			: details?.level === "warning"
				? TranscriptNotice.line("warning", text)
				: new StatusNotice(text, {
						toast: false,
						styleFn: (value: string) => theme.fg("dim", value)
					});
	}

	#toolBlock(
		message: Extract<AgentMessage, { role: "toolResult" }>,
		options?: AddMessageOptions
	): Component[] {
		// A background/read-group component still live in `pendingTools` owns this row; replaying
		// it here would paint a second card (#6516).
		if (options?.preservedLiveToolCallIds?.has(message.toolCallId)) return [];
		const details = isRecord(message.details) ? message.details : undefined;
		const args = isRecord(details?.args) ? details.args : {};
		// ADR-10 D8.2a: the wire name stays on the message; the render key only picks the renderer.
		const tool = this.ctx.session.getToolByName(message.toolName) as Parameters<typeof toolRenderName>[1];
		const component = new ToolExecutionComponent(
			toolRenderName(message.toolName, tool),
			args,
			{
				useBuiltInRenderer: this.ctx.session.hasBuiltInTool(message.toolName),
				showImages: cfgTerminalShowImages.get(this.ctx.settings)
			},
			tool,
			this.ctx.ui,
			this.ctx.sessionManager.getCwd(),
			message.toolCallId
		);
		component.setArgsComplete(message.toolCallId);
		component.setExecutionStarted(message.toolCallId);
		component.setExpanded(this.ctx.toolOutputExpanded);
		component.updateResult(
			{ content: message.content, ...(details ? { details } : {}), isError: message.isError },
			false,
			message.toolCallId
		);
		return [this.#child(component)];
	}

	#userBlock(
		message: Extract<AgentMessage, { role: "user" | "developer" }>,
		options?: AddMessageOptions
	): Component[] {
		const text = textContent(message.content);
		if (!text) return [];
		const cached = options?.reuseSettledComponent
			? this.ctx.transcriptMessageComponents.get(message)
			: undefined;
		if (cached instanceof UserMessageComponent) return [this.#child(cached)];
		const images = imageContent(message.content);
		const component = new UserMessageComponent(text, {
			synthetic: message.role === "developer" ? true : (message.synthetic ?? false),
			imageLinks: options?.imageLinks ?? imageLinksForMessage(images),
			images,
			liveSteered: message.role === "user" && message.liveSteered === true,
			timestamp: message.timestamp
		});
		this.ctx.transcriptMessageComponents.set(message, component);
		return [this.#child(component)];
	}

	#assistantBlock(
		message: Extract<AgentMessage, { role: "assistant" }>,
		options?: AddMessageOptions
	): Component[] {
		const cached = options?.reuseSettledComponent
			? this.ctx.transcriptMessageComponents.get(message)
			: undefined;
		const component =
			cached instanceof AssistantMessageComponent
				? cached
				: new AssistantMessageComponent(
						message,
						this.ctx.hideThinkingBlock,
						() => this.ctx.ui.requestRender(),
						[],
						this.ctx.ui.imageBudget,
						this.ctx.proseOnlyThinking,
						EMPTY_LINK_TARGETS,
						this.ctx.expandThinkingBlocks
					);
		if (cached !== component) {
			component.setExpanded(this.ctx.toolOutputExpanded);
			component.setToolResultImagesVisible(!this.ctx.hideToolActivity);
			this.ctx.transcriptMessageComponents.set(message, component);
		}
		return [this.#child(component)];
	}

	/**
	 * Rebuild the transcript from the session's message ledger. Calls run in order, so
	 * a rebuild requested while another is still walking the ledger appends after it.
	 * @param options.updateFooter Repaint the status line from the replayed session state
	 * @param options.reuseSettledComponents Re-attach the settled components a previous render built
	 */
	renderSessionContext(options: RenderSessionContextOptions = {}): void {
		// A rebuild re-derives every turn's markers from usage, so the last turn becomes
		// the live baseline; the same holds for the served-model announcement set.
		this.ctx.lastAssistantUsage = undefined;
		this.ctx.servedModelTracker = new ServedModelTracker();
		this.ctx.pendingTools.clear();
		if (options.updateFooter) this.ctx.statusLine.invalidate();

		let turnStartedAt: number | undefined;
		for (const message of this.ctx.sessionManager.getMessages()) {
			if (message.role === "assistant") {
				const [component] = this.addMessageToChat(message, {
					reuseSettledComponent: options.reuseSettledComponents,
					preservedLiveToolCallIds: options.preservedLiveToolCallIds,
				});
				if (component instanceof AssistantMessageComponent) this.#markAssistantTurn(component, message);
				const usage = message.usage;
				if (cfgDisplayShowTokenUsage.get(this.ctx.settings) && assistantUsageIsBilled(usage)) {
					this.ctx.chatContainer.addChild(
						createUsageRowBlock(
							usage,
							message.duration,
							message.ttft,
							message.timestamp,
							cfgDisplayShowTurnTime.get(this.ctx.settings)
								? turnElapsedMs(turnStartedAt, message)
								: undefined,
						),
					);
				}
				continue;
			}
			// Only a genuine operator turn anchors the elapsed-time delta.
			if (message.role === "user" && message.attribution !== "agent") turnStartedAt = message.timestamp;
			this.addMessageToChat(message, {
				reuseSettledComponent: options.reuseSettledComponents,
				preservedLiveToolCallIds: options.preservedLiveToolCallIds,
			});
		}
	}

	/**
	 * Replace the transcript with a replay of the session's messages. `hermes omp`
	 * calls this on boot and after a resume; the queued-message band is repainted
	 * from the authority's queue, which survives the rebuild.
	 */
	renderInitialMessages(options: RenderInitialMessagesOptions = {}): Promise<void> {
		if (!options.preserveExistingChat) {
			this.ctx.chatContainer.disposeChildren();
			this.ctx.transcriptMessageComponents = new WeakMap<AgentMessage, Component>();
		}
		this.updatePendingMessagesDisplay();
		this.renderSessionContext({ updateFooter: true });
		if (options.clearTerminalHistory) this.ctx.ui.requestRender(true, { clearScrollback: true });
		else this.ctx.ui.requestRender();
		return Promise.resolve();
	}

	/** Repaint the queued-message band above the composer from the authority's queue. */
	updatePendingMessagesDisplay(): void {
		this.ctx.pendingMessagesContainer.disposeChildren();
		const queue = this.ctx.session.getQueuedMessages();
		const groups = [
			{ label: "Steering", messages: [...queue.steering] },
			{ label: "After yield", messages: [...queue.followUp] },
		].filter(group => group.messages.length > 0);
		if (groups.length > 0) {
			const dequeueKey = this.ctx.keybindings.getKeys("app.message.dequeue")[0] ?? "alt+up";
			this.ctx.pendingMessagesContainer.addChild(
				new QueuedMessagesBand(groups, dequeueKey, () => this.ctx.handleDequeue()),
			);
		}
		this.ctx.ui.requestComponentRender(this.ctx.pendingMessagesContainer);
	}

	findLastAssistantMessage(): AssistantMessage | undefined {
		const messages = this.ctx.session.messages;
		for (let index = messages.length - 1; index >= 0; index -= 1) {
			const message = messages[index];
			if (message?.role === "assistant") return message;
		}
		return undefined;
	}

	/** The assistant message's visible prose, with thinking and tool blocks left out. */
	extractAssistantText(message: AssistantMessage): string {
		return message.content
			.filter((block): block is { type: "text"; text: string } => block.type === "text")
			.map(block => block.text)
			.join("");
	}

	/**
	 * Whether `text` is a command rather than a prompt. The authority owns the command
	 * catalog (`command.dispatch`), so every slash-prefixed line is a command: either the
	 * shell handles it locally or the authority does.
	 */
	isKnownSlashCommand(text: string): boolean {
		const trimmed = text.trimStart();
		if (!trimmed.startsWith("/")) return false;
		return trimmed.length > 1;
	}

	/** Cache-miss and served-model markers: transcript detail derived from the turn's usage. */
	#markAssistantTurn(component: AssistantMessageComponent, message: AssistantMessage): void {
		const usage = message.usage;
		if (cfgDisplayCacheMissMarker.get(this.ctx.settings)) {
			const invalidation = detectCacheInvalidation(this.ctx.lastAssistantUsage, usage);
			if (invalidation) component.setCacheInvalidation(invalidation);
		}
		if (usage.cacheRead + usage.cacheWrite + usage.input > 0) this.ctx.lastAssistantUsage = usage;
		component.setServedModelMismatch(this.ctx.servedModelTracker.check(message));
	}
}
