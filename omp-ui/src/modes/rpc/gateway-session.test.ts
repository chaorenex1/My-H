/**
 * The main-screen projections: gateway notifications → `AgentSessionEvent`, and the canonical
 * snapshot → the shell's message list. Envelope mechanics live in `gateway-client.test.ts`; this file
 * pins the business mapping the shell renders.
 */

import { describe, expect, test } from "bun:test"

import type { AgentSessionEvent } from "../../session/agent-session-events"
import type { TranscriptMessage } from "./gateway-contract.generated"
import { GatewayClient } from "./gateway-client"
import {
	GatewayOmpSession,
	type GatewaySessionSnapshot,
	gatewayCreateParams,
	gatewayResumeParams,
	gatewayTodoPhases,
	toOmpUsage
} from "./gateway-session"
import { executeCodeDetails } from "./gateway-session-eval"

const SNAPSHOT = (overrides: Partial<GatewaySessionSnapshot> = {}): GatewaySessionSnapshot => ({
	sessionId: "sid-1",
	storedSessionId: "sid-1",
	model: "m-1",
	provider: "p-1",
	cwd: "/tmp",
	messages: [],
	pending: [],
	revision: 3,
	executionGeneration: 2,
	authorityEpoch: 1,
	todos: [],
	running: false,
	...overrides
})

function harness(overrides: Partial<GatewaySessionSnapshot> = {}) {
	const sent: Record<string, unknown>[] = []
	const client = new GatewayClient({
		transport: { send: (text) => sent.push(JSON.parse(text) as Record<string, unknown>) },
		autoNegotiate: false,
		requestTimeoutMs: 0
	})
	const connection = {
		client,
		grant: { url: "ws://127.0.0.1/x", protocols: [] as string[], profile_id: "p", instance_id: "i" },
		close: () => {}
	}
	const session = new GatewayOmpSession({ connection }, SNAPSHOT(overrides))
	const events: AgentSessionEvent[] = []
	session.subscribe((event) => events.push(event))
	return { session, events, sent, client }
}

describe("message projection", () => {
	test("thinking and reply text sharing one content index both survive", () => {
		const { session, events } = harness()
		session.handleNotification({ type: "message.start", payload: { message_id: "m1" } })
		session.handleNotification({ type: "reasoning.delta", payload: { text: "thinking hard" } })
		session.handleNotification({ type: "message.delta", payload: { text: "hi", content_index: 0 } })
		session.handleNotification({
			type: "message.complete",
			payload: { message_id: "m1", text: "hi there", status: "complete" }
		})

		const end = events.find((event) => event.type === "message_end")
		expect(end).toBeDefined()
		const message = (end as Extract<AgentSessionEvent, { type: "message_end" }>).message
		expect(message.role).toBe("assistant")
		if (message.role !== "assistant") throw new Error("expected assistant")
		expect(message.content.map((block) => block.type)).toEqual(["thinking", "text"])
		expect(message.content.at(-1)).toMatchObject({ type: "text", text: "hi there" })
		expect(message.stopReason).toBe("stop")
	})

	test("deltas are cumulative updates the shell can render as they arrive", () => {
		const { session, events } = harness()
		session.handleNotification({ type: "message.start", payload: { message_id: "m1" } })
		session.handleNotification({ type: "message.delta", payload: { text: "a", content_index: 0 } })
		session.handleNotification({ type: "message.delta", payload: { text: "b", content_index: 0 } })

		const updates = events.filter(
			(event): event is Extract<AgentSessionEvent, { type: "message_update" }> => event.type === "message_update"
		)
		expect(updates.length).toBeGreaterThan(0)
		const last = updates.at(-1)!
		if (last.message.role !== "assistant") throw new Error("expected assistant")
		expect(last.message.content).toEqual([{ type: "text", text: "ab" }])
		expect(last.assistantMessageEvent).toMatchObject({ type: "text_delta", delta: "b" })
	})

	test("an interrupted completion maps to stopReason aborted", () => {
		const { session, events } = harness()
		session.handleNotification({ type: "message.start", payload: { message_id: "m1" } })
		session.handleNotification({
			type: "message.complete",
			payload: { text: "partial", status: "interrupted" }
		})
		const end = events.find(
			(event): event is Extract<AgentSessionEvent, { type: "message_end" }> => event.type === "message_end"
		)!
		expect(end.message.role === "assistant" && end.message.stopReason).toBe("aborted")
	})

	test("a tool closes the pre-tool segment; the answer opens a new block below it", () => {
		const { session, events } = harness()
		session.handleNotification({ type: "message.start", payload: { message_id: "m1" } })
		session.handleNotification({ type: "message.delta", payload: { text: "before", content_index: 0 } })
		session.handleNotification({ type: "tool.start", payload: { tool_id: "t1", name: "terminal" } })
		session.handleNotification({ type: "message.delta", payload: { text: "after", content_index: 0 } })
		session.handleNotification({
			type: "message.complete",
			payload: { message_id: "m1", text: "beforeafter", status: "complete" }
		})

		const ends = events.filter(
			(event): event is Extract<AgentSessionEvent, { type: "message_end" }> => event.type === "message_end"
		)
		const texts = ends.map((event) => {
			if (event.message.role !== "assistant") throw new Error("expected assistant")
			return { text: event.message.content.at(-1), stop: event.message.stopReason }
		})
		expect(texts).toEqual([
			{ text: { type: "text", text: "before" }, stop: "toolUse" },
			{ text: { type: "text", text: "after" }, stop: "stop" }
		])
	})

	test("a complete-only answer after a tool renders only the unshown suffix", () => {
		const { session, events } = harness()
		session.handleNotification({ type: "message.start", payload: { message_id: "m1" } })
		session.handleNotification({ type: "message.delta", payload: { text: "before", content_index: 0 } })
		session.handleNotification({ type: "tool.start", payload: { tool_id: "t1", name: "terminal" } })
		session.handleNotification({
			type: "message.complete",
			payload: { message_id: "m1", text: "beforeafter", status: "complete" }
		})

		const ends = events.filter(
			(event): event is Extract<AgentSessionEvent, { type: "message_end" }> => event.type === "message_end"
		)
		const last = ends.at(-1)!
		if (last.message.role !== "assistant") throw new Error("expected assistant")
		expect(last.message.content.at(-1)).toEqual({ type: "text", text: "after" })
	})

	test("prompt.result settles the turn with agent_end", () => {
		const { session, events } = harness()
		session.handleNotification({
			type: "prompt.result",
			payload: { admission_id: "a1", agent_invoked: true, status: "completed" }
		})
		expect(events.some((event) => event.type === "agent_end")).toBe(true)
		expect(session.running).toBe(false)
	})
})

