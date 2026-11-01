import * as os from "node:os";
import * as path from "node:path";

import type { AgentMessage } from "../vendor/agent/src/index.ts";
import type { Usage } from "../vendor/ai/src/index.ts";
import { randomUUID } from "node:crypto";

/**
 * The shell's session projection.
 *
 * The authority owns the durable transcript (`session.create`/`session.history`), so this manager
 * keeps only what the client renders between snapshots: the message ledger the renderer walks, the
 * session's id/title/cwd, and the cumulative usage the status line and `/session` report. The
 * constructor is deliberately storage-free — nothing is written to disk.
 */
export class SessionManager {
	#cwd: string;
	#sessionId: string = randomUUID();
	#sessionName: string | undefined;
	#titleRevision = 0;
	#messages: AgentMessage[] = [];
	#usage = {
		input: 0,
		output: 0,
		reasoning: 0,
		cacheRead: 0,
		cacheWrite: 0,
		cost: 0,
		requests: 0
	};
	#nameListeners = new Set<() => void>();

	private constructor(cwd: string, sessionId?: string) {
		this.#cwd = cwd;
		if (sessionId) this.#sessionId = sessionId;
	}

	/** A projection with no backing file: the wired `hermes omp` path. */
	static inMemory(cwd: string, sessionId?: string): SessionManager {
		return new SessionManager(cwd, sessionId);
	}

	getCwd(): string {
		return this.#cwd;
	}

	setCwdWithoutRelocation(newCwd: string): void {
		this.#cwd = newCwd;
	}

	getSessionDir(): string {
		return path.join(os.tmpdir(), "omp-ui-sessions");
	}

	getSessionId(): string {
		return this.#sessionId;
	}

	/** Adopt the authority's session id (create/resume and every session switch). */
	adoptSessionId(sessionId: string): void {
		this.#sessionId = sessionId;
	}

	getSessionFile(): string | undefined {
		return undefined;
	}

	getArtifactsDir(): string | null {
		return null;
	}

	/** Append one message to the local ledger; usage is folded into the cumulative counters. */
	appendMessage(message: AgentMessage): void {
		this.#messages.push(message);
		if (message.role === "assistant") this.#accumulateUsage(message.usage);
	}

	#accumulateUsage(usage: Usage | undefined): void {
		if (!usage) return;
		this.#usage.input += usage.input ?? 0;
		this.#usage.output += usage.output ?? 0;
		this.#usage.reasoning += usage.reasoningTokens ?? 0;
		this.#usage.cacheRead += usage.cacheRead ?? 0;
		this.#usage.cacheWrite += usage.cacheWrite ?? 0;
		this.#usage.cost += usage.cost?.total ?? 0;
		this.#usage.requests += 1;
	}

	getMessages(): readonly AgentMessage[] {
		return this.#messages;
	}

	getUsageStatistics(): {
		input: number;
		output: number;
		reasoning: number;
		cacheRead: number;
		cacheWrite: number;
		cost: number;
		requests: number;
	} {
		return { ...this.#usage };
	}

	getSessionName(): string | undefined {
		return this.#sessionName;
	}

	get titleRevision(): number {
		return this.#titleRevision;
	}

	setSessionName(name: string | undefined): boolean {
		if (this.#sessionName === name) return false;
		this.#sessionName = name;
		this.#titleRevision += 1;
		for (const listener of [...this.#nameListeners]) listener();
		return true;
	}

	onSessionNameChanged(callback: () => void): () => void {
		this.#nameListeners.add(callback);
		return () => this.#nameListeners.delete(callback);
	}

	/** Clear the ledger for a session switch; the caller re-seeds the new snapshot. */
	reset(messages: readonly AgentMessage[] = []): void {
		this.#messages = [...messages];
	}
}
