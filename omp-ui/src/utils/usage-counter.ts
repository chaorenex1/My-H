/**
 * Usage counts, read synchronously on hot UI paths and persisted to
 * `<agentDir>/usage-counts.json` so they survive restarts.
 *
 * - {@link commandUsage}: keyed by canonical slash-command name.
 *   {@link InputController} records every submitted known command; the
 *   combined autocomplete provider uses the counts to break text-match-score
 *   ties.
 * - {@link hintUsage}: keyed by composer hint id; a hint retires once its
 *   gesture has been used often enough (see `COMPOSER_HINTS`).
 *
 * The first {@link UsageCounter.record} reads the file before writing, and
 * {@link UsageCounter.load} lets the UI warm the counts up front.
 */

import * as path from "node:path";
import type { ComposerHintId } from "@oh-my-pi/pi-tui/prompt/composer-hints";
import { getAgentDir, isEnoent, isRecord, logger } from "../vendor/utils/src/index.ts";
import { writeFileAtomically } from "./atomic-file";

/** Which UI surface a {@link UsageCounter} counts. */
export type UsageKind = "command" | "hint";

/** Shape of the counts file: one name→count map per kind. */
type UsageCountsFile = Partial<Record<UsageKind, Record<string, number>>>;

/** Beside config.yml; kept out of settings because it churns on every use. */
const USAGE_COUNTS_FILE = "usage-counts.json";

async function readUsageCountsFile(): Promise<UsageCountsFile> {
	try {
		const raw: unknown = await Bun.file(path.join(getAgentDir(), USAGE_COUNTS_FILE)).json();
		return isRecord(raw) ? (raw as UsageCountsFile) : {};
	} catch (error) {
		if (!isEnoent(error)) logger.warn("Failed to read usage counts", { error: String(error) });
		return {};
	}
}

/** The persisted counts for one kind, dropping anything that is not a number. */
function countsFor(file: UsageCountsFile, kind: UsageKind): Record<string, number> {
	const counts = file[kind];
	if (!isRecord(counts)) return {};
	const clean: Record<string, number> = {};
	for (const [name, value] of Object.entries(counts)) {
		if (typeof value === "number" && Number.isFinite(value)) clean[name] = value;
	}
	return clean;
}

/** Process-wide use counts for one {@link UsageKind}, keyed by `Name`. */
export class UsageCounter<Name extends string = string> {
	#counts: Record<string, number> = {};
	#loadPromise: Promise<void> | undefined;
	#writes: Promise<void> = Promise.resolve();

	constructor(readonly kind: UsageKind) {}

	/** Load persisted counts once per process; concurrent calls share one read. */
	load(): Promise<void> {
		this.#loadPromise ??= (async () => {
			const persisted = countsFor(await readUsageCountsFile(), this.kind);
			// Keep hits recorded while the load was in flight.
			for (const name in this.#counts) persisted[name] = (persisted[name] ?? 0) + this.#counts[name]!;
			this.#counts = persisted;
		})();
		return this.#loadPromise;
	}

	get(name: Name): number {
		return this.#counts[name] ?? 0;
	}

	/** Increment a count and persist it. */
	record(name: Name): void {
		this.#counts[name] = (this.#counts[name] ?? 0) + 1;
		this.#writes = this.#writes.then(() => this.#persist());
	}

	/** Merge this counter into the file so counts of other kinds survive. */
	async #persist(): Promise<void> {
		try {
			await this.load();
			const file = await readUsageCountsFile();
			file[this.kind] = { ...countsFor(file, this.kind), ...this.#counts };
			await writeFileAtomically(path.join(getAgentDir(), USAGE_COUNTS_FILE), `${JSON.stringify(file, null, 2)}\n`);
		} catch (error) {
			logger.warn("Failed to persist usage counts", { kind: this.kind, error: String(error) });
		}
	}
}

/** Slash-command invocation counts. */
export const commandUsage = new UsageCounter("command");

/** Composer hint gesture counts. */
export const hintUsage = new UsageCounter<ComposerHintId>("hint");