describe("tool projection", () => {
	test("a JSON-string result becomes AgentToolResult content + details", () => {
		const { session, events } = harness()
		session.handleNotification({
			type: "tool.complete",
			payload: {
				tool_id: "t1",
				name: "terminal",
				result: JSON.stringify({ content: [{ type: "text", text: "output" }] }),
				result_text: "output",
				duration_s: 1.2,
				summary: "ls",
				is_error: false
			}
		})
		const end = events.find(
			(event): event is Extract<AgentSessionEvent, { type: "tool_execution_end" }> =>
				event.type === "tool_execution_end"
		)!
		const result = end.result as { content: { type: string; text?: string }[]; details?: Record<string, unknown> }
		expect(result.content).toEqual([{ type: "text", text: "output" }])
		expect(result.details).toMatchObject({ summary: "ls", duration_s: 1.2 })
		expect(end.isError).toBe(false)
	})

	test("tool.progress carries a content block, not a bare progress object", () => {
		const { session, events } = harness()
		session.handleNotification({
			type: "tool.progress",
			payload: { tool_id: "t1", name: "terminal", progress: 1, total: 3, message: "step 1" }
		})
		const update = events.find(
			(event): event is Extract<AgentSessionEvent, { type: "tool_execution_update" }> =>
				event.type === "tool_execution_update"
		)!
		expect(update.partialResult).toMatchObject({ content: [{ type: "text", text: "step 1" }] })
	})

	test("a todo tool result exposes details.phases for the HUD", () => {
		const { session, events } = harness()
		session.handleNotification({
			type: "tool.complete",
			payload: {
				tool_id: "t1",
				name: "todo",
				result: "{}",
				todos: [
					{ content: "write tests", status: "in_progress" },
					{ content: "ship", status: "done" }
				]
			}
		})
		const end = events.find(
			(event): event is Extract<AgentSessionEvent, { type: "tool_execution_end" }> =>
				event.type === "tool_execution_end"
		)!
		const details = (end.result as { details?: { op?: string; phases?: unknown } }).details
		expect(details?.op).toBe("init")
		expect(details?.phases).toEqual([
			{
				name: "Tasks",
				tasks: [
					{ content: "write tests", status: "in_progress" },
					{ content: "ship", status: "completed" }
				]
			}
		])
	})
})

describe("session state", () => {
	test("session.info adopts the live execution generation used by interrupt/mutate", () => {
		const { session } = harness()
		session.handleNotification({
			type: "session.info",
			payload: { model: "m-2", provider: "p-2", running: true, execution_generation: 9, revision: 11 }
		})
		expect(session.executionGeneration).toBe(9)
		expect(session.revision).toBe(11)
		expect(session.model).toBe("m-2")
		expect(session.running).toBe(true)
	})

	test("a session.info model change repaints the status line, an unchanged one does not", () => {
		const { session, events } = harness({ model: "m-1", provider: "p-1" })
		session.handleNotification({
			type: "session.info",
			payload: { model: "m-2", provider: "p-2", execution_generation: 9, revision: 11 }
		})
		expect(session.model).toBe("m-2")
		expect(events.filter((event) => event.type === "model_changed")).toHaveLength(1)

		session.handleNotification({
			type: "session.info",
			payload: { model: "m-2", provider: "p-2", execution_generation: 9, revision: 11 }
		})
		expect(events.filter((event) => event.type === "model_changed")).toHaveLength(1)
	})

	test("usage maps Hermes snake_case counters onto the vendored Usage shape", () => {
		expect(
			toOmpUsage({
				input: 10,
				output: 3,
				cache_read: 7,
				cache_write: 1,
				total: 21,
				reasoning: 2,
				context_used: 100,
				cost_usd: 0.5
			} as never)
		).toMatchObject({
			input: 10,
			output: 3,
			cacheRead: 7,
			cacheWrite: 1,
			totalTokens: 21,
			reasoningTokens: 2,
			contextTokens: 100,
			cost: { total: 0.5 }
		})
	})

	test("queue.updated becomes a queue_update event", () => {
		const { session, events } = harness()
		session.handleNotification({
			type: "queue.updated",
			payload: { steering: ["a"], follow_up: ["b"], live_steered: 1 }
		})
		expect(events.at(-1)).toEqual({ type: "queue_update", steering: ["a"], followUp: ["b"], liveSteered: 1 })
		expect(session.queue).toEqual({ steering: ["a"], followUp: ["b"], liveSteered: 1 })
	})

	test("todo rows project onto named phases", () => {
		expect(
			gatewayTodoPhases([
				{ content: "a", status: "pending", phase: "Build" },
				{ text: "b", status: "done", phase: "Build" },
				{ title: "c", status: "blocked" }
			])
		).toEqual([
			{
				name: "Build",
				tasks: [
					{ content: "a", status: "pending" },
					{ content: "b", status: "completed" }
				]
			},
			{ name: "Tasks", tasks: [{ content: "c", status: "blocked" }] }
		])
	})
})

