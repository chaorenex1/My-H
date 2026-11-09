/**
 * The shell's session object: a view of one Hermes gateway session.
 *
 * `hermes omp` renders a session owned by the Hermes session authority. This class is the client
 * half of that split — it owns the renderer-facing state (the transcript the renderer walks, the
 * model shown on the status line, the thinking selector, the queue pill, notices) and forwards every
 * turn operation (`prompt`, `steer`, `interrupt`, model/reasoning mutation) to
 * `modes/rpc/gateway-session.ts`. There is no local agent loop, no tool execution, and nothing is
 * persisted here: `session.create`/`session.history` remain the durable transcript.
 */

import type { Agent, AgentMessage, AgentState, ThinkingLevel } from "../vendor/agent/src/index.ts";
import type { Effort, ImageContent, Model } from "../vendor/ai/src/index.ts";
import type { ContextUsage } from "@oh-my-pi/pi-tui/status-line/types";
import type { ConfiguredThinkingLevel } from "@oh-my-pi/pi-tui/thinking";
import type { TodoPhase } from "@oh-my-pi/pi-tui/tools/todo";

import type { ModelRegistry } from "../config/model-registry";
import type { Settings } from "../config/settings";
import type { SessionStats as GatewaySessionStats, SessionUsageResult } from "../modes/rpc/gateway-contract.generated";
import { gatewayTodoPhases, type GatewayOmpSession } from "../modes/rpc/gateway-session";
import type { AgentSessionEvent, AgentSessionEventListener } from "./agent-session-events";
import type {
	AgentSessionConfig,
	DroppedPrompt,
	FollowUpOptions,
	ModelCycleResult,
	PromptOptions,
	RestoredQueuedMessage,
	SendUserMessageOptions,
	SessionStats,
	SteerOptions
} from "./agent-session-types";
import type { SessionManager } from "./session-manager";

export * from "./agent-session-types";

/** One `tools.list` row's tool entry: the authority sends names, tolerate an object spelling. */
function toolNameOf(tool: unknown): string | undefined {
	if (typeof tool === "string") return tool;
	if (typeof tool === "object" && tool !== null && "name" in tool && typeof tool.name === "string") {
		return tool.name;
	}
	return undefined;
}

/**
 * ADR-10 D8.2a: the renderer projection of one authority tool. `name` is the render key
 * (`read_file` → `read`) used for renderer selection and read-group behavior; `label` is the
 * authority's wire name shown in the card title.
 */
export interface GatewayToolProjection {
	name: string;
	label: string;
}

/** Hermes wire names that render with an omp built-in card under a different render key. */
const TOOL_RENDER_NAMES: Readonly<Record<string, string>> = {
	read_file: "read",
	todo_list: "todo",
	// ADR-4 D12: `execute_code` renders with omp's native eval card (multi-cell, views, meta).
	execute_code: "eval"
};

/** Token/cost snapshot the status line and `/session` render. */
interface UsageSnapshot {
	tokens: number;
	window: number;
	cost: number;
	input: number;
	output: number;
	cacheRead: number;
	cacheWrite: number;
	requests: number;
}

export class AgentSession {
	readonly settings: Settings;
	readonly sessionManager: SessionManager;
	readonly modelRegistry: ModelRegistry;
	readonly agent: Agent;

	#gateway: GatewayOmpSession | undefined;
	#gatewayUnsubscribe: (() => void) | undefined;
	#listeners = new Set<AgentSessionEventListener>();
	#runStateListeners = new Set<(state: "running" | "idle") => void>();
	#disposers: Array<() => void> = [];
	#model: Model | undefined;
	#thinkingLevel: ConfiguredThinkingLevel | undefined;
	#scopedModels: Array<{ model: Model; thinkingLevel?: ThinkingLevel }>;
	#disposed = false;
	#runState: "running" | "idle" = "idle";
	#runStartedAt: number | undefined;
	#tools = new Set<string>();
	#toolRefresh: Promise<void> | undefined;
	#stats: GatewaySessionStats | undefined;
	#usage: SessionUsageResult | undefined;
	#dialogChain: Promise<unknown> = Promise.resolve();
	#promptDropped: ((prompt: DroppedPrompt) => void) | undefined;

