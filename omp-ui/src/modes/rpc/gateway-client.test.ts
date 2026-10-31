import { describe, expect, test } from "bun:test"

import {
	GATEWAY_PROTOCOL_FALLBACK,
	GatewayClient,
	GatewayClientError,
	GatewayProtocolError,
	decodeNotification,
	isGatewayEvent,
	normalizeGatewayError,
	toJsonRpcRequest,
	toOmpEventFrame,
	toOmpResponse
} from "./gateway-client"
import type { GatewayClientOptions, GatewayNotification } from "./gateway-client"

function harness(overrides: Partial<GatewayClientOptions> = {}) {
	const sent: Record<string, unknown>[] = []
	const events: GatewayNotification[] = []
	const errors: Error[] = []
	const client = new GatewayClient({
		transport: { send: (text) => sent.push(JSON.parse(text) as Record<string, unknown>) },
		autoNegotiate: false,
		requestTimeoutMs: 0,
		onEvent: (notification) => events.push(notification),
		onError: (error) => errors.push(error),
		...overrides
	})
	const frame = (value: unknown) => client.handleFrame(JSON.stringify(value))
	return { client, sent, events, errors, frame }
}

const READY = {
	jsonrpc: "2.0",
	method: "event",
	params: {
		type: "gateway.ready",
		payload: {
			skin: {},
			change_events: true,
			replay_epoch: "r-7",
			heartbeat: true,
			session_authority: true,
			protocol_version: 1,
			supported_protocol_versions: [1, 2],
			max_frame_bytes: 65536,
			max_reassembled_frame_bytes: 262144
		}
	}
}

describe("business RPC envelope", () => {
	test("a request is JSON-RPC 2.0 with a numeric id and the params unchanged", async () => {
		const { client, sent, frame } = harness()
		const pending = client.request<{ value: string }>("config.get", { key: "model" })
		expect(sent[0]).toEqual({ jsonrpc: "2.0", id: 1, method: "config.get", params: { key: "model" } })
		frame({ jsonrpc: "2.0", id: 1, result: { value: "m" } })
		expect(await pending).toEqual({ value: "m" })
	})

	test("an error keeps the message and exposes data.reason as the string code", async () => {
		const { client, frame } = harness()
		const pending = client.request<unknown>("session.mutate", { session_id: "s" })
		frame({
			jsonrpc: "2.0",
			id: 1,
			error: { code: 4090, message: "session is busy", data: { reason: "SESSION_OWNER_BUSY" } }
		})
		const error = await pending.then(
			() => null,
			(caught: unknown) => caught
		)
		expect(error).toBeInstanceOf(GatewayClientError)
		expect(error).toMatchObject({ message: "session is busy", rpcCode: 4090, reason: "SESSION_OWNER_BUSY" })
	})

	test("normalizeGatewayError prefers data.reason but still reports the numeric code", () => {
		expect(normalizeGatewayError({ code: 4001, message: "invalid_params", data: { reason: "invalid_params" } })).toMatchObject(
			{ code: "invalid_params", rpcCode: 4001, reason: "invalid_params" }
		)
		expect(normalizeGatewayError({ code: -32601, message: "unknown method" })).toMatchObject({
			code: "-32601",
			rpcCode: -32601,
			reason: undefined
		})
	})

	test("omp's response envelope is derived from result / error", () => {
		expect(toOmpResponse("7", "set_model", { id: "7", result: { model: "m" } })).toEqual({
			type: "response",
			id: "7",
			command: "set_model",
			success: true,
			data: { model: "m" }
		})
		expect(toOmpResponse(7, "set_model", { id: 7, error: { code: 4040, message: "no", data: { reason: "not_found" } } })).toEqual({
			type: "response",
			id: "7",
			command: "set_model",
			success: false,
			error: "no",
			code: "not_found"
		})
	})

	test("toJsonRpcRequest projects an omp command through its route and injects the envelope", () => {
		const request = toJsonRpcRequest(
			9,
			{ type: "rename", id: "cmd-9", name: "new title" } as { type: string; id?: string; name: string },
			{ method: "session.title", params: (command) => ({ title: (command as unknown as { name: string }).name }) },
			{ sessionId: "s1", expectedRevision: 4, expectedGeneration: 2 }
		)
		expect(request).toEqual({
			jsonrpc: "2.0",
			id: 9,
			method: "session.title",
			params: { title: "new title", session_id: "s1", request_id: "cmd-9", expected_revision: 4, expected_generation: 2 }
		})
	})

	test("the envelope adds profile for session-less profile-scoped methods", () => {
		const request = toJsonRpcRequest(3, { type: "profiles_list" }, { method: "profiles.list" }, { profile: "work" })
		expect(request.params).toEqual({ profile: "work" })
	})

	test("sendCommand answers omp's envelope for success and for a refusal", async () => {
		const { client, sent, frame } = harness({ sessionId: "s1" })
		const route = { method: "session.title", params: () => ({ title: "t" }) } as const

		const ok = client.sendCommand({ type: "rename", id: "c1" }, route)
		expect(sent[0]).toMatchObject({ method: "session.title", params: { title: "t", session_id: "s1", request_id: "c1" } })
		frame({ jsonrpc: "2.0", id: 1, result: { title: "t" } })
		expect(await ok).toMatchObject({ type: "response", command: "rename", success: true, data: { title: "t" } })

		const refused = client.sendCommand({ type: "rename", id: "c2" }, route)
		frame({ jsonrpc: "2.0", id: 2, error: { code: 4001, message: "bad", data: { reason: "invalid_params" } } })
		expect(await refused).toMatchObject({
			type: "response",
			command: "rename",
			success: false,
			error: "bad",
			code: "invalid_params"
		})
	})
})

