/** Behavior-compatible reimplementation of @xterm/headless's used surface. */
export * from "./vterm/buffer.ts";
export * from "./vterm/query-responder.ts";
export * from "./vterm/terminal.ts";

import { Terminal } from "./vterm/terminal.ts";

const vterm = { Terminal };
export default vterm;
