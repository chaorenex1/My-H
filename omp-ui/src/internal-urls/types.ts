/**
 * Types for the internal URL routing system.
 *
 * Every scheme is a {@link ProtocolHandler} registered with the router. Its
 * {@link SchemeSpec} declares how consumers may address it (backing, selectors,
 * linkability), so renderers consult the router instead of branching on scheme
 * names.
 */

import type { Settings } from "../config/settings";
import type { LocalProtocolOptions } from "./local-protocol";

/**
 * How a scheme's resources exist.
 * - `file`: resolved content is the byte-identical content of the file
 *   {@link ProtocolHandler.locate} returns.
 * - `virtual`: content is rendered by the handler (it may still `locate` a
 *   backing file).
 * - `device`: session-bound control surface rather than stored content.
 */
export type SchemeBacking = "file" | "virtual" | "device";

/**
 * Read-selector grammar after the URL.
 * - `lines`: any trailing `:<selector>` chain is a read selector
 *   (`local://notes.md:1-50`).
 * - `none`: never peel; the URL is passed through as written.
 * - `opaque`: URIs that may legitimately end in selector-shaped tails.
 */
export type SchemeSelectors = "lines" | "none" | "opaque";

/**
 * Declared facts about a scheme. Consumed by the router, the TUI (selector
 * peeling) and renderers (hyperlinks).
 */
export interface SchemeSpec {
	backing: SchemeBacking;
	selectors: SchemeSelectors;
	/** A trailing `:N` with no path after the authority is a port, not a selector. */
	portAuthority?: boolean;
	/** The authority is the first path segment under one root (local://a/b), so a glob may start there; other schemes' authority is an id. */
	pathAuthority?: true;
	/** Default immutability of resolved resources; a resource may override it per URL. */
	immutable: boolean;
	/** Renderers may call `locate` to hyperlink these URLs: locate is local, cheap, never spawns or fetches. */
	linkable?: boolean;
	/** Mistyped spellings are aliases of `scheme://x`: single-slash `scheme:/x` and a path-prefixed `/cwd/scheme://x` (local://). */
	singleSlashAlias?: true;
}

/**
 * Raw resource payload returned by protocol handlers. The router defaults
 * `immutable` from {@link SchemeSpec.immutable}, so handlers set it only to
 * override the scheme default for a specific URL.
 */
export interface InternalResource {
	/** Canonical URL that was resolved */
	url: string;
	/** Resolved text content */
	content: string;
	/** MIME type: text/markdown, application/json, or text/plain */
	contentType: "text/markdown" | "application/json" | "text/plain";
	/** Content size in bytes */
	size?: number;
	/** Underlying filesystem path (for debugging, not exposed to agent) */
	sourcePath?: string;
	/** Additional notes about resolution */
	notes?: string[];
	/**
	 * True when the resolved content cannot be edited. Mutable resources (e.g.
	 * local://) behave like editable files.
	 */
	immutable?: boolean;
	/**
	 * True when the resource is a directory listing rather than file content.
	 * Traversal needs a `sourcePath`; the rendered listing is never searched as
	 * file text.
	 */
	isDirectory?: boolean;
}

/**
 * A single autocomplete candidate for the host/path portion of a `scheme://`
 * URL, produced by {@link ProtocolHandler.complete}.
 */
export interface UrlCompletion {
	/**
	 * The text that follows `scheme://` for this candidate (e.g.
	 * `subdir/data.json`). The caller renders it as `scheme://<value>`.
	 */
	value: string;
	/** Human-facing label for the dropdown. Defaults to {@link value}. */
	label?: string;
	/** Optional one-line description shown beside the candidate. */
	description?: string;
}

/**
 * Parsed internal URL with preserved host casing.
 */
export interface InternalUrl extends URL {
	/**
	 * Raw host segment extracted from input, preserving case.
	 */
	rawHost: string;
	/**
	 * Raw pathname extracted from input, preserving traversal markers before URL normalization.
	 */
	rawPathname?: string;
	/**
	 * Exact input string this URL was parsed from, before any normalization.
	 * Set by `parseInternalUrl`; used where byte-exact URI matching matters.
	 */
	rawHref?: string;
}

/**
 * Caller-supplied context that the router threads into protocol handlers, so
 * handlers resolve relative defaults against the session that initiated the
 * call instead of process-global state.
 */
export interface ResolveContext {
	/** Working directory of the calling session. */
	cwd?: string;
	/** Calling session's session file, when it has one. */
	sessionFile?: string;
	/** Calling session's stable session-manager id. */
	sessionId?: string;
	/** Settings of the calling session. */
	settings?: Settings;
	/** Caller's abort signal. */
	signal?: AbortSignal;
	/** Calling session's `local://` root mapping. */
	localProtocolOptions?: LocalProtocolOptions;
	/**
	 * When set, handlers that would otherwise materialize an expensive directory
	 * listing instead return the directory shape (`isDirectory: true`) with empty
	 * content.
	 */
	skipDirectoryListing?: boolean;
}

/**
 * Handler for a specific internal URL scheme (e.g. local://).
 */
export interface ProtocolHandler {
	/** The scheme this handler processes (without trailing ://) */
	readonly scheme: string;
	/** Declared consumption contract; see {@link SchemeSpec}. */
	readonly spec: SchemeSpec;
	/**
	 * Resolve an internal URL to its content. The router defaults
	 * {@link InternalResource.immutable} from {@link SchemeSpec.immutable}.
	 *
	 * @param url Parsed URL object
	 * @param context Optional caller context. Handlers that depend on caller
	 *   identity (working directory, settings) **MUST** consume this in
	 *   preference to global state.
	 * @throws Error with user-friendly message if resolution fails
	 */
	resolve(url: InternalUrl, context?: ResolveContext): Promise<InternalResource>;
	/**
	 * Optional autocomplete hook. Returns candidate completions for the
	 * host/path portion of a `scheme://` URL while the user composes a prompt.
	 *
	 * Implementations **MUST** be fast and local — this runs on every keystroke.
	 * The caller fuzzy-filters the returned set against the partially typed
	 * `query`, so handlers return their full (bounded) candidate list; `query` is
	 * provided only so handlers can scope expensive enumeration.
	 */
	complete?(query?: string, context?: ResolveContext): Promise<UrlCompletion[]>;
	/**
	 * Absolute path of the local file or directory backing `url`, without
	 * materializing content. Returns `null` when the URL has no local backing
	 * (virtual target, or a missing entry).
	 * Throws on malformed URLs and containment violations with the same messages
	 * `resolve` uses. Never touches the network.
	 */
	locate?(url: InternalUrl, context?: ResolveContext): Promise<string | null>;
	/**
	 * Synchronous {@link locate} for renderers that cannot await (OSC 8 links
	 * while a message streams). Returns `undefined` when not resolvable
	 * synchronously; never throws.
	 */
	locateSync?(url: InternalUrl, context?: ResolveContext): string | undefined;
}