describe("snapshot", () => {
	test("create snapshot uses the display projection and the live info", () => {
		const snapshot = GatewayOmpSession.snapshotFrom({
			session_id: "sid",
			stored_session_id: "stored",
			messages: [{ role: "user", text: "stored" }],
			display_messages: [{ role: "user", text: "display" }],
			message_count: 1,
			running: true,
			authority_epoch: 4,
			replay_epoch: "r",
			last_sequence: 0,
			subscription_id: "sub",
			revision: 5,
			execution_generation: 6,
			pending: [],
			prompts: [],
			info: { model: "m", provider: "p", cwd: "/w", title: "T" },
			todo_state: { todos: [{ content: "x" }], revision: 1 }
		} as never)
		expect(snapshot).toMatchObject({
			sessionId: "sid",
			storedSessionId: "stored",
			model: "m",
			provider: "p",
			cwd: "/w",
			title: "T",
			revision: 5,
			executionGeneration: 6,
			running: true
		})
		expect(snapshot.messages).toEqual([{ role: "user", text: "display" }])
		expect(snapshot.todos).toHaveLength(1)
	})
})

describe("custom row projection", () => {
	test("an advisor row reads its details and never parses the note text", () => {
		const message = GatewayOmpSession.rowToAgentMessage({
			role: "custom",
			text: "[advisor — advisory only]\n- [concern · reviewer] re-read the failing test\n[end advisor]",
			custom_type: "advisor",
			display: true,
			details: { notes: [{ note: "re-read the failing test", severity: "concern", advisor: "reviewer" }] },
			attribution: "agent",
			timestamp: 1
		} as never)
		expect(message?.role).toBe("custom")
		if (message?.role !== "custom") throw new Error("expected custom")
		expect(message.customType).toBe("advisor")
		expect(message.details).toEqual({
			notes: [{ note: "re-read the failing test", severity: "concern", advisor: "reviewer" }]
		})
	})

	test("a row without details renders the generic custom block, not parsed text", () => {
		const message = GatewayOmpSession.rowToAgentMessage({
			role: "custom",
			text: "- [nit] guessed from text",
			custom_type: "advisor",
			timestamp: 1
		} as never)
		if (message?.role !== "custom") throw new Error("expected custom")
		expect(message.details).toBeUndefined()
	})

	test("a skill-prompt row keeps the gateway's name, path, args and prompt", () => {
		const message = GatewayOmpSession.rowToAgentMessage({
			role: "custom",
			text: "/skill:review src/main.ts",
			custom_type: "skill-prompt",
			details: { name: "review", path: "/skills/review.md", args: "src/main.ts", prompt: "body", lineCount: 3 },
			attribution: "user",
			timestamp: 1
		} as never)
		if (message?.role !== "custom") throw new Error("expected custom")
		expect(message.customType).toBe("skill-prompt")
		expect(message.details).toMatchObject({
			name: "review",
			path: "/skills/review.md",
			args: "src/main.ts",
			prompt: "body"
		})
	})

	test("a stripped-tool-calls row keeps its count", () => {
		const message = GatewayOmpSession.rowToAgentMessage({
			role: "custom",
			text: "",
			custom_type: "stripped-tool-calls",
			display: true,
			details: { count: 2 },
			attribution: "agent",
			timestamp: 1
		} as never)
		if (message?.role !== "custom") throw new Error("expected custom")
		expect(message.customType).toBe("stripped-tool-calls")
		expect(message.details).toEqual({ count: 2 })
	})

	test("display:false still projects; the renderer decides visibility", () => {
		const message = GatewayOmpSession.rowToAgentMessage({
			role: "custom",
			text: "internal marker",
			custom_type: "internal-notification",
			display: false,
			details: { level: "info", source: "gateway" },
			timestamp: 1
		} as never)
		if (message?.role !== "custom") throw new Error("expected custom")
		expect(message.display).toBe(false)
	})

	test("steer rows remain user messages with the live-steer flag", () => {
		const message = GatewayOmpSession.rowToAgentMessage({
			role: "user",
			text: "change direction",
			synthetic_kind: "steer",
			live_steered: true,
			timestamp: 1
		} as never)
		expect(message).toMatchObject({
			role: "user",
			content: "change direction",
			liveSteered: true
		})
	})

	test("rows without a custom shape never invent a custom block", () => {
		const message = GatewayOmpSession.rowToAgentMessage({
			role: "user",
			text: "switched to m-2",
			synthetic_kind: "auto_continue",
			timestamp: 1
		} as never)
		expect(message).toMatchObject({ role: "user", content: "switched to m-2" })
	})

	test("assistant reasoning sidecars become thinking blocks", () => {
		const message = GatewayOmpSession.rowToAgentMessage({
			role: "assistant",
			text: "answer",
			reasoning: "why",
			timestamp: 1
		} as never)
		if (message?.role !== "assistant") throw new Error("expected assistant")
		expect(message.content).toEqual([
			{ type: "thinking", thinking: "why" },
			{ type: "text", text: "answer" }
		])
	})

	test("tool rows keep args, labels and structured content", () => {
		const message = GatewayOmpSession.rowToAgentMessage({
			role: "tool",
			name: "read_file",
			tool_call_id: "t1",
			text: "body",
			content: [{ type: "text", text: "body" }],
			args: { path: "a.ts" },
			labels: [{ kind: "tool", app: "files", action: "read", emoji: "", text: "Read", name: "read_file" }],
			is_error: false,
			timestamp: 1
		} as never)
		if (message?.role !== "toolResult") throw new Error("expected toolResult")
		expect(message.toolName).toBe("read_file")
		expect(message.content).toEqual([{ type: "text", text: "body" }])
		expect(message.isError).toBe(false)
		expect(message.details).toMatchObject({
			args: { path: "a.ts" },
			labels: [{ name: "read_file" }]
		})
	})

	test("tool rows carry display-safe content and the durable error verdict", () => {
		const message = GatewayOmpSession.rowToAgentMessage({
			role: "tool",
			name: "browser_exec",
			tool_call_id: "t2",
			content: [
				{ type: "text", text: "shot" },
				{ type: "image", data: "AAAA", mimeType: "image/png" }
			],
			is_error: true,
			row_id: 9,
			timestamp: 1
		} as never)
		if (message?.role !== "toolResult") throw new Error("expected toolResult")
		expect(message.toolName).toBe("browser_exec")
		expect(message.content).toEqual([
			{ type: "text", text: "shot" },
			{ type: "image", data: "AAAA", mimeType: "image/png" }
		])
		expect(message.isError).toBe(true)
		expect(message.details).toMatchObject({ row_id: 9 })
	})

	test("a custom timeline row keeps its gateway custom type", () => {
		const message = GatewayOmpSession.rowToAgentMessage({
			role: "custom",
			text: "switched to m-2",
			custom_type: "model-switch",
			display_only: true,
			details: { level: "info", source: "model_switch" },
			timestamp: 1
		} as never)
		if (message?.role !== "custom") throw new Error("expected custom")
		expect(message.customType).toBe("model-switch")
		expect(message.details).toEqual({ level: "info", source: "model_switch" })
	})
})

