// Persistent Bun runner for execute_code(language: "js") (ADR-4 D9).
//
// One process per (owner, bun, cwd) kernel. Cells arrive as JSON lines on
// stdin; each is transpiled and executed through INDIRECT eval in this
// runner's global scope. `node:vm` is deliberately not used: Bun aborts the
// whole worker on `Worker.terminate()` while `vm.runInContext` is running
// (SIGTRAP), and Bun 1.4.4's vm lacks `importModuleDynamically`.
//
// Cancellation: the host kills this process (and its group); no in-process
// interrupt is attempted. Killing the process drops all VM state, which is the
// documented contract for a timed-out cell.

import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { format } from "node:util";
import path from "node:path";
import net from "node:net";
import readline from "node:readline";
import { transformCell } from "./eval_js_transform.ts";

const SENTINEL = process.env.HERMES_JS_SENTINEL ?? "";
const RPC_ENDPOINT = process.env.HERMES_RPC_SOCKET ?? "";
const RPC_TOKEN = process.env.HERMES_RPC_TOKEN ?? "";
const CAPTURE_LIMIT = 1_000_000;
const IMPORT_HELPER = "__hermes_import__";

// Captured before any user code runs: frames must never be re-entered through
// the process.stdout.write override below.
const realStdoutWrite = process.stdout.write.bind(process.stdout);

function writeFrame(payload: Record<string, unknown>): void {
  const body = JSON.stringify(payload);
  realStdoutWrite(`\n${SENTINEL} ${Buffer.byteLength(body)}\n${body}`);
}

let activeCellId = "";
let activeCellIndex = 0;
let stdoutChunks: string[] = [];
let stderrChunks: string[] = [];
let stdoutSize = 0;
let stderrSize = 0;
let stdoutClipped = false;
let stderrClipped = false;

function capture(stream: "stdout" | "stderr", text: string): void {
  if (!text) return;
  if (stream === "stdout") {
    const keep = text.slice(0, CAPTURE_LIMIT - stdoutSize);
    stdoutChunks.push(keep);
    stdoutSize += keep.length;
    if (keep.length < text.length) stdoutClipped = true;
  } else {
    const keep = text.slice(0, CAPTURE_LIMIT - stderrSize);
    stderrChunks.push(keep);
    stderrSize += keep.length;
    if (keep.length < text.length) stderrClipped = true;
  }
  writeFrame({ type: "stream", id: activeCellId, cell_index: activeCellIndex, stream, text });
}

function resetCapture(): void {
  stdoutChunks = [];
  stderrChunks = [];
  stdoutSize = 0;
  stderrSize = 0;
  stdoutClipped = false;
  stderrClipped = false;
}

// console.log in Bun writes straight to fd 1, not through process.stdout.write,
// so both surfaces are overridden. Direct fd writes still bypass capture; the
// host keeps them as raw output (same contract as the Python runner).
const CONSOLE_STREAM: Record<string, "stdout" | "stderr"> = {
  log: "stdout", info: "stdout", debug: "stdout", dir: "stdout", trace: "stdout",
  warn: "stderr", error: "stderr",
};
for (const [method, stream] of Object.entries(CONSOLE_STREAM)) {
  Object.defineProperty(console, method, {
    configurable: true,
    writable: true,
    value: (...args: unknown[]) => capture(stream, format(...args) + "\n"),
  });
}

function overrideWriter(target: NodeJS.WriteStream, stream: "stdout" | "stderr"): void {
  Object.defineProperty(target, "write", {
    configurable: true,
    writable: true,
    value: (chunk: unknown, encodingOrCallback?: unknown, callback?: unknown) => {
      const text = typeof chunk === "string"
        ? chunk
        : chunk instanceof Uint8Array ? Buffer.from(chunk).toString("utf8") : String(chunk);
      capture(stream, text);
      const done = typeof encodingOrCallback === "function" ? encodingOrCallback : callback;
      if (typeof done === "function") (done as () => void)();
      return true;
    },
  });
}
overrideWriter(process.stdout, "stdout");
overrideWriter(process.stderr, "stderr");

// ---- Tool bridge (line-JSON RPC served by the Python host) ----------------

let rpcSocket: net.Socket | null = null;
let rpcBuffer = "";
let rpcWaiter:
  | { resolve: (line: string) => void; reject: (error: Error) => void }
  | null = null;