describe("realtime RPC envelope", () => {
	test("an event notification is unwrapped and stamped with session, seq and replay epoch", () => {
		const { client, events, frame } = harness({ autoNegotiate: false })
		frame({ jsonrpc: "2.0", method: "event", params: { ...READY.params } })
		frame({
			jsonrpc: "2.0",
			method: "event",
			params: { type: "tool.start", session_id: "s1", seq: 41, payload: { tool_id: "t", name: "read" } }
		})
		expect(events.at(-1)).toEqual({
			type: "tool.start",
			payload: { tool_id: "t", name: "read" },
			sessionId: "s1",
			seq: 41,
			replayEpoch: "r-7"
		})
	})

	test("omp's flat frame carries the payload keys plus the transport metadata", () => {
		const notification = decodeNotification(
			{ type: "message.delta", session_id: "s1", seq: 2, payload: { text: "hi", turn_id: "t" } },
			"r-1"
		)
		expect(toOmpEventFrame(notification)).toEqual({
			type: "message.delta",
			text: "hi",
			turn_id: "t",
			sessionId: "s1",
			seq: 2,
			replayEpoch: "r-1"
		})
	})

	test("a notification without payload still has an empty object, and a missing type is a protocol error", () => {
		expect(decodeNotification({ type: "session.settled" })).toEqual({ type: "session.settled", payload: {} })
		expect(() => decodeNotification({ payload: {} })).toThrow(GatewayProtocolError)
	})

	test("replay-row association metadata is promoted beside the payload", () => {
		const notification = decodeNotification(
			{
				type: "message.complete",
				session_id: "s1",
				seq: 7,
				authority_epoch: 12,
				execution_generation: 3,
				admission_id: "adm-1",
				payload: { text: "done" }
			},
			"r-1"
		)
		expect(notification).toMatchObject({
			authorityEpoch: 12,
			executionGeneration: 3,
			admissionId: "adm-1"
		})
		expect(toOmpEventFrame(notification)).toMatchObject({
			type: "message.complete",
			authorityEpoch: 12,
			executionGeneration: 3,
			admissionId: "adm-1"
		})
	})

	test("a canonical replay row's own epoch outranks the connection's", () => {
		const notification = decodeNotification(
			{ type: "message.complete", session_id: "s1", seq: 8, replay_epoch: "r-2", payload: {} },
			"r-1"
		)
		expect(notification.replayEpoch).toBe("r-2")
		expect(decodeNotification({ type: "message.delta", payload: {} }, "r-1").replayEpoch).toBe("r-1")
	})

	test("isGatewayEvent narrows a known event name", () => {
		const notification = decodeNotification({ type: "message.delta", payload: { text: "x" } })
		expect(isGatewayEvent(notification, "message.delta")).toBe(true)
		expect(isGatewayEvent(notification, "tool.start")).toBe(false)
	})

	test("a server→client request is delivered with its JSON-RPC id and answered on the same id", () => {
		const sent: Record<string, unknown>[] = []
		const client = new GatewayClient({
			transport: { send: (text) => sent.push(JSON.parse(text) as Record<string, unknown>) },
			autoNegotiate: false,
			requestTimeoutMs: 0,
			onServerRequest: (request) => {
				expect(request).toEqual({ id: "srq-1", method: "clarify", params: { session_id: "s1" } })
				client.respond<"clarify">("srq-1", { answers: { q1: "a" } })
				return true
			}
		})
		client.handleFrame(
			JSON.stringify({ jsonrpc: "2.0", id: "srq-1", method: "clarify", params: { session_id: "s1" } })
		)
		expect(sent).toEqual([{ jsonrpc: "2.0", id: "srq-1", result: { answers: { q1: "a" } } }])
	})

	test("an unhandled server→client request is answered -32601 instead of leaving the backend waiting", () => {
		const { client, sent } = harness()
		client.handleFrame(JSON.stringify({ jsonrpc: "2.0", id: "srq-2", method: "approval", params: {} }))
		expect(sent).toEqual([
			{ jsonrpc: "2.0", id: "srq-2", error: { code: -32601, message: "unknown server request: approval" } }
		])
	})

	test("a server→client handler that throws is answered -32603 and reported, not left open", () => {
		const thrown = new Error("boom")
		const { client, sent, errors } = harness({
			onServerRequest: () => {
				throw thrown
			}
		})
		client.handleFrame(JSON.stringify({ jsonrpc: "2.0", id: "srq-3", method: "clarify", params: {} }))
		expect(sent).toEqual([
			{ jsonrpc: "2.0", id: "srq-3", error: { code: -32603, message: "server request handler failed: clarify" } }
		])
		expect(errors).toEqual([thrown])
	})
})

