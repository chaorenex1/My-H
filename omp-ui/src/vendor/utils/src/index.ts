export { once, untilAborted } from "./abortable.ts";
export * from "./async.ts";
export * from "./binary.ts";
export * from "./color.ts";
export * from "./dirs.ts";
export * from "./env.ts";
export * from "./executable.ts";
export * from "./fetch-retry.ts";
export * from "./file-lock.ts";
export * from "./format.ts";
export * from "./frontmatter.ts";
export * from "./fs-error.ts";
export * from "./incoming-json.ts";
export * from "./json.ts";
export * from "./json-parse.ts";
export * as logger from "./logger.ts";
export * from "./loop-phase.ts";
export * from "./math-delimiters.ts";
export * from "./materialize-string.ts";
export * from "./mermaid-ascii.ts";
export * from "./mime.ts";
export * from "./path.ts";
export * from "./path-tree.ts";
export * from "./peek-file.ts";
export * as postmortem from "./postmortem.ts";
export * from "./process-name.ts";
export * as procmgr from "./procmgr.ts";
export * as prompt from "./prompt.ts";
export * as ptree from "./ptree.ts";
export { AbortError, ChildProcess, Exception, NonZeroExitError } from "./ptree.ts";
export * from "./runtime-install.ts";
export * from "./sanitize-text.ts";
export * from "./slugify.ts";
export * from "./snowflake.ts";
export * from "./sqlite.ts";
export * from "./stderr-guard.ts";
export * from "./stream.ts";
export * from "./tab-spacing.ts";
export * from "./temp.ts";
export * from "./tls-fetch.ts";
export * from "./type-guards.ts";
export * from "./version.ts";
export * from "./which.ts";
export * from "./yaml-config.ts";

function isPlainObject(val: object): val is Record<string, unknown> {
	return Object.getPrototypeOf(val) === Object.prototype || Array.isArray(val);
}

export function structuredCloneJSON<T>(value: T): T {
	// primitives|null|undefined, copy
	if (!value || typeof value !== "object") {
		return value;
	}

	// deep clone
	if (isPlainObject(value)) {
		try {
			return structuredClone(value);
		} catch {
			// might still fail due to nested structures
		}
	}
	return JSON.parse(JSON.stringify(value)) as T;
}

/**
 * Deep-copies a JSON-shaped tree (arrays and plain or null-prototype objects)
 * so later mutation of the source can never reach the copy. Strings and other
 * primitives are immutable and shared, so the cost is O(containers) instead of
 * `structuredClone`'s O(bytes) — the difference matters for hot paths that
 * re-copy large string payloads (e.g. streamed tool-call arguments) per delta.
 *
 * Copies own enumerable string keys only. Any other object (class instance,
 * Date, Map, inherited-prototype object) is copied with {@link structuredCloneJSON}.
 * Not cycle-safe: callers MUST pass acyclic data, as any JSON-serializable value is.
 */
export function cloneJsonTree<T>(value: T): T {
	return cloneJsonNode(value) as T;
}

function isJsonRecord(value: object): value is Record<string, unknown> {
	const proto = Object.getPrototypeOf(value);
	return proto === Object.prototype || proto === null;
}

function cloneJsonNode(value: unknown): unknown {
	if (value === null || typeof value !== "object") return value;
	if (Array.isArray(value)) return value.map(cloneJsonNode);
	if (!isJsonRecord(value)) return structuredCloneJSON(value);
	const out: Record<string, unknown> = {};
	for (const key in value) {
		if (!Object.hasOwn(value, key)) continue;
		const child = cloneJsonNode(value[key]);
		// Assigning `__proto__` would swap the copy's prototype instead of
		// creating the own data property JSON.parse produces.
		if (key === "__proto__") {
			Object.defineProperty(out, key, { value: child, enumerable: true, writable: true, configurable: true });
		} else {
			out[key] = child;
		}
	}
	return out;
}
