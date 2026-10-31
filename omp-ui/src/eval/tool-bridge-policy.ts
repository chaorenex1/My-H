import type { AgentTool } from "../vendor/agent/src/index.ts";

/** These tools need direct toolResult events to commit their session changes. */
export function isEvalBridgeToolCallable(tool: Pick<AgentTool, "name">): boolean {
	return tool.name !== "checkpoint" && tool.name !== "rewind";
}
