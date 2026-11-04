import { describe, expect, test } from "bun:test"

import { Settings } from "../config/settings"
import type { AgentMessage } from "../vendor/agent/src/index.ts"
import { AgentSession } from "./agent-session"
import type { AgentSessionEvent } from "./agent-session-events"
import { SessionManager } from "./session-manager"

/** The smallest gateway attachment the event projector needs; turns are never dispatched. */
function attachedSession(): {
	sessionManager: SessionManager
	emit: (event: AgentSessionEvent) => void
} {
	const sessionManager = SessionManager.inMemory("/w")
	const listeners = new Set<(event: AgentSessionEvent) => void>()
	const session = new AgentSession({
		agent: { replaceMessages: () => {} } as never,
		sessionManager,
		settings: Settings.isolated(),
		modelRegistry: { find: () => undefined, getAll: () => [] } as never
	})
	session.attachGateway({
		cwd: undefined,
		sessionId: "s",
		title: undefined,
		initialMessages: () => [],
		subscribe: (listener: (event: AgentSessionEvent) => void) => {
			listeners.add(listener)
			return () => listeners.delete(listener)
		},
		close: () => {},
		refresh: () => Promise.resolve()
	} as never)
	return {
		sessionManager,
		emit: event => {
			for (const listener of listeners) listener(event)
		}
	}
}

describe("gateway event projection", () => {
	test("a live compaction boundary joins the replay ledger", () => {
		const { sessionManager, emit } = attachedSession()
		const boundary: AgentMessage = {
			role: "compactionSummary",
			summary: "summary body",
			tokensBefore: 1,
			timestamp: 2
		}
		emit({ type: "compaction_summary", message: boundary })
		// ADR-5 D6: `renderSessionContext` walks this ledger, so a rebuild keeps the divider and
		// `display.collapseCompacted` has a boundary to collapse to.
		expect(sessionManager.getMessages()).toEqual([boundary])
	})
})
