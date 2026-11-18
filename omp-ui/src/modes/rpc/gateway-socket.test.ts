import { afterEach, describe, expect, test, vi } from "bun:test"

import { GatewayBootstrapError, bootstrapLocalGateway, connectGateway, isTerminalBootstrapFailure } from "./gateway-socket"
import type { GatewayConnection } from "./gateway-socket"

const READY = {
	jsonrpc: "2.0",
	method: "event",
	params: {
		type: "gateway.ready",
		payload: {
			skin: {},
			change_events: true,
			replay_epoch: "r-1",
			heartbeat: true,
			session_authority: true,
			protocol_version: 1,
			supported_protocol_versions: [1],
			max_frame_bytes: 0,
			max_reassembled_frame_bytes: 0
		}
	}
}

const grantFor = (port: number) => ({
	url: `ws://127.0.0.1:${port}`,
	protocols: [] as string[],
	profile_id: "p-1",
	instance_id: "i-1"
})

/** A real event-loop turn (captured before fake timers), never a wall-clock delay. */
const nextTurn = setImmediate
const flushTurns = async (turns = 12) => {
	for (let i = 0; i < turns; i += 1) await new Promise((resolve) => nextTurn(resolve))
}

let connection: GatewayConnection | null = null
let server: ReturnType<typeof Bun.serve> | null = null

afterEach(() => {
	connection?.close("test done")
	connection = null
	server?.stop(true)
	server = null
	vi.useRealTimers()
})

describe("gateway pairing", () => {
	test("runs the helper, passes --start, and parses the grant", async () => {
		const grant = await bootstrapLocalGateway({
			run: async (python, args) => {
				expect(python).toBeTruthy()
				expect(args[0]).toMatch(/gateway_bootstrap\.py$/)
				expect(args.slice(1)).toEqual(["--start"])
				return JSON.stringify({ url: "ws://127.0.0.1:1", protocols: ["hermes"], profile_id: "p", instance_id: "i" })
			}
		})
		expect(grant).toEqual({ url: "ws://127.0.0.1:1", protocols: ["hermes"], profile_id: "p", instance_id: "i" })
	})

	test("a terminal helper verdict is carried as `terminal`", async () => {
		const failure = Object.assign(new Error("Command failed"), {
			stderr: "gateway unavailable: bootstrap_dependencies\nrun `hermes pm repair`, then restart"
		})
		const caught = await bootstrapLocalGateway({
			run: async () => {
				throw failure
			}
		}).then(
			() => null,
			(error: unknown) => error
		)
		expect(caught).toBeInstanceOf(GatewayBootstrapError)
		expect((caught as GatewayBootstrapError).terminal).toBe(true)
		expect(isTerminalBootstrapFailure((caught as Error).message)).toBe(true)
	})

	test("a malformed grant is refused instead of dialing nonsense", async () => {
		await expect(bootstrapLocalGateway({ run: async () => JSON.stringify({ url: 7 }) })).rejects.toBeInstanceOf(
			GatewayBootstrapError
		)
	})
})

describe("gateway socket", () => {
	test("dials the granted URL, buffers early frames, and negotiates on the real wire", async () => {
		let onReady: (() => void) | undefined
		const ready = new Promise<void>((resolve) => {
			onReady = resolve
		})
		server = Bun.serve({
			port: 0,
			fetch(request, srv) {
				if (srv.upgrade(request, { data: null })) return
				return new Response("upgrade required", { status: 400 })
			},
			websocket: {
				open(ws) {
					ws.send(JSON.stringify(READY))
				},
				message(ws, raw) {
					const frame = JSON.parse(String(raw)) as { id?: unknown; method?: string }
					if (frame.method === "client.capabilities") {
						ws.send(
							JSON.stringify({
								jsonrpc: "2.0",
								id: frame.id,
								result: {
									server_requests: [],
									protocol_version: 1,
									supported_protocol_versions: [1],
									max_frame_bytes: 0,
									max_reassembled_frame_bytes: 0
								}
							})
						)
					}
				}
			}
		})
		connection = await connectGateway({
			bootstrap: async () => grantFor(server!.port!),
			heartbeatIntervalMs: 0,
			requestTimeoutMs: 5000,
			onEvent: (notification) => {
				if (notification.type === "gateway.ready") onReady?.()
			}
		})
		await ready
		const capabilities = await connection.client.negotiate()
		expect(connection.grant.instance_id).toBe("i-1")
		expect(connection.client.protocol).toMatchObject({ version: 1, replayEpoch: "r-1", heartbeat: true })
		expect(capabilities.protocol_version).toBe(1)
	})

	test("sends gateway.ping keepalives and stays open while the peer answers", async () => {
		vi.useFakeTimers()
		const errors: Error[] = []
		let pings = 0
		let onReady: (() => void) | undefined
		const ready = new Promise<void>((resolve) => {
			onReady = resolve
		})
		server = Bun.serve({
			port: 0,
			fetch(request, srv) {
				if (srv.upgrade(request, { data: null })) return
				return new Response("upgrade required", { status: 400 })
			},
			websocket: {
				open(ws) {
					ws.send(JSON.stringify(READY))
				},
				message(ws, raw) {
					const frame = JSON.parse(String(raw)) as { id?: unknown; method?: string }
					if (frame.method === "gateway.ping") {
						pings += 1
						ws.send(JSON.stringify({ jsonrpc: "2.0", id: frame.id, result: { ok: true } }))
					}
					if (frame.method === "client.capabilities") {
						ws.send(JSON.stringify({ jsonrpc: "2.0", id: frame.id, result: { server_requests: [] } }))
					}
				}
			}
		})
		connection = await connectGateway({
			bootstrap: async () => grantFor(server!.port!),
			heartbeatIntervalMs: 10,
			heartbeatDeadlineMs: 2000,
			requestTimeoutMs: 5000,
			onError: (error) => errors.push(error),
			onEvent: (notification) => {
				if (notification.type === "gateway.ready") onReady?.()
			}
		})
		await ready
		vi.advanceTimersByTime(10)
		await flushTurns()
		vi.advanceTimersByTime(10)
		await flushTurns()
		expect(pings).toBeGreaterThanOrEqual(2)
		expect(errors).toEqual([])
		expect(connection.client.ready).not.toBeNull()
	})

	test("a silent socket trips the heartbeat deadline and closes the client", async () => {
		vi.useFakeTimers()
		const errors: Error[] = []
		let onReady: (() => void) | undefined
		const ready = new Promise<void>((resolve) => {
			onReady = resolve
		})
		server = Bun.serve({
			port: 0,
			fetch(request, srv) {
				if (srv.upgrade(request, { data: null })) return
				return new Response("upgrade required", { status: 400 })
			},
			websocket: {
				open(ws) {
					ws.send(JSON.stringify(READY))
				},
				message() {
					// Deliberately never answers gateway.ping.
				}
			}
		})
		connection = await connectGateway({
			bootstrap: async () => grantFor(server!.port!),
			heartbeatIntervalMs: 10,
			heartbeatDeadlineMs: 40,
			requestTimeoutMs: 5000,
			onError: (error) => errors.push(error),
			onEvent: (notification) => {
				if (notification.type === "gateway.ready") onReady?.()
			}
		})
		await ready
		vi.advanceTimersByTime(50)
		await flushTurns()
		expect(errors[0]?.message).toContain("heartbeat deadline")
		expect(connection.client.ready).toBeNull()
	})
})
