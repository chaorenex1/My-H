/**
 * Socket half of the transport: pair with a Hermes gateway and speak JSON-RPC over one WebSocket
 * text frame per message (`docs/appendix-omp-ui-realtime-rpc.md` A.2/A.3).
 *
 * {@link bootstrapLocalGateway} runs `ui-tui/scripts/gateway_bootstrap.py` on the launcher's
 * interpreter and parses its grant (`{url, protocols, profile_id, instance_id}`), exactly like the
 * canonical Ink client's `localGatewayBootstrap.ts`; {@link connectGateway} dials that URL with the
 * granted subprotocols and wires the socket to {@link GatewayClient}. Pairing stays outside the
 * envelope module so the protocol half has no process/socket dependency.
 *
 * The framing contract is the gateway's, not omp's: one JSON object per text frame, no `rpc_chunk`
 * (the backend advertises protocol version 1 and `max_frame_bytes = 0`).
 */

import { execFile } from "node:child_process"
import { resolve } from "node:path"
import { fileURLToPath } from "node:url"
import { promisify } from "node:util"

import {
	GatewayClient,
	type GatewayClientOptions,
	type GatewayNotification,
	type GatewayServerRequestHandler,
	type GatewayTransport
} from "./gateway-client"

/** The grant `gateway_bootstrap.py` prints; the socket's URL already carries the ticket. */
export interface LocalGatewayGrant {
	url: string
	protocols: string[]
	profile_id: string
	instance_id: string
}

/** The helper's terminal verdict: no reconnect fixes it, so the caller should stop retrying. */
export const isTerminalBootstrapFailure = (message: string): boolean => /^gateway unavailable: /m.test(message)

export class GatewayBootstrapError extends Error {
	readonly terminal: boolean

	constructor(message: string) {
		super(message)
		this.name = "GatewayBootstrapError"
		this.terminal = isTerminalBootstrapFailure(message)
	}
}

export class GatewaySocketError extends Error {
	constructor(message: string) {
		super(message)
		this.name = "GatewaySocketError"
	}
}

export interface GatewayBootstrapOptions {
	/** Ensure a gateway owner (first launch). Defaults to `true`. */
	start?: boolean
	/** Re-ensure an owner that is not ready (crashed / idle-exited). */
	recover?: boolean
	/** Defaults to `HERMES_PYTHON`, then the platform `python`. */
	python?: string
	/** Repo root; defaults to `HERMES_PYTHON_SRC_ROOT`, then four levels above this file. */
	root?: string
	timeoutMs?: number
	/** Test seam: return the helper's stdout (JSON) instead of spawning Python. */
	run?: (python: string, args: string[], options: { cwd: string; env: NodeJS.ProcessEnv; timeoutMs: number }) => Promise<string>
}

const execFileAsync = promisify(execFile)

const defaultBootstrapRun: NonNullable<GatewayBootstrapOptions["run"]> = async (command, args, options) => {
	const { stdout } = await execFileAsync(command, args, { ...options, maxBuffer: 1024 * 1024 })
	return String(stdout)
}

const defaultPython = (): string =>
	process.env.HERMES_PYTHON?.trim() || (process.platform === "win32" ? "python" : "python3")

const defaultRoot = (): string =>
	process.env.HERMES_PYTHON_SRC_ROOT?.trim() || resolve(fileURLToPath(new URL(".", import.meta.url)), "../../../..")

