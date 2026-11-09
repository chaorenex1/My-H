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
import type { CustomMessage } from "../../vendor/agent/src/compaction/messages.ts"
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
	RuntimeDescribeResult,
	SessionHistoryResult,
	SessionListRow,
	SessionLiveInfo,
	SessionStats,
	SessionTranscriptFile,
	SessionTranscriptReadResult,
	SessionUsageResult,
	TodoState,
	TranscriptBlockCapability,
	TranscriptMessage,
	TranscriptRole
} from "./gateway-contract.generated"
import {
	GatewayClientError,
	type GatewayNotification,
	type GatewayServerRequest,
	type GatewayServerRequestHandler
} from "./gateway-client"
import { connectGateway, type GatewayConnection, type GatewayConnectOptions } from "./gateway-socket"
import { executeCodeDetails } from "./gateway-session-eval"
import { setLiveGatewayClient } from "./gateway-live-client"
import { GatewayRolesClient, type ModelRoleRows } from "./gateway-roles"

/** One step of the ctrl+p role cycle: the role and the model route it pins. */
export interface RoleCycleRoute {
	role: string
	provider: string
	model: string
}

/**
 * The pinned routes of `roles.cycleOrder`, skipping rows that are auto or carry no route. The
 * caller resolves each route against the live catalog, so an unknown model is skipped there.
 */
export function roleCycleRoutes(roles: ModelRoleRows): RoleCycleRoute[] {
	const routes: RoleCycleRoute[] = []
	for (const role of roles.cycleOrder) {
		const row = roles.byId(role)
		const provider = row?.provider?.trim()
		const model = row?.model?.trim()
		if (!provider || !model || provider === "auto") continue
		routes.push({ role, provider, model })
	}
	return routes
}

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

/** Display text/content of a tool row, preserving images when the projection included them. */
function toolRowContent(row: TranscriptMessage, text: string): (TextContent | ImageContent)[] {
	const content = row.content
	if (Array.isArray(content)) {
		const blocks = content.filter(
			(part): part is TextContent | ImageContent =>
				asRecord(part)?.type === "text" || asRecord(part)?.type === "image"
		)
		if (blocks.length > 0) return blocks
	}
	return text ? [textContent(text)] : []
}

/**
 * ADR-10 D8.1/P5: one gateway custom row as a display-only message. The client reads
 * `custom_type`/`details` and never parses advisor/skill/todo text; a row without details
 * renders as the generic custom block. The legacy marker pair no longer exists on the wire.
 */
function customRowToMessage(row: TranscriptMessage): CustomMessage | undefined {
	const customType = asString(row.custom_type)
	if (!customType) return undefined
	const text = asString(row.text) ?? ""
	const details = asRecord(row.details)
	let shaped = details
	if (customType === "advisor" && details && !Array.isArray(details.notes)) shaped = { notes: [] }
	if (customType === "todo-reminder" && details) {
		const source = details
		shaped = {
			...(Array.isArray(source.todos) ? { todos: source.todos } : {}),
			attempt: asNumber(source.attempt) ?? 0,
			maxAttempts: asNumber(source.max_attempts) ?? asNumber(source.maxAttempts) ?? 0
		}
	}
	if (customType === "stripped-tool-calls" && details) {
		shaped = { count: asNumber(details.count) ?? 0 }
	}
	return {
		role: "custom",
		customType,
		content: text,
		display: row.display !== false,
		...(shaped ? { details: shaped } : {}),
		attribution: asString(row.attribution) === "user" ? "user" : "agent",
		timestamp: asNumber(row.timestamp) ?? now()
	}
}


/** ADR-10 D8.1: one wire row → the shell's message, per role. Seated roles have no entry. */
type RowProjector = (row: TranscriptMessage, text: string, timestamp: number) => AgentMessage;

function userRow(row: TranscriptMessage, text: string, timestamp: number): AgentMessage {
	if (row.live_steered === true) {
		return { role: "user", content: text, liveSteered: true, attribution: "user", timestamp };
	}
	return { role: "user", content: text, timestamp };
}

function developerRow(_row: TranscriptMessage, text: string, timestamp: number): AgentMessage {
	return { role: "user", content: text, attribution: "agent", timestamp };
}

function assistantRow(row: TranscriptMessage, text: string, timestamp: number): AgentMessage {
	const content: AssistantMessage["content"] = [];
	const reasoning = asString(row.reasoning) ?? asString(row["reasoning_content"]);
	if (reasoning) content.push({ type: "thinking", thinking: reasoning } as ThinkingContent);
	if (text) content.push(textContent(text));
	return {
		role: "assistant",
		content,
		api: "openai-completions",
		provider: "gateway",
		model: asString(row["model"]) ?? "",
		usage: zeroUsage(),
		stopReason: "stop",
		timestamp
	};
}

function toolRow(row: TranscriptMessage, text: string, timestamp: number): AgentMessage {
	const details: Record<string, unknown> = {};
	if (asRecord(row.args)) details.args = row.args;
	if (asString(row.context)) details.context = row.context;
	if (row.labels) details.labels = row.labels;
	if (asNumber(row.row_id) !== undefined) details.row_id = row.row_id;
	// ADR-4 D11.1/D13: the durable sidecar's display block and generated titles merge into the
	// card; they never replace the row's own args/context/labels/row_id.
	const sidecar = asRecord(row.details);
	if (typeof sidecar?.card_title === "string" && sidecar.card_title) details.card_title = sidecar.card_title;
	if (Array.isArray(sidecar?.cell_titles)) details.cell_titles = sidecar.cell_titles;
	const toolName = asString(row.name) ?? "";
	// ADR-4 D12: `include_tool_content:"none"` has no result body, so no cells are built — the
	// eval renderer's no-cells branch draws the code box + text from args instead of a generic card.
	const body = typeof row.content === "string"
		? row.content
		: Array.isArray(row.content)
			? row.content.map(part => asString(asRecord(part)?.text) ?? "").join("")
			: undefined;
	if (toolName === "execute_code" && body) {
		const evalDetails = executeCodeDetails(row.args, decodeToolResult(body), {
			isError: row.is_error === true,
			toolCallId: asString(row.tool_call_id),
			cardTitle: asString(sidecar?.card_title),
			cellTitles: Array.isArray(sidecar?.cell_titles) ? (sidecar.cell_titles as (string | null)[]) : undefined,
			display: asRecord(sidecar?.display)
		});
		if (evalDetails) Object.assign(details, evalDetails);
	}
	return {
		role: "toolResult",
		toolCallId: asString(row.tool_call_id) ?? "",
		// ADR-10 D8.2a: the wire name stays; the render key is chosen at the renderer boundary.
		toolName: asString(row.name) ?? "",
		content: toolRowContent(row, text),
		...(Object.keys(details).length > 0 ? { details } : {}),
		isError: row.is_error === true,
		timestamp
	};
}

