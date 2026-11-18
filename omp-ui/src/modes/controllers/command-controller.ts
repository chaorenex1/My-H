/**
 * Local slash-command router for the Hermes shell.
 *
 * A handful of commands only drive this terminal (pickers, transcript, status); every other
 * slash command is a Hermes command and travels to the gateway's `command.dispatch` catalog,
 * which owns its behaviour and its transcript side effects.
 */
import { Markdown } from "@oh-my-pi/pi-tui";
import { buildHotkeysMarkdown, HotkeysSheetComponent } from "@oh-my-pi/pi-tui/hotkeys-markdown";
import { isNativeRendering } from "@oh-my-pi/pi-tui/native/state";
import { getMarkdownTheme, theme } from "@oh-my-pi/pi-tui/theme";
import { openPath } from "../../utils/open";
import type { CanonicalSnapshot, CommandDispatchResult, SessionListRow } from "../rpc/gateway-contract.generated";
import { gatewayCreateParams } from "../rpc/gateway-session";
import type { InteractiveModeContext } from "../types";

/** A dispatch payload may also carry the slash worker's mirrored side-effect warning. */
type DispatchResult = CommandDispatchResult & { warning?: string | null };

const HELP_MARKDOWN = `# Commands

Handled by this shell:

- \`/model\` — model picker
- \`/thinking\` — reasoning effort picker
- \`/settings\` — settings menu (Appearance, theme, and the rest)
- \`/theme\` — settings menu, where the theme picker lives
- \`/resume [id|title]\` — resume a session from the gateway's session list
- \`/session\` — session report (id, workspace, model, messages, tokens, cost)
- \`/usage\` — token, cost and context usage of this session
- \`/context\` — context window usage
- \`/tools\` — tools the session advertises
- \`/todo\` — todo HUD
- \`/copy\` — copy selector
- \`/rename <title>\` — rename this session
- \`/clear\` — start a new gateway session
- \`/dequeue\` — remove the last queued message
- \`/hotkeys\` — keyboard shortcuts
- \`/quit\` — close the shell

Every other command is a Hermes command and runs on the gateway.`;

export class CommandController {
	constructor(private readonly ctx: InteractiveModeContext) {}

	/**
	 * Route one submitted slash command.
	 *
	 * Returns false only when `text` is not a slash command, so the caller submits it as a prompt;
	 * a recognised command is consumed here (rendered locally or dispatched to the gateway).
	 */
	async dispatch(text: string): Promise<boolean> {
		return await this.#route(text, 0);
	}