function settleWaiter(error: Error | null, line?: string): void {
  const waiter = rpcWaiter;
  if (!waiter) return;
  rpcWaiter = null;
  if (error) waiter.reject(error);
  else waiter.resolve(line ?? "");
}

function consumeRpcBuffer(): void {
  const newline = rpcBuffer.indexOf("\n");
  if (newline < 0 || !rpcWaiter) return;
  const line = rpcBuffer.slice(0, newline);
  rpcBuffer = rpcBuffer.slice(newline + 1);
  settleWaiter(null, line);
}

function resetRpc(): void {
  settleWaiter(new Error("Agent RPC connection closed"));
  const socket = rpcSocket;
  rpcSocket = null;
  rpcBuffer = "";
  socket?.destroy();
}

function ensureSocket(): Promise<net.Socket> {
  if (rpcSocket) return Promise.resolve(rpcSocket);
  const { promise, resolve, reject } = Promise.withResolvers<net.Socket>();
  let socket: net.Socket;
  if (RPC_ENDPOINT.startsWith("tcp://")) {
    const hostPort = RPC_ENDPOINT.slice("tcp://".length);
    const separator = hostPort.lastIndexOf(":");
    socket = net.createConnection({
      host: hostPort.slice(0, separator) || "127.0.0.1",
      port: Number(hostPort.slice(separator + 1)),
    });
  } else {
    socket = net.createConnection({ path: RPC_ENDPOINT });
  }
  socket.setEncoding("utf8");
  socket.on("data", (chunk: string) => {
    rpcBuffer += chunk;
    consumeRpcBuffer();
  });
  socket.on("error", (error: Error) => {
    if (rpcSocket === socket) resetRpc();
    settleWaiter(error);
    reject(error);
  });
  socket.on("close", () => {
    if (rpcSocket === socket) {
      rpcSocket = null;
      rpcBuffer = "";
    }
    settleWaiter(new Error("Agent RPC connection closed"));
  });
  socket.once("connect", () => {
    rpcSocket = socket;
    resolve(socket);
  });
  return promise;
}

async function sendOnce(request: string): Promise<string> {
  const socket = await ensureSocket();
  const { promise, resolve, reject } = Promise.withResolvers<string>();
  rpcWaiter = { resolve, reject };
  socket.write(request, (error?: Error | null) => {
    if (error) settleWaiter(error);
  });
  consumeRpcBuffer();
  return await promise;
}

async function rpcCallRaw(request: Record<string, unknown>): Promise<string> {
  const line = JSON.stringify(request) + "\n";
  let lastError: unknown;
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      return await sendOnce(line);
    } catch (error) {
      lastError = error;
      resetRpc();
    }
  }
  throw lastError instanceof Error ? lastError : new Error(String(lastError));
}

function parseToolResult(raw: string): unknown {
  try {
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "string") return parsed;
    try {
      return JSON.parse(parsed);
    } catch {
      return parsed;
    }
  } catch {
    return raw;
  }
}

// The RPC server serves one connection serially with no request id: every call
// in this runner is queued so concurrent cell calls cannot cross replies.
let rpcQueue: Promise<unknown> = Promise.resolve();

function callTool(name: string, args: unknown): Promise<unknown> {
  const run = () => rpcCallRaw({ tool: name, args, token: RPC_TOKEN }).then(parseToolResult);
  const next = rpcQueue.then(run, run);
  rpcQueue = next.then(
    () => undefined,
    () => undefined,
  );
  return next;
}

function installToolGlobals(tools: string[]): void {
  for (const name of tools) {
    Object.defineProperty(globalThis, name, {
      configurable: true,
      writable: true,
      enumerable: false,
      value: (args?: unknown) => callTool(name, args ?? {}),
    });
  }
}

// ---- Eval prelude (ADR-4 D11) ---------------------------------------------
// The same host ops the Python prelude exposes: display/status plus per-session
// agent/completion/workpool handles. Handles outlive the cell that made them.
// A cell may call an async prelude helper without awaiting (display(x)); the
// runner settles these before the result frame so the host sees the display
// block deterministically.

const pendingPrelude = new Set<Promise<unknown>>();

function trackPrelude<T>(promise: Promise<T>): Promise<T> {
  const tracked = promise.finally(() => pendingPrelude.delete(tracked));
  pendingPrelude.add(tracked);
  return tracked;
}

function defineGlobal(name: string, value: unknown): void {
  Object.defineProperty(globalThis, name, {
    configurable: true, writable: true, enumerable: false, value,
  });
}

