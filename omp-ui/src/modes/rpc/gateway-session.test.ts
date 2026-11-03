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
			text: "[adviser — advisory only]\n- [concern · reviewer] re-read the failing test\n[end adviser]",
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
			include_tool_content: "full"
		})
	})
})

describe("gateway block events", () => {
	test("message.adviser emits one custom message pair from its details", () => {
		const { session, events } = harness()
		session.handleNotification({
			type: "message.adviser",
			payload: {
				id: "a1",
				text: "[adviser — advisory only]\n- [nit] tighten the loop\n[end adviser]",
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

	test("an adviser row already in the snapshot is not appended twice", () => {
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
			type: "message.adviser",
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
			adviser: { supported: true, status: "ready" },
			notice: { supported: false, status: "unsupported", needs: "a notice producer" },
			todo_reminder: { supported: true, status: "ready" }
		})
		expect(session.transcriptBlock("adviser")?.supported).toBe(true)
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
		session.adoptTranscriptBlocks({ adviser: { supported: false, status: "unsupported", needs: "x" } })
		session.adoptSnapshot(SNAPSHOT({ messages: [row] }))
		expect(session.initialMessages()).toHaveLength(1)
	})

	test("live non-role blocks stay unmounted while their capability is unsupported", () => {
		const { session, events } = harness()
		session.adoptTranscriptBlocks({
			adviser: { supported: false, status: "unsupported", needs: "x" },
			todo_reminder: { supported: false, status: "unsupported", needs: "y" }
		})
		session.handleNotification({
			type: "message.adviser",
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
