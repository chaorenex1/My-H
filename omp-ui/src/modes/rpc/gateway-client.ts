/**
 * Hermes gateway transport: JSON-RPC 2.0 envelopes for omp-ui.
 *
 * This module is the **protocol** half of the omp-ui ↔ Hermes alignment (`docs/appendix-omp-ui-
 * realtime-rpc.md` A.2 and `docs/appendix-omp-ui-business-rpc.md` B.2), not a business client:
 *
 * - one wire: newline-delimited JSON-RPC over stdio or one JSON object per WebSocket text frame;
 *   {@link GatewayClient.feed} buffers a pipe chunk until its newline, so a split frame is never
 *   half-parsed;
 * - realtime RPC (`server → client`): `event` notifications are unwrapped from `params.type` /
 *   `params.payload` and stamped with `session_id`, the per-session `seq` and the connection's
 *   `replay_epoch`; `seq` is also the duplicate guard — a session event not newer than the last one
 *   dispatched under the same epoch is dropped, and the per-session cursor ({@link
 *   GatewayClient.watermarks}) survives `close()` for the reconnect replay. Server→client requests
 *   keep their JSON-RPC `id` for the reply;
 * - business RPC (`client → server`): requests carry `jsonrpc`/`id`/`method`/`params`, and the
 *   omp response envelope (`{type:"response", id, command, success, data|error, code}`) is derived
 *   from `result` / `error`, with `error.data.reason` normalized to omp's string `code`;
 * - transport negotiation: `gateway.ready` advertises `protocol_version`, the supported versions and
 *   the frame limits; `client.capabilities` re-declares them (the gateway never emits `rpc_chunk`).
 *
 * Method names and params are intentionally NOT mapped here: `GatewayCommandRoute` is the seam a
 * caller fills with one entry per omp command (`type → method` + params projection). Everything in
 * this file is envelope-level and applies to every method unchanged.
 */

import type {
	BackendGatewayEventMap,
	BackendGatewayEventName,
	ClientCapabilitiesParams,
	ClientCapabilitiesResult,
	GatewayReadyPayload,
	RpcMethod,
	RpcMethods,
	ServerRequestMap,
	ServerRequestMethod
} from "./gateway-contract.generated"

export type GatewayRequestId = string | number

/** Fallbacks used before `gateway.ready` arrives; the payload is authoritative once seen. */
export const GATEWAY_PROTOCOL_FALLBACK = {
	protocolVersion: 1,
	supportedProtocolVersions: [1],
	maxFrameBytes: 0,
	maxReassembledFrameBytes: 0
} as const

/** JSON-RPC error member as the gateway sends it (`{code, message, data:{reason,…}}`). */
export interface JsonRpcErrorObject {
	code?: number
	message?: string
	data?: unknown
}

/** Any frame on the wire. `event` notifications have `method` but no `id`. */
export interface JsonRpcFrame {
	jsonrpc?: string
	id?: GatewayRequestId | null
	method?: string
	params?: Record<string, unknown>
	result?: unknown
	error?: JsonRpcErrorObject
}

/** Params of an `event` notification, before the payload is unwrapped. */
export interface JsonRpcEventParams {
	type?: string
	session_id?: string | null
	seq?: number | null
	payload?: Record<string, unknown> | null
	/** Replay rows only (`session.events.since`): the authority CAS/admission association. */
	authority_epoch?: number | null
	execution_generation?: number | null
	admission_id?: string | null
	/** Replay rows carry their own epoch; live frames only get the connection's. */
	replay_epoch?: string | null
}

/**
 * One decoded notification. This is omp's frame metadata promoted off the JSON-RPC envelope:
 * `type` is the discriminator, `payload` is the business object, and the three transport fields are
 * what a replay/duplicate guard needs (`seq` is monotonic per session; `replayEpoch` changes when the
 * backend restarts, so the client resets its per-session watermark).
 */
export interface GatewayNotification<P = Record<string, unknown>> {
	type: string
	payload: P
	sessionId?: string
	seq?: number
	replayEpoch?: string
	/** Replay-row association (`session.events.since`); live frames normally omit these. */
	authorityEpoch?: number
	executionGeneration?: number
	admissionId?: string
}

/** omp's flat notification envelope: `{type, …payload}`. */
export type OmpEventFrame = { type: string } & Record<string, unknown>

/** omp's `RpcResponse` envelope (`wire/frames.ts`), with Hermes' association id. */
export interface OmpResponseFrame<T = unknown> {
	type: "response"
	id?: string
	command: string
	success: boolean
	data?: T
	error?: string
	code?: string
}