function safeJson(value: unknown): string {
  try {
    return JSON.stringify(value, (_key, item) => typeof item === "bigint" ? String(item) : item, 2)
      ?? String(value);
  } catch {
    return String(value);
  }
}

function viewFor(value: unknown, kind?: string): unknown {
  if (kind === "markdown") return { kind: "markdown", text: String(value) };
  if (kind === "table" && Array.isArray(value)) {
    const rows = value.map(row => Array.isArray(row) ? row : Object.values(row as object));
    const head = value.length && !Array.isArray(value[0])
      ? Object.keys(value[0] as object)
      : rows.length ? rows[0].map((_, index) => String(index)) : [];
    return { kind: "table", columns: head.map(name => ({ head: String(name) })),
             rows: rows.map(row => row.map(cell => String(cell))) };
  }
  try {
    return { kind: "json", value: JSON.parse(safeJson(value)) };
  } catch {
    return { kind: "json", value: safeJson(value) };
  }
}

async function display(value?: unknown, options?: Record<string, unknown>): Promise<unknown> {
  const opts = options ?? {};
  let images = Array.isArray(opts.images) ? opts.images : undefined;
  if (!images && value !== null && typeof value === "object"
      && typeof (value as Record<string, unknown>).data === "string"
      && typeof (value as Record<string, unknown>).mimeType === "string") {
    images = [value];
    value = undefined;
  }
  const text = typeof opts.text === "string"
    ? opts.text
    : typeof value === "string" ? value : safeJson(value);
  if (text) capture("stdout", text + "\n");
  const payload: Record<string, unknown> = { images: images ?? [] };
  if (value !== undefined && text) {
    payload.views = [{ text, view: opts.view ?? viewFor(value, opts.kind as string | undefined) }];
  }
  await trackPrelude(callTool("__display__", payload));
  return value;
}

async function status(op: string, fields?: Record<string, unknown>): Promise<unknown> {
  return await trackPrelude(callTool("__status__", { op: String(op), ...(fields ?? {}) }));
}

class EvalHandle {
  readonly kind: string;
  readonly id: string;
  private resolved: { value: unknown } | null = null;

  constructor(kind: string, id: string) { this.kind = kind; this.id = id; }

  async status(): Promise<string> {
    const snapshot = await callTool("__status__", { item: { kind: this.kind, id: this.id } }) as Record<string, unknown> | null;
    return typeof snapshot?.status === "string" ? snapshot.status : "failed";
  }

  async done(): Promise<boolean> { return (await this.status()) !== "running"; }

  async wait(timeout?: number): Promise<unknown> {
    if (this.resolved) return this.resolved.value;
    const args: Record<string, unknown> = { items: [{ kind: this.kind, id: this.id }] };
    if (typeof timeout === "number") args.timeoutMs = Math.max(0, timeout * 1000);
    const response = await callTool("__wait__", args) as Record<string, unknown> | null;
    const snapshot = (Array.isArray(response?.items) ? response.items[0] : {}) as Record<string, unknown>;
    if (snapshot.status === "running") throw new Error(`${this.kind} ${this.id} is still running`);
    if (snapshot.status === "failed" || snapshot.status === "cancelled") {
      throw new Error(String(snapshot.error ?? `${this.kind} ${this.id} failed`));
    }
    const value = "text" in snapshot ? snapshot.text : snapshot.result;
    this.resolved = { value };
    return value;
  }

  async cancel(): Promise<boolean> {
    const result = await callTool("__cancel__", { item: { kind: this.kind, id: this.id } }) as Record<string, unknown> | null;
    return Boolean(result?.cancelled);
  }
}

class AgentHandle extends EvalHandle {
  readonly agent?: string;
  readonly handle: string;

  constructor(id: string, agent?: string) {
    super("agent", id);
    this.agent = agent;
    this.handle = `agent://${id}`;
  }

  async send(message: string): Promise<unknown> {
    return await callTool("__send__", { id: this.id, message: String(message) });
  }
}

class CompletionHandle extends EvalHandle {
  constructor(id: string) { super("completion", id); }
}

function handleId(payload: unknown, kind: string): string {
  const id = (payload as Record<string, unknown> | null)?.id;
  if (typeof id !== "string" || !id) {
    const error = (payload as Record<string, unknown> | null)?.error;
    throw new Error(typeof error === "string" ? error : `${kind}() did not return a handle`);
  }
  return id;
}