describe("history and session params", () => {
	test("history opts into full tool content and the selector outline", async () => {
		const { session, sent, client } = harness()
		const pending = session.history(50)
		const request = sent.at(-1)!
		expect(request).toMatchObject({
			method: "session.history",
			params: {
				session_id: "sid-1",
				limit: 50,
				include_tool_content: "full",
				include_outline: true
			}
		})
		client.handleFrame(
			JSON.stringify({ jsonrpc: "2.0", id: request.id, result: { count: 0, messages: [], entries: [] } })
		)
		await pending
	})

	test("create and resume send the omp-ui opt-ins", () => {
		expect(gatewayCreateParams({ cwd: "/w", title: "T" })).toMatchObject({
			source: "tui",
			client: { name: "omp-ui" },
			custom_messages: true,
			cwd: "/w",
			title: "T"
		})
		expect(gatewayResumeParams("sid")).toEqual({
			session_id: "sid",
			display_projection: true,
			include_tool_content: "full",
			include_compaction: true
		})
	})
})

describe("compaction summary", () => {
	test("a resumed row reads the counters and method from details.compaction", () => {
		const message = GatewayOmpSession.rowToAgentMessage({
			role: "compaction_summary",
			text: "summary body",
			row_id: 7,
			timestamp: 11,
			details: {
				compaction: {
					tokens_before: 256000,
					tokens_after: 20000,
					token_method: "estimate_rough",
					method: "llm_summary",
					boundary_row_id: 7
				}
			}
		} as TranscriptMessage)
		expect(message).toEqual({
			role: "compactionSummary",
			summary: "summary body",
			tokensBefore: 256000,
			tokensAfter: 20000,
			method: "llm_summary",
			timestamp: 11
		})
	})

	test("a pre-stamp row has no badge", () => {
		const message = GatewayOmpSession.rowToAgentMessage({
			role: "compaction_summary", text: "old summary", timestamp: 1
		} as TranscriptMessage)
		if (message?.role !== "compactionSummary") throw new Error("expected a compaction row")
		expect(message.tokensBefore).toBe(0)
		expect(message.tokensAfter).toBeUndefined()
		expect(message.method).toBeUndefined()
	})

	test("the live event appends one entry and merges a repeated boundary", () => {
		const { session, events } = harness()
		const payload = {
			text: "summary body",
			tokens_before: 256000,
			tokens_after: 20000,
			token_method: "estimate_rough",
			method: "llm_summary",
			trigger: "pre_api",
			timestamp: 12,
			boundary_row_id: 9
		}
		session.handleNotification({ type: "compaction.summary", payload })
		session.handleNotification({ type: "compaction.summary", payload })

		const emitted = events.filter((event) => event.type === "compaction_summary")
		expect(emitted).toHaveLength(1)
		expect(session.messages.filter((message) => message.role === "compactionSummary")).toHaveLength(1)
		const message = (emitted[0] as Extract<AgentSessionEvent, { type: "compaction_summary" }>).message
		expect(message).toMatchObject({
			role: "compactionSummary", summary: "summary body", tokensBefore: 256000, tokensAfter: 20000,
			method: "llm_summary", timestamp: 12
		})
	})

	test("an empty event is ignored; an unanchored one still renders once", () => {
		const { session, events } = harness()
		session.handleNotification({ type: "compaction.summary", payload: { text: "", tokens_before: 1 } })
		session.handleNotification({
			type: "compaction.summary",
			payload: { text: "no row", tokens_before: 1, boundary_row_id: null }
		})
		expect(events.filter((event) => event.type === "compaction_summary")).toHaveLength(1)
		expect(session.messages.filter((message) => message.role === "compactionSummary")).toHaveLength(1)
	})
})

