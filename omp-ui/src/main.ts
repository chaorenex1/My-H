/**
 * Hermes entry point for the omp shell.
 *
 * `hermes omp` boots `main()` → `modes/rpc/hermes-entry.ts`: pair with the Hermes session authority,
 * create or resume one session, and render it. The shell has no local agent, no CLI launch graph and
 * no tool execution — prompts, commands and model changes all travel over the gateway JSON-RPC wire.
 */

import type { AgentSession } from "./session/agent-session";
import type { InteractiveMode } from "./modes/interactive-mode";
import type { SubmittedUserInput } from "./modes/types";

export async function main(args: string[]): Promise<void> {
	const { runHermesOmp } = await import("./modes/rpc/hermes-entry");
	await runHermesOmp(args);
}

if (import.meta.main) {
	main(process.argv.slice(2)).catch(async error => {
		const { fatal } = await import("./vendor/utils/src/postmortem.ts");
		fatal(error);
	});
}

/** The shell surface one submission needs; kept structural so the controller layer stays decoupled. */
type SubmissionHost = Pick<
	InteractiveMode,
	"markPendingSubmissionStarted" | "finishPendingSubmission" | "showError" | "checkShutdownRequested"
>;

/**
 * Dispatch one composer submission.
 *
 * The composer already resolved local UI commands (`/model`, `/settings`, …) before calling this;
 * everything left is a turn for the authority, either as a fresh prompt or as a queue intent while
 * a turn is running.
 */
export async function submitInteractiveInput(
	mode: SubmissionHost,
	session: Pick<AgentSession, "prompt" | "resume">,
	input: SubmittedUserInput
): Promise<void> {
	if (input.cancelled) return;
	try {
		if (!input.started && !mode.markPendingSubmissionStarted(input)) return;
		if (input.resume) {
			await session.resume();
		} else {
			await session.prompt(input.text, {
				...(input.images ? { images: input.images } : {}),
				...(input.streamingBehavior ? { streamingBehavior: input.streamingBehavior } : {})
			});
		}
	} catch (error) {
		mode.showError(error instanceof Error ? error.message : "Unknown error occurred");
	} finally {
		mode.finishPendingSubmission(input);
		await mode.checkShutdownRequested();
	}
}
