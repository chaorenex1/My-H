/**
 * ADR-5 P4: the client's half of the idle recap.
 *
 * The timer, the composer draft and the current-view check live here because only this client has
 * them; the authority owns "is this tail still recap-worthy" (`recap.generate`, ADR-5 D8). A recap
 * is not a transcript entry: it is delivered once to the owner's `onRecap` and mounted as a live
 * `RecapNotice` (D9), never appended to the session ledger or durable state.
 *
 * Arming mirrors omp's idle maintenance: a stretch ends at `agent_end` only when the tail is a
 * generated assistant turn, and new activity, compaction or a session transition supersedes it.
 */

import type { Settings } from "../config/settings";
import type { AgentSessionEvent } from "../session/agent-session-events";
import type { AgentMessage } from "../vendor/agent/src/index.ts";
import { cfgRecap } from "./settings";

const RECAP_MIN_SECONDS = 1;
const RECAP_MAX_SECONDS = 3600;

/** The gateway surface the recap calls; the shell's `GatewayOmpSession` satisfies it. */
export interface IdleRecapGateway {
	readonly sessionId: string;
	readonly client: { request(method: string, params: object): Promise<unknown> };
}

/** The session surface the client timer reads; the shell's `AgentSession` satisfies it. */
export interface IdleRecapSession {
	readonly model: unknown;
	readonly messages: readonly AgentMessage[];
	readonly isStreaming: boolean;
	readonly isCompacting: boolean;
	readonly queuedMessageCount: number;
	readonly gateway: IdleRecapGateway | undefined;
	subscribe(listener: (event: AgentSessionEvent) => void): () => void;
}

export interface IdleRecapOptions {
	/** True while the owner must not receive autonomous work (not ready, shutting down, composing). */
	isBlocked?: () => boolean;
	/** Receives each non-blank recap text; the owner mounts it as a live notice. */
	onRecap: (text: string) => void;
}

export class IdleRecap {
	readonly #session: IdleRecapSession;
	readonly #settings: Settings;
	readonly #options: IdleRecapOptions;
	readonly #stops: Array<() => void>;
	/** The current stretch still owes a recap (the tail has not been recapped for it). */
	#pending = false;
	#timer: NodeJS.Timeout | undefined;
	/** Bumped by every invalidation so a reply for a superseded stretch is dropped. */
	#run = 0;
	/** The token of the one request in flight; no second one may be armed over the same tail. */
	#inFlight: number | undefined;
	#disposed = false;

	constructor(session: IdleRecapSession, settings: Settings, options: IdleRecapOptions) {
		this.#session = session;
		this.#settings = settings;
		this.#options = options;
		this.#stops = [
			session.subscribe(event => this.#observe(event)),
			cfgRecap.listen(settings, () => this.refresh()),
		];
	}

	/** The owner's inputs changed (draft cleared, settings): re-arm whatever this stretch still owes. */
	refresh(): void {
		this.#schedule();
	}

	/** New activity, compaction or a session transition: nothing captured before may arm or announce. */
	invalidate(): void {
		this.#pending = false;
		this.#inFlight = undefined;
		this.#clearTimer();
		this.#run += 1;
	}

	dispose(): void {
		if (this.#disposed) return;
		this.#disposed = true;
		this.invalidate();
		for (const stop of this.#stops.splice(0)) stop();
	}

	#observe(event: AgentSessionEvent): void {
		switch (event.type) {
			case "agent_start":
			case "turn_start":
			case "compaction_state_changed":
				this.invalidate();
				return;
			case "agent_end":
				this.invalidate();
				this.#pending = priorMessageWasGenerated(event.messages);
				this.#schedule();
				return;
			case "queue_update":
				// A queued message suppresses the recap; dequeueing one must re-arm the stretch.
				this.refresh();
				return;
			default:
				return;
		}
	}

	#isBlocked(): boolean {
		return this.#options.isBlocked?.() === true || this.#session.isCompacting;
	}

	/** Nothing is live or queued: a recap request would race a turn otherwise. */
	#isQuiet(): boolean {
		return !this.#session.isStreaming && this.#session.queuedMessageCount === 0;
	}

	#schedule(): void {
		this.#clearTimer();
		if (this.#disposed || !this.#pending || this.#inFlight !== undefined) return;
		if (!cfgRecap.get(this.#settings).enabled || this.#isBlocked() || !this.#isQuiet()) return;
		const seconds = Math.max(
			RECAP_MIN_SECONDS,
			Math.min(RECAP_MAX_SECONDS, cfgRecap.get(this.#settings).idleSeconds),
		);
		this.#timer = setTimeout(() => {
			this.#timer = undefined;
			this.#onDue();
		}, seconds * 1000);
		this.#timer.unref?.();
	}

	#onDue(): void {
		if (this.#disposed || !this.#pending) return;
		if (this.#isBlocked()) return; // owed: a later `refresh()` re-arms after the blocker clears
		if (!this.#isQuiet()) {
			this.#schedule();
			return;
		}
		void this.#runRecap();
	}

	async #runRecap(): Promise<void> {
		const session = this.#session;
		const gateway = session.gateway;
		if (!gateway || !session.model || !priorMessageWasGenerated(session.messages)) {
			this.#complete();
			return;
		}
		const token = ++this.#run;
		this.#inFlight = token;
		try {
			const result = (await gateway.client.request("recap.generate", {
				session_id: gateway.sessionId,
			})) as { text?: unknown } | undefined;
			if (this.#disposed || token !== this.#run) return;
			// Re-check after the reply: a draft, a disabled setting or a new turn makes it stale.
			if (!cfgRecap.get(this.#settings).enabled) return;
			if (this.#isBlocked()) return;
			if (!this.#isQuiet()) {
				// Hand the stretch back with no timer; the queue/composer refresh re-arms it.
				this.#inFlight = undefined;
				this.#schedule();
				return;
			}
			const text = typeof result?.text === "string" ? result.text.trim() : "";
			this.#complete();
			if (text) this.#options.onRecap(text);
		} catch {
			if (this.#disposed || token !== this.#run) return;
			this.#complete();
		} finally {
			if (this.#inFlight === token) this.#inFlight = undefined;
		}
	}

	#complete(): void {
		this.#pending = false;
		this.#inFlight = undefined;
		this.#clearTimer();
		this.#run += 1;
	}

	#clearTimer(): void {
		if (this.#timer) clearTimeout(this.#timer);
		this.#timer = undefined;
	}
}

/**
 * Prior message is the last message and it is an assistant turn with output. `stopReason` is not a
 * veto: an aborted or errored turn that already produced text/thinking/tool calls still recaps.
 */
function priorMessageWasGenerated(messages: readonly AgentMessage[]): boolean {
	const last = messages.at(-1);
	if (last?.role !== "assistant") return false;
	return last.content.some(block => {
		if (block.type === "text") return block.text.length > 0;
		if (block.type === "thinking") return block.thinking.length > 0;
		return block.type === "toolCall";
	});
}