describe("gateway block events", () => {
	test("message.advisor emits one custom message pair from its details", () => {
		const { session, events } = harness()
		session.handleNotification({
			type: "message.advisor",
			payload: {
				id: "a1",
				text: "[advisor — advisory only]\n- [nit] tighten the loop\n[end advisor]",
				severity: "nit",
				label: "nit",
					row_id: 41,
				details: { notes: [{ note: "tighten the loop", severity: "nit" }] }
			}
		})
		const start = events.find((event) => event.type === "message_start")
		const end = events.find((event) => event.type === "message_end")
		expect(start?.message.role).toBe("custom")
		expect(end?.message.role).toBe("custom")
		if (start?.message.role !== "custom") throw new Error("expected custom")
		expect(start.message.customType).toBe("advisor")
		expect(start.message.details).toMatchObject({ notes: [{ note: "tighten the loop", severity: "nit" }] })
	})

	test("an advisor row already in the snapshot is not appended twice", () => {
		const snapshotRow = {
			role: "custom",
			text: "advice",
			custom_type: "advisor",
			display: true,
			details: { notes: [{ note: "advice" }] },
			row_id: 41,
			timestamp: 1
		}
		const { session, events } = harness({ messages: [snapshotRow as never] })
		session.handleNotification({
			type: "message.advisor",
			payload: { id: "a1", text: "advice", row_id: 41, details: { notes: [{ note: "advice" }] } }
		})
		expect(events.filter((event) => event.type === "message_end")).toHaveLength(0)
		expect(session.initialMessages()).toHaveLength(1)
	})

	test("notification.show becomes a notice; agent.print is ignored", () => {
		const { session, events } = harness()
		session.handleNotification({
			type: "notification.show",
			payload: { text: "low disk", level: "warning", kind: "agent" }
		})
		session.handleNotification({ type: "agent.print", payload: { text: "compacted" } })
		expect(events.filter((event) => event.type === "notice")).toEqual([
			{ type: "notice", level: "warning", message: "low disk", source: "agent" }
		])
	})

	test("transcript.notice is deduped by id and ignores the legacy channels", () => {
		const { session, events } = harness()
		session.handleNotification({ type: "agent.print", payload: { text: "compacting context" } })
		session.handleNotification({
			type: "status.update",
			payload: { kind: "lifecycle", text: "compacting context" }
		})
		const payload = { id: "n1", level: "info", text: "compacting context", source: "agent" }
		session.handleNotification({ type: "transcript.notice", payload })
		session.handleNotification({ type: "transcript.notice", payload })
		expect(events.filter((event) => event.type === "notice")).toEqual([
			{ type: "notice", level: "info", message: "compacting context", source: "agent" }
		])
	})

	test("a transcript.notice whose row is already rendered is not repeated", () => {
		const snapshotRow = {
			role: "custom",
			text: "switched",
			custom_type: "model-switch",
			display: true,
			details: { level: "info", source: "model-switch" },
			row_id: 9,
			timestamp: 1
		}
		const { session, events } = harness({ messages: [snapshotRow as never] })
		session.handleNotification({
			type: "transcript.notice",
			payload: { id: "n2", level: "info", text: "switched", source: "agent", row_id: 9 }
		})
		expect(events.filter((event) => event.type === "notice")).toHaveLength(0)
	})

	test("a failed resume without a message names its phase", () => {
		const { session, events } = harness()
		session.handleNotification({
			type: "session.resume_progress",
			payload: { phase: "hydrate", status: "failed" }
		})
		expect(events.find((event) => event.type === "notice")).toMatchObject({
			level: "error",
			message: "Session resume failed (hydrate)"
		})
	})

	test("todo.reminder merges the live card by its durable row id", () => {
		const { session, events } = harness()
		const payload = {
			todos: [{ content: "ship", status: "pending" }],
			attempt: 1,
			max_attempts: 3,
			row_id: 12
		}
		session.handleNotification({ type: "todo.reminder", payload })
		session.handleNotification({ type: "todo.reminder", payload })
		const ends = events.filter((event) => event.type === "message_end")
		expect(ends).toHaveLength(1)
		const end = ends[0]!
		if (end.message.role !== "custom") throw new Error("expected custom")
		expect(end.message.customType).toBe("todo-reminder")
		expect(end.message.details).toMatchObject({
			todos: [{ content: "ship", status: "pending" }],
			attempt: 1,
			maxAttempts: 3
		})
	})

	test("transcript.custom merges the live row by row_id and projects custom details", () => {
		const { session, events } = harness()
		const payload = {
			message: {
				role: "custom",
				text: "<system-notice>Diagnostics for the files you edited</system-notice>",
				custom_type: "lsp-late-diagnostic",
				display: true,
				display_only: false,
				attribution: "agent",
				details: {
					files: [{ path: "C:/w/a.py", summary: "1 error(s)", errored: true, messages: ["C:/w/a.py:1:1 [error] [ty] boom"] }]
				},
				row_id: 77,
				timestamp: 2
			}
		}
		session.handleNotification({ type: "transcript.custom", payload })
		session.handleNotification({ type: "transcript.custom", payload })
		const ends = events.filter((event) => event.type === "message_end")
		expect(ends).toHaveLength(1)
		const end = ends[0]!
		if (end.message.role !== "custom") throw new Error("expected custom")
		expect(end.message.customType).toBe("lsp-late-diagnostic")
		expect(end.message.details).toMatchObject({
			files: [{ path: "C:/w/a.py", summary: "1 error(s)", errored: true }]
		})
	})

	test("a transcript.custom row already in the snapshot is not appended twice", () => {
		const snapshotRow = {
			role: "custom",
			text: "diagnostics",
			custom_type: "lsp-late-diagnostic",
			display: true,
			details: { files: [] },
			row_id: 77,
			timestamp: 1
		}
		const { session, events } = harness({ messages: [snapshotRow as never] })
		session.handleNotification({
			type: "transcript.custom",
			payload: { message: { ...snapshotRow, text: "diagnostics again" } }
		})
		expect(events.filter((event) => event.type === "message_end")).toHaveLength(0)
		expect(session.initialMessages()).toHaveLength(1)
	})

	test("step_usage feeds the assistant message, not the cumulative usage", () => {
		const { session, events } = harness()
		session.handleNotification({ type: "message.start", payload: { message_id: "m1" } })
		session.handleNotification({ type: "message.delta", payload: { text: "hi", content_index: 0 } })
		session.handleNotification({
			type: "message.complete",
			payload: {
				message_id: "m1",
				text: "hi",
				status: "complete",
				usage: { input: 900, output: 300, total: 1200, cost_usd: 9 },
				step_usage: { input: 10, output: 2, cache_read: 7, cache_write: 1, total: 21, cost_usd: 0.5 },
				served_model: "m-served",
				requested_model: "m-1"
			}
		})
		const end = events.find((event) => event.type === "message_end")
		if (end?.message.role !== "assistant") throw new Error("expected assistant")
		expect(end.message.usage).toMatchObject({
			input: 10,
			output: 2,
			cacheRead: 7,
			cacheWrite: 1,
			totalTokens: 21,
			cost: { total: 0.5 }
		})
		expect(end.message.upstreamModel).toBe("m-served")
	})

	test("a completion without step_usage does not borrow the cumulative usage", () => {
		const { session, events } = harness()
		session.handleNotification({ type: "message.start", payload: { message_id: "m1" } })
		session.handleNotification({
			type: "message.complete",
			payload: {
				message_id: "m1",
				text: "hi",
				status: "complete",
				usage: { input: 900, output: 300, total: 1200 }
			}
		})
		const end = events.find((event) => event.type === "message_end")
		if (end?.message.role !== "assistant") throw new Error("expected assistant")
		expect(end.message.usage).toMatchObject({ input: 0, output: 0, totalTokens: 0 })
		expect(end.message.upstreamModel).toBeUndefined()
	})

	test("tool.start.step_usage labels the assistant segment it closes", () => {
		const { session, events } = harness()
		session.handleNotification({ type: "message.start", payload: { message_id: "m1" } })
		session.handleNotification({ type: "message.delta", payload: { text: "checking", content_index: 0 } })
		session.handleNotification({
			type: "tool.start",
			payload: {
				tool_id: "c1",
				name: "read_file",
				args: {},
				step_usage: { input: 20, output: 4, cache_read: 16, cache_write: 2, total: 26 }
			}
		})
		const end = events.find((event) => event.type === "message_end")
		if (end?.message.role !== "assistant") throw new Error("expected assistant")
		expect(end.message.stopReason).toBe("toolUse")
		expect(end.message.usage).toMatchObject({
			input: 20,
			output: 4,
			cacheRead: 16,
			cacheWrite: 2,
			totalTokens: 26
		})
	})

	test("a tool-only step carries step_usage on tool_execution_start", () => {
		const { session, events } = harness()
		session.handleNotification({
			type: "tool.start",
			payload: {
				tool_id: "c1",
				name: "read_file",
				args: {},
				step_usage: { input: 20, output: 4, cache_read: 16, cache_write: 2, total: 26 }
			}
		})
		const start = events.find((event) => event.type === "tool_execution_start")
		if (start?.type !== "tool_execution_start") throw new Error("expected tool start")
		expect(start.stepUsage).toMatchObject({ input: 20, output: 4, totalTokens: 26 })
		expect(events.some((event) => event.type === "message_end")).toBe(false)
	})

	test("the capability table is the only seat authority for optional blocks", () => {
		const { session, events } = harness()
		// Not advertised yet: legacy behavior keeps the live channels working.
		expect(session.transcriptBlock("notice")).toBeUndefined()
		expect(session.transcriptBlock("recap")).toBeUndefined()
		session.adoptTranscriptBlocks({
			advisor: { supported: true, status: "ready" },
			notice: { supported: false, status: "unsupported", needs: "a notice producer" },
			todo_reminder: { supported: true, status: "ready" }
		})
		expect(session.transcriptBlock("advisor")?.supported).toBe(true)
		expect(session.transcriptBlock("notice")).toMatchObject({ status: "unsupported", needs: "a notice producer" })

		session.handleNotification({ type: "transcript.notice", payload: { id: "n1", text: "hidden", level: "info" } })
		expect(events.some((event) => event.type === "notice")).toBe(false)

		// A remount replaces the table: the block is unadvertised again, never faked.
		session.adoptTranscriptBlocks(undefined)
		expect(session.transcriptBlock("notice")).toBeUndefined()
	})

	test("a snapshot role row renders even when its subtype block is unsupported", () => {
		const { session } = harness()
		const row = {
			role: "custom",
			custom_type: "advisor",
			text: "advice",
			display: true,
			timestamp: 1
		} as unknown as TranscriptMessage
		session.adoptTranscriptBlocks({ advisor: { supported: false, status: "unsupported", needs: "x" } })
		session.adoptSnapshot(SNAPSHOT({ messages: [row] }))
		expect(session.initialMessages()).toHaveLength(1)
	})

	test("live non-role blocks stay unmounted while their capability is unsupported", () => {
		const { session, events } = harness()
		session.adoptTranscriptBlocks({
			advisor: { supported: false, status: "unsupported", needs: "x" },
			todo_reminder: { supported: false, status: "unsupported", needs: "y" }
		})
		session.handleNotification({
			type: "message.advisor",
			payload: { text: "advice", details: { notes: [{ note: "advice" }] }, row_id: 3 }
		})
		session.handleNotification({
			type: "todo.reminder",
			payload: { todos: [{ content: "ship", status: "pending" }], attempt: 1, max_attempts: 3, row_id: 4 }
		})
		expect(session.messages).toEqual([])
		expect(events).toEqual([])
	})
})

