import type { ImageContent } from "../vendor/ai/src/index.ts";
import type { InteractiveMode } from "./interactive-mode";

/**
 * The surface controllers see: the concrete shell.
 *
 * Controllers live inside the shell (`src/modes/controllers/*`) and are constructed with it, so the
 * alias keeps one source of truth for the members they may touch instead of a hand-maintained
 * interface copy of the class.
 */
export type InteractiveModeContext = InteractiveMode;

/** One composer submission handed from the input controller to the shell. */
export type SubmittedUserInput = {
	text: string;
	images?: ImageContent[];
	imageLinks?: (string | undefined)[];
	customType?: string;
	/** The `.`/`c` continue shortcut: dispatch through `session.resume()` instead of
	 *  prompting with `text`, so no visible user turn is added. */
	resume?: boolean;
	display?: boolean;
	/** Queue intent when the authority is (or becomes) busy: "steer" or "followUp". */
	streamingBehavior?: "steer" | "followUp";
	/** Set by `submitInteractiveInput` for a custom-message submission: the session took it. */
	dispatched?: boolean;
	cancelled: boolean;
	started: boolean;
};

export interface InteractiveModeInitOptions {
	suppressWelcomeIntro?: boolean;
	clearInitialTerminalHistory?: boolean;
}

/** One row of the gateway's resume list (`session.list`). */
export interface SessionInfo {
	id: string;
	title?: string;
	updatedAt?: number;
	cwd?: string;
	running?: boolean;
}

export type SessionPickHandler = (session: SessionInfo) => void | Promise<void>;

/** How {@link InteractiveModeContext.handleResumeSession} resumes a session. */
export interface ResumeSessionOptions {
	/** The user chose this session to resume (picker, `/resume <id>`, welcome card). */
	followContinuation?: boolean;
}

export interface RenderSessionContextOptions {
	updateFooter?: boolean;
	reuseSettledComponents?: boolean;
	/** Tool calls whose existing live component remains the sole render owner across a rebuild. */
	preservedLiveToolCallIds?: ReadonlySet<string>;
}

/** How {@link InteractiveModeContext.showStatus} shows a notice. */
export interface ShowStatusOptions {
	/** Dims the ANSI line (default true). */
	dim?: boolean;
	/** Toasts it on a native terminal (default true); false keeps it to the ANSI transcript. */
	toast?: boolean;
}

export interface SelectOnlyModelSelectorOptions {
	/** Use the selected `provider/id` for the caller's operation without mutating session defaults. */
	onSelect: (selector: string) => void | Promise<void>;
	/** `provider/id` initially highlighted in the picker. */
	currentSelector?: string;
}

/** One queued message shown above the composer. */
export interface CompactionQueuedMessage {
	text: string;
}
