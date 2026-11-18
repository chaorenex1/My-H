import type { Agent } from "../vendor/agent/src/index.ts";
import type { ImageContent, Model } from "../vendor/ai/src/index.ts";
import type { ThinkingLevel } from "../vendor/agent/src/thinking.ts";
import type { ConfiguredThinkingLevel } from "@oh-my-pi/pi-tui/thinking";
import type { ContextUsage } from "@oh-my-pi/pi-tui/status-line/types";
import type { ModelRegistry } from "../config/model-registry";
import type { Settings } from "../config/settings";
import type { SessionManager } from "./session-manager";

/**
 * Dependencies and initial state used to construct the gateway-backed session.
 *
 * The shell renders one Hermes session: the model registry and settings are the client's display
 * state (status line, pickers, appearance), the agent holds the transcript the renderer reads, and
 * the session manager keeps the in-memory accounting projection. Turns are dispatched by
 * `GatewayOmpSession` (`attachGateway`), so nothing here starts a local agent.
 */
export interface AgentSessionConfig {
	agent: Agent;
	sessionManager: SessionManager;
	settings: Settings;
	/** Model registry for the pickers and the status line. */
	modelRegistry: ModelRegistry;
	/** Models to cycle through with Ctrl+P. */
	scopedModels?: Array<{ model: Model; thinkingLevel?: ThinkingLevel }>;
	/** Initial session thinking selector; the gateway owns the effective effort. */
	thinkingLevel?: ConfiguredThinkingLevel;
	/** Accepted for the shared construction shape; the gateway owns memory. */
	memoryEnabled?: boolean;
	/** Whether dialogs may reach a human. */
	interactivePrompts?: boolean;
}

/** Options for AgentSession.prompt(). */
export interface PromptOptions {
	/** Image attachments; submitted through `image.attach_bytes` before the prompt. */
	images?: ImageContent[];
	/** Queue behavior while the session is running. */
	streamingBehavior?: "steer" | "followUp";
}

/** Payload for {@link AgentSession.setPromptDropped}: a prompt cancelled before the authority took it. */
export interface DroppedPrompt {
	/** The prompt exactly as typed. */
	text: string;
	/** Image attachments submitted with the prompt. */
	images?: ImageContent[];
	/** Why the session refused it, for hosts to show beside the restored text. */
	reason?: string;
}

/** Options for AgentSession.followUp(). */
export interface FollowUpOptions {}

/** Options for AgentSession.steer(). */
export interface SteerOptions {}

/** Options for AgentSession.sendUserMessage(). */
export interface SendUserMessageOptions {
	/** Queue behavior; omitted steers while running and starts a turn when idle. */
	deliverAs?: "steer" | "followUp";
}

/** Result from cycleModel(). */
export interface ModelCycleResult {
	model: Model;
	thinkingLevel: ThinkingLevel | undefined;
	/** Whether cycling through scoped models or all available models. */
	isScoped: boolean;
}

/** Queued user content restored to the editor. */
export type RestoredQueuedMessage = { text: string; images?: ImageContent[] };

/** Session statistics for the `/session` command, projected from the authority's counters. */
export interface SessionStats {
	sessionFile: string | undefined;
	sessionId: string;
	userMessages: number;
	assistantMessages: number;
	toolCalls: number;
	toolResults: number;
	totalMessages: number;
	tokens: {
		input: number;
		output: number;
		reasoning: number;
		cacheRead: number;
		cacheWrite: number;
		total: number;
	};
	premiumRequests: number;
	cost: number;
	contextUsage?: ContextUsage;
}
