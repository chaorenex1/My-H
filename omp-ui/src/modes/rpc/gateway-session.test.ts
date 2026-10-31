/**
 * The main-screen projections: gateway notifications → `AgentSessionEvent`, and the canonical
 * snapshot → the shell's message list. Envelope mechanics live in `gateway-client.test.ts`; this file
 * pins the business mapping the shell renders.
 */

import { describe, expect, test } from "bun:test"

import type { AgentSessionEvent } from "../../session/agent-session-events"
import { GatewayClient } from "./gateway-client"
import {
	GatewayOmpSession,
	type GatewaySessionSnapshot,
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