/** Run the pairing helper and parse its grant; a helper failure becomes `GatewayBootstrapError`. */
export async function bootstrapLocalGateway(options: GatewayBootstrapOptions = {}): Promise<LocalGatewayGrant> {
	const python = options.python?.trim() || defaultPython()
	const root = options.root?.trim() || defaultRoot()
	const timeoutMs = options.timeoutMs ?? 40_000
	const args = [
		resolve(root, "ui-tui/scripts/gateway_bootstrap.py"),
		...(options.start === false ? [] : ["--start"]),
		...(options.recover ? ["--recover"] : [])
	]
	const env = { ...process.env, PYTHONPATH: root, HERMES_PYTHON_SRC_ROOT: root }
	let stdout: string
	try {
		stdout = await (options.run ?? defaultBootstrapRun)(python, args, { cwd: root, env, timeoutMs })
	} catch (error) {
		const message =
			error instanceof Error
				? String((error as Error & { stderr?: unknown }).stderr ?? error.message).trim()
				: String(error)
		throw new GatewayBootstrapError(message || "gateway bootstrap failed")
	}
	let parsed: unknown
	try {
		parsed = JSON.parse(stdout)
	} catch {
		throw new GatewayBootstrapError("gateway bootstrap returned no JSON grant")
	}
	if (typeof parsed !== "object" || parsed === null) {
		throw new GatewayBootstrapError("gateway bootstrap returned no JSON grant")
	}
	const grant = parsed as Partial<LocalGatewayGrant>
	if (
		typeof grant.url !== "string" ||
		!Array.isArray(grant.protocols) ||
		!grant.protocols.every((protocol) => typeof protocol === "string") ||
		typeof grant.profile_id !== "string" ||
		typeof grant.instance_id !== "string"
	) {
		throw new GatewayBootstrapError("gateway bootstrap returned a malformed grant")
	}
	return { url: grant.url, protocols: grant.protocols, profile_id: grant.profile_id, instance_id: grant.instance_id }
}

/** The subset of the browser/Bun WebSocket this transport uses (a test can substitute one). */
export interface GatewayWebSocketLike {
	readonly readyState?: number
	send(data: string): void
	close(code?: number, reason?: string): void
	addEventListener(type: "open" | "error", listener: () => void, options?: { once?: boolean }): void
	addEventListener(
		type: "message",
		listener: (event: { data: unknown }) => void,
		options?: { once?: boolean }
	): void
	addEventListener(
		type: "close",
		listener: (event: { code?: number; reason?: string }) => void,
		options?: { once?: boolean }
	): void
}

export type GatewaySocketFactory = (url: string, protocols: string[]) => GatewayWebSocketLike

const WS_OPEN = 1

const defaultSocketFactory: GatewaySocketFactory = (url, protocols) => {
	if (typeof WebSocket === "undefined") throw new GatewaySocketError("WebSocket API is unavailable")
	return (
		protocols.length ? new WebSocket(url, protocols) : new WebSocket(url)
	) as unknown as GatewayWebSocketLike
}

const frameText = (data: unknown): string | undefined => {
	if (typeof data === "string") return data
	if (data instanceof ArrayBuffer) return new TextDecoder().decode(data)
	return undefined
}

export interface GatewayConnectOptions {
	/** Defaults to {@link bootstrapLocalGateway} with `start: true`. */
	bootstrap?: () => Promise<LocalGatewayGrant>
	/** Forwarded to {@link GatewayClient}. */
	sessionId?: string
	profile?: string
	autoNegotiate?: boolean
	requestTimeoutMs?: number
	onEvent?: (notification: GatewayNotification) => void
	onServerRequest?: GatewayServerRequestHandler
	onError?: (error: Error) => void
	/** The socket closed (peer, heartbeat deadline, or {@link GatewayConnection.close}). */
	onClose?: (reason: string) => void
	socketFactory?: GatewaySocketFactory
	openTimeoutMs?: number
	/** `0` disables the `gateway.ping` keepalive. Defaults to 15 s. */
	heartbeatIntervalMs?: number
	/** A silent socket is dead after this long. Defaults to 45 s. */
	heartbeatDeadlineMs?: number
}

export interface GatewayConnection {
	client: GatewayClient
	grant: LocalGatewayGrant
	close(reason?: string): void
}