function hookRow(row: TranscriptMessage, text: string, timestamp: number): AgentMessage {
	return {
		role: "hookMessage",
		customType: asString(row.custom_type) ?? "hook",
		content: text,
		display: row.display !== false,
		...(asRecord(row.details) ? { details: row.details } : {}),
		timestamp
	};
}

function compactionRow(row: TranscriptMessage, text: string, timestamp: number): AgentMessage {
	// ADR-5 D3: counters and method ride `details.compaction`; a pre-stamp row has neither, so the
	// component draws no badge. `row["tokens_before"]` stays as old-payload compatibility only.
	const stamp = asRecord(asRecord(row.details)?.compaction);
	const tokensAfter = asNumber(stamp?.tokens_after);
	const method = asString(stamp?.method);
	return {
		role: "compactionSummary",
		summary: text,
		tokensBefore: asNumber(stamp?.tokens_before) ?? asNumber(row["tokens_before"]) ?? 0,
		...(tokensAfter !== undefined ? { tokensAfter } : {}),
		...(method !== undefined ? { method } : {}),
		timestamp
	};
}

function branchRow(row: TranscriptMessage, text: string, timestamp: number): AgentMessage {
	return {
		role: "branchSummary",
		summary: text,
		fromId: asString(row["from_id"]) ?? "",
		timestamp
	};
}

// ADR-10 D10: a complete Record makes a new TranscriptRole a typecheck error. Seated roles
// (custom/system/bash/python/file) have no projector and reach no renderer branch.
const ROW_PROJECTORS: Record<TranscriptRole, RowProjector | undefined> = {
	user: userRow,
	developer: developerRow,
	assistant: assistantRow,
	tool: toolRow,
	custom: undefined,
	hook_message: hookRow,
	bash_execution: undefined,
	python_execution: undefined,
	file_mention: undefined,
	branch_summary: branchRow,
	compaction_summary: compactionRow,
	system: undefined
};


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
function decodeToolResult(raw: unknown): unknown {
	if (typeof raw !== "string") return raw
	try {
		return JSON.parse(raw)
	} catch {
		return undefined
	}
}

/** Structured content blocks the decoded result carries, when it has any. */
function toolResultContent(decoded: unknown): (TextContent | ImageContent)[] | undefined {
	const record = asRecord(decoded)
	return Array.isArray(record?.content) ? (record.content as (TextContent | ImageContent)[]) : undefined
}

/** Display text: explicit `result_text`, then the decoded record's text, then the raw payload. */
function toolResultText(payload: Record<string, unknown>, decoded: unknown): string {
	const record = asRecord(decoded)
	const explicit = asString(payload.result_text) ?? (record && typeof record.text === "string" ? record.text : undefined)
	if (explicit) return explicit
	// ADR-4 D12: an eval card's text fallback is the cell output, never the wrapper JSON. This
	// covers the no-cells path (bad/unrecognized result) as well as clients that show text.
	if (asString(payload.name) === "execute_code" && record) {
		const text = [asString(record.output), asString(record.error), asString(record.stderr)]
			.filter((part): part is string => typeof part === "string" && part.length > 0)
			.join("\n")
		if (text) return text
	}
	return (typeof decoded === "string" ? decoded : undefined)
		?? (typeof payload.result === "string" ? payload.result : "")
}

/** Card metadata: summary/duration/diff/labels plus the todo phases the HUD reads. */
function toolResultDetails(payload: Record<string, unknown>): Record<string, unknown> {
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
	return details
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
	const decoded = decodeToolResult(payload.result)
	const content = toolResultContent(decoded)
	const text = toolResultText(payload, decoded)
	const details = toolResultDetails(payload)
	const isError = payload.is_error === true
	if (asString(payload.name) === "execute_code") {
		const evalDetails = executeCodeDetails(payload.args, decoded, {
			isError,
			toolCallId: asString(payload.tool_id),
			cardTitle: asString(payload.card_title),
			cellTitles: Array.isArray(payload.cell_titles) ? (payload.cell_titles as (string | null)[]) : undefined,
			display: asRecord(payload.display)
		})
		if (evalDetails) Object.assign(details, evalDetails)
	}
	return {
		content: content?.length ? content : [textContent(text)],
		...(Object.keys(details).length ? { details } : {}),
		isError
	}
}

/**
 * ADR-4 D12: `session_tool_events.bounded_result` degrades a >64KiB result to
 * `{output, truncated:true}` (or `{error,truncated:true}`); that is not the card's data, so the
 * client refetches the durable row instead of drawing escaped JSON or a guessed status.
 */
function isDegradedEvalResult(payload: Record<string, unknown>, decoded: unknown): boolean {
	if (asString(payload.name) !== "execute_code") return false
	const record = asRecord(decoded)
	if (!record || record.truncated !== true) return false
	return !Array.isArray(record.cells) && record.status === undefined &&
		(typeof record.output === "string" || typeof record.error === "string")
}

/** Hermes row status vocabulary → the shell's task status. */
const TODO_ROW_STATUS: Readonly<Record<string, TodoPhase["tasks"][number]["status"]>> = {
	done: "completed",
	complete: "completed",
	completed: "completed",
	in_progress: "in_progress",
	active: "in_progress",
	cancelled: "abandoned",
	abandoned: "abandoned",
	dropped: "abandoned",
	blocked: "blocked"
}

function todoRowContent(row: Record<string, unknown>): string | undefined {
	for (const key of ["content", "text", "title"] as const) {
		const value = row[key]
		if (typeof value === "string" && value) return value
	}
	return undefined
}

function todoRowStatus(row: Record<string, unknown>): TodoPhase["tasks"][number]["status"] {
	const raw = typeof row.status === "string" ? row.status.toLowerCase().replace(/[\s-]/g, "_") : ""
	return TODO_ROW_STATUS[raw] ?? "pending"
}

