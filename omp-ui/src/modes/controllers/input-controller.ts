/**
 * Composer input for the Hermes-backed shell.
 *
 * The editor, the transcript and the status line are rendered locally; every prompt, command and
 * turn control travels over the gateway JSON-RPC wire. There is no local agent loop, no bash/python
 * mode, no skills, no drafts on disk and no prompt history store: a submission is a prompt
 * (`submitInteractiveInput` → `session.prompt`, queued/steered by the authority) or a command
 * (`ctx.executeCommandText`). Slash-command completions come from the gateway's `complete.slash`.
 */

import {
	type AutocompleteItem,
	type AutocompleteProvider,
	findLeadingSlashCommandStart,
} from "@oh-my-pi/pi-tui";
import { AssistantMessageComponent } from "@oh-my-pi/pi-tui/chat/assistant-message";
import { ensureSupportedImageInput } from "@oh-my-pi/pi-tui/chat/image-loading";
import { appKey } from "@oh-my-pi/pi-tui/chrome/keybinding-hints";
import { chipLabel, formatVisionMarker, shiftImageMarkers } from "@oh-my-pi/pi-tui/prompt/composer-attachments";
import { extractImagePathFromText } from "@oh-my-pi/pi-tui/prompt/custom-editor";
import { setCachedImageDimensions } from "@oh-my-pi/pi-tui/prompt/image-references";
import { submitInteractiveInput } from "../../main";
import type { RestoredQueuedMessage } from "../../session/agent-session-types";
import { cfgHideThinkingBlock } from "../../session/settings";
import {
	isClipboardTerminalLocal,
	readImageFromClipboard,
	readMacFileUrlsFromClipboard,
	readTextFromClipboard,
} from "../../utils/clipboard";
import { getEditorCommand, openInEditor } from "../../utils/external-editor";
import { resizeImage } from "../../utils/image-resize";
import { resendProgramStatus } from "../../utils/run-status";
import type { ImageContent, Model } from "../../vendor/ai/src/index.ts";
import type { CompleteSlashResult } from "../rpc/gateway-contract.generated";
import type { RoleCycleRoute } from "../rpc/gateway-session";
import { cfgImagesAutoResize } from "../settings";
import type { InteractiveModeContext, SubmittedUserInput } from "../types";

const REMOTE_CLIPBOARD_STATUS =
	"Clipboard here belongs to the SSH host — use your terminal's paste shortcut to paste from your machine";

/** How long a second Ctrl+C waits after the first before it closes the session. */
const DOUBLE_SIGINT_WINDOW_MS = 500;

/** Minimal contract for any component that exposes a whole-block expand/collapse toggle. */
interface Expandable {
	setExpanded(expanded: boolean): void;
}

function isExpandable(obj: unknown): obj is Expandable {
	return typeof obj === "object" && obj !== null && "setExpanded" in obj && typeof obj.setExpanded === "function";
}

/** One resolved step of the ctrl+p model cycle. */
export interface CycleModelTarget {
	model: Model;
	/** Role whose pinned route won; absent when the catalog pool was cycled instead. */
	role?: string;
}

/**
 * Resolve the next ctrl+p model: the roles' pinned routes in the gateway's cycle order first,
 * the session catalog pool (`session.cycleModel`) only when no route resolves. Routes the
 * catalog does not know are skipped, so an uninstalled model never stalls the cycle.
 */
export async function cycleSessionModel(
	routes: readonly RoleCycleRoute[],
	find: (provider: string, id: string) => Model | undefined,
	current: Model | undefined,
	direction: "forward" | "backward",
	cycleCatalog: () => Promise<{ model: Model } | undefined>,
): Promise<CycleModelTarget | undefined> {
	const pool = routes.flatMap((route) => {
		const model = find(route.provider, route.model);
		return model ? [{ role: route.role, model }] : [];
	});
	if (pool.length === 0) {
		const fallback = await cycleCatalog();
		return fallback ? { model: fallback.model } : undefined;
	}
	const index = pool.findIndex(
		(entry) => entry.model.provider === current?.provider && entry.model.id === current.id,
	);
	const step = direction === "forward" ? 1 : -1;
	const next = index < 0 ? (step === 1 ? 0 : pool.length - 1) : (index + step + pool.length) % pool.length;
	return pool[next];
}