const waitForOpen = (socket: GatewayWebSocketLike, timeoutMs: number): Promise<void> => {
	if (socket.readyState === WS_OPEN) return Promise.resolve()
	return new Promise((resolvePromise, reject) => {
		let settled = false
		const finish = (action: () => void) => {
			if (settled) return
			settled = true
			clearTimeout(timer)
			action()
		}
		const timer = setTimeout(() => {
			finish(() => reject(new GatewaySocketError("gateway websocket open timed out")))
		}, timeoutMs)
		;(timer as unknown as { unref?: () => void }).unref?.()
		socket.addEventListener("open", () => finish(resolvePromise), { once: true })
		socket.addEventListener("error", () => finish(() => reject(new GatewaySocketError("gateway websocket connect failed"))), {
			once: true
		})
		socket.addEventListener(
			"close",
			(event) =>
				finish(() =>
					reject(new GatewaySocketError(`gateway websocket closed during connect (${event.code ?? "?"})`))
				),
			{ once: true }
		)
	})
}

/**
 * Pair with the gateway, dial it, and return a {@link GatewayClient} bound to the socket.
 *
 * Frames that arrive before the client exists (the backend writes `gateway.ready` immediately after
 * accept) are buffered and replayed in order, so readiness can never race the wiring.
 */
export async function connectGateway(options: GatewayConnectOptions = {}): Promise<GatewayConnection> {
	const grant = await (options.bootstrap ?? (() => bootstrapLocalGateway()))()
	const socket = (options.socketFactory ?? defaultSocketFactory)(grant.url, grant.protocols)
	const inbox: string[] = []
	let client: GatewayClient | null = null
	let closed = false
	let heartbeat: ReturnType<typeof setInterval> | null = null
	let lastInbound = Date.now()
	let pingId = 0

	const fail = (error: Error) => options.onError?.(error)

	const close = (reason = "gateway socket closed") => {
		if (closed) return
		closed = true
		if (heartbeat) clearInterval(heartbeat)
		heartbeat = null
		client?.close(reason)
		try {
			socket.close(1000, reason)
		} catch {
			// The peer is already gone; close is best-effort.
		}
		options.onClose?.(reason)
	}

	socket.addEventListener("message", (event) => {
		lastInbound = Date.now()
		const text = frameText(event.data)
		if (text === undefined) {
			fail(new GatewaySocketError("gateway frame is not text"))
			return
		}
		if (client) client.handleFrame(text)
		else inbox.push(text)
	})
	await waitForOpen(socket, options.openTimeoutMs ?? 15_000)

	socket.addEventListener("error", () => fail(new GatewaySocketError("gateway websocket error")))
	socket.addEventListener("close", (event) => close(`gateway socket closed (${event.code ?? "?"})`))

	const transport: GatewayTransport = {
		send: (text) => {
			if (closed) throw new GatewaySocketError("gateway socket is closed")
			socket.send(text)
		}
	}
	const clientOptions: GatewayClientOptions = {
		transport,
		sessionId: options.sessionId,
		profile: options.profile,
		autoNegotiate: options.autoNegotiate,
		requestTimeoutMs: options.requestTimeoutMs,
		onEvent: options.onEvent,
		onServerRequest: options.onServerRequest,
		onError: options.onError
	}
	client = new GatewayClient(clientOptions)
	for (const text of inbox.splice(0)) client.handleFrame(text)

	const interval = options.heartbeatIntervalMs ?? 15_000
	const deadline = options.heartbeatDeadlineMs ?? 45_000
	if (interval > 0) {
		heartbeat = setInterval(() => {
			if (closed) return
			if (Date.now() - lastInbound > deadline) {
				fail(new GatewaySocketError(`gateway heartbeat deadline exceeded (${deadline} ms)`))
				close("gateway heartbeat deadline exceeded")
				return
			}
			try {
				socket.send(JSON.stringify({ jsonrpc: "2.0", id: `hb-${++pingId}`, method: "gateway.ping" }))
			} catch (error) {
				fail(error instanceof Error ? error : new GatewaySocketError(String(error)))
			}
		}, interval)
		;(heartbeat as unknown as { unref?: () => void }).unref?.()
	}

	return { client, grant, close }
}