/** Server→client request as handed to a handler; the JSON-RPC id is the reply correlation. */
export interface GatewayServerRequest<M extends string = string, P extends Record<string, unknown> = Record<string, unknown>> {
	id: string
	method: M
	params: P
}

/** Answers a server→client request; return `false`/`void` when the caller has no handler. */
export type GatewayServerRequestHandler = (request: GatewayServerRequest) => boolean | void

/** Anything that can carry one serialized JSON-RPC frame (stdio writer, WebSocket, test spy). */
export interface GatewayTransport {
	send(text: string): void
}

/**
 * Protocol-level projection of one omp command onto a Hermes method.
 *
 * `params` receives the omp command (everything except `id`) and returns the Hermes params; the
 * client then adds the envelope fields it owns (`session_id`, `request_id`, CAS fields). Keeping the
 * table outside this module is deliberate: the wire layer must not grow a second copy of the
 * method catalog.
 */
export interface GatewayCommandRoute<C = Record<string, unknown>> {
	method: string
	params?: (command: C) => Record<string, unknown>
}

export type GatewayCommandRoutes<C = Record<string, unknown>> = Record<string, GatewayCommandRoute<C>>

/** Envelope fields every write on a session carries (`docs/…-business-rpc.md` B.2.1). */
export interface GatewayEnvelopeFields {
	sessionId?: string
	/** Profile for session-less profile-scoped methods; omp commands carry no profile dimension. */
	profile?: string
	requestId?: string
	expectedRevision?: number
	expectedGeneration?: number
}

/** The connection's negotiated transport facts. */
export interface GatewayProtocolState {
	version: number
	supportedVersions: number[]
	maxFrameBytes: number
	maxReassembledFrameBytes: number
	replayEpoch?: string
	heartbeat: boolean
	sessionAuthority: boolean
}

export class GatewayClientError extends Error {
	readonly rpcCode?: number
	readonly reason?: string
	readonly data?: unknown

	constructor(message: string, options: { rpcCode?: number; reason?: string; data?: unknown } = {}) {
		super(message)
		this.name = "GatewayClientError"
		this.rpcCode = options.rpcCode
		this.reason = options.reason
		this.data = options.data
	}
}

export class GatewayProtocolError extends GatewayClientError {
	constructor(message: string) {
		super(message)
		this.name = "GatewayProtocolError"
	}
}

/** JSON-RPC "method not found", what an unhandled server→client request answers. */
export const JSON_RPC_METHOD_NOT_FOUND = -32601
/** JSON-RPC "internal error", what a throwing server→client request handler answers. */
export const JSON_RPC_INTERNAL_ERROR = -32603

const isObject = (value: unknown): value is Record<string, unknown> =>
	typeof value === "object" && value !== null && !Array.isArray(value)

/**
 * Normalize a JSON-RPC error member for omp: the string `code` is `data.reason` when the gateway
 * sends one (canonical dispatch always does), otherwise the numeric code rendered as a string, so a
 * caller's `switch (code)` never has to handle two shapes.
 */
export function normalizeGatewayError(error: JsonRpcErrorObject | undefined): {
	message: string
	code?: string
	rpcCode?: number
	reason?: string
	data?: unknown
} {
	const message = typeof error?.message === "string" && error.message ? error.message : "Hermes RPC failed"
	const data = error?.data
	const reason = isObject(data) && typeof data.reason === "string" ? data.reason : undefined
	const rpcCode = typeof error?.code === "number" ? error.code : undefined
	const code = reason ?? (rpcCode === undefined ? undefined : String(rpcCode))
	return { message, code, rpcCode, reason, data }
}