/**
 * Slash-command completions for a leading `/` token.
 *
 * The command catalog (built-ins, aliases and the profile's skills) lives on the authority, so the
 * popover asks it for the ranked list on every keystroke: `complete.slash` returns the items plus the
 * column they replace from, which keeps the provider stateless across profile/skill changes.
 */
class GatewaySlashAutocompleteProvider implements AutocompleteProvider {
	constructor(private readonly ctx: InteractiveModeContext) {}

	async getSuggestions(
		lines: string[],
		cursorLine: number,
		cursorCol: number,
		signal?: AbortSignal,
	): Promise<{ items: AutocompleteItem[]; prefix: string } | null> {
		if (signal?.aborted) return null;
		const before = (lines[cursorLine] ?? "").slice(0, cursorCol);
		const slash = findLeadingSlashCommandStart(before);
		if (slash === null) return null;
		const gateway = this.ctx.session.gateway;
		if (!gateway) return null;
		let result: CompleteSlashResult;
		try {
			result = await gateway.client.request("complete.slash", {
				text: before,
				session_id: this.ctx.session.sessionId,
			});
		} catch {
			return null;
		}
		if (signal?.aborted) return null;
		const items: AutocompleteItem[] = [];
		for (const item of result.items ?? []) {
			if (!item.text) continue;
			items.push({
				value: item.text,
				label: item.display ?? item.text,
				...(item.meta ? { description: item.meta } : {}),
			});
		}
		if (items.length === 0) return null;
		const from = Math.min(before.length, Math.max(0, result.replace_from ?? slash));
		return { items, prefix: before.slice(from) };
	}

	applyCompletion(
		lines: string[],
		cursorLine: number,
		cursorCol: number,
		item: AutocompleteItem,
		prefix: string,
	): { lines: string[]; cursorLine: number; cursorCol: number } {
		const line = lines[cursorLine] ?? "";
		const from = Math.max(0, cursorCol - prefix.length);
		// A command name takes arguments, so the accepted row is followed by a space.
		const insert = item.value.startsWith("/") && !item.value.endsWith(" ") ? `${item.value} ` : item.value;
		const nextLine = line.slice(0, from) + insert + line.slice(cursorCol);
		const next = [...lines];
		next[cursorLine] = nextLine;
		return { lines: next, cursorLine, cursorCol: from + insert.length };
	}
}

export class InputController {
	constructor(private readonly ctx: InteractiveModeContext) {}

	#draftText: string | undefined;
	#lastSigintTime = 0;
	#globalListenerInstalled = false;
	/** In-flight submissions, so a teardown can wait for the prompt each one handed to the session. */
	readonly #submissionDispatches = new Set<Promise<void>>();

	/** The last full editor snapshot delivered by its change contract. */
	getDraftText(): string {
		return this.#draftText ?? this.ctx.editor.getText();
	}

	/** Slash-command provider; the shell installs it from {@link setupKeyHandlers}. */
	createAutocompleteProvider(): AutocompleteProvider {
		return new GatewaySlashAutocompleteProvider(this.ctx);
	}

	// ------------------------------------------------------------------
	// Editor submit
	// ------------------------------------------------------------------

	setupEditorSubmitHandler(): void {
		this.ctx.editor.onSubmit = text => this.#trackSubmission(() => this.#submitText(text));
	}

	/**
	 * Submit one composer draft: commands first (the authority owns the slash catalog), everything
	 * else as a prompt, which the session steers or queues while a turn is running.
	 */
	async #submitText(rawText: string): Promise<void> {
		const editor = this.ctx.editor;
		const text = rawText.trim();
		const images = editor.pendingImages.length > 0 ? [...editor.pendingImages] : undefined;
		const imageLinks = images && editor.pendingImageLinks.length > 0 ? [...editor.pendingImageLinks] : undefined;
		if (images) {
			editor.pendingImages = [];
			editor.pendingImageLinks = [];
			editor.imageLinks = undefined;
		}

