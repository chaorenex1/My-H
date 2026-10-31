/**
 * The omp-ui main screen's session, driven by the Hermes gateway.
 *
 * This is the **business** half of the omp-ui ↔ Hermes alignment: one session (create or resume),
 * one prompt queue, one event stream. It owns no rendering and no omp types beyond the session-event
 * vocabulary the shell already consumes (`AgentSessionEvent`), so the shell keeps its own controllers
 * and `AgentSession` keeps its public class — only the data source changes.
 *
 * Protocol notes (`docs/appendix-omp-ui-business-rpc.md`, `docs/appendix-omp-ui-realtime-rpc.md`):
 * - requests are canonical Hermes JSON-RPC methods (`session.create`, `prompt.submit`, `session.steer`,
 *   `session.interrupt`, `session.history`, `session.usage`, `session.stats`, `session.mutate`);
 * - `gateway.ready` / `event` frames are unwrapped by {@link GatewayClient}; this module projects
 *   `message.*`, `tool.*`, `turn.*`, `prompt.result`, `session.settled`, `queue.updated`,
 *   `todo.updated`, `status.update` and `session.info` onto the shell's session events;
 * - writes carry `request_id` (idempotency) and `session.mutate` carries the snapshot revision +
 *   execution generation (CAS), as the canonical contract requires.
 */

import { randomUUID } from "node:crypto"

import type { AgentMessage } from "../../vendor/agent/src/index.ts"
import type { ThinkingLevel } from "../../vendor/agent/src/thinking.ts"
import type {
	AssistantMessage,
	ImageContent,
	StopReason,
	TextContent,
	ThinkingContent,
	ToolCall,
	ToolResultMessage,
	Usage
} from "../../vendor/ai/src/index.ts"
import type { AgentSessionEvent } from "../../session/agent-session-events"
import type { TodoPhase } from "@oh-my-pi/pi-tui/tools/todo"
import type {
	AdmissionStatus,
	CanonicalSessionInfo,
	CanonicalSnapshot,
	ModelOptionsResult,
	SessionHistoryResult,
	SessionListRow,
	SessionLiveInfo,
	SessionStats,
	SessionUsageResult,
	TodoState,
	TranscriptMessage
} from "./gateway-contract.generated"
import {
	GatewayClientError,
	type GatewayNotification,
	type GatewayServerRequest,
	type GatewayServerRequestHandler
} from "./gateway-client"
import { connectGateway, type GatewayConnection, type GatewayConnectOptions } from "./gateway-socket"

/** One session event plus the gateway metadata the shell does not need but a replay guard does. */
export type GatewaySessionEvent = AgentSessionEvent

/** The canonical snapshot after create/resume, in the shape the main screen reads. */
export interface GatewaySessionSnapshot {
	sessionId: string
	storedSessionId: string
	title?: string
	model?: string
	provider?: string
	cwd?: string
	contextLength?: number
	messages: TranscriptMessage[]
	pending: CanonicalSnapshot["pending"]
	revision: number
	executionGeneration: number
	authorityEpoch: number
	todos: unknown[]
	running: boolean
}

export interface GatewaySessionOptions {
	/** The paired connection; {@link connectGatewayOmpSession} builds one. */
	connection: GatewayConnection
	/** Create a fresh session (default) or resume the given stored/runtime id. */
	resumeSessionId?: string
	cwd?: string
	title?: string
	/** Reports protocol/transport failures that are not part of the event stream. */
	onError?: (error: Error) => void
	/** Ask/approval routing; returning `false` leaves the request unanswered (-32601). */
	onServerRequest?: GatewayServerRequestHandler
}

const now = (): number => Date.now()

const asRecord = (value: unknown): Record<string, unknown> | undefined =>
	typeof value === "object" && value !== null && !Array.isArray(value)
		? (value as Record<string, unknown>)
		: undefined

const asString = (value: unknown): string | undefined => (typeof value === "string" ? value : undefined)

const asNumber = (value: unknown): number | undefined =>
	typeof value === "number" && Number.isFinite(value) ? value : undefined

const zeroUsage = (): Usage => ({
	input: 0,
	output: 0,
	cacheRead: 0,
	cacheWrite: 0,
	totalTokens: 0,
	cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 }
})

/** Hermes gateway `Usage` (snake_case, optional) → the vendored `Usage` the shell renders. */
export function toOmpUsage(usage: SessionUsageResult | SessionLiveInfo["usage"] | null | undefined): Usage | undefined {
	if (!usage) return undefined
	const value = usage as Record<string, unknown>
	const cost = asNumber(value.cost_usd) ?? 0
	const input = asNumber(value.input) ?? asNumber(value.prompt) ?? 0
	const output = asNumber(value.output) ?? asNumber(value.completion) ?? 0
	const cacheRead = asNumber(value.cache_read) ?? 0
	const cacheWrite = asNumber(value.cache_write) ?? 0
	const totalTokens = asNumber(value.total) ?? input + output + cacheRead + cacheWrite
	const result: Usage = {
		input,
		output,
		cacheRead,
		cacheWrite,
		totalTokens,
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: cost }
	}
	const reasoning = asNumber(value.reasoning)
	if (reasoning !== undefined) result.reasoningTokens = reasoning
	const contextTokens = asNumber(value.context_used)
	if (contextTokens !== undefined) result.contextTokens = contextTokens
	return result
}