describe("transport negotiation", () => {
	test("gateway.ready advertises the transport; capabilities echoes the same budget", async () => {
		const { client, sent, events, frame } = harness({ autoNegotiate: true })
		frame(READY)
		expect(events[0]?.type).toBe("gateway.ready")
		expect(client.protocol).toEqual({
			version: 1,
			supportedVersions: [1, 2],
			maxFrameBytes: 65536,
			maxReassembledFrameBytes: 262144,
			replayEpoch: "r-7",
			heartbeat: true,
			sessionAuthority: true
		})

		const negotiation = client.negotiate()
		expect(sent[0]).toMatchObject({
			jsonrpc: "2.0",
			method: "client.capabilities",
			params: {
				server_requests: true,
				protocol_version: 1,
				supported_protocol_versions: [1, 2],
				max_frame_bytes: 65536,
				max_reassembled_frame_bytes: 262144
			}
		})
		frame({
			jsonrpc: "2.0",
			id: sent[0]?.id,
			result: { server_requests: ["clarify"], protocol_version: 1, supported_protocol_versions: [1, 2] }
		})
		expect(await negotiation).toMatchObject({ protocol_version: 1 })
		expect(client.capabilities).toMatchObject({ server_requests: ["clarify"] })
	})

	test("before gateway.ready the protocol falls back to the no-chunking defaults", () => {
		const { client } = harness()
		expect(client.protocol).toEqual({
			version: GATEWAY_PROTOCOL_FALLBACK.protocolVersion,
			supportedVersions: [...GATEWAY_PROTOCOL_FALLBACK.supportedProtocolVersions],
			maxFrameBytes: GATEWAY_PROTOCOL_FALLBACK.maxFrameBytes,
			maxReassembledFrameBytes: GATEWAY_PROTOCOL_FALLBACK.maxReassembledFrameBytes,
			replayEpoch: undefined,
			heartbeat: false,
			sessionAuthority: false
		})
	})

	test("a request past its deadline rejects instead of leaking a pending id", async () => {
		const { client } = harness({ requestTimeoutMs: 5 })
		await expect(client.request<unknown>("config.get", { key: "model" })).rejects.toBeInstanceOf(GatewayClientError)
	})
})