		if (!text && !images) {
			// Enter on an empty composer while work is in flight stops the turn so the
			// authority's queue drains; otherwise there is nothing to submit.
			if (this.ctx.session.isStreaming && this.ctx.session.hasInterruptibleInput) {
				await this.#interrupt("interrupt");
				this.ctx.updatePendingMessagesDisplay();
				this.ctx.ui.requestRender();
			}
			return;
		}

		// `.` / `c` continue the conversation without a new visible user turn. `started`
		// keeps the shell from rendering a pending user message for it.
		if (text === "." || text === "c") {
			editor.clearDraft();
			await submitInteractiveInput(this.ctx, this.ctx.session, {
				text,
				resume: true,
				cancelled: false,
				started: true,
			});
			return;
		}

		if (text.startsWith("/")) {
			try {
				if (await this.ctx.executeCommandText(text)) {
					this.ctx.ui.requestRender();
					return;
				}
			} catch (error) {
				this.#restoreDraft(text, images, imageLinks);
				this.ctx.showError(error instanceof Error ? error.message : String(error));
				return;
			}
		}

		await this.#dispatch(text, images, imageLinks);
	}

	/** Hand a prompt to the shell's submission path, queuing behind a running turn. */
	async #dispatch(
		text: string,
		images?: ImageContent[],
		imageLinks?: (string | undefined)[],
		streamingBehavior?: "steer" | "followUp",
	): Promise<void> {
		const behavior = streamingBehavior ?? (this.ctx.session.isStreaming ? ("steer" as const) : undefined);
		const input: SubmittedUserInput = {
			text,
			...(images ? { images } : {}),
			...(imageLinks ? { imageLinks } : {}),
			...(behavior ? { streamingBehavior: behavior } : {}),
			cancelled: false,
			started: false,
		};
		await submitInteractiveInput(this.ctx, this.ctx.session, input);
	}

	/** Ctrl+Enter: queue the draft as a follow-up behind the running turn. */
	async #handleFollowUp(): Promise<void> {
		const editor = this.ctx.editor;
		const text = editor.getExpandedText().trim();
		const images = editor.pendingImages.length > 0 ? [...editor.pendingImages] : undefined;
		const imageLinks = images && editor.pendingImageLinks.length > 0 ? [...editor.pendingImageLinks] : undefined;
		if (!text && !images) return;
		editor.clearDraft();
		if (!images && text.startsWith("/") && (await this.ctx.executeCommandText(text))) {
			this.ctx.ui.requestRender();
			return;
		}
		await this.#dispatch(text, images, imageLinks, "followUp");
	}

	#restoreDraft(text: string, images?: ImageContent[], imageLinks?: (string | undefined)[]): void {
		const editor = this.ctx.editor;
		editor.setText(text);
		if (images && images.length > 0) {
			editor.pendingImages = [...images];
			editor.pendingImageLinks = imageLinks ? [...imageLinks] : images.map(() => undefined);
			editor.imageLinks = editor.pendingImageLinks;
			editor.setCollapsedText(text);
		}
		this.ctx.ui.requestRender();
	}

	/** Track a submission from the moment the editor takes it until its dispatch ends. */
	async #trackSubmission(dispatch: () => Promise<void>): Promise<void> {
		const done = Promise.withResolvers<void>();
		this.#submissionDispatches.add(done.promise);
		try {
			await dispatch();
		} finally {
			this.#submissionDispatches.delete(done.promise);
			done.resolve();
		}
	}

	/** Resolves once every submission reached the session or finished dispatching. */
	async waitForSubmissionDispatches(): Promise<void> {
		while (this.#submissionDispatches.size > 0) await Promise.all([...this.#submissionDispatches]);
	}

	// ------------------------------------------------------------------
	// Keys
	// ------------------------------------------------------------------

	setupKeyHandlers(): void {
		const { editor, keybindings } = this.ctx;
		this.#draftText ??= editor.getText();

		editor.setActionKeys("app.interrupt", keybindings.getKeys("app.interrupt"));
		editor.onEscape = () => void this.#handleEscape();

		editor.setActionKeys("app.clear", keybindings.getKeys("app.clear"));
		editor.onClear = () => this.handleCtrlC();

		editor.setActionKeys("app.exit", keybindings.getKeys("app.exit"));
		editor.onExit = () => this.handleCtrlD();

		editor.setActionKeys("app.suspend", keybindings.getKeys("app.suspend"));
		editor.onSuspend = () => this.handleCtrlZ();

		editor.setActionKeys("app.display.reset", keybindings.getKeys("app.display.reset"));
		editor.onDisplayReset = () => this.ctx.ui.resetDisplay();

		editor.setActionKeys("app.thinking.cycle", keybindings.getKeys("app.thinking.cycle"));
		editor.onCycleThinkingLevel = () => this.cycleThinkingLevel();

		editor.setActionKeys("app.model.cycleForward", keybindings.getKeys("app.model.cycleForward"));
		editor.onCycleModelForward = () => void this.#cycleModel("forward");
		editor.setActionKeys("app.model.cycleBackward", keybindings.getKeys("app.model.cycleBackward"));
		editor.onCycleModelBackward = () => void this.#cycleModel("backward");

		editor.setActionKeys("app.model.select", keybindings.getKeys("app.model.select"));
		editor.onSelectModel = () => this.ctx.showModelSelector();
		editor.setActionKeys("app.model.selectTemporary", keybindings.getKeys("app.model.selectTemporary"));
		editor.onSelectModelTemporary = () => this.ctx.showModelSelector({ temporaryOnly: true });

		editor.setActionKeys("app.clipboard.pasteImage", keybindings.getKeys("app.clipboard.pasteImage"));
		editor.onPasteImage = () => this.handleImagePaste();
		editor.onPasteImagePath = path => this.handleImagePathPaste(path);

		editor.setActionKeys("app.message.dequeue", keybindings.getKeys("app.message.dequeue"));
		editor.onDequeue = () => this.handleDequeue();

		editor.onChange = text => {
			const wasComposing = (this.#draftText ?? "").trim() !== "";
			this.#draftText = text;
			// ADR-5 D7: a draft suppresses the idle recap; clearing it re-arms the stretch.
			if (wasComposing !== (text.trim() !== "")) this.ctx.refreshIdleRecap();
		};

		editor.clearCustomKeyHandlers();
		for (const key of keybindings.getKeys("app.session.resume")) {
			editor.setCustomKeyHandler(key, () => this.ctx.showSessionSelector());
		}
		for (const key of keybindings.getKeys("app.message.followUp")) {
			editor.setCustomKeyHandler(key, () => void this.#handleFollowUp());
		}

		this.ctx.composer.editor.setAutocompleteProvider(this.createAutocompleteProvider());

		if (!this.#globalListenerInstalled) {
			this.#globalListenerInstalled = true;
			// Actions that target the transcript or open a surface, not the editor buffer. They
			// defer while an overlay owns the keyboard so a dialog keeps its own bindings.
			this.ctx.ui.addInputListener(data => {
				if (this.ctx.ui.hasOverlay()) return undefined;
				if (keybindings.matches(data, "app.display.reset")) {
					this.ctx.ui.resetDisplay();
					return { consume: true };
				}
				if (keybindings.matches(data, "app.thinking.toggle")) {
					this.toggleThinkingBlockVisibility();
					return { consume: true };
				}
				if (keybindings.matches(data, "app.tools.expand")) {
					this.toggleToolOutputExpansion();
					return { consume: true };
				}
				if (keybindings.matches(data, "app.editor.external")) {
					void this.openExternalEditor();
					return { consume: true };
				}
				return undefined;
			});
		}
	}

	/** Esc stops the running turn; an idle editor keeps its draft. */
	async #handleEscape(): Promise<void> {
		if (!this.ctx.session.isStreaming && this.ctx.session.queuedMessageCount === 0) return;
		await this.#interrupt("interrupt");
	}

	async #interrupt(label: string): Promise<void> {
		try {
			await this.ctx.session.interrupt();
		} catch (error) {
			// Esc/Ctrl+C must never surface a teardown error as a crash.
			this.ctx.showError(`Failed to ${label}: ${error instanceof Error ? error.message : String(error)}`);
		}
	}

	handleCtrlC(): void {
		const now = Date.now();
		if (now - this.#lastSigintTime < DOUBLE_SIGINT_WINDOW_MS) {
			this.ctx.requestShutdown();
			return;
		}
		this.#lastSigintTime = now;
		if (this.ctx.session.isStreaming || this.ctx.session.queuedMessageCount > 0) {
			void this.#interrupt("interrupt");
		}
		this.ctx.clearEditor();
		this.ctx.showStatus(
			`Press ${appKey(this.ctx.keybindings, "app.clear")} again to close the session`,
		);
	}

	handleCtrlD(): void {
		// The editor only calls this on an empty buffer, so there is no draft to preserve.
		this.ctx.requestShutdown();
	}

	handleCtrlZ(): void {
		if (process.platform === "win32") {
			this.ctx.showStatus(
				`Suspend (${appKey(this.ctx.keybindings, "app.suspend")}) is not supported on this platform`,
			);
			return;
		}
		// TUI teardown pauses stdin; keep the event loop alive across SIGSTOP so Bun can
		// deliver SIGCONT and restart the TUI after `fg`.
		const suspendKeepalive = setInterval(() => {}, 2 ** 30);
		const onResume = (): void => {
			clearInterval(suspendKeepalive);
			this.ctx.ui.start();
			this.ctx.ui.requestRender(true);
			resendProgramStatus();
		};
		process.once("SIGCONT", onResume);
		this.ctx.ui.stop();
		try {
			// SIGSTOP cannot be caught, so a child shell holding SIGTSTP cannot swallow it.
			process.kill(0, "SIGSTOP");
		} catch (error) {
			clearInterval(suspendKeepalive);
			process.removeListener("SIGCONT", onResume);
			this.ctx.ui.start();
			this.ctx.ui.requestRender(true);
			this.ctx.showError(`Failed to suspend: ${error instanceof Error ? error.message : String(error)}`);
		}
	}

	// ------------------------------------------------------------------
	// Queue
	// ------------------------------------------------------------------

	/**
	 * Queue `/queue` input behind the running turn, or submit it immediately when idle.
	 * `detached` is a submission whose draft already left the editor: its attachments are
	 * queued, and its text is restored if the authority refuses it.
	 */
	async handleQueueCommand(
		text: string,
		detached?: Pick<SubmittedUserInput, "text" | "images" | "imageLinks">,
	): Promise<void> {
		const sourceImages = detached ? detached.images : this.ctx.editor.pendingImages;
		const images = sourceImages && sourceImages.length > 0 ? [...sourceImages] : undefined;
		try {
			await this.ctx.session.prompt(text, {
				...(this.ctx.session.isStreaming ? { streamingBehavior: "followUp" as const } : {}),
				...(images ? { images } : {}),
			});
		} catch (error) {
			if (detached?.text) this.#restoreEntriesToEditor([{ text: detached.text, images }]);
			this.ctx.showError(error instanceof Error ? error.message : String(error));
			return;
		}
		this.ctx.updatePendingMessagesDisplay();
		this.ctx.ui.requestRender();
	}

	/** Alt+Up: take the newest queued message back into the editor. */
	handleDequeue(): void {
		const popped = this.ctx.session.popLastQueuedMessage();
		if (!popped) {
			this.ctx.showStatus("No queued messages to restore");
			return;
		}
		this.#restoreEntriesToEditor([popped]);
		this.ctx.showStatus("Restored last queued message to editor");
	}

	/**
	 * Return the authority's queued input to the editor. The shell calls this when the
	 * conversation behind the composer changed, so text queued for the old one is not lost.
	 */
	restoreCompactionQueueToEditor(status = "Conversation changed: queued input returned to the editor"): void {
		const { steering, followUp } = this.ctx.session.getQueuedMessages();
		const entries: RestoredQueuedMessage[] = [...steering, ...followUp].map(text => ({ text }));
		if (entries.length === 0) return;
		this.#restoreEntriesToEditor(entries);
		this.ctx.showStatus(status);
	}

	/**
	 * Merge queued entries (oldest→newest) ahead of the current draft, folding their images back
	 * into the pending-image buffer and refreshing the pending bar. Returns the entries restored.
	 */
	#restoreEntriesToEditor(entries: RestoredQueuedMessage[]): number {
		const editor = this.ctx.editor;
		if (entries.length === 0) {
			this.ctx.updatePendingMessagesDisplay();
			return 0;
		}
		// Image markers are positional (`[Image #N]` ↔ `pendingImages[N-1]`); prepending queued
		// text while appending queued images shifts the slot every marker points at.
		const queuedImages = entries.flatMap(entry => entry.images ?? []);
		let queuedText: string;
		if (queuedImages.length > 0) {
			const parts: string[] = [];
			let imageOffset = editor.pendingImages.length;
			for (const entry of entries) {
				parts.push(shiftImageMarkers(entry.text, imageOffset));
				imageOffset += entry.images?.length ?? 0;
			}
			queuedText = parts.join("\n\n");
		} else {
			queuedText = entries.map(entry => entry.text).join("\n\n");
		}
		const combinedText = [queuedText, editor.getText()].filter(text => text.trim()).join("\n\n");
		if (queuedImages.length > 0) {
			editor.pendingImages.push(...queuedImages);
			editor.pendingImageLinks.push(...queuedImages.map(() => undefined));
			editor.imageLinks = editor.pendingImageLinks;
		}
		editor.setCollapsedText(combinedText);
		this.ctx.updatePendingMessagesDisplay();
		this.ctx.ui.requestRender();
		return entries.length;
	}

	// ------------------------------------------------------------------
	// Display / model controls
	// ------------------------------------------------------------------

	cycleThinkingLevel(): void {
		const level = this.ctx.session.cycleThinkingLevel();
		if (level === undefined) {
			this.ctx.showStatus("Current model does not support thinking");
			return;
		}
		this.ctx.statusLine.invalidate();
		this.ctx.ui.requestRender();
	}

	async #cycleModel(direction: "forward" | "backward"): Promise<void> {
		try {
			const session = this.ctx.session;
			const gateway = session.gateway;
			let routes: readonly RoleCycleRoute[] = [];
			if (gateway) {
				// A failed role read must not block ctrl+p: the catalog pool still cycles.
				try {
					routes = await gateway.roleCycle();
				} catch {
					routes = [];
				}
			}
			const next = await cycleSessionModel(
				routes,
				(provider, id) => session.modelRegistry.find(provider, id),
				session.model,
				direction,
				() => session.cycleModel(direction),
			);
			if (!next) {
				this.ctx.showStatus("No models available to cycle");
				return;
			}
			// The catalog branch already switched the session model.
			if (next.role) await session.setModel(next.model);
			this.ctx.statusLine.invalidate();
			const suffix = next.role ? ` (${next.role})` : "";
			this.ctx.showStatus(`Model: ${next.model.provider}/${next.model.id}${suffix}`);
		} catch (error) {
			this.ctx.showError(error instanceof Error ? error.message : String(error));
		}
	}

	toggleToolOutputExpansion(): void {
		if (this.ctx.hideToolActivity) {
			const visibilityKey = appKey(this.ctx.keybindings, "app.tools.toggleVisibility");
			this.ctx.showStatus(
				`Tool activity is hidden — show it with ${visibilityKey} or /settings before expanding`,
			);
			return;
		}
		this.setToolsExpanded(!this.ctx.toolOutputExpanded);
		this.ctx.showStatus(`Tool output expansion: ${this.ctx.toolOutputExpanded ? "enabled" : "disabled"}`);
	}

	setToolsExpanded(expanded: boolean): void {
		this.ctx.toolOutputExpanded = expanded;
		for (const child of this.ctx.chatContainer.children) {
			if (isExpandable(child)) child.setExpanded(expanded);
		}
		this.ctx.ui.requestRender(true);
	}

	toggleThinkingBlockVisibility(): void {
		this.ctx.hideThinkingBlock = !this.ctx.hideThinkingBlock;
		cfgHideThinkingBlock.set(this.ctx.settings, this.ctx.hideThinkingBlock);
		for (const child of this.ctx.chatContainer.children) {
			if (child instanceof AssistantMessageComponent) {
				child.setHideThinkingBlock(this.ctx.hideThinkingBlock);
			}
		}
		if (this.ctx.streamingComponent && this.ctx.streamingMessage) {
			this.ctx.streamingComponent.setHideThinkingBlock(this.ctx.hideThinkingBlock);
			this.ctx.streamingComponent.updateContent(this.ctx.streamingMessage);
		}
		// Committed rows keep their presentation, so an explicit toggle replays history.
		this.ctx.chatContainer.resetStableEmission();
		this.ctx.ui.resetDisplay();
		this.ctx.showStatus(`Thinking blocks: ${this.ctx.hideThinkingBlock ? "hidden" : "visible"}`);
	}

	/** Forget the hover band (mouse capture turned off); no repaint — there is no new hover. */
	clearHoverHighlight(): void {
		this.ctx.composer.setHoveredClickId(undefined);
	}

	// ------------------------------------------------------------------
	// External editor
	// ------------------------------------------------------------------

	async openExternalEditor(): Promise<void> {
		const editorCmd = getEditorCommand();
		if (!editorCmd) {
			this.ctx.showWarning("No editor configured. Set $VISUAL or $EDITOR environment variable.");
			return;
		}
		const currentText = this.ctx.editor.getExpandedText();
		try {
			this.ctx.ui.stop();
			const result = await openInEditor(editorCmd, currentText, { extension: ".omp.md" });
			if (result !== null) this.ctx.editor.setText(result);
		} catch (error) {
			this.ctx.showWarning(
				`Failed to open external editor: ${error instanceof Error ? error.message : String(error)}`,
			);
		} finally {
			this.ctx.ui.start();
			this.ctx.ui.requestRender();
		}
	}

	// ------------------------------------------------------------------
	// Image paste
	// ------------------------------------------------------------------

	/** Clipboard image/text paste fallback for the paste-image chord (Cmd+V / Ctrl+V). */
	async handleImagePaste(): Promise<boolean> {
		if (!isClipboardTerminalLocal()) {
			this.ctx.showStatus(REMOTE_CLIPBOARD_STATUS);
			return false;
		}
		if (this.ctx.ui.hasOverlay()) return false;
		let textPromise: Promise<string> | undefined;
		try {
			// macOS Finder puts both a file URL and a generated icon bitmap on the pasteboard;
			// the file bytes are what the user copied, so probe file URLs first.
			const fileUrls = await readMacFileUrlsFromClipboard();
			let attachedFromFiles = false;
			for (const url of fileUrls) {
				const candidate = extractImagePathFromText(url);
				if (!candidate) continue;
				await this.handleImagePathPaste(candidate);
				attachedFromFiles = true;
			}
			if (attachedFromFiles) return true;

			// Both bridges can shell out, so start the text read alongside the image read.
			textPromise = readTextFromClipboard();
			textPromise.then(
				() => {},
				() => {},
			);
			const image = await readImageFromClipboard();
			if (image) {
				return await this.#attachClipboardImage(image.data, image.mimeType);
			}
			const text = await textPromise;
			if (!text) {
				this.ctx.showStatus("Clipboard is empty");
				return false;
			}
			// A pasted image path becomes an attachment instead of literal text.
			const imagePath = extractImagePathFromText(text);
			if (imagePath) {
				await this.handleImagePathPaste(imagePath);
				return true;
			}
			this.ctx.editor.pasteText(text);
			this.ctx.ui.requestRender();
			return true;
		} catch {
			this.ctx.showStatus("Failed to read clipboard");
			return false;
		}
	}

	/** Bracketed paste of an image file path: attach the bytes, else paste the path as text. */
	async handleImagePathPaste(pastedPath: string): Promise<void> {
		const editor = this.ctx.editor;
		try {
			const file = Bun.file(pastedPath);
			if (!(await file.exists())) {
				editor.pasteText(pastedPath);
				this.ctx.showStatus(`Image not found at ${pastedPath}`);
				return;
			}
			if (!file.type.startsWith("image/")) {
				editor.pasteText(pastedPath);
				this.ctx.showStatus("Pasted path is not a supported image");
				return;
			}
			const data = Buffer.from(await file.arrayBuffer()).toString("base64");
			const normalized = await this.#normalizePastedImage(
				{ type: "image", data, mimeType: file.type },
				`Unsupported pasted image format: ${file.type}`,
			);
			if (normalized) await this.#insertPendingImage(normalized);
		} catch {
			if (await this.#tryPasteClipboardImage()) return;
			editor.pasteText(pastedPath);
			this.ctx.showStatus("Failed to read pasted image path");
		}
	}

	async #attachClipboardImage(data: Uint8Array, mimeType: string): Promise<boolean> {
		const normalized = await this.#normalizePastedImage(
			{ type: "image", data: Buffer.from(data).toString("base64"), mimeType },
			`Unsupported clipboard image format: ${mimeType}`,
		);
		if (!normalized) return false;
		await this.#insertPendingImage(normalized);
		return true;
	}

	async #tryPasteClipboardImage(): Promise<boolean> {
		if (!isClipboardTerminalLocal()) return false;
		try {
			const image = await readImageFromClipboard();
			if (!image) return false;
			return await this.#attachClipboardImage(image.data, image.mimeType);
		} catch {
			return false;
		}
	}

	/** Normalize to a format the model accepts, then shrink it when the setting asks. */
	async #normalizePastedImage(image: ImageContent, unsupportedMessage: string): Promise<ImageContent | null> {
		let normalized = await ensureSupportedImageInput(image);
		if (!normalized) {
			this.ctx.showStatus(unsupportedMessage);
			return null;
		}
		if (cfgImagesAutoResize.get(this.ctx.settings)) {
			try {
				const resized = await resizeImage({
					type: "image",
					data: normalized.data,
					mimeType: normalized.mimeType,
				});
				normalized = { type: "image", data: resized.data, mimeType: resized.mimeType };
			} catch {
				// Keep the normalized image when resizing fails.
			}
		}
		return normalized;
	}

	/** Add an attachment to the composer as a chip token the editor expands on submit. */
	async #insertPendingImage(image: ImageContent): Promise<void> {
		const editor = this.ctx.editor;
		editor.pendingImages.push(image);
		editor.pendingImageLinks.push(undefined);
		editor.imageLinks = editor.pendingImageLinks;
		const imageNum = editor.pendingImages.length;
		const dims = await this.#imageDimensions(image);
		setCachedImageDimensions(image, dims ?? null);
		editor.insertAtom(chipLabel("image", imageNum), formatVisionMarker("image", imageNum, dims));
		this.ctx.ui.requestRender();
	}

	async #imageDimensions(image: ImageContent): Promise<{ width: number; height: number } | undefined> {
		try {
			const { width, height } = await new Bun.Image(Buffer.from(image.data, "base64")).metadata();
			if (width && height) return { width, height };
		} catch {
			// Unknown/corrupt header: the marker falls back to a bare `[Image #N]`.
		}
		return undefined;
	}
}