	constructor(config: AgentSessionConfig) {
		this.agent = config.agent;
		this.sessionManager = config.sessionManager;
		this.settings = config.settings;
		this.modelRegistry = config.modelRegistry;
		this.#thinkingLevel = config.thinkingLevel;
		this.#scopedModels = config.scopedModels ? [...config.scopedModels] : [];
	}

	// ------------------------------------------------------------------
	// Gateway attachment
	// ------------------------------------------------------------------

	get gateway(): GatewayOmpSession | undefined {
		return this.#gateway;
	}

	/** Adopt a gateway session as this shell session's turn source. */
	attachGateway(gateway: GatewayOmpSession): void {
		if (this.#gateway) throw new Error("session is already attached to the Hermes gateway");
		this.#bindGateway(gateway);
	}

	/**
	 * Await the authority's tool list. The first transcript render picks its tool renderers from
	 * it (ADR-10 D8.2a), so boot and session switches await this before painting cards.
	 */
	refreshTools(): Promise<void> {
		return this.#refreshTools();
	}

	/**
	 * Switch the viewed session: pair a new gateway connection resumed on `sessionId`, retire the
	 * current one, and re-seed the transcript from the new snapshot.
	 */
	async switchGatewaySession(sessionId: string): Promise<boolean> {
		const current = this.#gateway;
		if (!current) throw new Error("no gateway attached");
		const { connectGatewayOmpSession } = await import("../modes/rpc/gateway-session");
		const { installGatewayModelCatalog } = await import("../modes/rpc/gateway-models");
		let next: GatewayOmpSession;
		try {
			next = await connectGatewayOmpSession({
				cwd: current.cwd ?? this.sessionManager.getCwd(),
				resumeSessionId: sessionId,
				onError: error => this.emitNotice("error", error.message, "gateway")
			});
		} catch (error) {
			this.emitNotice("error", error instanceof Error ? error.message : String(error), "gateway");
			return false;
		}
		this.#gatewayUnsubscribe?.();
		this.#gatewayUnsubscribe = undefined;
		this.#gateway = undefined;
		const previous = current;
		this.#bindGateway(next);
		previous.close("session switched");
		// The new authority may expose a different toolset; resolve it before the shell repaints.
		await this.refreshTools();
		try {
			installGatewayModelCatalog(this.modelRegistry as never, await next.modelOptions());
		} catch {
			// The catalog stays as it was; the session itself is usable.
		}
		this.#emit({ type: "model_changed" });
		return true;
	}

	#bindGateway(gateway: GatewayOmpSession): void {
		this.#gateway = gateway;
		if (gateway.cwd) this.sessionManager.setCwdWithoutRelocation(gateway.cwd);
		this.sessionManager.adoptSessionId(gateway.sessionId);
		if (gateway.title) this.sessionManager.setSessionName(gateway.title);
		// One initial transcript for the shell: the gateway snapshot, not a local session file.
		this.agent.replaceMessages(gateway.initialMessages());
		this.#applyGatewayModel();
		this.#gatewayUnsubscribe?.();
		this.#gatewayUnsubscribe = gateway.subscribe(event => {
			this.#onGatewayEvent(event);
		});
		this.addDisposer(() => gateway.close());
		// The served model's context window and the tool list land after the snapshot.
		void gateway
			.refresh()
			.then(() => {
				this.#stats = gateway.stats;
				this.#usage = gateway.usage;
				this.#applyGatewayModel();
			})
			.catch(() => {});
		// A switched session may expose a different toolset: refetch for the new authority. Until it
		// lands, an unknown tool renders the generic card rather than a stale session's renderer.
		this.#tools = new Set();
		this.#toolRefresh = undefined;
		void this.#refreshTools();
	}

	/** Adopt gateway-side session state (title/model/cwd) after a live update. */
	#applyGatewayModel(): void {
		const gateway = this.#gateway;
		if (!gateway) return;
		if (gateway.title) this.sessionManager.setSessionName(gateway.title);
		const id = gateway.model;
		if (!id) return;
		const provider = gateway.provider;
		const exact = provider ? this.modelRegistry.find(provider, id) : undefined;
		const current = this.#model;
		if (exact) {
			this.#model =
				gateway.contextLength !== undefined && gateway.contextLength !== exact.contextWindow
					? { ...exact, contextWindow: gateway.contextLength }
					: exact;
			return;
		}
		const models = this.modelRegistry.getAll();
		const base = models.find(model => model.provider === provider) ?? current ?? models[0];
		if (!base) return;
		this.#model = {
			...base,
			id,
			name: id,
			contextWindow: gateway.contextLength ?? base.contextWindow,
			...(provider ? { provider } : {})
		};
	}

	/** Lazily fetch the authority's tool names (`tools.list`) for the `/tools` report and renderer choice. */
	#refreshTools(): Promise<void> {
		this.#toolRefresh ??= (async () => {
			const gateway = this.#gateway;
			if (!gateway) return;
			try {
				const result = (await gateway.client.request("tools.list", {})) as unknown as {
					toolsets?: Array<{ tools?: unknown[] }> | null;
				};
				// The authority lists tools per toolset (`resolved_tools`), not as a flat `tools` array.
				const names = (result.toolsets ?? [])
					.flatMap(toolset => (Array.isArray(toolset?.tools) ? toolset.tools : []))
					.map(toolNameOf)
					.filter((name): name is string => name !== undefined);
				// A switch may have replaced this gateway while the request was in flight.
				if (this.#gateway === gateway) this.#tools = new Set(names);
			} catch {
				// The report degrades to the tool names carried by the transcript.
			}
		})();
		return this.#toolRefresh;
	}

	// ------------------------------------------------------------------
	// Events
	// ------------------------------------------------------------------

	subscribe(listener: AgentSessionEventListener): () => void {
		this.#listeners.add(listener);
		return () => this.#listeners.delete(listener);
	}

	subscribeRunState(listener: (state: "running" | "idle") => void): () => void {
		this.#runStateListeners.add(listener);
		return () => this.#runStateListeners.delete(listener);
	}

	#emit(event: AgentSessionEvent): void {
		for (const listener of [...this.#listeners]) {
			try {
				listener(event);
			} catch (error) {
				this.emitNotice("error", error instanceof Error ? error.message : String(error), "session");
			}
		}
	}

	#emitRunState(state: "running" | "idle"): void {
		if (this.#runState === state) return;
		this.#runState = state;
		this.#runStartedAt = state === "running" ? Date.now() : undefined;
		for (const listener of [...this.#runStateListeners]) listener(state);
	}

	emitNotice(level: "info" | "warning" | "error", message: string, source?: string): void {
		this.#emit({ type: "notice", level, message, source });
	}

	#onGatewayEvent(event: AgentSessionEvent): void {
		switch (event.type) {
			case "agent_start":
			case "turn_start":
				this.#emitRunState("running");
				break;
			case "agent_end":
				this.#emitRunState("idle");
				break;
			case "message_end":
				// Assistant turns and injected custom cards belong to the replayable ledger; tool
				// results are rendered from the gateway snapshot's own tool rows.
				if (
					event.message.role === "assistant" ||
					event.message.role === "custom" ||
					event.message.role === "hookMessage"
				) {
					this.sessionManager.appendMessage(event.message);
				}
				break;
			case "compaction_summary":
				// ADR-5 D6: a committed boundary is a transcript entry. Without it in the replay
				// ledger, a rebuild (settings toggle, theme change, cancel) drops the live divider
				// and `display.collapseCompacted` finds no boundary to collapse to. Resume/switch
				// re-seeds this ledger from the snapshot, so the row is never duplicated.
				this.sessionManager.appendMessage(event.message);
				break;
			case "model_changed":
				this.#applyGatewayModel();
				break;
			default:
				break;
		}
		if (event.type === "message_end" || event.type === "agent_end") {
			const gateway = this.#gateway;
			if (gateway) this.#stats = gateway.stats;
		}
		this.#emit(event);
	}

	// ------------------------------------------------------------------
	// Turn operations (all forwarded to the authority)
	// ------------------------------------------------------------------

	sendUserMessage(text: string, options: SendUserMessageOptions & { images?: ImageContent[] } = {}): Promise<void> {
		const deliverAs = options.deliverAs ?? (this.isStreaming ? "steer" : undefined);
		return this.prompt(text, {
			...(options.images ? { images: options.images } : {}),
			...(deliverAs ? { streamingBehavior: deliverAs } : {})
		}).then(() => undefined);
	}

	/** Submit one prompt; queued/steered when the authority is already running a turn. */
	async prompt(text: string, options: PromptOptions = {}): Promise<boolean> {
		const gateway = this.#gateway;
		if (!gateway) throw new Error("no gateway attached");
		const behavior = options.streamingBehavior ?? (gateway.running ? "steer" : undefined);
		if (gateway.running && behavior !== "followUp") {
			await gateway.steer(text);
			return true;
		}
		await gateway.submit(text, { queued: behavior === "followUp", ...(options.images ? { images: options.images } : {}) });
		return true;
	}

	async steer(text: string, _images?: ImageContent[], _options?: SteerOptions): Promise<void> {
		await this.#requireGateway().steer(text);
	}

	async followUp(text: string, images?: ImageContent[], _options?: FollowUpOptions): Promise<void> {
		await this.#requireGateway().submit(text, { queued: true, ...(images ? { images } : {}) });
	}

	/** Continue the conversation (a `continue` shortcut): one prompt with no new user text shown. */
	async resume(): Promise<void> {
		await this.#requireGateway().submit("continue", { displayKind: "hidden" });
	}

	/** Stop the running turn (`prompt.cancel` on this viewer's admission, else `session.interrupt`). */
	async interrupt(): Promise<void> {
		await this.#requireGateway().interrupt();
	}

	async abort(): Promise<void> {
		await this.interrupt();
	}

	/** Re-submit the last user prompt (`F5`/`/retry`). */
	async retry(): Promise<boolean> {
		const messages = this.messages;
		for (let index = messages.length - 1; index >= 0; index -= 1) {
			const message = messages[index];
			if (message?.role !== "user") continue;
			const text = typeof message.content === "string" ? message.content : "";
			if (!text) return false;
			await this.#requireGateway().submit(text);
			return true;
		}
		return false;
	}

	async waitForIdle(): Promise<void> {
		if (!this.isStreaming) return;
		const { promise, resolve } = Promise.withResolvers<void>();
		const unsubscribe = this.subscribeRunState(state => {
			if (state !== "idle") return;
			unsubscribe();
			resolve();
		});
		await promise;
	}

	#requireGateway(): GatewayOmpSession {
		const gateway = this.#gateway;
		if (!gateway) throw new Error("no gateway attached");
		return gateway;
	}

	// ------------------------------------------------------------------
	// Queue (the authority owns the FIFO; `queue.updated` is the snapshot)
	// ------------------------------------------------------------------

	get queuedMessageCount(): number {
		const queue = this.#gateway?.queue;
		return (queue?.steering.length ?? 0) + (queue?.followUp.length ?? 0);
	}

	getQueuedMessages(): { steering: readonly string[]; followUp: readonly string[]; liveSteered: number } {
		const queue = this.#gateway?.queue;
		return { steering: queue?.steering ?? [], followUp: queue?.followUp ?? [], liveSteered: queue?.liveSteered ?? 0 };
	}

	get hasInterruptibleInput(): boolean {
		return this.isStreaming || this.queuedMessageCount > 0;
	}

	popLastQueuedMessage(): RestoredQueuedMessage | undefined {
		const queue = this.#gateway?.queue;
		const text = queue?.followUp.at(-1) ?? queue?.steering.at(-1);
		return text ? { text } : undefined;
	}

	setPromptDropped(handler: ((prompt: DroppedPrompt) => void) | undefined): void {
		this.#promptDropped = handler;
	}

	/** Deliver a prompt the host decided not to send (a dropped submission) to the restore hook. */
	reportDroppedPrompt(prompt: DroppedPrompt): void {
		this.#promptDropped?.(prompt);
	}

	// ------------------------------------------------------------------
	// Model / reasoning
	// ------------------------------------------------------------------

	get model(): Model | undefined {
		return this.#model;
	}

	getAvailableModels(): Model[] {
		const models = this.modelRegistry.getAvailable();
		return models.length > 0 ? models : this.modelRegistry.getAll();
	}

	async setModel(model: Model): Promise<void> {
		const gateway = this.#gateway;
		if (gateway) await gateway.setModel({ id: model.id, provider: model.provider });
		this.#model = model;
		this.#emit({ type: "model_changed" });
	}

	async cycleModel(direction: "forward" | "backward" = "forward"): Promise<ModelCycleResult | undefined> {
		const isScoped = this.#scopedModels.length > 1;
		const pool: Array<{ model: Model; thinkingLevel?: ThinkingLevel }> = isScoped
			? this.#scopedModels
			: this.getAvailableModels().map(model => ({ model, thinkingLevel: undefined }));
		if (pool.length === 0) return undefined;
		const currentIndex = pool.findIndex(entry => entry.model.id === this.#model?.id && entry.model.provider === this.#model?.provider);
		const step = direction === "forward" ? 1 : -1;
		const next = pool[(currentIndex + step + pool.length) % pool.length]!;
		await this.setModel(next.model);
		return { model: next.model, thinkingLevel: next.thinkingLevel, isScoped };
	}

	get scopedModels(): ReadonlyArray<{ model: Model; thinkingLevel?: ThinkingLevel }> {
		return this.#scopedModels;
	}

	setScopedModels(models: Array<{ model: Model; thinkingLevel?: ThinkingLevel }>): void {
		this.#scopedModels = [...models];
	}

	setModelTemporary(model: Model): void {
		this.#model = model;
		this.#emit({ type: "model_changed" });
	}

	get thinkingLevel(): ThinkingLevel | undefined {
		return this.#thinkingLevel as ThinkingLevel | undefined;
	}

	configuredThinkingLevel(): ConfiguredThinkingLevel | undefined {
		return this.#thinkingLevel;
	}

	getAvailableThinkingLevels(): ReadonlyArray<Effort> {
		const levels = this.#model?.thinking?.efforts;
		return levels && levels.length > 0 ? levels : [];
	}

	getAvailableEffortSelectors(): ConfiguredThinkingLevel[] {
		const levels = this.getAvailableThinkingLevels();
		return levels.length > 0 ? ["auto", ...(levels as unknown as Effort[])] : ["auto"];
	}

	setThinkingLevel(level: ConfiguredThinkingLevel | undefined, _persist = false): void {
		if (this.#thinkingLevel === level) return;
		this.#thinkingLevel = level;
		this.#emit({
			type: "thinking_level_changed",
			thinkingLevel: level as unknown as ThinkingLevel | undefined,
			configured: level
		});
		const gateway = this.#gateway;
		if (gateway) {
			void gateway.setReasoning(level === "auto" ? undefined : (level as unknown as string)).catch(error => {
				this.emitNotice("warning", error instanceof Error ? error.message : String(error), "gateway");
			});
		}
	}

	cycleThinkingLevel(): ConfiguredThinkingLevel | undefined {
		const levels = this.getAvailableEffortSelectors();
		if (levels.length === 0) return undefined;
		const index = levels.indexOf(this.#thinkingLevel ?? "auto");
		const next = levels[(index + 1) % levels.length];
		this.setThinkingLevel(next);
		return next;
	}

	// ------------------------------------------------------------------
	// Session/message state
	// ------------------------------------------------------------------

	get sessionId(): string {
		return this.sessionManager.getSessionId();
	}

	get sessionFile(): string | undefined {
		return undefined;
	}

	get sessionName(): string | undefined {
		return this.#gateway?.title ?? this.sessionManager.getSessionName();
	}

	async setSessionName(name: string): Promise<boolean> {
		await this.#requireGateway().setTitle(name);
		this.sessionManager.setSessionName(name);
		return true;
	}

	get isStreaming(): boolean {
		return this.#gateway?.running === true;
	}

	get isCompacting(): boolean {
		return this.#gateway?.compacting === true;
	}

	get isAborting(): boolean {
		return false;
	}

	get isRetrying(): boolean {
		return false;
	}

	get retryAttempt(): number {
		return 0;
	}

	get isDisposed(): boolean {
		return this.#disposed;
	}

	get state(): AgentState {
		return this.agent.state;
	}

	get messages(): AgentMessage[] {
		return this.agent.state.messages;
	}

	getLastAssistantMessage(): AgentMessage | undefined {
		const messages = this.messages;
		for (let index = messages.length - 1; index >= 0; index -= 1) {
			if (messages[index]?.role === "assistant") return messages[index];
		}
		return undefined;
	}

	getLastAssistantText(): string | undefined {
		const message = this.getLastAssistantMessage();
		if (!message || message.role !== "assistant") return undefined;
		return message.content
			.filter(block => block.type === "text")
			.map(block => (block.type === "text" ? block.text : ""))
			.join("");
	}

	getTodoPhases(): TodoPhase[] {
		return gatewayTodoPhases(this.#gateway?.todos ?? []);
	}

	getContextUsage(options?: { contextWindow?: number }): ContextUsage | undefined {
		const usage = this.#gateway?.usage ?? this.#usage;
		const tokens = usage?.context_used ?? usage?.prompt ?? usage?.input;
		const window = options?.contextWindow ?? usage?.context_max ?? this.#gateway?.contextLength ?? this.#model?.contextWindow;
		if (typeof tokens !== "number" || !window) return undefined;
		return { tokens, contextWindow: window, percent: Math.min(100, (tokens / window) * 100) };
	}

	getSessionStats(): SessionStats {
		const stats = this.#gateway?.stats ?? this.#stats;
		const usage = this.#gateway?.usage ?? this.#usage;
		const messages = this.messages;
		return {
			sessionFile: undefined,
			sessionId: this.sessionId,
			userMessages: stats?.user_messages ?? messages.filter(message => message.role === "user").length,
			assistantMessages: stats?.assistant_messages ?? messages.filter(message => message.role === "assistant").length,
			toolCalls: stats?.tool_calls ?? 0,
			toolResults: stats?.tool_results ?? 0,
			totalMessages: stats?.total_messages ?? messages.length,
			tokens: {
				input: stats?.input_tokens ?? 0,
				output: stats?.output_tokens ?? 0,
				reasoning: stats?.reasoning_tokens ?? 0,
				cacheRead: stats?.cache_read_tokens ?? 0,
				cacheWrite: stats?.cache_write_tokens ?? 0,
				total: stats?.total_tokens ?? 0
			},
			premiumRequests: stats?.api_calls ?? 0,
			cost: stats?.cost_usd ?? usage?.cost_usd ?? 0,
			contextUsage: this.getContextUsage()
		};
	}

	get runStartedAt(): number | undefined {
		return this.#runStartedAt;
	}

	// ------------------------------------------------------------------
	// Tools (display only: the authority executes them)
	// ------------------------------------------------------------------

	getEnabledToolNames(): string[] {
		return [...this.#tools];
	}

	getAllToolNames(): string[] {
		return [...this.#tools];
	}

	/**
	 * ADR-10 D8.2a: the renderer projection for one wire tool, or `undefined` when the authority
	 * does not list it (the caller falls back to the wire name and the generic card — never guesses).
	 */
	getToolByName(name: string): GatewayToolProjection | undefined {
		if (!this.#tools.has(name)) return undefined;
		return { name: TOOL_RENDER_NAMES[name] ?? name, label: name };
	}

	hasBuiltInTool(name: string): boolean {
		return this.#tools.has(name);
	}

	// ------------------------------------------------------------------
	// Lifecycle
	// ------------------------------------------------------------------

	addDisposer(dispose: () => void): void {
		if (this.#disposed) {
			dispose();
			return;
		}
		this.#disposers.push(dispose);
	}

	/** Serialize a dialog against other dialogs, so one modal at a time reaches the terminal. */
	async waitForUser<T>(dialog: () => Promise<T>): Promise<T> {
		const run = this.#dialogChain.then(dialog, dialog);
		this.#dialogChain = run.catch(() => {});
		return run;
	}

	beginDispose(): void {
		this.#disposed = true;
	}

	async dispose(): Promise<void> {
		if (this.#disposed && this.#disposers.length === 0) return;
		this.#disposed = true;
		this.#gatewayUnsubscribe?.();
		this.#gatewayUnsubscribe = undefined;
		const disposers = this.#disposers.splice(0);
		for (const dispose of disposers) {
			try {
				await dispose();
			} catch (error) {
				this.emitNotice(
					"warning",
					`Session teardown step failed: ${error instanceof Error ? error.message : String(error)}`,
					"session"
				);
			}
		}
		this.#gateway = undefined;
	}
}
