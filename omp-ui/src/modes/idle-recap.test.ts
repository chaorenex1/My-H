/**
 * ADR-5 P4: the client-side idle recap timer. The authority's `recap.generate` guards and the
 * side-turn itself are covered server-side; these tests pin when this client asks at all.
 */

import { afterEach, describe, expect, test, vi } from "bun:test"

import { Settings } from "../config/settings"
import type { AgentSessionEvent } from "../session/agent-session-events"
import type { AgentMessage } from "../vendor/agent/src/index.ts"
import { IdleRecap, type IdleRecapSession } from "./idle-recap"
import { cfgRecapEnabled } from "./settings"

const assistant = (content: unknown[]): AgentMessage =>
	({ role: "assistant", content, api: "a", provider: "p", model: "m" }) as unknown as AgentMessage
const user = (text: string): AgentMessage => ({ role: "user", content: text, timestamp: 1 }) as unknown as AgentMessage

interface HarnessOptions {
	enabled?: boolean
	idleSeconds?: number
	reply?: (method: string, params: object) => Promise<unknown>
}

interface IdleRecapHarness {
	session: IdleRecapSession & {
		messages: AgentMessage[]
		isStreaming: boolean
		isCompacting: boolean
		queuedMessageCount: number
	}
	requests: Array<{ method: string; params: Record<string, unknown> }>
	recaps: string[]
	settings: Settings
	blocked: { value: boolean }
	idle: IdleRecap
	emit: (event: AgentSessionEvent) => void
}

function harness(options: HarnessOptions = {}): IdleRecapHarness {
	const listeners = new Set<(event: AgentSessionEvent) => void>()
	const requests: Array<{ method: string; params: Record<string, unknown> }> = []
	const settings = Settings.isolated({
		"recap.enabled": options.enabled ?? true,
		"recap.idleSeconds": options.idleSeconds ?? 30,
	})
	const session = {
		model: {},
		messages: [] as AgentMessage[],
		isStreaming: false,
		isCompacting: false,
		queuedMessageCount: 0,
		gateway: {
			sessionId: "sid-1",
			client: {
				request: (method: string, params: object) => {
					requests.push({ method, params: params as Record<string, unknown> })
					return options.reply?.(method, params) ?? Promise.resolve({ text: "next step", generated_at: 1 })
				},
			},
		},
		subscribe(listener: (event: AgentSessionEvent) => void) {
			listeners.add(listener)
			return () => listeners.delete(listener)
		},
	} satisfies IdleRecapSession
	const blocked = { value: false }
	const recaps: string[] = []
	const idle = new IdleRecap(session, settings, {
		isBlocked: () => blocked.value,
		onRecap: text => recaps.push(text),
	})
	const emit = (event: AgentSessionEvent): void => {
		for (const listener of [...listeners]) listener(event)
	}
	return { session, requests, recaps, settings, blocked, idle, emit }
}

/** Bun's fake timers are synchronous; drain the microtasks each timer callback queues. */
async function advance(ms: number): Promise<void> {
	vi.advanceTimersByTime(ms)
	for (let i = 0; i < 8; i += 1) await Promise.resolve()
}

function generatedTail(h: IdleRecapHarness): void {
	h.session.messages = [assistant([{ type: "text", text: "done" }])]
	h.emit({ type: "agent_end", messages: h.session.messages })
}

afterEach(() => {
	vi.useRealTimers()
})

