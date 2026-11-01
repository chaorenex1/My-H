/**
 * The newest paired gateway connection.
 *
 * Boot-time bridges outlive a single connection: `hermes omp` installs the Appearance write-through
 * on the connection it pairs at start, but `/resume` (and any later session switch) closes that one
 * and pairs another. Those bridges resolve their client here, so writes follow the live socket
 * instead of failing against the closed one.
 */

import type { GatewayClient } from "./gateway-client"

let active: GatewayClient | undefined

export function setLiveGatewayClient(client: GatewayClient | undefined): void {
	active = client
}

export function liveGatewayClient(fallback: GatewayClient): GatewayClient {
	return active ?? fallback
}