async function agent(prompt: string, opts?: Record<string, unknown>): Promise<AgentHandle> {
  const options = opts ?? {};
  const payload = await trackPrelude(callTool("__agent__", { prompt, ...options }));
  return new AgentHandle(handleId(payload, "agent"), (payload as Record<string, unknown>)?.agent as string | undefined);
}

async function completion(prompt: string, opts?: Record<string, unknown>): Promise<CompletionHandle> {
  const payload = await trackPrelude(callTool("__completion__", { prompt, ...(opts ?? {}) }));
  return new CompletionHandle(handleId(payload, "completion"));
}

async function wait(handles: unknown, timeout?: number): Promise<unknown[]> {
  const items = Array.isArray(handles) ? handles : [handles];
  const handlesList = items as EvalHandle[];
  const args: Record<string, unknown> = {
    items: handlesList.map(handle => ({ kind: handle.kind, id: handle.id })),
  };
  if (typeof timeout === "number") args.timeoutMs = Math.max(0, timeout * 1000);
  const response = await callTool("__wait__", args) as Record<string, unknown> | null;
  const snapshots = Array.isArray(response?.items) ? response.items : [];
  return handlesList.map((handle, index) => {
    const snapshot = (snapshots[index] ?? {}) as Record<string, unknown>;
    if (snapshot.status === "running") throw new Error(`${handle.kind} ${handle.id} is still running`);
    if (snapshot.status === "failed" || snapshot.status === "cancelled") {
      throw new Error(String(snapshot.error ?? `${handle.kind} ${handle.id} failed`));
    }
    return "text" in snapshot ? snapshot.text : snapshot.result;
  });
}

class WorkPool {
  readonly name: string;
  readonly agent?: string;
  readonly limit?: number;

  constructor(name: string, agent?: string, limit?: number) {
    this.name = name;
    this.agent = agent;
    this.limit = limit;
  }

  async push(...items: string[]): Promise<string[]> {
    const result = await callTool("__workpool__", { op: "push", name: this.name, items }) as Record<string, unknown> | null;
    return Array.isArray(result?.ids) ? result.ids as string[] : [];
  }

  async status(): Promise<unknown> { return await callTool("__workpool__", { op: "status", name: this.name }); }
  async peek(): Promise<unknown> { return await callTool("__workpool__", { op: "peek", name: this.name }); }
  async close(): Promise<unknown> { return await callTool("__workpool__", { op: "close", name: this.name }); }
}

async function workpool(agentName?: string, opts?: Record<string, unknown>): Promise<WorkPool> {
  const options = opts ?? {};
  const result = await trackPrelude(callTool("__workpool__", { op: "create", agent: agentName, ...options })) as Record<string, unknown> | null;
  if (typeof result?.name !== "string") throw new Error("workpool() did not return a pool");
  return new WorkPool(result.name, result.agent as string | undefined, result.limit as number | undefined);
}

function installPreludeGlobals(): void {
  defineGlobal("display", display);
  defineGlobal("status", status);
  defineGlobal("out", (text: unknown) =>
    trackPrelude(callTool("__out__", { text: String(text ?? "") })));
  defineGlobal("agent", agent);
  defineGlobal("wait", wait);
  defineGlobal("workpool", workpool);
  defineGlobal("completion", completion);
  defineGlobal("judge", (state: unknown, options?: Record<string, unknown>) =>
    trackPrelude(callTool("__judge__", { state, ...(options ?? {}) })));
  defineGlobal("judge_batch", (items: unknown, options?: Record<string, unknown>) =>
    trackPrelude(callTool("__judge_batch__", { items, ...(options ?? {}) })));
  defineGlobal("log", (message: unknown) => status("log", { message: String(message) }));
  defineGlobal("phase", (title: unknown) => status("phase", { title: String(title) }));
}

// ---- Init and cell execution ---------------------------------------------

interface InitMessage { type: "init"; cwd: string; tools: string[]; packages?: string }
interface CellMessage { type: "cell"; id: string; code: string; cell_index?: number }
interface StopMessage { type: "stop" }
type RunnerMessage = InitMessage | CellMessage | StopMessage;