	/** One routing pass; `aliasDepth` bounds gateway alias chains. */
	async #route(text: string, aliasDepth: number): Promise<boolean> {
		const trimmed = text.trim();
		if (!trimmed.startsWith("/")) return false;
		const [rawName, ...rest] = trimmed.slice(1).split(/\s+/);
		const name = (rawName ?? "").toLowerCase();
		const handler = this.#localHandlers[name];
		if (!handler) return await this.#dispatchToGateway(trimmed, aliasDepth);
		await handler(rest.join(" ").trim());
		return true;
	}

	/** Local shell commands; every name not listed here is a gateway command. */
	readonly #localHandlers: Record<string, (argument: string) => void | Promise<void>> = {
		model: () => this.ctx.showModelSelector(),
		models: () => this.ctx.showModelHub(),
		thinking: () => this.ctx.showThinkingSelector(),
		settings: () => this.ctx.showSettingsSelector(),
		theme: () => this.ctx.showSettingsSelector(),
		resume: argument => this.#resume(argument),
		help: () => this.#presentMarkdown(HELP_MARKDOWN),
		hotkeys: () => this.ctx.handleHotkeysCommand(),
		quit: () => this.#quit(),
		clear: () => this.ctx.handleClearCommand(),
		rename: argument => this.ctx.handleRenameCommand(argument),
		copy: () => this.ctx.showCopySelector(),
		todo: argument => this.ctx.handleTodoCommand(argument),
		usage: () => this.ctx.handleUsageCommand(),
		session: () => this.ctx.handleSessionCommand(),
		tools: () => this.ctx.handleToolsCommand(),
		context: () => this.ctx.handleContextCommand(),
		dequeue: () => this.ctx.handleDequeue(),
	};

	async #quit(): Promise<void> {
		this.ctx.shutdownRequested = true;
		await this.ctx.checkShutdownRequested();
	}

	/** `/resume`: the picker, or the session whose id or title matches `target`. */
	async #resume(target: string): Promise<void> {
		if (!target) {
			this.ctx.showSessionSelector();
			return;
		}
		const gateway = this.ctx.session.gateway;
		if (!gateway) {
			this.ctx.showWarning("Resuming a session needs the Hermes gateway.");
			return;
		}
		let rows: SessionListRow[];
		try {
			rows = await gateway.listSessions();
		} catch (error) {
			this.ctx.showError(`Failed to list sessions: ${error instanceof Error ? error.message : String(error)}`);
			return;
		}
		const needle = target.toLowerCase();
		const match = rows.find(row => row.id === target) ?? rows.find(row => row.title?.toLowerCase() === needle);
		if (!match) {
			this.ctx.showWarning(`No session matches "${target}".`);
			this.ctx.showSessionSelector();
			return;
		}
		await this.ctx.handleResumeSession(match.id, { followContinuation: true });
	}

	/** Everything the shell does not own: the gateway command catalog runs it. */
	async #dispatchToGateway(text: string, aliasDepth: number): Promise<boolean> {
		const gateway = this.ctx.session.gateway;
		if (!gateway) {
			this.ctx.showWarning("Not connected to the Hermes gateway.");
			return true;
		}
		let result: DispatchResult;
		try {
			result = (await gateway.dispatchCommand(text)) as unknown as DispatchResult;
		} catch (error) {
			const command = text.split(/\s+/, 1)[0] ?? text;
			this.ctx.showError(`${command} failed: ${error instanceof Error ? error.message : String(error)}`);
			return true;
		}
		if (result.notice) this.ctx.showStatus(result.notice);
		switch (result.type) {
			case "alias": {
				// The catalog maps a quick command onto another command: run that one with the same tail.
				if (!result.target || aliasDepth >= 3) {
					this.ctx.showWarning("The gateway returned an unusable command alias.");
					return true;
				}
				const separator = text.search(/\s/);
				const tail = separator === -1 ? "" : text.slice(separator);
				return await this.#route(`/${result.target}${tail}`, aliasDepth + 1);
			}
			case "send":
			case "skill": {
				// `display` is the invocation the gateway projected; `message` is the model-facing body.
				if (result.display) this.ctx.showStatus(result.display);
				if (result.message) await this.#submit(result.message);
				else this.ctx.showWarning("The command returned no prompt text.");
				break;
			}
			case "prefill":
				// `/undo` and friends hand text back for the composer instead of submitting it.
				if (result.message) this.ctx.editor.setText(result.message);
				break;
			default:
				if (result.output) this.ctx.showStatus(result.output);
				break;
		}
		if (result.warning) this.ctx.showWarning(result.warning);
		return true;
	}

	/** Submit a directive's prompt as an ordinary turn. */
	async #submit(message: string | null | undefined): Promise<void> {
		if (!message) return;
		try {
			await this.ctx.session.prompt(message);
		} catch (error) {
			this.ctx.showError(`Prompt failed: ${error instanceof Error ? error.message : String(error)}`);
		}
	}

	/** `/model` and friends are shell pickers; this reports what the gateway session holds. */
	async handleSessionCommand(): Promise<void> {
		const session = this.ctx.session;
		const stats = session.getSessionStats();
		const model = session.model;
		const usage = session.getContextUsage({ contextWindow: model?.contextWindow ?? undefined });
		const lines = [
			`${theme.fg("dim", "Session:")} ${session.sessionName ?? "(untitled)"}`,
			`${theme.fg("dim", "ID:")} ${stats.sessionId}`,
			`${theme.fg("dim", "Workspace:")} ${this.ctx.sessionManager.getCwd()}`,
			`${theme.fg("dim", "Model:")} ${model ? `${model.provider}/${model.id}` : "none"}`,
			`${theme.fg("dim", "Messages:")} ${stats.totalMessages} (${stats.userMessages} user, ${stats.assistantMessages} assistant, ${stats.toolCalls} tool calls)`,
			`${theme.fg("dim", "Tokens:")} ${stats.tokens.total.toLocaleString()} (${stats.tokens.input.toLocaleString()} in, ${stats.tokens.output.toLocaleString()} out)`,
			`${theme.fg("dim", "Cost:")} ${stats.cost.toFixed(4)}`,
		];
		if (usage) {
			lines.push(
				`${theme.fg("dim", "Context:")} ${usage.tokens.toLocaleString()} / ${usage.contextWindow.toLocaleString()} (${Math.round(usage.percent)}%)`,
			);
		}
		this.ctx.showStatus(lines.join("\n"));
	}

	/** `/usage`: gateway-reported tokens, cost and context for this session. */
	async handleUsageCommand(): Promise<void> {
		const session = this.ctx.session;
		// The authority refreshes stats/usage after every settled turn; read its latest snapshot.
		await session.gateway?.refresh();
		const stats = session.getSessionStats();
		const model = session.model;
		const usage = session.getContextUsage({ contextWindow: model?.contextWindow ?? undefined });
		const lines = [
			`${theme.fg("dim", "Tokens:")} ${stats.tokens.total.toLocaleString()} (${stats.tokens.input.toLocaleString()} in, ${stats.tokens.output.toLocaleString()} out, ${stats.tokens.reasoning.toLocaleString()} reasoning)`,
		];
		if (stats.tokens.cacheRead > 0 || stats.tokens.cacheWrite > 0) {
			lines.push(
				`${theme.fg("dim", "Cache:")} ${stats.tokens.cacheRead.toLocaleString()} read, ${stats.tokens.cacheWrite.toLocaleString()} written`,
			);
		}
		lines.push(`${theme.fg("dim", "Cost:")} ${stats.cost.toFixed(4)}`);
		if (stats.premiumRequests > 0) {
			lines.push(`${theme.fg("dim", "API calls:")} ${stats.premiumRequests}`);
		}
		if (usage) {
			lines.push(
				`${theme.fg("dim", "Context:")} ${usage.tokens.toLocaleString()} / ${usage.contextWindow.toLocaleString()} (${Math.round(usage.percent)}%)`,
			);
		}
		this.ctx.showStatus(lines.join("\n"));
	}

	/** `/context`: the gateway's context-window accounting. */
	handleContextCommand(): void {
		const session = this.ctx.session;
		const model = session.model;
		const usage = session.getContextUsage({ contextWindow: model?.contextWindow ?? undefined });
		if (!usage) {
			this.ctx.showWarning(
				"Context usage is unavailable: the gateway has not reported tokens and a context window for this session.",
			);
			return;
		}
		const remaining = Math.max(0, usage.contextWindow - usage.tokens);
		const lines = [
			`${theme.fg("dim", "Model:")} ${model ? `${model.provider}/${model.id}` : "none"}`,
			`${theme.fg("dim", "Tokens:")} ${usage.tokens.toLocaleString()} / ${usage.contextWindow.toLocaleString()} (${Math.round(usage.percent)}%)`,
			`${theme.fg("dim", "Remaining:")} ${remaining.toLocaleString()}`,
		];
		this.ctx.showStatus(lines.join("\n"));
	}

	/** `/tools`: the tool names the gateway advertised for this session. */
	handleToolsCommand(): void {
		const tools = this.ctx.session.getAllToolNames().sort((a, b) => a.localeCompare(b));
		if (tools.length === 0) {
			this.ctx.showWarning("The Hermes session advertises no tools.");
			return;
		}
		this.#presentMarkdown(`# Available Tools\n\n${tools.map(name => `- ${name}`).join("\n")}`);
	}

	/** `/hotkeys`: the live keybinding sheet. */
	handleHotkeysCommand(): void {
		const bindings = { keybindings: this.ctx.keybindings };
		if (isNativeRendering()) {
			// A native terminal gets a dismissable sheet with keycaps instead of a markdown table.
			const sheet = new HotkeysSheetComponent(bindings, () => {
				handle.hide();
				this.ctx.ui.setFocus(this.ctx.editorContainer.children[0] ?? this.ctx.editor);
				this.ctx.ui.requestRender();
			});
			const handle = this.ctx.ui.showOverlay(sheet, { anchor: "center", width: "90%", maxHeight: "90%" });
			this.ctx.ui.setFocus(sheet);
			this.ctx.ui.requestRender();
			return;
		}
		this.#presentMarkdown(buildHotkeysMarkdown(bindings));
	}

	/** `/clear`: create a fresh gateway session and re-seed the transcript from it. */
	async handleClearCommand(): Promise<void> {
		const session = this.ctx.session;
		const gateway = session.gateway;
		if (!gateway) {
			this.ctx.showWarning("Starting a new session needs the Hermes gateway.");
			return;
		}
		if (session.isStreaming) {
			this.ctx.showWarning("Wait for the current turn to finish (or interrupt it) before starting a new session.");
			return;
		}
		let sessionId: string;
		try {
			const created = await gateway.client.request<CanonicalSnapshot>(
				"session.create",
				gatewayCreateParams({ cwd: this.ctx.sessionManager.getCwd() })
			);
			sessionId = created.session_id;
		} catch (error) {
			this.ctx.showError(`Failed to start a new session: ${error instanceof Error ? error.message : String(error)}`);
			return;
		}
		if (!(await session.switchGatewaySession(sessionId))) return;
		// Same re-seed as a resume: the projection and the transcript both follow the new session.
		this.ctx.resetTranscript();
		this.ctx.sessionManager.reset(session.gateway?.initialMessages() ?? []);
		await this.ctx.renderInitialMessages({ preserveExistingChat: true });
		await this.ctx.reloadTodos();
		this.ctx.ui.requestRender(true);
		this.ctx.showStatus("New session started");
	}

	async handleRenameCommand(title: string): Promise<void> {
		const name = title.trim();
		if (!name) {
			this.ctx.showWarning("Usage: /rename <title>");
			return;
		}
		try {
			await this.ctx.session.setSessionName(name);
			this.ctx.showStatus(`Session renamed to "${name}".`);
		} catch (error) {
			this.ctx.showError(`Rename failed: ${error instanceof Error ? error.message : String(error)}`);
		}
	}

	openInBrowser(urlOrPath: string): void {
		openPath(urlOrPath);
	}

	#presentMarkdown(markdown: string): void {
		this.ctx.presentCommandOutput(new Markdown(markdown.trim(), 0, 0, getMarkdownTheme()));
	}
}