/** One assistant message under construction from the half-open `message.*` stream. */
interface AssistantDraft {
	messageId: string
	message: AssistantMessage
	/** Text/thinking accumulated per `content_index`. */
	text: Map<number, string>
	thinking: Map<number, string>
	lastIndex: number
	/** At least one `message.delta` was appended (the final payload text must not overwrite it). */
	sawDelta: boolean
}

const textContent = (text: string): TextContent => ({ type: "text", text })

const assistantDraft = (model: string, provider: string, messageId: string): AssistantDraft => ({
	messageId,
	message: {
		role: "assistant",
		content: [],
		api: "openai-completions",
		provider,
		model,
		usage: zeroUsage(),
		stopReason: "stop",
		timestamp: now()
	},
	text: new Map(),
	thinking: new Map(),
	lastIndex: 0,
	sawDelta: false
})

/** Fill `content` from the per-index maps, preserving index order and block kinds. */
const materialize = (draft: AssistantDraft): AssistantMessage => {
	const indices = [...new Set([...draft.thinking.keys(), ...draft.text.keys()])].sort((a, b) => a - b)
	const content: AssistantMessage["content"] = []
	for (const index of indices) {
		// A reasoning stream and the reply text can share the gateway's `content_index` (reasoning
		// frames carry none, text deltas do): keep both blocks instead of letting thinking hide text.
		const thinking = draft.thinking.get(index)
		if (thinking !== undefined) content.push({ type: "thinking", thinking } as ThinkingContent)
		const text = draft.text.get(index)
		if (text !== undefined) content.push(textContent(text))
	}
	draft.message.content = content
	return draft.message
}

/**
 * Hermes `tool.complete.result` is a bounded JSON *string*; the shell's tool cards consume
 * `AgentToolResult` (`content` blocks + `details`). Decode whichever shape arrived and always answer
 * with the structured form, so `result.content.filter(...)` and `updateResult({...result})` cannot
 * see a string.
 */
function projectToolResult(payload: Record<string, unknown>): {
	content: (TextContent | ImageContent)[]
	details?: Record<string, unknown>
	isError: boolean
} {
	const isError = payload.is_error === true
	const raw = payload.result
	let decoded: unknown = raw
	if (typeof raw === "string") {
		try {
			decoded = JSON.parse(raw)
		} catch {
			decoded = undefined
		}
	}
	const record = asRecord(decoded)
	const content = Array.isArray(record?.content)
		? (record.content as (TextContent | ImageContent)[])
		: undefined
	const text =
		asString(payload.result_text) ??
		(record && typeof record.text === "string" ? record.text : undefined) ??
		(typeof decoded === "string" ? decoded : undefined) ??
		(typeof raw === "string" ? raw : "") ??
		""
	const details: Record<string, unknown> = {}
	if (asString(payload.summary)) details.summary = payload.summary
	if (asNumber(payload.duration_s) !== undefined) details.duration_s = payload.duration_s
	if (asString(payload.inline_diff)) details.inline_diff = payload.inline_diff
	if (payload.labels) details.labels = payload.labels
	if (payload.todos) details.todos = payload.todos
	if (asNumber(payload.revision) !== undefined) details.revision = payload.revision
	// The todo tool's card and HUD read `details.phases` (+ `op`), not the raw rows.
	if (Array.isArray(payload.todos) && asString(payload.name) === "todo") {
		details.op = "init"
		details.phases = gatewayTodoPhases(payload.todos)
	}
	return {
		content: content?.length ? content : [textContent(text)],
		...(Object.keys(details).length ? { details } : {}),
		isError
	}
}

/** Hermes todo rows → the shell's phase/task projection (shared with the session bridge). */
export function gatewayTodoPhases(todos: readonly unknown[]): TodoPhase[] {
	const phases = new Map<string, TodoPhase["tasks"]>()
	for (const raw of todos) {
		if (!raw || typeof raw !== "object") continue
		const row = raw as Record<string, unknown>
		const content =
			typeof row.content === "string"
				? row.content
				: typeof row.text === "string"
					? row.text
					: typeof row.title === "string"
						? row.title
						: undefined
		if (!content) continue
		const rawStatus =
			typeof row.status === "string" ? row.status.toLowerCase().replace(/[\s-]/g, "_") : "pending"
		const status: TodoPhase["tasks"][number]["status"] =
			rawStatus === "done" || rawStatus === "complete" || rawStatus === "completed"
				? "completed"
				: rawStatus === "in_progress" || rawStatus === "active"
					? "in_progress"
					: rawStatus === "cancelled" || rawStatus === "abandoned" || rawStatus === "dropped"
						? "abandoned"
						: rawStatus === "blocked"
							? "blocked"
							: "pending"
		const phase = typeof row.phase === "string" && row.phase ? row.phase : ""
		const tasks = phases.get(phase) ?? []
		tasks.push({
			content,
			status,
			...(typeof row.blocker === "string" ? { blocker: row.blocker } : {})
		})
		phases.set(phase, tasks)
	}
	return [...phases.entries()].map(([name, tasks]) => ({ name: name || "Tasks", tasks }))
}

/** Concatenated visible text of an assistant message (empty for other roles). */
const assistantText = (message: AgentMessage): string =>
	message.role === "assistant"
		? message.content
				.filter((block): block is TextContent => block.type === "text")
				.map((block) => block.text)
				.join("")
		: ""