/** Decode the `params` of an `event` notification (A.2.1). Throws on a frame with no `type`. */
export function decodeNotification(params: unknown, replayEpoch?: string): GatewayNotification {
	if (!isObject(params)) throw new GatewayProtocolError("event notification params are not an object")
	const type = params.type
	if (typeof type !== "string" || !type) throw new GatewayProtocolError("event notification has no params.type")
	const payload = isObject(params.payload) ? params.payload : {}
	const sessionId = typeof params.session_id === "string" ? params.session_id : undefined
	const seq = typeof params.seq === "number" ? params.seq : undefined
	const notification: GatewayNotification = { type, payload }
	if (sessionId !== undefined) notification.sessionId = sessionId
	if (seq !== undefined) notification.seq = seq
	// A canonical replay row carries its own epoch (CanonicalReplayEvent.replay_epoch); a live
	// frame only has the connection's. The row wins so the watermark guard compares like with like.
	const epoch = typeof params.replay_epoch === "string" ? params.replay_epoch : replayEpoch
	if (epoch !== undefined) notification.replayEpoch = epoch
	const authorityEpoch = typeof params.authority_epoch === "number" ? params.authority_epoch : undefined
	const executionGeneration = typeof params.execution_generation === "number" ? params.execution_generation : undefined
	const admissionId = typeof params.admission_id === "string" ? params.admission_id : undefined
	if (authorityEpoch !== undefined) notification.authorityEpoch = authorityEpoch
	if (executionGeneration !== undefined) notification.executionGeneration = executionGeneration
	if (admissionId !== undefined) notification.admissionId = admissionId
	return notification
}

/** omp's flat event frame: payload keys at the top level plus the transport metadata. */
export function toOmpEventFrame(notification: GatewayNotification): OmpEventFrame {
	const frame: OmpEventFrame = { type: notification.type, ...notification.payload }
	if (notification.sessionId !== undefined) frame.sessionId = notification.sessionId
	if (notification.seq !== undefined) frame.seq = notification.seq
	if (notification.replayEpoch !== undefined) frame.replayEpoch = notification.replayEpoch
	if (notification.authorityEpoch !== undefined) frame.authorityEpoch = notification.authorityEpoch
	if (notification.executionGeneration !== undefined) frame.executionGeneration = notification.executionGeneration
	if (notification.admissionId !== undefined) frame.admissionId = notification.admissionId
	return frame
}

/** Derive omp's `RpcResponse` from a JSON-RPC response frame (`command` is the caller's request). */
export function toOmpResponse<T = unknown>(
	id: GatewayRequestId | null | undefined,
	command: string,
	frame: JsonRpcFrame
): OmpResponseFrame<T> {
	const normalized = normalizeGatewayError(frame.error)
	const base = { type: "response" as const, ...(id == null ? {} : { id: String(id) }), command }
	if (frame.error) {
		return { ...base, success: false, error: normalized.message, ...(normalized.code ? { code: normalized.code } : {}) }
	}
	return { ...base, success: true, data: frame.result as T }
}

/**
 * Build the JSON-RPC request for one omp command.
 *
 * @param id JSON-RPC id; omp omits it, Hermes requires one per request.
 * @param command the omp frame (`{type, …}`); `type` is not sent and `id` becomes `request_id`.
 */
export function toJsonRpcRequest<C extends { type: string; id?: string }>(
	id: GatewayRequestId,
	command: C,
	route: GatewayCommandRoute<C>,
	envelope: GatewayEnvelopeFields = {}
): JsonRpcFrame & { jsonrpc: "2.0"; id: GatewayRequestId; method: string; params: Record<string, unknown> } {
	const { type: _type, id: commandId, ...rest } = command as Record<string, unknown> & { type: string; id?: string }
	const projected = route.params ? route.params(rest as C) : Object.fromEntries(Object.entries(rest))
	const params: Record<string, unknown> = { ...projected }
	const sessionId = envelope.sessionId
	if (sessionId !== undefined && params.session_id === undefined) params.session_id = sessionId
	if (envelope.profile !== undefined && params.profile === undefined) params.profile = envelope.profile
	const requestId = envelope.requestId ?? commandId
	if (requestId !== undefined && params.request_id === undefined) params.request_id = requestId
	if (envelope.expectedRevision !== undefined && params.expected_revision === undefined) {
		params.expected_revision = envelope.expectedRevision
	}
	if (envelope.expectedGeneration !== undefined && params.expected_generation === undefined) {
		params.expected_generation = envelope.expectedGeneration
	}
	return { jsonrpc: "2.0", id, method: route.method, params }
}

interface PendingRequest {
	resolve: (value: unknown) => void
	reject: (error: Error) => void
	cancel?: () => void
}