describe("compaction state", () => {
	test("status edges are projected once per transition for the idle recap timer", () => {
		const { session, events } = harness()
		session.handleNotification({ type: "status.update", payload: { kind: "compacting" } })
		session.handleNotification({ type: "status.update", payload: { kind: "compacting" } })
		session.handleNotification({ type: "status.update", payload: { kind: "compacted" } })
		session.handleNotification({ type: "status.update", payload: { kind: "compressing" } })
		session.handleNotification({ type: "status.update", payload: { kind: "ready" } })
		expect(events).toEqual([
			{ type: "compaction_state_changed", compacting: true },
			{ type: "compaction_state_changed", compacting: false },
			{ type: "compaction_state_changed", compacting: true },
			{ type: "compaction_state_changed", compacting: false }
		])
		expect(session.compacting).toBe(false)
	})
})

describe("ttsr projection", () => {
	test("a matched rule becomes a shell-owned ttsr_triggered event", () => {
		const { session, events } = harness()
		session.handleNotification({
			type: "ttsr.triggered",
			payload: { rules: [{ name: "ts-no-any", description: "d", content: "c" }] }
		})
		expect(events).toEqual([
			{ type: "ttsr_triggered", rules: [{ name: "ts-no-any", description: "d", content: "c" }] }
		])
	})

	test("rules without a usable name are dropped, not rendered as an empty notice", () => {
		const { session, events } = harness()
		session.handleNotification({
			type: "ttsr.triggered",
			payload: { rules: [{ description: "no name" }, { name: "ok" }] }
		})
		expect(events).toEqual([{ type: "ttsr_triggered", rules: [{ name: "ok", description: undefined, content: undefined }] }])
	})
})