const stopReasonFor = (status: string | null | undefined, raw: string | null | undefined): StopReason => {
	const value = (status ?? raw ?? "").toLowerCase()
	if (value === "error") return "error"
	if (value === "interrupted" || value === "aborted" || value === "cancelled") return "aborted"
	if (value === "length") return "length"
	if (value === "tooluse" || value === "tool_use") return "toolUse"
	return "stop"
}

/**
 * One gateway-backed omp session. The shell subscribes once ({@link subscribe}) and every projection
 * is delivered as an ordinary `AgentSessionEvent`; the controller layer is unchanged.
 */
export class GatewayOmpSession {
	readonly client: GatewayConnection["client"]
	readonly connection: GatewayConnection
	readonly profileId: string

	#state: GatewaySessionSnapshot
	#listeners = new Set<(event: AgentSessionEvent) => void>()
	#draft: AssistantDraft | undefined
	#messages: AgentMessage[] = []
	#running = false
	#compacting = false
	#lastAdmissionId: string | undefined
	#queue = { steering: [] as string[], followUp: [] as string[], liveSteered: 0 }
	#usage: SessionUsageResult | undefined
	#stats: SessionStats | undefined
	#todos: unknown[] = []
	#closed = false
	#onError?: (error: Error) => void
	#onServerRequest?: GatewayServerRequestHandler

	constructor(options: GatewaySessionOptions, snapshot: GatewaySessionSnapshot) {
		this.connection = options.connection
		this.client = options.connection.client
		this.profileId = options.connection.grant.profile_id
		this.#state = snapshot
		this.#messages = snapshot.messages.map(GatewayOmpSession.rowToAgentMessage).filter((m): m is AgentMessage => m !== undefined)
		this.#todos = snapshot.todos
		this.#running = snapshot.running
		this.#onError = options.onError
		this.#onServerRequest = options.onServerRequest
	}

	/** Project a persisted transcript row onto the shell's `AgentMessage` vocabulary. */
	static rowToAgentMessage(row: TranscriptMessage): AgentMessage | undefined {
		const role = row.role
		const timestamp = asNumber(row.timestamp) ?? now()
		const text = asString(row.text) ?? ""
		if (role === "user") return { role: "user", content: text, timestamp }
		if (role === "assistant") {
			return {
				role: "assistant",
				content: text ? [textContent(text)] : [],
				api: "openai-completions",
				provider: "gateway",
				model: asString(row["model"]) ?? "",
				usage: zeroUsage(),
				stopReason: "stop",
				timestamp
			}
		}
		if (role === "tool" || role === "toolResult") {
			return {
				role: "toolResult",
				toolCallId: asString(row.tool_call_id) ?? "",
				toolName: asString(row.name) ?? "",
				content: text ? [textContent(text)] : [],
				isError: row["is_error"] === true,
				timestamp
			}
		}
		return undefined
	}

	get sessionId(): string {
		return this.#state.sessionId
	}
	get storedSessionId(): string {
		return this.#state.storedSessionId
	}
	get running(): boolean {
		return this.#running
	}
	/** A compaction pass is running (`status.update` kind `compacting`). */
	get compacting(): boolean {
		return this.#compacting
	}
	get model(): string | undefined {
		return this.#state.model
	}
	get provider(): string | undefined {
		return this.#state.provider
	}
	get cwd(): string | undefined {
		return this.#state.cwd
	}
	/** The served model's context window, when the owner reported one. */
	get contextLength(): number | undefined {
		return this.#state.contextLength
	}
	get title(): string | undefined {
		return this.#state.title
	}
	get revision(): number {
		return this.#state.revision
	}
	get executionGeneration(): number {
		return this.#state.executionGeneration
	}
	get todos(): readonly unknown[] {
		return this.#todos
	}
	get usage(): SessionUsageResult | undefined {
		return this.#usage
	}
	get stats(): SessionStats | undefined {
		return this.#stats
	}
	get queue(): { steering: string[]; followUp: string[]; liveSteered: number } {
		return this.#queue
	}
	get messages(): readonly AgentMessage[] {
		return this.#messages
	}

	/** Subscribe to the projected session events; returns the unsubscribe. */
	subscribe(listener: (event: AgentSessionEvent) => void): () => void {
		this.#listeners.add(listener)
		return () => this.#listeners.delete(listener)
	}

	#emit(event: AgentSessionEvent): void {
		for (const listener of [...this.#listeners]) {
			try {
				listener(event)
			} catch (error) {
				this.#onError?.(error instanceof Error ? error : new Error(String(error)))
			}
		}
	}

	/** Replace the session state with a fresh canonical snapshot (resume/refresh). */
	adoptSnapshot(snapshot: GatewaySessionSnapshot): void {
		this.#state = snapshot
		this.#messages = snapshot.messages.map(GatewayOmpSession.rowToAgentMessage).filter((m): m is AgentMessage => m !== undefined)
		this.#todos = snapshot.todos
		this.#running = snapshot.running
	}

