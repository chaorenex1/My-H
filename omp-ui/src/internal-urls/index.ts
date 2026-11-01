/**
 * Internal URL routing system: the `local://` scheme handler, the
 * process-global router that dispatches to it, and the parser renderers use
 * to turn internal URLs in transcript markdown into hyperlinks.
 *
 * One process-global `InternalUrlRouter` is shared across sessions. Handlers
 * are stateless; they read the calling session's root mapping from the caller
 * supplied context on every resolve call.
 */

export * from "./filesystem-resource";
export * from "./local-protocol";
export * from "./parse";
export * from "./router";
export type * from "./types";
