import type { AgentSession } from "../../session/agent-session";

/**
 * The shell renders exactly one gateway session: the authority owns every agent,
 * and a subagent's conversation never reaches this client. What remains of the
 * focus concept is the single-session holder the shell's `viewSession` accessor
 * consults — it is always unfocused.
 */
export class SessionFocusController {
	/** There is no focused subagent session in the gateway shell. */
	readonly target: AgentSession | undefined = undefined;

	/** No subagent is focused, so no id is ever reported. */
	readonly focusedAgentId: string | undefined = undefined;

	/** Nothing is attached or subscribed; kept for the shell's teardown sequence. */
	dispose(): void {}
}