describe("execute_code eval card (ADR-4 D12/D13/D15)", () => {
	test("multi-cell results project language, status, exit/duration, views, images and meta", () => {
		const args = {
			cells: [
				{ code: "print(1 + 2)", language: "py" },
				{ code: "const x = 1", language: "js", title: "JS cell" }
			]
		}
		const decoded = {
			status: "error",
			output: "3\nboom",
			stdout_truncated: true,
			stdout_bytes_total: 1000,
			stdout_bytes_captured: 200,
			stdout_bytes_omitted: 800,
			stdout_lines_total: 20,
			stdout_lines_shown: 4,
			display: { images: [{ type: "image", data: "AA", mimeType: "image/png" }] },
			cells: [
				{
					index: 0,
					code: "print(1 + 2)",
					language: "py",
					status: "success",
					output: "3\n",
					exit_code: 0,
					duration_seconds: 0.25,
					display: {
						views: [{ text: "3", view: { kind: "markdown", text: "3" } }],
						images: [{ type: "image", data: "BB", mimeType: "image/png" }]
					}
				},
				{
					index: 1,
					code: "const x = 1",
					language: "js",
					title: "JS cell",
					status: "timeout",
					output: "boom",
					exit_code: -1
				}
			]
		}

		const details = executeCodeDetails(args, decoded, {
			cellTitles: ["Print one", "JS cell"],
			toolCallId: "t1"
		})!

		expect(details.languages).toEqual(["python", "js"])
		expect(details.language).toBe("python")
		expect(details.cells).toHaveLength(2)
		expect(details.cells![0]).toMatchObject({
			title: "Print one",
			language: "python",
			status: "complete",
			output: "3",
			durationMs: 250,
			exitCode: 0
		})
		expect(details.cells![0]!.views).toEqual([{ text: "3", view: { kind: "markdown", text: "3" } }])
		// timeout/nterrupted/negative exits never claim an exit code.
		expect(details.cells![1]).toMatchObject({ title: "JS cell", language: "js", status: "error" })
		expect(details.cells![1]!.exitCode).toBeUndefined()
		// Top-level aggregate images plus each cell's own display block.
		expect(details.images).toEqual([
			{ type: "image", data: "AA", mimeType: "image/png" },
			{ type: "image", data: "BB", mimeType: "image/png" }
		])
		expect(details.meta?.truncation).toEqual({
			direction: "middle",
			truncatedBy: "bytes",
			totalBytes: 1000,
			outputBytes: 200,
			elidedBytes: 800,
			totalLines: 20,
			outputLines: 4
		})
		expect(details.notice).toContain('agent_out(mode="artifact", kind="tool", tool_call_id="t1")')
	})

	test("a single cell synthesizes one cell from args and omits an unrecognizable result", () => {
		const details = executeCodeDetails({ code: "print(1)" }, { status: "success", output: "1\n" })!
		expect(details.cells).toHaveLength(1)
		expect(details.cells![0]).toMatchObject({ code: "print(1)", status: "complete", output: "1" })
		expect(executeCodeDetails({}, { nope: true })).toBeUndefined()
	})

	test("is_error is the call verdict; a cell with its own status keeps it", () => {
		const details = executeCodeDetails({ code: "exit(1)" }, { status: "success", output: "" }, { isError: true })!
		expect(details.cells![0]!.status).toBe("complete")
		expect(details.isError).toBe(true)
	})

	test("live tool.output coalesces into a running partial card with the same cell shape", async () => {
		const { session, events } = harness()
		session.handleNotification({
			type: "tool.start",
			payload: {
				tool_id: "t1",
				name: "execute_code",
				args: { code: "print(1)" },
				title: "Print one",
				cell_titles: ["Print one"]
			}
		})
		session.handleNotification({
			type: "tool.output",
			payload: { tool_id: "t1", name: "execute_code", cell_index: 0, stream: "stdout", text: "x".repeat(4096) }
		})

		const start = events.find((event) => event.type === "tool_execution_start")!
		expect(start).toMatchObject({ title: "Print one", cellTitles: ["Print one"] })
		const update = events.find(
			(event): event is Extract<AgentSessionEvent, { type: "tool_execution_update" }> =>
				event.type === "tool_execution_update"
		)!
		const details = update.partialResult.details as { cells: { status: string; output: string }[] }
		expect(details.cells[0]).toMatchObject({ status: "running" })
		expect(details.cells[0]!.output).toHaveLength(4096)
	})

	test("the live text fallback is the cell output, not the wrapper JSON", () => {
		const { session, events } = harness()
		session.handleNotification({
			type: "tool.start",
			payload: { tool_id: "t1", name: "execute_code", args: { code: "print(1 + 2)" } }
		})
		session.handleNotification({
			type: "tool.complete",
			payload: {
				tool_id: "t1",
				name: "execute_code",
				result: JSON.stringify({ status: "success", output: "3\n" })
			}
		})
		const end = events.find(
			(event): event is Extract<AgentSessionEvent, { type: "tool_execution_end" }> =>
				event.type === "tool_execution_end"
		)!
		expect(end.result.content).toEqual([{ type: "text", text: "3\n" }])
	})

	test("tool.title projects a shell event for the pending card and one cell", () => {
		const { session, events } = harness()
		session.handleNotification({
			type: "tool.title",
			payload: { tool_id: "t1", name: "execute_code", title: "Print one", cell_index: 0 }
		})
		expect(events).toEqual([{ type: "tool_title", toolCallId: "t1", title: "Print one", cellIndex: 0 }])
	})

	test("a resumed execute_code row projects the durable sidecar into the same cells", () => {
		const row = {
			role: "tool" as const,
			name: "execute_code",
			tool_call_id: "t1",
			content: JSON.stringify({ status: "success", output: "3\n" }),
			args: { code: "print(1 + 2)" },
			row_id: 7,
			details: {
				display: { views: [{ text: "3", view: { kind: "markdown", text: "3" } }] },
				card_title: "Print one",
				cell_titles: ["Print one"]
			}
		}
		const message = GatewayOmpSession.rowToAgentMessage(row as never)
		if (message?.role !== "toolResult") throw new Error("expected tool result")
		const details = message.details as { cells: { title?: string; status: string; views?: unknown[] }[] }
		expect(details.cells).toHaveLength(1)
		expect(details.cells[0]).toMatchObject({ title: "Print one", status: "complete" })
		expect(details.cells[0]!.views).toHaveLength(1)
	})

	test("without a result body (include_tool_content none) the row keeps args and builds no cells", () => {
		const message = GatewayOmpSession.rowToAgentMessage({
			role: "tool",
			name: "execute_code",
			tool_call_id: "t1",
			args: { code: "print(1)" },
			details: { card_title: "Print one" }
		} as never)
		if (message?.role !== "toolResult") throw new Error("expected tool result")
		const details = message.details as { cells?: unknown; args?: unknown; card_title?: string }
		expect(details.cells).toBeUndefined()
		expect(details.args).toEqual({ code: "print(1)" })
		expect(details.card_title).toBe("Print one")
	})
})