/** Hermes todo rows → the shell's phase/task projection (shared with the session bridge). */
export function gatewayTodoPhases(todos: readonly unknown[]): TodoPhase[] {
	const phases = new Map<string, TodoPhase["tasks"]>()
	for (const raw of todos) {
		if (!raw || typeof raw !== "object") continue
		const row = raw as Record<string, unknown>
		const content = todoRowContent(row)
		if (!content) continue
		const phase = typeof row.phase === "string" && row.phase ? row.phase : ""
		const tasks = phases.get(phase) ?? []
		tasks.push({
			content,
			status: todoRowStatus(row),
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
	#roles: GatewayRolesClient | undefined
	#draft: AssistantDraft | undefined
	#messages: AgentMessage[] = []
	/** ADR-10 D8.3: durable row id -> the one message a snapshot/live event may update in place. */
	#rowMessages = new Map<number, AgentMessage>()
	/** ADR-10 D6: `runtime.describe.transcript_blocks`; the gateway's producer readiness. */
	#transcriptBlocks = new Map<string, TranscriptBlockCapability>()
	#running = false
	#compacting = false
	#lastAdmissionId: string | undefined
	#lastNoticeKey: string | undefined
	#lastNoticeAt = 0
	#noticeIds = new Set<string>()
	#queue = { steering: [] as string[], followUp: [] as string[], liveSteered: 0 }
	#usage: SessionUsageResult | undefined
	#stats: SessionStats | undefined
	#todos: unknown[] = []
	#closed = false
	/** ADR-4 D13/D14: live eval-card state per tool call (args, titles, coalesced cell output). */
	#liveTools = new Map<
		string,
		{
			name: string
			args: Record<string, unknown>
			title?: string
			cellTitles: (string | undefined)[]
			display?: Record<string, unknown>
			stream?: { cells: Map<number, string>; timer?: NodeJS.Timeout; pending: number }
		}
	>()
	/** ADR-4 D12: events that arrive while a 64KiB tool row is being refetched, in order. */
	#deferredEmits: AgentSessionEvent[] = []
	#pendingToolReconciles = 0
	#onError?: (error: Error) => void
	#onServerRequest?: GatewayServerRequestHandler

	constructor(options: GatewaySessionOptions, snapshot: GatewaySessionSnapshot) {
		this.connection = options.connection
		this.client = options.connection.client
		this.profileId = options.connection.grant.profile_id
		this.#state = snapshot
		this.#rebuildMessages(snapshot.messages)
		this.#todos = snapshot.todos
		this.#running = snapshot.running
		this.#onError = options.onError
		this.#onServerRequest = options.onServerRequest
	}

	/** Role table over `model.roles.*`; one cache per session, created on first `/models`. */
	get roles(): GatewayRolesClient {
		return (this.#roles ??= new GatewayRolesClient(this.client))
	}

	/**
	 * The ctrl+p model cycle: `omp.cycle_order` resolved to the roles' pinned routes. The first
	 * use loads the role table, so cycling works before `/models` was opened.
	 */
	async roleCycle(): Promise<readonly RoleCycleRoute[]> {
		const roles = this.roles
		if (!roles.loaded) await roles.refresh()
		return roleCycleRoutes(roles)
	}

	/**
	 * Project a persisted transcript row onto the shell's `AgentMessage` vocabulary (ADR-10 D8.1).
	 *
	 * Wire rows carry `role` + `custom_type`/`details`; the client never parses advisor, skill or
	 * todo text and never reads an internal marker. ADR-10 P5 removed `display_kind`/`display_metadata`.
	 */
	static rowToAgentMessage(row: TranscriptMessage): AgentMessage | undefined {
		const timestamp = asNumber(row.timestamp) ?? now();
		const text = asString(row.text) ?? "";

		if (row.role === "custom" || asString(row.custom_type)) {
			const custom = customRowToMessage(row);
			if (custom) return custom;
		}
		return ROW_PROJECTORS[row.role]?.(row, text, timestamp);
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

	/**
	 * ADR-10 P4: adopt `runtime.describe.transcript_blocks`. This table is the only runtime seat
	 * authority; a gateway that omits a block leaves it unadvertised rather than pending.
	 */
	adoptTranscriptBlocks(blocks: Record<string, TranscriptBlockCapability> | undefined): void {
		this.#transcriptBlocks.clear()
		for (const [id, capability] of Object.entries(blocks ?? {})) {
			if (capability && typeof capability.supported === "boolean") this.#transcriptBlocks.set(id, capability)
		}
	}

	/** A block's producer readiness, or `undefined` when the gateway did not advertise the block. */
	transcriptBlock(id: string): TranscriptBlockCapability | undefined {
		return this.#transcriptBlocks.get(id)
	}

	/** Only an advertised `supported:true` mounts; an old gateway that advertises nothing keeps rendering. */
	#blockEnabled(id: string): boolean {
		const capability = this.#transcriptBlocks.get(id)
		return capability === undefined || capability.supported === true
	}

	/** Subscribe to the projected session events; returns the unsubscribe. */
	subscribe(listener: (event: AgentSessionEvent) => void): () => void {
		this.#listeners.add(listener)
		return () => this.#listeners.delete(listener)
	}

	#emit(event: AgentSessionEvent): void {
		if (this.#pendingToolReconciles > 0) {
			this.#deferredEmits.push(event)
			return
		}
		for (const listener of [...this.#listeners]) {
			try {
				listener(event)
			} catch (error) {
				this.#onError?.(error instanceof Error ? error : new Error(String(error)))
			}
		}
	}

	/** ADR-10 D8.3: rebuild both the render list and the durable-row map from one snapshot. */
	#rebuildMessages(rows: TranscriptMessage[]): void {
		const messages: AgentMessage[] = []
		this.#rowMessages.clear()
		for (const row of rows) {
			const message = GatewayOmpSession.rowToAgentMessage(row)
			if (!message) continue
			messages.push(message)
			const rowId = asNumber(row.row_id)
			if (rowId !== undefined) this.#rowMessages.set(rowId, message)
		}
		this.#messages = messages
	}

	/** Replace the session state with a fresh canonical snapshot (resume/refresh). */
	adoptSnapshot(snapshot: GatewaySessionSnapshot): void {
		this.#state = snapshot
		this.#rebuildMessages(snapshot.messages)
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

	/** Read the durable transcript (the gateway's projection, newest window / rows after `since`). */
	async history(limit = 200, since?: number): Promise<SessionHistoryResult> {
		return this.client.request("session.history", {
			session_id: this.#state.sessionId,
			limit,
			// ADR-10 D4/D8.5: one read carries display-safe tool content and the selector outline.
			include_tool_content: "full",
			include_outline: true,
			...(since !== undefined ? { since } : {})
		})
	}

	/** Refresh info, stats and usage; called at boot and after settle. */
	async refresh(): Promise<void> {
		const [info, stats, usage] = await Promise.allSettled([
			this.client.request("session.info", { session_id: this.#state.sessionId }),
			this.client.request("session.stats", { session_id: this.#state.sessionId }),
			this.client.request("session.usage", { session_id: this.#state.sessionId })
		])
		if (info.status === "fulfilled" && this.#adoptInfo(info.value as CanonicalSessionInfo | SessionLiveInfo)) {
			// The served model can differ from the create snapshot (another client switched it, or the
			// agent resolved its config default late); the shell only follows the status line when told.
			this.#emit({ type: "model_changed" })
		}
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

	/**
	 * The profile's mirrored bundles (`session.transcript.list`), ADR-16 P4.
	 *
	 * File metadata only: the picker merges it with `listSessions` so a row can say whether its
	 * transcript still exists as a cold copy. It is read without touching the host filesystem —
	 * a remote gateway's `sessions/` is not this process's.
	 */
	async listTranscripts(limit = 200): Promise<SessionTranscriptFile[]> {
		const result = (await this.client.request("session.transcript.list", { limit })) as {
			projects?: { sessions?: SessionTranscriptFile[] | null }[] | null
		}
		return (result.projects ?? []).flatMap(project => project.sessions ?? [])
	}

	/** One page of a mirrored bundle (`session.transcript.read`); the id is the only address. */
	async readTranscript(sessionId: string, offset = 0, limit = 200): Promise<SessionTranscriptReadResult> {
		return (await this.client.request("session.transcript.read", {
			session_id: sessionId,
			offset,
			limit
		})) as SessionTranscriptReadResult
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

	/** Adopt a live `session.info` snapshot; `true` when the served model/provider changed. */
	#adoptInfo(info: CanonicalSessionInfo | SessionLiveInfo): boolean {
		const previousModel = this.#state.model
		const previousProvider = this.#state.provider
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
		return this.#state.model !== previousModel || this.#state.provider !== previousProvider
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
	/** Every live frame the shell projects; unknown types are ignored by the table lookup. */
	readonly #notificationHandlers: Record<string, (payload: Record<string, unknown>) => void> = {
		"gateway.ready": () => {},
		"session.info": payload => this.#onSessionInfo(payload),
		"session.title": payload => this.#onSessionTitle(payload),
		"turn.start": () => this.#onTurnStart(),
		"turn.end": payload => this.#onTurnEnd(payload),
		"message.start": payload => this.#onMessageStart(payload),
		"message.block_start": payload => this.#onMessageBlockStart(payload),
		"message.delta": payload => this.#onMessageDelta(payload),
		"message.interim": payload => this.#onMessageInterim(payload),
		"reasoning.delta": payload => this.#onReasoningDelta(payload),
		"thinking.delta": payload => this.#onReasoningDelta(payload),
		"message.complete": payload => this.#complete(payload),
		"tool.start": payload => this.#onToolStart(payload),
		"tool.progress": payload => this.#onToolProgress(payload),
		"tool.output": payload => this.#onToolOutput(payload),
		"tool.title": payload => this.#onToolTitle(payload),
		"tool.complete": payload => this.#onToolComplete(payload),
		"todo.updated": payload => this.#onTodoUpdated(payload),
		"queue.updated": payload => this.#onQueueUpdated(payload),
		"prompt.result": payload => this.#onPromptResult(payload),
		"session.settled": () => this.#onSessionSettled(),
		"session.usage": payload => {
			this.#usage = payload as unknown as SessionUsageResult
		},
		"status.update": payload => this.#onStatusUpdate(payload),
		// ADR-10 D7: transcript.notice is the one-shot line channel; the two legacy events stay
		// for old clients and are ignored here.
		"agent.print": () => {},
		"command.output": payload => this.#onTextNotice(payload, "info", asString(payload.name) ?? "command"),
		"notification.show": payload => this.#onNotificationShow(payload),
		"message.advisor": payload => this.#onAdvisorMessage(payload),
		// Seat: contract-only until the gateway stop gate emits it (ADR-2 D11).
		"todo.reminder": payload => this.#onTodoReminder(payload),
		"transcript.notice": payload => this.#onTranscriptNotice(payload),
		// ADR-9 D4: a durable custom row injected live; merged by row_id with the resume snapshot.
		"transcript.custom": payload => this.#onTranscriptCustom(payload),
		// ADR-5 D4: a committed compaction boundary; merged with the resume snapshot by row id.
		"compaction.summary": payload => this.#onCompactionSummary(payload),
		"session.reclaimed": payload => this.#onTextNotice(payload, "warning", "session"),
		"session.resume_progress": payload => this.#onResumeProgress(payload),
		"approval.cancelled": payload => this.#onApprovalCancelled(payload),
		"notice": payload => this.#onNotice(payload),
		"ttsr.triggered": payload => this.#onTtsrTriggered(payload),
		"message.rewind": () => this.#onMessageRewind(),
		"error": payload => this.#onErrorNotice(payload)
	}

	handleNotification(notification: GatewayNotification): void {
		if (this.#closed) return
		const sessionId = notification.sessionId
		if (sessionId !== undefined && this.#state.sessionId && sessionId !== this.#state.sessionId) return
		this.#notificationHandlers[notification.type]?.(notification.payload as Record<string, unknown>)
	}

	#onSessionInfo(payload: Record<string, unknown>): void {
		if (this.#adoptInfo(payload as unknown as SessionLiveInfo)) this.#emit({ type: "model_changed" })
	}

	#onSessionTitle(payload: Record<string, unknown>): void {
		const title = asString(payload.title)
		if (title) this.#state.title = title
	}

	#onTurnStart(): void {
		this.#running = true
		this.#emit({ type: "turn_start" })
	}

	#onTurnEnd(payload: Record<string, unknown>): void {
		this.#setCompacting(false)
		const message = asRecord(payload.message)
		this.#emit({
			type: "turn_end",
			message: message ? (message as unknown as AgentMessage) : this.#lastAssistantMessage(),
			toolResults: []
		})
	}

	#onMessageStart(payload: Record<string, unknown>): void {
		this.#openDraft(asString(payload.message_id) ?? `${this.#state.sessionId}:${now()}`)
	}

	#onMessageBlockStart(payload: Record<string, unknown>): void {
		this.#ensureDraft()
		const index = asNumber(payload.content_index)
		if (index !== undefined) this.#draft!.lastIndex = index
	}

	#onMessageDelta(payload: Record<string, unknown>): void {
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
	}

	#onMessageInterim(payload: Record<string, unknown>): void {
		const draft = this.#ensureDraft()
		const text = asString(payload.text)
		if (!text) return
		const index = draft.lastIndex
		draft.text.set(index, text)
		const partial = materialize(draft)
		this.#emit({
			type: "message_update",
			message: partial,
			assistantMessageEvent: { type: "text_end", contentIndex: index, content: text, partial }
		})
	}

	#onReasoningDelta(payload: Record<string, unknown>): void {
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
	}

	#onToolStart(payload: Record<string, unknown>): void {
		// Hermes streams one assistant message across tool rounds: text before the tool and the answer
		// after it would share one block, leaving the tool cards below the answer. Close the pre-tool
		// segment here; the next delta opens the post-tool block under the cards.
		const toolCallId = asString(payload.tool_id) ?? ""
		this.#forgetLiveTool(toolCallId)
		const cellTitles = Array.isArray(payload.cell_titles)
			? payload.cell_titles.map(value => (typeof value === "string" ? value : undefined))
			: []
		this.#liveTools.set(toolCallId, {
			name: asString(payload.name) ?? "",
			args: asRecord(payload.args) ?? {},
			title: asString(payload.title),
			cellTitles
		})
		const stepUsage = toOmpUsage(payload.step_usage as never)
		const closed = this.#closeSegmentForTool(stepUsage)
		this.#emit({
			type: "tool_execution_start",
			toolCallId,
			toolName: asString(payload.name) ?? "",
			args: payload.args ?? {},
			intent: asString(payload.context),
			...(asString(payload.title) ? { title: asString(payload.title) } : {}),
			...(cellTitles.length > 0 ? { cellTitles } : {}),
			// A tool-only step has no assistant block to close; the shell counts this usage directly.
			...(closed || !stepUsage ? {} : { stepUsage })
		})
	}

	/** ADR-4 D13: a generated title for a card or one cell; display-only. */
	#onToolTitle(payload: Record<string, unknown>): void {
		const toolCallId = asString(payload.tool_id)
		const title = asString(payload.title)
		if (!toolCallId || !title) return
		const cellIndex = asNumber(payload.cell_index)
		const live = this.#liveTools.get(toolCallId)
		if (live) {
			if (cellIndex === undefined) live.title = title
			else live.cellTitles[cellIndex] = title
		}
		this.#emit({
			type: "tool_title",
			toolCallId,
			title,
			...(cellIndex !== undefined ? { cellIndex } : {})
		})
	}

	/** ADR-4 D14: eval cell output, coalesced (50ms / 4KiB) into the card's running cells. */
	#onToolOutput(payload: Record<string, unknown>): void {
		const toolCallId = asString(payload.tool_id)
		const text = asString(payload.text)
		if (!toolCallId || !text) return
		const live = this.#liveTools.get(toolCallId)
		if (!live) return
		const state: NonNullable<typeof live.stream> = (live.stream ??= {
			cells: new Map<number, string>(),
			pending: 0
		})
		const index = asNumber(payload.cell_index) ?? 0
		state.cells.set(index, (state.cells.get(index) ?? "") + text)
		state.pending += text.length
		if (state.pending >= 4096) {
			this.#flushToolStream(toolCallId)
		} else if (!state.timer) {
			state.timer = setTimeout(() => {
				state.timer = undefined
				this.#flushToolStream(toolCallId)
			}, 50)
		}
	}

	#flushToolStream(toolCallId: string): void {
		const live = this.#liveTools.get(toolCallId)
		const state = live?.stream
		if (!live || !state || this.#closed) return
		if (state.timer) {
			clearTimeout(state.timer)
			state.timer = undefined
		}
		state.pending = 0
		const argCells = Array.isArray(live.args.cells)
			? live.args.cells.filter((cell): cell is Record<string, unknown> => asRecord(cell) !== undefined)
			: []
		const cells = (argCells.length > 0 ? argCells : [{ code: live.args.code, language: live.args.language }]).map(
			(cell, index) => ({
				...cell,
				index,
				status: "running",
				output: state.cells.get(index) ?? ""
			})
		)
		const details = executeCodeDetails(live.args, { status: "running", cells }, {
			toolCallId,
			cardTitle: live.title,
			cellTitles: live.cellTitles
		})
		if (!details) return
		this.#emit({
			type: "tool_execution_update",
			toolCallId,
			toolName: live.name,
			args: live.args,
			partialResult: { content: [], details: details as unknown as Record<string, unknown> }
		})
	}

	/** Drop one call's live state, cancelling any pending stream flush. */
	#forgetLiveTool(toolCallId: string): void {
		const state = this.#liveTools.get(toolCallId)?.stream
		if (state?.timer) clearTimeout(state.timer)
		this.#liveTools.delete(toolCallId)
	}

	#onToolProgress(payload: Record<string, unknown>): void {
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
	}

	#onToolComplete(payload: Record<string, unknown>): void {
		const toolCallId = asString(payload.tool_id) ?? ""
		const toolName = asString(payload.name) ?? ""
		const isError = payload.is_error === true
		const live = this.#liveTools.get(toolCallId)
		// A background job's terminal frame has no args/display/titles: reuse the ones its start
		// and foreground completion carried, so the settled card is the same card.
		const effective: Record<string, unknown> = { ...payload }
		if (!asString(effective.card_title) && live?.title) effective.card_title = live.title
		if (!Array.isArray(effective.cell_titles) && live?.cellTitles.length) effective.cell_titles = live.cellTitles
		if (!asRecord(effective.display) && live?.display) effective.display = live.display
		// The job's terminal frame carries no call args; the single-cell result JSON has no `code`.
		if (!asRecord(effective.args) && live) effective.args = live.args
		const decoded = decodeToolResult(effective.result)
		const state = this.#liveTools.get(toolCallId)?.stream
		if (state?.timer) {
			clearTimeout(state.timer)
			state.timer = undefined
		}
		const finish = (result: { content: (TextContent | ImageContent)[]; details?: unknown }, error: boolean) => {
			// A refetched end must precede events queued during the fetch (notably `agent_end`),
			// or the controller seals the card before its result lands.
			this.#emitToolEnd({
				type: "tool_execution_end",
				toolCallId,
				toolName,
				result,
				isError: error
			})
			// ADR-4 D14: a background job's card stays tracked until its terminal frame fills it.
			const asyncState = asRecord(asRecord(result.details)?.async)?.state
			if (asyncState !== "running") this.#forgetLiveTool(toolCallId)
		}
		if (isDegradedEvalResult(payload, decoded)) {
			// ADR-4 D12: the 64KiB wire form is not the card's data — refetch the durable row that
			// was flushed before this event, instead of drawing escaped JSON or a guessed status.
			// Keep every later event (notably the turn's `agent_end`) behind this refetch so the
			// rebuilt card lands on the live component instead of being sealed away first.
			this.#beginToolReconcile(this.#reconcileDegradedToolRow(toolCallId)
				.then(row => {
					if (row) {
						finish({ content: row.content, ...(row.details ? { details: row.details } : {}) }, row.isError)
						return
					}
					const text = asString(asRecord(decoded)?.output) ?? asString(asRecord(decoded)?.error) ?? ""
					finish({
						content: [textContent(text)],
						details: { notice: `Full output: agent_out(mode="artifact", kind="tool", tool_call_id="${toolCallId}")` }
					}, isError)
				})
				.catch(() => {
					finish({
						content: [textContent(asString(asRecord(decoded)?.output) ?? "")],
						details: { notice: "The transcript row could not be reloaded; resend to refresh it." }
					}, isError)
				}))
			return
		}
		const projected = projectToolResult(effective)
		// A backgrounded foreground completion keeps its card data for the job's terminal frame.
		const asyncState = asRecord(asRecord(projected.details)?.async)?.state
		if (live && asyncState === "running") {
			const title = asString(effective.card_title)
			if (title) live.title = title
			if (Array.isArray(effective.cell_titles)) {
				live.cellTitles = effective.cell_titles.map(value => (typeof value === "string" ? value : undefined))
			}
			const display = asRecord(effective.display)
			if (display) live.display = display
		}
		finish(projected, isError)
	}

	/** A reconciled tool end bypasses the queue head so it lands before events it was waiting on. */
	#emitToolEnd(event: AgentSessionEvent): void {
		if (this.#pendingToolReconciles > 0) this.#deferredEmits.unshift(event)
		else this.#emit(event)
	}

	/** Hold every outgoing event behind one refetch; flush in arrival order when the last settles. */
	#beginToolReconcile(task: Promise<unknown>): void {
		this.#pendingToolReconciles++
		void task
			.catch(error => this.#report(error))
			.finally(() => {
				this.#pendingToolReconciles--
				if (this.#pendingToolReconciles > 0 || this.#closed) return
				const queued = this.#deferredEmits
				this.#deferredEmits = []
				for (const event of queued) this.#emit(event)
			})
	}

	/** The durable tool row for one call, projected exactly like a resumed transcript row. */
	async #reconcileDegradedToolRow(
		toolCallId: string
	): Promise<Extract<AgentMessage, { role: "toolResult" }> | undefined> {
		const page = await this.history(200)
		for (const row of page.messages) {
			if (row.tool_call_id !== toolCallId) continue
			const message = GatewayOmpSession.rowToAgentMessage(row)
			if (message?.role === "toolResult") return message
		}
		return undefined
	}

	#onTodoUpdated(payload: Record<string, unknown>): void {
		this.#todos = Array.isArray(payload.todos) ? payload.todos : []
	}

	#onQueueUpdated(payload: Record<string, unknown>): void {
		this.#queue = {
			steering: Array.isArray(payload.steering) ? payload.steering.filter((v): v is string => typeof v === "string") : [],
			followUp: Array.isArray(payload.follow_up)
				? payload.follow_up.filter((v): v is string => typeof v === "string")
				: [],
			liveSteered: asNumber(payload.live_steered) ?? 0
		}
		this.#emit({ type: "queue_update", ...this.#queue })
	}

	#onPromptResult(payload: Record<string, unknown>): void {
		this.#running = false
		this.#lastAdmissionId = undefined
		const status = asString(payload.status)
		const error = asRecord(payload.error)
		if ((status === "error" || error) && payload.error !== null && payload.error !== undefined) {
			// A failed turn otherwise leaves only a partial (or empty) assistant block; name it.
			this.#emitNotice("error", asString(error?.message) ?? "Turn failed", "gateway")
		}
		this.#emit({ type: "agent_end", messages: [...this.#messages], isTerminal: true, yielded: true })
		// Stats/usage lag the terminal frame: refetch right after so the status line's cost/context
		// segments paint the turn that just ended, then repaint.
		void this.refresh()
			.then(() => {
				if (!this.#closed) this.#emit({ type: "model_changed" })
			})
			.catch((error: unknown) => this.#report(error))
	}

	#onSessionSettled(): void {
		if (!this.#running) return
		this.#running = false
		this.#emit({ type: "agent_end", messages: [...this.#messages], isTerminal: true, yielded: true })
	}

	/** Emit a notice unless the same level+text already landed in the last 1.5s. `_emit_status_kind`
	 *  prints through `agent.print` and then `status.update`, so both arrive for one lifecycle line. */
	#emitNotice(level: "info" | "warning" | "error", message: string, source: string): void {
		const key = `${level}\u0000${message}`
		const at = now()
		if (key === this.#lastNoticeKey && at - this.#lastNoticeAt < 1500) return
		this.#lastNoticeKey = key
		this.#lastNoticeAt = at
		this.#emit({ type: "notice", level, message, source })
	}

	#onStatusUpdate(payload: Record<string, unknown>): void {
		const kind = (asString(payload.kind) ?? "").toLowerCase()
		if (kind === "compressing") {
			this.#setCompacting(true)
			return
		}
		if (kind === "ready") {
			// `session.compress` pins "compressing" and always releases with a generic "ready".
			if (this.#compacting) this.#setCompacting(false)
			return
		}
		if (!kind.includes("compact")) return
		const done =
			kind.includes("done") || kind.includes("end") || kind.includes("compacted") || kind.includes("compressed")
		this.#setCompacting(!done)
	}

	/** ADR-5 D7: the idle recap drops its stretch on either edge of a compaction pass. */
	#setCompacting(compacting: boolean): void {
		if (this.#compacting === compacting) return
		this.#compacting = compacting
		this.#emit({ type: "compaction_state_changed", compacting })
	}

	#onNotice(payload: Record<string, unknown>): void {
		const text = asString(payload.message)
		if (text) this.#emitNotice("info", text, "gateway")
	}

	/** ADR-6 D6: shape-check only; merging, truncation and copy belong to the pi-tui component. */
	#onTtsrTriggered(payload: Record<string, unknown>): void {
		const rules = (Array.isArray(payload.rules) ? payload.rules : [])
			.map(asRecord)
			.filter((rule): rule is Record<string, unknown> => rule !== undefined)
			.map(rule => ({
				name: asString(rule.name) ?? "",
				description: asString(rule.description),
				content: asString(rule.content)
			}))
			.filter(rule => rule.name.length > 0)
		if (rules.length > 0) this.#emit({ type: "ttsr_triggered", rules })
	}

	/** ADR-6 D13: the gateway discarded the live draft; the retry reopens it via `message.start`. */
	#onMessageRewind(): void {
		this.#draft = undefined
		this.#emit({ type: "message_rewind" })
	}

	#onErrorNotice(payload: Record<string, unknown>): void {
		this.#emitNotice("error", asString(payload.message) ?? "Gateway error", "gateway")
	}

	#onTextNotice(
		payload: Record<string, unknown>,
		level: "info" | "warning" | "error",
		source: string
	): void {
		const text = asString(payload.text) ?? asString(payload.message) ?? asString(payload.reason)
		if (text) this.#emitNotice(level, text, source)
	}

	#onNotificationShow(payload: Record<string, unknown>): void {
		const text = asString(payload.text)
		if (!text) return
		const detail = asString(payload.detail)
		const level =
			payload.level === "error"
				? "error"
				: payload.level === "warning" || payload.level === "warn"
					? "warning"
					: "info"
		this.#emitNotice(level, detail && detail !== text ? `${text}\n${detail}` : text, asString(payload.kind) ?? "gateway")
	}

	#emitCustomMessage(message: CustomMessage, rowId?: number): void {
		// ADR-10 D8.3: a snapshot that already carries this durable row owns the block; the live
		// event only confirms it, never appends a second card.
		if (rowId !== undefined) {
			if (this.#rowMessages.has(rowId)) return
			this.#rowMessages.set(rowId, message)
		}
		this.#messages.push(message)
		this.#emit({ type: "message_start", message })
		this.#emit({ type: "message_end", message })
	}

	/** ADR-9 D4: one live custom row, projected exactly like a resumed snapshot row. */
	#onTranscriptCustom(payload: Record<string, unknown>): void {
		if (!this.#blockEnabled("late_diagnostics")) return
		const row = asRecord(payload.message)
		if (!row) return
		const message = GatewayOmpSession.rowToAgentMessage(row as unknown as TranscriptMessage)
		if (message?.role === "custom") this.#emitCustomMessage(message, asNumber(row.row_id))
	}

	/**
	 * ADR-5 D4/D6: one committed compaction boundary. It is a real entry (omp's summary is a
	 * transcript entry, so `/copy` and tree see it), projected through the same row mapper the
	 * resume snapshot uses. `boundary_row_id` merges it with the snapshot copy, whichever arrives
	 * first; `#rowMessages` is keyed by durable row id for exactly that.
	 */
	#onCompactionSummary(payload: Record<string, unknown>): void {
		if (!this.#blockEnabled("compaction_summary")) return
		const text = asString(payload.text)
		if (!text) return
		const boundaryRowId = asNumber(payload.boundary_row_id)
		if (boundaryRowId !== undefined && this.#rowMessages.has(boundaryRowId)) return
		const row = {
			role: "compaction_summary",
			text,
			timestamp: asNumber(payload.timestamp) ?? now(),
			...(boundaryRowId !== undefined ? { row_id: boundaryRowId } : {}),
			details: {
				compaction: {
					tokens_before: asNumber(payload.tokens_before) ?? 0,
					tokens_after: asNumber(payload.tokens_after),
					token_method: asString(payload.token_method) ?? "estimate_rough",
					method: asString(payload.method) ?? "llm_summary",
					boundary_row_id: boundaryRowId
				}
			}
		} as TranscriptMessage
		const message = GatewayOmpSession.rowToAgentMessage(row)
		if (!message) return
		if (boundaryRowId !== undefined) this.#rowMessages.set(boundaryRowId, message)
		this.#messages.push(message)
		this.#emit({ type: "compaction_summary", message })
	}

	#onAdvisorMessage(payload: Record<string, unknown>): void {
		if (!this.#blockEnabled("advisor")) return
		const text = asString(payload.text)
		if (!text) return
		const message = customRowToMessage({
			role: "custom",
			text,
			custom_type: "advisor",
			display: true,
			display_only: false,
			details: asRecord(payload.details) ?? { notes: [] },
			attribution: "agent",
			row_id: asNumber(payload.row_id),
			timestamp: now()
		} as TranscriptMessage)
		if (message) this.#emitCustomMessage(message, asNumber(payload.row_id))
	}

	#onTodoReminder(payload: Record<string, unknown>): void {
		if (!this.#blockEnabled("todo_reminder")) return
		const message = customRowToMessage({
			role: "custom",
			text: asString(payload.text) ?? "",
			custom_type: "todo-reminder",
			display: true,
			display_only: false,
			details: {
				todos: payload.todos,
				attempt: asNumber(payload.attempt) ?? 0,
				max_attempts: asNumber(payload.max_attempts) ?? 0
			},
			attribution: "agent",
			row_id: asNumber(payload.row_id),
			timestamp: now()
		} as TranscriptMessage)
		if (message) this.#emitCustomMessage(message, asNumber(payload.row_id))
	}

	/** ADR-10 D7/D8.3: one deduped one-shot notice; a row already in the snapshot is not repeated. */
	#onTranscriptNotice(payload: Record<string, unknown>): void {
		if (!this.#blockEnabled("notice")) return
		const id = asString(payload.id)
		if (id) {
			if (this.#noticeIds.has(id)) return
			this.#noticeIds.add(id)
		}
		const rowId = asNumber(payload.row_id)
		if (rowId !== undefined && this.#rowMessages.has(rowId)) return
		const level = payload.level === "error" ? "error" : payload.level === "warning" ? "warning" : "info"
		const text = asString(payload.text)
		if (text) this.#emitNotice(level, text, asString(payload.source) ?? "gateway")
	}

	#onResumeProgress(payload: Record<string, unknown>): void {
		const failed = payload.status === "failed"
		const text =
			asString(payload.message) ??
			(failed ? `Session resume failed (${asString(payload.phase) ?? "unknown"})` : undefined)
		if (!text) return
		this.#emitNotice(failed ? "error" : "info", text, "session")
	}

	#onApprovalCancelled(payload: Record<string, unknown>): void {
		const reason = asString(payload.reason) ?? "cancelled"
		const count = asNumber(payload.cancelled_count) ?? 0
		this.#emitNotice(
			"warning",
			`Approval ${reason}${count > 0 ? ` (${count} pending)` : ""}`,
			"approval"
		)
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
		// ADR-2 D1: the assistant message carries THIS step's usage, never the session-cumulative
		// `payload.usage` (missing step usage stays zero, so markers never fire on a cumulative value).
		const stepUsage = toOmpUsage(payload.step_usage as never)
		const servedModel = asString(payload.served_model)
		const status = stopReasonFor(asString(payload.status), asString(payload.stop_reason))
		const finalText = typeof payload.text === "string" ? payload.text : undefined
		if (!this.#draft) {
			// The pre-tool segment was closed at `tool.start`; a complete-only answer (no deltas after
			// the tool) still has to be rendered, but only its suffix that was not shown yet.
			const shown = assistantText(this.#lastAssistantMessage())
			const suffix =
				finalText === undefined ? undefined : shown && finalText.startsWith(shown) ? finalText.slice(shown.length) : finalText
			if (!suffix) {
				this.#mergeLastAssistant(stepUsage, status, servedModel)
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
		if (stepUsage) draft.message.usage = stepUsage
		if (servedModel) draft.message.upstreamModel = servedModel
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

	/** Close the in-flight assistant segment when a tool starts, so later text opens a new block.
	 *  Returns true when a visible segment was finalized (its usage is already on that message). */
	#closeSegmentForTool(stepUsage?: Usage): boolean {
		const draft = this.#draft
		if (!draft) return false
		this.#draft = undefined
		const hasContent = draft.text.size > 0 || draft.thinking.size > 0
		if (!hasContent) return false
		if (stepUsage) draft.message.usage = stepUsage
		draft.message.stopReason = "toolUse"
		const message = materialize(draft)
		this.#emit({ type: "message_update", message, assistantMessageEvent: { type: "done", reason: "toolUse", message } })
		this.#emit({ type: "message_end", message })
		this.#messages.push(message)
		return true
	}

	/** Fold a late complete's usage/status into the last assistant block (no duplicate render). */
	#mergeLastAssistant(usage: Usage | undefined, status: StopReason, upstreamModel?: string): void {
		for (let index = this.#messages.length - 1; index >= 0; index -= 1) {
			const message = this.#messages[index]
			if (message?.role !== "assistant") continue
			if (usage) message.usage = usage
			if (upstreamModel) message.upstreamModel = upstreamModel
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
		this.#deferredEmits = []
		for (const toolCallId of [...this.#liveTools.keys()]) this.#forgetLiveTool(toolCallId)
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
	/**
	 * Boot-time setup that needs the authority but must land before the session exists and before
	 * the shell renders (the appearance panel's load + write-through). A failure here fails the
	 * connection like a failed session.create; callers catch what they can run without.
	 */
	beforeSession?: (client: GatewayConnection["client"]) => Promise<void>
}

/**
 * ADR-10 D8.5: the `session.create` params omp-ui sends. `custom_messages` is create-only session
 * state; Desktop/Ink omit it and keep the legacy projection. `client` is the self-declared
 * identity object (attribution only).
 */
export function gatewayCreateParams(options: { cwd?: string; title?: string } = {}): Record<string, unknown> {
	return {
		source: "tui",
		client: { name: "omp-ui" },
		custom_messages: true,
		request_id: randomUUID(),
		...(options.cwd ? { cwd: options.cwd } : {}),
		...(options.title ? { title: options.title } : {})
	}
}

/**
 * ADR-10 D8.5: the `session.resume` params omp-ui sends. The display projection carries custom
 * rows and display-safe tool content, so one snapshot is enough (no second `session.history`).
 */
export function gatewayResumeParams(sessionId: string): Record<string, unknown> {
	return {
		session_id: sessionId,
		display_projection: true,
		include_tool_content: "full",
		// ADR-5 D6: the divider is opt-in and this viewer renders it.
		include_compaction: true
	}
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
	setLiveGatewayClient(connection.client)
	let value: CanonicalSnapshot
	try {
		await options.beforeSession?.(connection.client)
		value = options.resumeSessionId
			? await connection.client.request("session.resume", gatewayResumeParams(options.resumeSessionId))
			: await connection.client.request("session.create", gatewayCreateParams(options))
	} catch (error) {
		connection.close("session create/resume failed")
		throw error instanceof GatewayClientError || error instanceof Error ? error : new Error(String(error))
	}
	instance = new GatewayOmpSession({ ...options, connection }, GatewayOmpSession.snapshotFrom(value))
	// ADR-10 D8.4: one non-blocking capability read after create/resume. A failure leaves the
	// role render set intact and advertises nothing — never a fake `supported:true`.
	void connection.client
		.request<RuntimeDescribeResult>("runtime.describe", {})
		.then((described) => instance?.adoptTranscriptBlocks(described.transcript_blocks))
		.catch(() => {})
	for (const notification of early.splice(0)) instance.handleNotification(notification)
	return instance
}