export interface GatewayClientOptions {
	transport: GatewayTransport
	sessionId?: string
	/** Profile injected into session-less profile-scoped methods (`params.profile`). */
	profile?: string
	/**
	 * Send `client.capabilities` as soon as `gateway.ready` arrives (A.2.1: both halves declare the
	 * transport once per connection). Defaults to `true`.
	 */
	autoNegotiate?: boolean
	/**
	 * Drop a session event whose `seq` is not newer than the last one dispatched under the same
	 * `replay_epoch` (A.2.1: the per-session watermark is the reconnect duplicate guard). Defaults
	 * to `true`; set `false` only for a consumer that needs every physical frame.
	 */
	dedupeSeq?: boolean
	/** `0` disables the per-request deadline. Defaults to 120 s. */
	requestTimeoutMs?: number
	onEvent?: (notification: GatewayNotification) => void
	/** Return `true` when you answer with `respond()`/`fail()`; leaving it unanswered sends -32601. */
	onServerRequest?: GatewayServerRequestHandler
	/** Transport-level failures (unhandled frames, negotiation errors). */
	onError?: (error: Error) => void
}

/**
 * One connection to a Hermes gateway. Transport-agnostic: the owner feeds inbound text through
 * {@link feed} (stdio lines) or decoded frames through {@link handleFrame} (WebSocket).
 */
export class GatewayClient {
	private readonly options: GatewayClientOptions
	private readonly pending = new Map<string, PendingRequest>()
	private nextId = 1
	private readyPayload: GatewayReadyPayload | null = null
	private capabilitiesResult: ClientCapabilitiesResult | null = null
	private negotiation: Promise<ClientCapabilitiesResult> | null = null
	private lastReplayEpoch: string | undefined
	/** Incomplete stdio line; a pipe chunk is not a frame boundary. */
	private lineBuffer = ""
	/** Last dispatched `seq` per session: the reconnect replay cursor (survives `close()`). */
	private readonly seqWatermarks = new Map<string, number>()
	/** Epoch the watermarks belong to; a backend restart invalidates them. */
	private watermarkEpoch: string | undefined

	constructor(options: GatewayClientOptions) {
		this.options = options
		this.lastReplayEpoch = undefined
	}

	/** Last dispatched `seq` per session, for `session.events.since` after a reconnect. */
	get watermarks(): ReadonlyMap<string, number> {
		return this.seqWatermarks
	}

	/** The last `gateway.ready` payload, or `null` before it arrives. */
	get ready(): GatewayReadyPayload | null {
		return this.readyPayload
	}

	/** The `client.capabilities` answer, or `null` before negotiation. */
	get capabilities(): ClientCapabilitiesResult | null {
		return this.capabilitiesResult
	}

	/** Negotiated transport facts; the gateway's own values once `gateway.ready` arrived. */
	get protocol(): GatewayProtocolState {
		const ready = this.readyPayload
		return {
			version: ready?.protocol_version ?? GATEWAY_PROTOCOL_FALLBACK.protocolVersion,
			supportedVersions: ready?.supported_protocol_versions ?? [...GATEWAY_PROTOCOL_FALLBACK.supportedProtocolVersions],
			maxFrameBytes: ready?.max_frame_bytes ?? GATEWAY_PROTOCOL_FALLBACK.maxFrameBytes,
			maxReassembledFrameBytes:
				ready?.max_reassembled_frame_bytes ?? GATEWAY_PROTOCOL_FALLBACK.maxReassembledFrameBytes,
			replayEpoch: ready?.replay_epoch ?? this.lastReplayEpoch,
			heartbeat: ready?.heartbeat === true,
			sessionAuthority: ready?.session_authority === true
		}
	}

	/**
	 * Feed transport text: one stdio chunk, not necessarily a whole line. Complete lines are routed;
	 * the tail is held until the newline (or {@link flush}) arrives, so a pipe split mid-frame is not
	 * parsed twice or half-parsed.
	 */
	feed(text: string): void {
		this.lineBuffer += text
		let newline = this.lineBuffer.indexOf("\n")
		while (newline !== -1) {
			const line = this.lineBuffer.slice(0, newline)
			this.lineBuffer = this.lineBuffer.slice(newline + 1)
			if (line.trim()) this.handleFrame(line)
			newline = this.lineBuffer.indexOf("\n")
		}
	}

	/** Route the trailing line at EOF (a stdio peer may end without a final newline). */
	flush(): void {
		const line = this.lineBuffer
		this.lineBuffer = ""
		if (line.trim()) this.handleFrame(line)
	}

	/** Decode and route one inbound frame (JSON text or an already-parsed object). */
	handleFrame(frame: string | JsonRpcFrame): void {
		let parsed: unknown
		if (typeof frame === "string") {
			try {
				parsed = JSON.parse(frame)
			} catch (error) {
				this.fail(error instanceof Error ? error : new Error(String(error)))
				return
			}
		} else {
			parsed = frame
		}
		if (!isObject(parsed)) {
			this.fail(new GatewayProtocolError("inbound frame is not a JSON object"))
			return
		}
		try {
			this.route(parsed as JsonRpcFrame)
		} catch (error) {
			this.fail(error instanceof Error ? error : new Error(String(error)))
		}
	}