describe("idle recap", () => {
	test("arms after a generated agent_end and asks once when the delay elapses", async () => {
		vi.useFakeTimers()
		const h = harness()
		generatedTail(h)

		await advance(29_999)
		expect(h.requests).toEqual([])
		await advance(1)

		expect(h.requests).toEqual([
			{ method: "recap.generate", params: { session_id: "sid-1" } },
		])
		expect(h.recaps).toEqual(["next step"])
		h.idle.dispose()
	})

	test("a tail without assistant output does not arm", async () => {
		vi.useFakeTimers()
		const h = harness()
		h.session.messages = [user("hello")]
		h.emit({ type: "agent_end", messages: h.session.messages })

		await advance(60_000)
		expect(h.requests).toEqual([])
		h.idle.dispose()
	})

	test("turn_start, agent_start and compaction supersede a pending stretch", async () => {
		vi.useFakeTimers()
		for (const event of [
			{ type: "turn_start" },
			{ type: "agent_start" },
			{ type: "compaction_state_changed", compacting: true },
		] as const) {
			const h = harness()
			generatedTail(h)
			h.emit(event)

			await advance(60_000)
			expect(h.requests).toEqual([])
			h.idle.dispose()
		}
	})

	test("a draft blocks arming and a refresh after it clears asks once", async () => {
		vi.useFakeTimers()
		const h = harness()
		h.blocked.value = true
		generatedTail(h)

		await advance(60_000)
		expect(h.requests).toEqual([])

		h.blocked.value = false
		h.idle.refresh()
		await advance(30_000)
		expect(h.requests).toHaveLength(1)
		expect(h.recaps).toEqual(["next step"])
		h.idle.dispose()
	})

	test("a draft that appears while the timer waits suppresses the recap", async () => {
		vi.useFakeTimers()
		const h = harness()
		generatedTail(h)
		h.blocked.value = true

		await advance(30_000)
		expect(h.requests).toEqual([])

		h.blocked.value = false
		h.idle.refresh()
		await advance(30_000)
		expect(h.requests).toHaveLength(1)
		h.idle.dispose()
	})

	test("a queued message suppresses the request until the queue empties", async () => {
		vi.useFakeTimers()
		const h = harness()
		generatedTail(h)
		h.session.queuedMessageCount = 1

		await advance(30_000)
		expect(h.requests).toEqual([])

		h.session.queuedMessageCount = 0
		h.emit({ type: "queue_update", steering: [], followUp: [], liveSteered: 0 })
		await advance(30_000)
		expect(h.requests).toHaveLength(1)
		h.idle.dispose()
	})

	test("a skipped or empty reply is silent", async () => {
		vi.useFakeTimers()
		const h = harness({ reply: () => Promise.resolve({ text: "", generated_at: 1, skipped: "busy" }) })
		generatedTail(h)

		await advance(30_000)
		expect(h.requests).toHaveLength(1)
		expect(h.recaps).toEqual([])
		h.idle.dispose()
	})

	test("a reply that lands after invalidation is dropped", async () => {
		vi.useFakeTimers()
		const reply = Promise.withResolvers<unknown>()
		const h = harness({ reply: () => reply.promise })
		generatedTail(h)

		await advance(30_000)
		expect(h.requests).toHaveLength(1)
		h.emit({ type: "agent_start" })
		reply.resolve({ text: "late", generated_at: 2 })
		await advance(0)

		expect(h.recaps).toEqual([])
		h.idle.dispose()
	})

	test("one request in flight blocks a second timer on the same tail", async () => {
		vi.useFakeTimers()
		const reply = Promise.withResolvers<unknown>()
		const h = harness({ reply: () => reply.promise })
		generatedTail(h)

		await advance(30_000)
		expect(h.requests).toHaveLength(1)
		h.emit({ type: "queue_update", steering: [], followUp: [], liveSteered: 0 })
		await advance(30_000)
		expect(h.requests).toHaveLength(1)

		reply.resolve({ text: "next step", generated_at: 1 })
		await advance(0)
		expect(h.recaps).toEqual(["next step"])
		h.idle.dispose()
	})

	test("disabling the setting while a reply is in flight drops it", async () => {
		vi.useFakeTimers()
		const reply = Promise.withResolvers<unknown>()
		const h = harness({ reply: () => reply.promise })
		generatedTail(h)

		await advance(30_000)
		expect(h.requests).toHaveLength(1)
		h.settings.writeValue(cfgRecapEnabled, false, "override")
		reply.resolve({ text: "next step", generated_at: 1 })
		await advance(0)

		expect(h.recaps).toEqual([])
		h.idle.dispose()
	})

	test("recap.enabled=false never arms; the delay is clamped to at least one second", async () => {
		vi.useFakeTimers()
		const disabled = harness({ enabled: false })
		generatedTail(disabled)
		await advance(60_000)
		expect(disabled.requests).toEqual([])
		disabled.idle.dispose()

		const fast = harness({ idleSeconds: 0 })
		generatedTail(fast)
		await advance(999)
		expect(fast.requests).toEqual([])
		await advance(1)
		expect(fast.requests).toHaveLength(1)
		fast.idle.dispose()
	})
})