	/** Turn the canonical create/resume result into the snapshot the shell reads. */
	static snapshotFrom(value: CanonicalSnapshot): GatewaySessionSnapshot {
		const info = value.info ?? ({} as CanonicalSnapshot["info"])
		const rows = value.display_messages ?? value.messages ?? []
		return {
			sessionId: value.session_id,
			storedSessionId: value.stored_session_id,
			title: info.title ?? undefined,
			model: info.model ?? undefined,
			provider: info.provider ?? undefined,
			cwd: info.cwd ?? undefined,
			contextLength: typeof info.context_length === "number" ? info.context_length : undefined,
			messages: rows,
			pending: value.pending ?? [],
			revision: value.revision,
			executionGeneration: value.execution_generation,
			authorityEpoch: value.authority_epoch,
			todos: value.todo_state?.todos ?? [],
			running: value.running === true
		}
	}

	/** The message list the transcript renders on boot/resume. */
	initialMessages(): AgentMessage[] {
		return [...this.#messages]
	}

	// ---------------------------------------------------------------------
	// Business RPC
	// ---------------------------------------------------------------------

	/** Submit one prompt. `queued` rides the FIFO instead of interrupting an idle session. */
	async submit(
		text: string,
		options: {
			queued?: boolean
			images?: ImageContent[]
			displayKind?: "hidden"
			submissionId?: string
		} = {}
	): Promise<AdmissionStatus> {
		const attachments = options.images?.length ? await this.#attachImages(options.images) : undefined
		const submissionId = options.submissionId ?? randomUUID()
		const receipt = await this.client.request("prompt.submit", {
			session_id: this.#state.sessionId,
			text,
			submission_id: submissionId,
			...(options.queued ? { queued: true } : {}),
			...(attachments?.length ? { attachments } : {}),
			...(options.displayKind ? { display_kind: options.displayKind } : {})
		})
		if (isVoiceStopped(receipt)) return receipt as unknown as AdmissionStatus
		const admission = receipt as AdmissionStatus
		const wasRunning = this.#running
		this.#lastAdmissionId = admission.admission_id
		// The receipt may already name the turn's authority/generation; keep the CAS fields current
		// before an Esc arrives, so `session.interrupt` does not have to recover from a stale quote.
		if (typeof admission.authority_epoch === "number") this.#state.authorityEpoch = admission.authority_epoch
		if (typeof admission.execution_generation === "number") {
			this.#state.executionGeneration = admission.execution_generation
		}
		this.#emitUserMessage(text, options.images)
		// A queued follow-up rides the running turn: no second `agent_start` (which would reset the
		// shell's turn state) — `queue.updated` owns its pending pill.
		if (!wasRunning) {
			this.#running = true
			this.#emit({ type: "agent_start" })
		}
		return admission
	}

	/** Steer the running turn (injected at the next tool/model boundary). */
	async steer(text: string): Promise<void> {
		await this.client.request("session.steer", {
			session_id: this.#state.sessionId,
			execution_generation: this.#state.executionGeneration,
			text
		})
	}

	/**
	 * Ask the owner to stop the running turn.
	 *
	 * Preferred path: `prompt.cancel` on the admission this viewer submitted. The authority latches a
	 * started admission's cancellation and needs no CAS fields, so this cannot race a generation. A
	 * turn started by another viewer has no admission here: fall back to `session.interrupt`, reading
	 * the replay rows once if the quoted generation went stale.
	 */
	async interrupt(): Promise<void> {
		const admissionId = this.#lastAdmissionId
		if (admissionId) {
			try {
				await this.client.request("prompt.cancel", {
					session_id: this.#state.sessionId,
					admission_id: admissionId
				})
				return
			} catch (error) {
				const reason = asString((error as { reason?: unknown } | null)?.reason)
				// Already terminal: the turn this caller wanted to stop is over.
				if (reason === "not_found") return
				// Any other refusal falls through to the generation-fenced control verb.
			}
		}
		const send = () =>
			this.client.request("session.interrupt", {
				session_id: this.#state.sessionId,
				execution_generation: this.#state.executionGeneration
			})
		try {
			await send()
		} catch (error) {
			if (!isGenerationRefusal(error)) throw error
			// The live fanout may not have reached us yet (a turn that began between two frames):
			// read the replay rows, adopt the newest generation, and let the retry quote it.
			await this.adoptLatestGeneration()
			await send()
		}
	}

	/** Adopt the newest `execution_generation` the replay rows know, for CAS retries. */
	async adoptLatestGeneration(): Promise<void> {
		const replay = (await this.client.request("session.events.since", {
			session_id: this.#state.sessionId,
			last_sequence: 0
		})) as { events?: { execution_generation?: number | null }[] | null }
		let latest = this.#state.executionGeneration
		for (const row of replay.events ?? []) {
			const generation = asNumber(row.execution_generation)
			if (generation !== undefined && generation > latest) latest = generation
		}
		this.#state.executionGeneration = latest
	}

	/** Drop one queued admission (`/queue` removal). */
	async cancelQueued(admissionId: string): Promise<void> {
		await this.client.request("prompt.cancel", { session_id: this.#state.sessionId, admission_id: admissionId })
	}

	/** Dispatch a Hermes slash command through the gateway's command catalog. */
	async dispatchCommand(text: string): Promise<Record<string, unknown>> {
		const trimmed = text.trim()
		const space = trimmed.search(/\s/)
		const name = (space === -1 ? trimmed : trimmed.slice(0, space)).replace(/^\//, "")
		const arg = space === -1 ? undefined : trimmed.slice(space + 1).trim()
		return (await this.client.request("command.dispatch", {
			name,
			arg: arg || undefined,
			session_id: this.#state.sessionId
		})) as unknown as Record<string, unknown>
	}

	/** Read the durable transcript (the gateway's projection, newest window). */
	async history(limit = 200): Promise<SessionHistoryResult> {
		return this.client.request("session.history", { session_id: this.#state.sessionId, limit })
	}

	/** Refresh info, stats and usage; called at boot and after settle. */
	async refresh(): Promise<void> {
		const [info, stats, usage] = await Promise.allSettled([
			this.client.request("session.info", { session_id: this.#state.sessionId }),
			this.client.request("session.stats", { session_id: this.#state.sessionId }),
			this.client.request("session.usage", { session_id: this.#state.sessionId })
		])
		if (info.status === "fulfilled") this.#adoptInfo(info.value as CanonicalSessionInfo | SessionLiveInfo)
		if (stats.status === "fulfilled") this.#stats = stats.value as SessionStats
		if (usage.status === "fulfilled") this.#usage = usage.value as SessionUsageResult
	}

	/**
	 * The authority's provider/model inventory: the display catalog the shell's pickers render.
	 *
	 * `include_unconfigured` also lists providers the authority knows but has no credential for;
	 * their model rows stay empty, so the catalog only gains an affordance to sign in.
	 */
	async modelOptions(params: { include_unconfigured?: boolean; refresh?: boolean } = {}): Promise<ModelOptionsResult> {
		return this.client.request("model.options", {
			session_id: this.#state.sessionId,
			...(params.include_unconfigured ? { include_unconfigured: true } : {}),
			...(params.refresh ? { refresh: true } : {})
		})
	}

	/**
	 * The authority's resume list (`session.list`): the rows `/resume` renders.
	 *
	 * Rows are the profile's durable sessions; `id` is the stored session id (what `session.resume`
	 * accepts). The shell keeps no local session table, so this is the only source for the picker.
	 */
	async listSessions(limit = 100): Promise<SessionListRow[]> {
		const result = (await this.client.request("session.list", { limit })) as { sessions?: SessionListRow[] | null }
		return result.sessions ?? []
	}

	/** Set the session model (`session.mutate model`), with the CAS fields the authority requires. */
	async setModel(model: { id: string; provider: string }): Promise<void> {
		const result = await this.client.request("session.mutate", {
			session_id: this.#state.sessionId,
			request_id: randomUUID(),
			expected_revision: this.#state.revision,
			expected_generation: this.#state.executionGeneration,
			operation: "model",
			payload: { model: model.id, provider: model.provider }
		})
		this.#adoptMutation(result as { revision?: number })
		this.#state.model = model.id
		this.#state.provider = model.provider
		this.#emit({ type: "model_changed" })
	}

	/** Set the session reasoning effort (`session.mutate reasoning`). */
	async setReasoning(effort: string | undefined): Promise<void> {
		const result = await this.client.request("session.mutate", {
			session_id: this.#state.sessionId,
			request_id: randomUUID(),
			expected_revision: this.#state.revision,
			expected_generation: this.#state.executionGeneration,
			operation: "reasoning",
			payload: { effort: effort ?? null }
		})
		this.#adoptMutation(result as { revision?: number })
		const level = ["off", "minimal", "low", "medium", "high", "xhigh", "max"].includes(effort ?? "")
			? (effort as ThinkingLevel)
			: undefined
		this.#emit({ type: "thinking_level_changed", thinkingLevel: level, configured: level })
	}

	async setTitle(title: string): Promise<void> {
		await this.client.request("session.title", { session_id: this.#state.sessionId, title })
		this.#state.title = title
	}

	async #attachImages(images: ImageContent[]): Promise<{ path: string; mime: string }[]> {
		const attached: { path: string; mime: string }[] = []
		for (const image of images) {
			const mime = image.mimeType ?? "image/png"
			const ext = mime.split("/")[1] ?? "png"
			const result = await this.client.request("image.attach_bytes", {
				session_id: this.#state.sessionId,
				content_base64: image.data,
				filename: `omp-ui.${ext}`,
				ext
			})
			const path = asString((result as { path?: unknown }).path)
			attached.push({ path: path ?? `omp-ui.${ext}`, mime })
		}
		return attached
	}

	#adoptMutation(result: { revision?: number | null; execution_generation?: number | null }): void {
		if (typeof result.revision === "number") this.#state.revision = result.revision
		if (typeof result.execution_generation === "number") {
			this.#state.executionGeneration = result.execution_generation
		}
	}

	#adoptInfo(info: CanonicalSessionInfo | SessionLiveInfo): void {
		if (info.model) this.#state.model = info.model
		if (info.provider) this.#state.provider = info.provider
		if (info.title) this.#state.title = info.title
		if (info.cwd) this.#state.cwd = info.cwd
		if (typeof info.context_length === "number") this.#state.contextLength = info.context_length
		if (typeof info.running === "boolean") this.#running = info.running
		// The live fanout carries the CAS generation/revision the next `session.mutate` and
		// `session.interrupt` must quote; the created snapshot's values go stale with the first turn.
		const live = info as Record<string, unknown>
		const generation = asNumber(live.execution_generation)
		if (generation !== undefined) this.#state.executionGeneration = generation
		const revision = asNumber(live.revision)
		if (revision !== undefined) this.#state.revision = revision
	}

	#emitUserMessage(text: string, images?: ImageContent[]): void {
		const content: string | (TextContent | ImageContent)[] = images?.length
			? [textContent(text), ...images]
			: text
		const message: AgentMessage = { role: "user", content, timestamp: now() }
		this.#messages.push(message)
		this.#emit({ type: "message_start", message })
		this.#emit({ type: "message_end", message })
	}

	// ---------------------------------------------------------------------
	// Realtime RPC projection
	// ---------------------------------------------------------------------

	/** Route one decoded notification; public for the replay drain in {@link connectGatewayOmpSession}. */
	handleNotification(notification: GatewayNotification): void {
		if (this.#closed) return
		const sessionId = notification.sessionId
		if (sessionId !== undefined && this.#state.sessionId && sessionId !== this.#state.sessionId) return
		const payload = notification.payload as Record<string, unknown>
		switch (notification.type) {
			case "gateway.ready":
				return
			case "session.info": {
				this.#adoptInfo(payload as unknown as SessionLiveInfo)
				return
			}
			case "session.title": {
				const title = asString(payload.title)
				if (title) this.#state.title = title
				return
			}
			case "turn.start": {
				this.#running = true
				this.#emit({ type: "turn_start" })
				return
			}
			case "turn.end": {
				this.#compacting = false
				const message = asRecord(payload.message)
				this.#emit({
					type: "turn_end",
					message: message ? (message as unknown as AgentMessage) : this.#lastAssistantMessage(),
					toolResults: []
				})
				return
			}
			case "message.start": {
				this.#openDraft(asString(payload.message_id) ?? `${this.#state.sessionId}:${now()}`)
				return
			}
			case "message.block_start": {
				this.#ensureDraft()
				const index = asNumber(payload.content_index)
				if (index !== undefined) this.#draft!.lastIndex = index
				return
			}
			case "message.delta": {
				const draft = this.#ensureDraft()
				const text = payload.text
				if (typeof text !== "string" || text === "") {
					// A null/empty chunk is the reply segment boundary: close the current text block.
					if (payload.text === null) this.#closeTextBlock(draft)
					return
				}
				const index = asNumber(payload.content_index) ?? draft.lastIndex
				draft.lastIndex = index
				draft.text.set(index, (draft.text.get(index) ?? "") + text)
				draft.sawDelta = true
				const partial = materialize(draft)
				this.#emit({
					type: "message_update",
					message: partial,
					assistantMessageEvent: { type: "text_delta", contentIndex: index, delta: text, partial }
				})
				return
			}
			case "message.interim": {
				const draft = this.#ensureDraft()
				const text = asString(payload.text)
				if (text) {
					const index = draft.lastIndex
					draft.text.set(index, text)
					const partial = materialize(draft)
					this.#emit({
						type: "message_update",
						message: partial,
						assistantMessageEvent: { type: "text_end", contentIndex: index, content: text, partial }
					})
				}
				return
			}
			case "reasoning.delta":
			case "thinking.delta": {
				const draft = this.#ensureDraft()
				const text = asString(payload.text)
				if (!text) return
				const index = draft.thinking.size === 0 ? draft.lastIndex : [...draft.thinking.keys()].at(-1)!
				draft.thinking.set(index, (draft.thinking.get(index) ?? "") + text)
				draft.sawDelta = true
				const partial = materialize(draft)
				this.#emit({
					type: "message_update",
					message: partial,
					assistantMessageEvent: { type: "thinking_delta", contentIndex: index, delta: text, partial }
				})
				return
			}
			case "message.complete": {
				this.#complete(payload)
				return
			}
			case "tool.start": {
				// Hermes streams one assistant message across tool rounds: text before the tool and the
				// answer after it would share one block, leaving the tool cards below the answer. Close
				// the pre-tool segment here; the next delta opens the post-tool block under the cards.
				this.#closeSegmentForTool()
				const toolId = asString(payload.tool_id) ?? ""
				const name = asString(payload.name) ?? ""
				this.#emit({
					type: "tool_execution_start",
					toolCallId: toolId,
					toolName: name,
					args: payload.args ?? {},
					intent: asString(payload.context)
				})
				return
			}
			case "tool.progress": {
				const message = asString(payload.message)
				this.#emit({
					type: "tool_execution_update",
					toolCallId: asString(payload.tool_id) ?? "",
					toolName: asString(payload.name) ?? "",
					args: {},
					partialResult: {
						content: message ? [textContent(message)] : [],
						details: {
							progress: asNumber(payload.progress) ?? 0,
							total: asNumber(payload.total),
							percent: asNumber(payload.percent),
							message
						}
					}
				})
				return
			}
			case "tool.complete": {
				this.#emit({
					type: "tool_execution_end",
					toolCallId: asString(payload.tool_id) ?? "",
					toolName: asString(payload.name) ?? "",
					result: projectToolResult(payload),
					isError: payload.is_error === true
				})
				return
			}
			case "todo.updated": {
				const todos = Array.isArray(payload.todos) ? payload.todos : []
				this.#todos = todos
				return
			}
			case "queue.updated": {
				this.#queue = {
					steering: Array.isArray(payload.steering) ? payload.steering.filter((v): v is string => typeof v === "string") : [],
					followUp: Array.isArray(payload.follow_up)
						? payload.follow_up.filter((v): v is string => typeof v === "string")
						: [],
					liveSteered: asNumber(payload.live_steered) ?? 0
				}
				this.#emit({ type: "queue_update", ...this.#queue })
				return
			}
			case "prompt.result": {
				this.#running = false
				this.#lastAdmissionId = undefined
				const status = asString(payload.status)
				const error = asRecord(payload.error)
				if ((status === "error" || error) && payload.error !== null && payload.error !== undefined) {
					// A failed turn otherwise leaves only a partial (or empty) assistant block; name it.
					this.#emit({
						type: "notice",
						level: "error",
						message: asString(error?.message) ?? "Turn failed",
						source: "gateway"
					})
				}
				this.#emit({ type: "agent_end", messages: [...this.#messages], isTerminal: true, yielded: true })
				// Stats/usage lag the terminal frame: refetch right after so the status line's
				// cost/context segments paint the turn that just ended, then repaint.
				void this.refresh()
					.then(() => {
						if (!this.#closed) this.#emit({ type: "model_changed" })
					})
					.catch((error: unknown) => this.#report(error))
				return
			}
			case "session.settled": {
				if (this.#running) {
					this.#running = false
					this.#emit({ type: "agent_end", messages: [...this.#messages], isTerminal: true, yielded: true })
				}
				return
			}
			case "session.usage": {
				this.#usage = payload as unknown as SessionUsageResult
				return
			}
			case "status.update": {
				// `status.update` is the TUI-facing projection of the same line `agent.print` carries;
				// rendering both would duplicate every lifecycle notice.
				const kind = (asString(payload.kind) ?? "").toLowerCase()
				if (kind.includes("compact")) this.#compacting = !kind.includes("done") && !kind.includes("end")
				const text = asString(payload.text) ?? asString(payload.message)
				if (text) this.#emit({ type: "notice", level: "info", message: text, source: "gateway" })
				return
			}
			case "agent.print": {
				return
			}
			case "notice": {
				const text = asString(payload.message)
				if (text) this.#emit({ type: "notice", level: "info", message: text, source: "gateway" })
				return
			}
			case "error": {
				const text = asString(payload.message) ?? "Gateway error"
				this.#emit({ type: "notice", level: "error", message: text, source: "gateway" })
				return
			}
			default:
				return
		}
	}

	/** Answer a server→client request; the caller's handler gets first refusal. */
	handleServerRequest(request: GatewayServerRequest): boolean {
		if (this.#onServerRequest?.(request) === true) return true
		// No UI route yet: answer safely instead of leaving the backend blocked. Approvals deny,
		// clarify cancels, and either is announced so the turn cannot stall silently.
		switch (request.method) {
			case "approval": {
				this.client.respond(request.id, { choice: "deny" } as never)
				this.#emit({
					type: "notice",
					level: "warning",
					message: `Approval denied (no approval UI yet): ${asString(request.params.description) ?? asString(request.params.command) ?? "command"}`,
					source: "gateway"
				})
				return true
			}
			case "clarify": {
				this.client.respond(request.id, { outcome: "cancelled" } as never)
				this.#emit({ type: "notice", level: "warning", message: "Clarify cancelled (no clarify UI yet)", source: "gateway" })
				return true
			}
			default:
				return false
		}
	}

	// ---------------------------------------------------------------------
	// Assistant message assembly
	// ---------------------------------------------------------------------

	#openDraft(messageId: string): AssistantDraft {
		const draft = assistantDraft(this.#state.model ?? "", this.#state.provider ?? "", messageId)
		this.#draft = draft
		const message = materialize(draft)
		this.#emit({ type: "message_start", message })
		this.#emit({ type: "message_update", message, assistantMessageEvent: { type: "start", partial: message } })
		return draft
	}

	#ensureDraft(): AssistantDraft {
		return this.#draft ?? this.#openDraft(`${this.#state.sessionId}:${now()}`)
	}

	#closeTextBlock(draft: AssistantDraft): void {
		const index = draft.lastIndex
		const content = draft.text.get(index)
		if (content === undefined) return
		const partial = materialize(draft)
		this.#emit({
			type: "message_update",
			message: partial,
			assistantMessageEvent: { type: "text_end", contentIndex: index, content, partial }
		})
		draft.lastIndex = index + 1
	}

	#complete(payload: Record<string, unknown>): void {
		const usage = toOmpUsage(payload.usage as never)
		const status = stopReasonFor(asString(payload.status), asString(payload.stop_reason))
		const finalText = typeof payload.text === "string" ? payload.text : undefined
		if (!this.#draft) {
			// The pre-tool segment was closed at `tool.start`; a complete-only answer (no deltas after
			// the tool) still has to be rendered, but only its suffix that was not shown yet.
			const shown = assistantText(this.#lastAssistantMessage())
			const suffix =
				finalText === undefined ? undefined : shown && finalText.startsWith(shown) ? finalText.slice(shown.length) : finalText
			if (!suffix) {
				this.#mergeLastAssistant(usage, status)
				return
			}
			const draft = this.#openDraft(asString(payload.message_id) ?? `${this.#state.sessionId}:${now()}`)
			draft.text.set(0, suffix)
		}
		const draft = this.#draft!
		this.#draft = undefined
		if (finalText !== undefined) {
			const indices = [...draft.text.keys()]
			if (indices.length === 0) {
				draft.text.set(0, finalText)
			} else {
				const last = Math.max(...indices)
				const accumulated = draft.text.get(last) ?? ""
				// A complete that extends the streamed prefix is authoritative; one that does not
				// start with it belongs to the pre-tool segment and must not overwrite this block.
				if (finalText.startsWith(accumulated) && finalText.length > accumulated.length) {
					draft.text.set(last, finalText)
				}
			}
		}
		if (usage) draft.message.usage = usage
		draft.message.stopReason = status
		const error = asString(payload.error)
		if (error) draft.message.errorMessage = error
		const message = materialize(draft)
		const assistantEvent: AssistantMessageEventDone | AssistantMessageEventError =
			message.stopReason === "error"
				? { type: "error", reason: "error", error: message }
				: {
						type: "done",
						reason: message.stopReason === "aborted" ? "stop" : (message.stopReason as "stop" | "length" | "toolUse"),
						message
					}
		this.#emit({ type: "message_update", message, assistantMessageEvent: assistantEvent })
		this.#emit({ type: "message_end", message })
		this.#messages.push(message)
	}

	/** Close the in-flight assistant segment when a tool starts, so later text opens a new block. */
	#closeSegmentForTool(): void {
		const draft = this.#draft
		if (!draft) return
		this.#draft = undefined
		const hasContent = draft.text.size > 0 || draft.thinking.size > 0
		if (!hasContent) return
		draft.message.stopReason = "toolUse"
		const message = materialize(draft)
		this.#emit({ type: "message_update", message, assistantMessageEvent: { type: "done", reason: "toolUse", message } })
		this.#emit({ type: "message_end", message })
		this.#messages.push(message)
	}

	/** Fold a late complete's usage/status into the last assistant block (no duplicate render). */
	#mergeLastAssistant(usage: Usage | undefined, status: StopReason): void {
		for (let index = this.#messages.length - 1; index >= 0; index -= 1) {
			const message = this.#messages[index]
			if (message?.role !== "assistant") continue
			if (usage) message.usage = usage
			message.stopReason = status
			return
		}
	}

	#lastAssistantMessage(): AgentMessage {
		for (let index = this.#messages.length - 1; index >= 0; index -= 1) {
			if (this.#messages[index]?.role === "assistant") return this.#messages[index]!
		}
		return materialize(assistantDraft(this.#state.model ?? "", this.#state.provider ?? "", ""))
	}

	#report(error: unknown): void {
		this.#onError?.(error instanceof Error ? error : new Error(String(error)))
	}

	/** Stop the session and close the socket. */
	close(reason = "omp-ui closed the gateway session"): void {
		if (this.#closed) return
		this.#closed = true
		this.#listeners.clear()
		try {
			this.connection.close(reason)
		} catch (error) {
			this.#report(error)
		}
	}
}

type AssistantMessageEventDone = { type: "done"; reason: "stop" | "length" | "toolUse"; message: AssistantMessage }
type AssistantMessageEventError = { type: "error"; reason: "error"; error: AssistantMessage }

/** The receipt union is `AdmissionStatus | {voice_stopped:true}`; the voice member has no admission. */
const isVoiceStopped = (value: unknown): boolean => asRecord(value)?.voice_stopped === true

/** The authority refused a write because the caller quoted an older execution generation. */
export const isGenerationRefusal = (error: unknown): boolean =>
	asString((error as { reason?: unknown } | null)?.reason) === "stale_generation"

export interface ConnectGatewayOmpSessionOptions extends Omit<GatewaySessionOptions, "connection"> {
	/** Override the transport (tests pass a fake connection). */
	connect?: GatewayConnectOptions
}

/**
 * Pair with the gateway, create or resume one session, and attach the event stream.
 *
 * Frames that arrive while the instance is being built (the backend writes `gateway.ready` right
 * after accept) are queued and replayed, so readiness and the first `session.info` cannot race.
 */
export async function connectGatewayOmpSession(options: ConnectGatewayOmpSessionOptions): Promise<GatewayOmpSession> {
	const early: GatewayNotification[] = []
	let instance: GatewayOmpSession | undefined
	const connection = await connectGateway({
		...(options.connect ?? {}),
		onEvent: (notification) => {
			if (instance) instance.handleNotification(notification)
			else early.push(notification)
		},
		onServerRequest: (request) => {
			if (!instance) return false
			return instance.handleServerRequest(request)
		},
		onError: (error) => {
			options.onError?.(error)
			options.connect?.onError?.(error)
		},
		onClose: (reason) => {
			options.connect?.onClose?.(reason)
		}
	})
	let value: CanonicalSnapshot
	try {
		value = options.resumeSessionId
			? await connection.client.request("session.resume", {
					session_id: options.resumeSessionId,
					display_projection: true
				})
			: await connection.client.request("session.create", {
					source: "tui",
					request_id: randomUUID(),
					...(options.cwd ? { cwd: options.cwd } : {}),
					...(options.title ? { title: options.title } : {})
				})
	} catch (error) {
		connection.close("session create/resume failed")
		throw error instanceof GatewayClientError || error instanceof Error ? error : new Error(String(error))
	}
	instance = new GatewayOmpSession({ ...options, connection }, GatewayOmpSession.snapshotFrom(value))
	for (const notification of early.splice(0)) instance.handleNotification(notification)
	return instance
}