	private route(frame: JsonRpcFrame): void {
		if (frame.method === "event") {
			const notification = decodeNotification(frame.params, this.lastReplayEpoch)
			if (notification.type === "gateway.ready") {
				this.readyPayload = notification.payload as unknown as GatewayReadyPayload
				this.lastReplayEpoch = notification.payload.replay_epoch as string | undefined
				// A backend restart restarts seq per session: the old watermarks would suppress the
				// new stream. A reconnect under the same epoch keeps them for the replay fetch.
				this.adoptReplayEpoch(this.lastReplayEpoch)
				if (this.options.autoNegotiate !== false) void this.negotiate().catch((error) => this.fail(error))
				this.options.onEvent?.(notification)
				return
			}
			if (this.options.dedupeSeq !== false && this.isDuplicate(notification)) return
			this.options.onEvent?.(notification)
			return
		}
		if (frame.id !== undefined && frame.id !== null && typeof frame.method === "string") {
			this.deliverServerRequest(frame)
			return
		}
		if (frame.id !== undefined && frame.id !== null && (frame.result !== undefined || frame.error !== undefined)) {
			this.settle(frame)
			return
		}
		throw new GatewayProtocolError(`unrecognized inbound frame: ${JSON.stringify(frame).slice(0, 200)}`)
	}

	/** Bind the watermarks to the connection's epoch, dropping them when the backend restarted. */
	private adoptReplayEpoch(epoch: string | undefined): void {
		if (this.watermarkEpoch !== epoch) {
			this.seqWatermarks.clear()
			this.watermarkEpoch = epoch
		}
	}

	/** True when this session event was already delivered (same epoch, seq not newer). */
	private isDuplicate(notification: GatewayNotification): boolean {
		const sessionId = notification.sessionId
		if (sessionId === undefined) return false
		// A canonical replay row names its own epoch; a row stamped under a different epoch is a
		// different sequence space, so its low seq is new, not a duplicate.
		if (notification.replayEpoch !== undefined && notification.replayEpoch !== this.watermarkEpoch) {
			this.seqWatermarks.clear()
			this.watermarkEpoch = notification.replayEpoch
		}
		const seq = notification.seq
		if (typeof seq !== "number" || !Number.isFinite(seq)) return false
		const last = this.seqWatermarks.get(sessionId)
		if (last !== undefined && seq <= last) return true
		this.seqWatermarks.set(sessionId, seq)
		return false
	}

	private deliverServerRequest(frame: JsonRpcFrame): void {
		const request: GatewayServerRequest = {
			id: String(frame.id),
			method: frame.method as string,
			params: isObject(frame.params) ? frame.params : {}
		}
		let handled: boolean
		try {
			handled = this.options.onServerRequest?.(request) === true
		} catch (error) {
			// A backend handler can block for a long time (clarify: 3600 s); a crashed client must
			// answer -32603 now, exactly as @hermes/shared's JsonRpcRequestChannel does.
			this.failRequest(request.id, JSON_RPC_INTERNAL_ERROR, `server request handler failed: ${request.method}`)
			this.fail(error instanceof Error ? error : new Error(String(error)))
			return
		}
		if (!handled) this.failRequest(request.id, JSON_RPC_METHOD_NOT_FOUND, `unknown server request: ${request.method}`)
	}

	private settle(frame: JsonRpcFrame): void {
		const key = String(frame.id)
		const pending = this.pending.get(key)
		if (!pending) return
		this.pending.delete(key)
		pending.cancel?.()
		if (frame.error) {
			const normalized = normalizeGatewayError(frame.error)
			pending.reject(
				new GatewayClientError(normalized.message, {
					rpcCode: normalized.rpcCode,
					reason: normalized.reason,
					data: normalized.data
				})
			)
			return
		}
		pending.resolve(frame.result)
	}

	/** Send one JSON-RPC request and resolve with its `result`. */
	request<M extends RpcMethod>(method: M, params: RpcMethods[M]["params"]): Promise<RpcMethods[M]["result"]>;
	request<T = unknown>(method: string, params: object): Promise<T>;
	request(method: string, params: object): Promise<unknown> {
		return this.call(this.nextId++, method, params)
	}

