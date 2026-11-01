import type { AgentEvent, ThinkingLevel } from "../vendor/agent/src/index.ts";
import type { Effort } from "../vendor/ai/src/index.ts";
import type { ConfiguredThinkingLevel } from "@oh-my-pi/pi-tui/thinking";

/**
 * The events the gateway-backed shell consumes.
 *
 * The shell is a client of the Hermes session authority: turns, messages and tool execution arrive
 * as `AgentEvent`s projected by `modes/rpc/gateway-session.ts`, and the few shell-owned events below
 * carry UI state the authority reports out of band (model/reasoning changes, the admission queue,
 * notices). omp's local turn vocabulary (compaction, retries, goals, TTSR, async jobs, extensions)
 * is not part of this client.
 */
export type AgentSessionEvent =
	| Exclude<AgentEvent, { type: "agent_end" }>
	| (Extract<AgentEvent, { type: "agent_end" }> & {
			/** False while the authority still has work for this session (queued input, a running turn). */
			isTerminal?: boolean;
			/** True when the turn finished: the session is idle until the next submission. */
			yielded?: boolean;
	  })
	| { type: "model_changed" }
	| {
			type: "thinking_level_changed";
			thinkingLevel: ThinkingLevel | undefined;
			/** The user-configured selector when it differs from the effective level. */
			configured?: ConfiguredThinkingLevel;
			/** The level `auto` resolved to this turn. */
			resolved?: Effort;
	  }
	| { type: "notice"; level: "info" | "warning" | "error"; message: string; source?: string }
	| { type: "config_warnings_changed" }
	// Coalesced snapshot of the authority's steering/follow-up queue; emitted whenever it differs
	// from the last `queue_update` (enqueue, delivery, removal, clear, session switch).
	| { type: "queue_update"; steering: string[]; followUp: string[]; liveSteered: number };

/** Listener function for agent session events. */
export type AgentSessionEventListener = (event: AgentSessionEvent) => void;
