import { describe, expect, it } from "bun:test"

import "../../config/all-settings"
import { all } from "../../config/registry"
import { Settings } from "../../config/settings"
import { cfgSymbolPreset, cfgThemeDark } from "../../modes/settings"
import {
	APPEARANCE_TAB,
	applyGatewayAppearance,
	installGatewayAppearancePersistence,
	loadGatewayAppearance,
	type AppearancePersistenceOptions
} from "./gateway-appearance"
import type { GatewayClient } from "./gateway-client"
import { setLiveGatewayClient } from "./gateway-live-client"

interface RpcCall {
	method: string
	params: Record<string, unknown>
}

function fakeClient(calls: RpcCall[], stored: Record<string, unknown> = {}): GatewayClient {
	return {
		async request(method: string, params: Record<string, unknown>) {
			calls.push({ method, params })
			if (method === "omp.appearance.get") return { values: stored }
			return { key: params.key, value: params.value }
		}
	} as unknown as GatewayClient
}

describe("gateway appearance", () => {
	it("loads the profile's stored values", async () => {
		const client = fakeClient([], { "theme.dark": "titanium", symbolPreset: "nerd" })
		expect(await loadGatewayAppearance(client)).toEqual({ "theme.dark": "titanium", symbolPreset: "nerd" })
	})

	it("applies only ids the Appearance tab renders", async () => {
		const settings = Settings.isolated()
		const applied = applyGatewayAppearance(settings, {
			"theme.dark": "titanium",
			symbolPreset: "nerd",
			"not.a.setting": true,
			"git.enabled": false
		})
		expect(applied).toEqual(["theme.dark", "symbolPreset"])
		expect(cfgThemeDark.get(settings)).toBe("titanium")
		expect(cfgSymbolPreset.get(settings)).toBe("nerd")
	})

	it("skips a value this build rejects instead of failing boot", async () => {
		const settings = Settings.isolated()
		expect(applyGatewayAppearance(settings, { symbolPreset: "not-a-preset", "theme.dark": "titanium" })).toEqual([
			"theme.dark"
		])
		expect(cfgThemeDark.get(settings)).toBe("titanium")
		expect(cfgSymbolPreset.get(settings)).toBe("unicode")
	})

	it("routes Appearance edits to the runtime override and the gateway, not the shell config", async () => {
		const settings = Settings.isolated()
		const calls: RpcCall[] = []
		const persistence = installGatewayAppearancePersistence({
			settings,
			client: fakeClient(calls)
		} satisfies AppearancePersistenceOptions)
		try {
			cfgSymbolPreset.set(settings, "ascii")
			expect(cfgSymbolPreset.get(settings)).toBe("ascii")
			cfgSymbolPreset.unset(settings)
			await persistence.flush()
			expect(calls).toEqual([
				{ method: "omp.appearance.set", params: { key: "symbolPreset", value: "ascii" } },
				{ method: "omp.appearance.set", params: { key: "symbolPreset", value: null } }
			])
		} finally {
			persistence.uninstall()
		}
		// Restored to the normal persistence path once the gateway sink is gone.
		cfgSymbolPreset.set(settings, "nerd")
		expect(cfgSymbolPreset.get(settings)).toBe("nerd")
	})

	it("writes follow the newest paired connection after a session switch", async () => {
		const settings = Settings.isolated()
		const first: RpcCall[] = []
		const second: RpcCall[] = []
		const persistence = installGatewayAppearancePersistence({ settings, client: fakeClient(first) })
		try {
			setLiveGatewayClient(fakeClient(second))
			cfgSymbolPreset.set(settings, "ascii")
			await persistence.flush()
			expect(first).toEqual([])
			expect(second).toEqual([{ method: "omp.appearance.set", params: { key: "symbolPreset", value: "ascii" } }])
		} finally {
			persistence.uninstall()
			setLiveGatewayClient(undefined)
		}
	})

	it("reports a failed write without reverting the value", async () => {
		const settings = Settings.isolated()
		const errors: Error[] = []
		const failing = {
			async request(method: string) {
				if (method === "omp.appearance.set") throw new Error("config locked")
				return { values: {} }
			}
		} as unknown as GatewayClient
		const persistence = installGatewayAppearancePersistence({
			settings,
			client: failing,
			onError: error => errors.push(error)
		})
		try {
			cfgThemeDark.set(settings, "titanium")
			await persistence.flush()
			expect(cfgThemeDark.get(settings)).toBe("titanium")
			expect(errors.map(error => error.message)).toEqual(["config locked"])
		} finally {
			persistence.uninstall()
		}
	})

	it("keeps Hermes' copy of the Appearance defaults in sync", async () => {
		const committed = (await Bun.file(
			new URL("../../../../hermes_cli/data/omp_appearance_defaults.json", import.meta.url)
		).json()) as Record<string, unknown>
		const current = Object.fromEntries(
			all()
				.filter(setting => setting.ui?.tab === APPEARANCE_TAB)
				.map(setting => [setting.id, setting.default])
		)
		expect(committed).toEqual(current)
	})
})