	private call(id: number, method: string, params: object): Promise<unknown> {
		const frame: JsonRpcFrame & { jsonrpc: "2.0"; id: number; method: string; params: Record<string, unknown> } = {
			jsonrpc: "2.0",
			id,
			method,
			params: params as Record<string, unknown>
		}
		return new Promise((resolve, reject) => {
			const timeoutMs = this.options.requestTimeoutMs ?? 120_000
			const entry: PendingRequest = { resolve, reject }
			if (timeoutMs > 0) {
				const timer = setTimeout(() => {
					if (this.pending.delete(String(id))) {
						reject(new GatewayClientError(`request timed out: ${method}`))
					}
				}, timeoutMs)
				entry.cancel = () => clearTimeout(timer)
			}
			this.pending.set(String(id), entry)
			try {
				this.options.transport.send(JSON.stringify(frame))
			} catch (error) {
				entry.cancel?.()
				this.pending.delete(String(id))
				reject(error instanceof Error ? error : new Error(String(error)))
			}
		})
	}

	/** Send one omp command through its protocol route and answer omp's response envelope. */
	async sendCommand<C extends { type: string; id?: string }>(
		command: C,
		route: GatewayCommandRoute<C>,
		envelope: GatewayEnvelopeFields = {}
	): Promise<OmpResponseFrame> {
		const id = this.nextId++
		const frame = toJsonRpcRequest(id, command, route, {
			sessionId: this.options.sessionId,
			profile: this.options.profile,
			...envelope
		})
		try {
			const result = await this.call(id, frame.method, frame.params)
			return toOmpResponse(id, command.type, { id, result })
		} catch (error) {
			const normalized =
				error instanceof GatewayClientError
					? { message: error.message, code: error.reason ?? (error.rpcCode === undefined ? undefined : String(error.rpcCode)) }
					: { message: error instanceof Error ? error.message : String(error), code: undefined }
			return {
				type: "response",
				id: String(id),
				command: command.type,
				success: false,
				error: normalized.message,
				...(normalized.code ? { code: normalized.code } : {})
			}
		}
	}

	/**
	 * Declare this connection's transport budget (`client.capabilities`, A.2.1/B.3.1) and remember
	 * the answer. Safe to call repeatedly; one in-flight negotiation is enough.
	 */
	async negotiate(overrides: Partial<ClientCapabilitiesParams> = {}): Promise<ClientCapabilitiesResult> {
		if (this.negotiation) return this.negotiation
		const protocol = this.protocol
		const params: ClientCapabilitiesParams = {
			server_requests: true,
			protocol_version: protocol.version,
			supported_protocol_versions: protocol.supportedVersions,
			max_frame_bytes: protocol.maxFrameBytes,
			max_reassembled_frame_bytes: protocol.maxReassembledFrameBytes,
			...overrides
		}
		const negotiation = this.request("client.capabilities", params) as Promise<ClientCapabilitiesResult>
		this.negotiation = negotiation
		try {
			const result = await negotiation
			this.capabilitiesResult = result
			return result
		} finally {
			this.negotiation = null
		}
	}

	/** Answer a server→client request with its result (`{id, result}` on the wire). */
	respond<M extends ServerRequestMethod>(id: string, result: ServerRequestMap[M]["result"]): void {
		this.options.transport.send(JSON.stringify({ jsonrpc: "2.0", id, result }))
	}

	/** Answer a server→client request with an error (the backend treats it as unanswered). */
	failRequest(id: string, code: number, message: string): void {
		this.options.transport.send(JSON.stringify({ jsonrpc: "2.0", id, error: { code, message } }))
	}

	/** Reject every in-flight request (transport closed) and forget the connection state. */
	close(reason = "gateway connection closed"): void {
		for (const [key, pending] of this.pending) {
			pending.cancel?.()
			pending.reject(new GatewayClientError(reason))
			this.pending.delete(key)
		}
		// A reconnect starts a new connection: the old ready/negotiation/partial line must not leak
		// into it. Watermarks do survive — they are what the reconnect replay asks from.
		this.readyPayload = null
		this.capabilitiesResult = null
		this.negotiation = null
		this.lastReplayEpoch = undefined
		this.lineBuffer = ""
	}

	private fail(error: Error): void {
		this.options.onError?.(error)
	}
}

/** Type guard for a notification of one known event name (narrows the payload type). */
export function isGatewayEvent<K extends BackendGatewayEventName>(
	notification: GatewayNotification,
	type: K
): notification is GatewayNotification & { type: K; payload: BackendGatewayEventMap[K] } {
	return notification.type === type
}