describe("transport framing", () => {
	test("feed splits newline-delimited stdio frames and ignores blanks", () => {
		const { client, events } = harness()
		client.feed(
			`${JSON.stringify(READY)}\n\n${JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "notice", payload: { message: "x" } } })}\n`
		)
		expect(events.map((event) => event.type)).toEqual(["gateway.ready", "notice"])
	})

	test("malformed input surfaces as a transport error rather than throwing at the caller", () => {
		const { client, errors } = harness()
		client.handleFrame("{not json")
		expect(errors[0]).toBeInstanceOf(SyntaxError)
	})

	test("close rejects in-flight requests and forgets the previous connection's negotiation", async () => {
		const { client, sent, frame } = harness({ autoNegotiate: true })
		frame(READY)
		const negotiation = client.negotiate()
		frame({ jsonrpc: "2.0", id: sent[0]?.id, result: { server_requests: [] } })
		await negotiation
		expect(client.ready).not.toBeNull()
		expect(client.capabilities).not.toBeNull()

		const pending = client.request<unknown>("config.get", { key: "model" })
		client.close("socket closed")
		await expect(pending).rejects.toThrow("socket closed")
		expect(client.ready).toBeNull()
		expect(client.capabilities).toBeNull()
		expect(client.protocol.replayEpoch).toBeUndefined()
	})

	test("feed holds a partial line until its newline, and flush routes the EOF tail", () => {
		const { client, events } = harness()
		const notice = JSON.stringify({
			jsonrpc: "2.0",
			method: "event",
			params: { type: "notice", payload: { message: "x" } }
		})
		client.feed(notice.slice(0, 20))
		expect(events).toHaveLength(0)
		client.feed(`${notice.slice(20)}\n`)
		expect(events.map((event) => event.type)).toEqual(["notice"])

		client.feed(notice)
		expect(events).toHaveLength(1)
		client.flush()
		expect(events.map((event) => event.type)).toEqual(["notice", "notice"])
	})

	test("a session event at or below its watermark is dropped; a new epoch resets the guard", () => {
		const { client, events, frame } = harness()
		frame(READY)
		const emit = (seq: number) =>
			frame({
				jsonrpc: "2.0",
				method: "event",
				params: { type: "tool.start", session_id: "s1", seq, payload: { tool_id: "t" } }
			})

		emit(41)
		emit(41)
		emit(40)
		emit(42)
		expect(events.filter((event) => event.type === "tool.start").map((event) => event.seq)).toEqual([41, 42])
		expect([...client.watermarks]).toEqual([["s1", 42]])

		// Same epoch (reconnect): the cursor survives and the replay's newer frames flow.
		client.close()
		frame(READY)
		emit(43)
		expect(client.watermarks.get("s1")).toBe(43)

		// Backend restart: a new epoch restarts seq, so a lower seq is new again.
		frame({
			jsonrpc: "2.0",
			method: "event",
			params: {
				type: "gateway.ready",
				payload: { ...READY.params.payload, replay_epoch: "r-8" }
			}
		})
		emit(1)
		expect(events.filter((event) => event.type === "tool.start").map((event) => event.seq)).toEqual([41, 42, 43, 1])
		expect(client.watermarks.get("s1")).toBe(1)
	})

	test("dedupeSeq:false hands every physical frame to the consumer", () => {
		const { client, events, frame } = harness({ dedupeSeq: false })
		frame(READY)
		for (const seq of [1, 1]) {
			frame({
				jsonrpc: "2.0",
				method: "event",
				params: { type: "notice", session_id: "s1", seq, payload: { message: "x" } }
			})
		}
		expect(events.filter((event) => event.type === "notice")).toHaveLength(2)
		expect(client.watermarks.size).toBe(0)
	})
})