function parseMessage(line: string): RunnerMessage | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(line);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) return null;
  const candidate = parsed as Record<string, unknown>;
  if (candidate.type === "init" && typeof candidate.cwd === "string") {
    const tools = Array.isArray(candidate.tools)
      ? candidate.tools.filter((item): item is string => typeof item === "string")
      : [];
    return { type: "init", cwd: candidate.cwd, tools,
             packages: typeof candidate.packages === "string" ? candidate.packages : undefined };
  }
  if (candidate.type === "cell" && typeof candidate.id === "string" && typeof candidate.code === "string") {
    return { type: "cell", id: candidate.id, code: candidate.code,
             cell_index: typeof candidate.cell_index === "number" ? candidate.cell_index : 0 };
  }
  if (candidate.type === "stop") return { type: "stop" };
  return null;
}

let executionCount = 0;

function initialize(message: InitMessage): void {
  // Started in the kernel staging dir so Bun never reads the project's
  // bunfig.toml/preload; chdir here makes relative cell paths session-relative.
  process.chdir(message.cwd);
  const requireFromCell = createRequire(path.join(message.cwd, "cell.cjs"));
  const requireFromPackages = message.packages
    ? createRequire(path.join(message.packages, "cell.cjs"))
    : null;
  const resolveSpecifier = (specifier: string): string => {
    try {
      return requireFromCell.resolve(specifier);
    } catch (error) {
      if (!requireFromPackages) throw error;
      // Managed installs are the fallback root; a missing module still fails (no implicit install).
      return requireFromPackages.resolve(specifier);
    }
  };
  const requireCell = (specifier: string): unknown => {
    try {
      return requireFromCell(specifier);
    } catch (error) {
      if (!requireFromPackages) throw error;
      return requireFromPackages(specifier);
    }
  };
  Object.defineProperty(globalThis, "require", {
    configurable: true,
    writable: true,
    enumerable: false,
    value: requireCell,
  });
  Object.defineProperty(globalThis, IMPORT_HELPER, {
    configurable: true,
    writable: true,
    enumerable: false,
    value: async (specifier: string) => {
      // The specifier is cell-supplied at runtime, so static imports cannot
      // express it; resolution is against the session cwd, never this file.
      const builtin = specifier.startsWith("node:")
        || specifier.startsWith("file:")
        || specifier.startsWith("data:");
      if (builtin) return await import(specifier);
      if (path.isAbsolute(specifier)) return await import(pathToFileURL(specifier).href);
      // Bare and relative specifiers resolve from the session cwd, not this runner file.
      return await import(pathToFileURL(resolveSpecifier(specifier)).href);
    },
  });
  installToolGlobals(message.tools);
  installPreludeGlobals();
}

async function runCell(message: CellMessage): Promise<void> {
  activeCellId = message.id;
  activeCellIndex = message.cell_index ?? 0;
  resetCapture();
  executionCount += 1;
  let status = "ok";
  let traceback = "";
  try {
    const transformed = transformCell(message.code, process.cwd());
    const code = transformed.wrapped
      ? `(async () => {\n${transformed.code}\n})()`
      : transformed.code;
    const value: unknown = globalThis.eval(code);
    if (value !== null && typeof value === "object"
        && "then" in value && typeof value.then === "function") {
      await value;
    }
  } catch (error) {
    status = "error";
    traceback = error instanceof Error ? (error.stack ?? error.message) : String(error);
  } finally {
    activeCellId = "";
    activeCellIndex = 0;
  }
  // A cell may fire display()/status() without awaiting; the host's display block
  // must reflect every call the cell made, so settle them before the result frame.
  if (pendingPrelude.size) {
    await Promise.allSettled([...pendingPrelude]);
  }
  writeFrame({
    type: "result",
    id: message.id,
    status,
    stdout: stdoutChunks.join(""),
    stderr: stderrChunks.join(""),
    exit_code: status === "ok" ? 0 : 1,
    execution_count: executionCount,
    stdout_clipped: stdoutClipped,
    stderr_clipped: stderrClipped,
    traceback,
  });
}

async function handle(message: RunnerMessage): Promise<void> {
  if (message.type === "init") {
    initialize(message);
    writeFrame({ type: "ready", execution_count: executionCount });
    return;
  }
  if (message.type === "cell") {
    await runCell(message);
    return;
  }
  process.exit(0);
}

// Parent-death watchdog: the host's process group/job object already covers
// the common case; this catches a runner orphaned from a host that vanished
// without tearing its descendants down.
setInterval(() => {
  try {
    process.kill(process.ppid, 0);
  } catch {
    process.exit(0);
  }
}, 2000);

const input = readline.createInterface({ input: process.stdin });
for await (const line of input) {
  const message = parseMessage(line.trim());
  if (message) await handle(message);
}
