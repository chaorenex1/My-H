import { describe, expect, test } from "bun:test"

import { getBundledModels, getBundledProviders } from "../vendor/catalog/src/models.ts"
import { Settings } from "../config/settings"
import { MODEL_ROLES } from "../config/model-roles"
import { ThinkingLevel } from "../vendor/agent/src/index.ts"
import type { Model } from "../vendor/ai/src/index.ts"
import { GatewayRolesClient, type ModelRoleRows, type ModelRoleStore } from "./rpc/gateway-roles"
import { createModelHubCallbacks } from "./controllers/selector-controller"
import { cycleSessionModel } from "./controllers/input-controller"
import { createHermesModelHubSource } from "./model-hub-source"
import { roleCycleRoutes } from "./rpc/gateway-session"
import type {
	ModelCycleSetParams,
	ModelPresetParams,
	ModelPresetsSummary,
	ModelRoleFallbackEntry,
	ModelRoleRow,
	ModelRolesSetParams,
} from "./rpc/gateway-contract.generated"

const chatModels = getBundledProviders()
	.flatMap((provider) => getBundledModels(provider))
	.filter((model) => (model.kind ?? "chat") === "chat")
const main = chatModels[0] as Model
const aux = chatModels.find((model) => model.provider !== main.provider) as Model

const row = (overrides: Partial<ModelRoleRow> & { id: string }): ModelRoleRow => ({
	label: overrides.id,
	provider: "auto",
	model: "auto",
	fallback_chain: [],
	...overrides,
})

const fakeRows = (
	rows: ModelRoleRow[],
	extra: Partial<Pick<ModelRoleRows, "cycleOrder" | "presets" | "modelChains">> = {},
): ModelRoleRows => ({
	revision: 0,
	rows,
	cycleOrder: extra.cycleOrder ?? [],
	presets: extra.presets,
	modelChains: extra.modelChains ?? {},
	byId: (id) => rows.find((entry) => entry.id === id),
})

/** Drain the microtask chain behind a fire-and-forget callback without real time. */
const flush = async () => {
	for (let turn = 0; turn < 16; turn++) await Promise.resolve()
}

describe("Hermes model hub source", () => {
	const rows = [
		row({
			id: "vision",
			label: "Image captions",
			tag: "VISION",
			provider: aux.provider,
			model: aux.id,
			reasoning_effort: "high",
			fallback_chain: [
				{ provider: main.provider, model: main.id },
				{ provider: "", model: "orphan" },
				{ provider: "orphan", model: "" },
			],
		}),
		row({ id: "web_tools", label: "", provider: "auto", model: "auto" }),
		row({ id: "advisor", label: "Advisor", tag: "ADVISOR", group: "imported", target: "adviser" }),
	]
	const modelChains: Record<string, ModelRoleFallbackEntry[]> = {
		"deepseek/*": [{ provider: "openrouter", model: "backup" }],
		"openai/gpt": [{ provider: "", model: "orphan" }],
	}
	const source = createHermesModelHubSource(
		fakeRows(rows, {
			cycleOrder: ["vision", "advisor"],
			presets: { names: ["fast", "deep"], active: "fast" },
			modelChains,
		}),
		Settings.isolated(),
		{ model: main },
	)

	test("lists default first, then every gateway role", () => {
		expect(source.knownRoleIds).toEqual(["default", "vision", "web_tools", "advisor"])
	})

	test("resolves pinned rows and leaves auto rows unresolved", () => {
		expect(source.getModelRole("vision")).toBe(`${aux.provider}/${aux.id}`)
		expect(source.getGlobalModelRole("vision")).toBe(`${aux.provider}/${aux.id}`)
		expect(source.getModelRoleSource("vision")).toBe("global")
		expect(source.getModelRole("web_tools")).toBeUndefined()
		expect(source.getModelRoleSource("web_tools")).toBe("default")
		expect(source.getProjectModelRole("vision")).toBeUndefined()
	})

	test("resolves the default role from the live session model", () => {
		expect(source.knownRoleIds[0]).toBe("default")
		expect(source.getModelRole("default")).toBe(`${main.provider}/${main.id}`)
		expect(source.getModelRoleSource("default")).toBe("default")
	})

	test("shows no omp auto-selection chain for Hermes roles", () => {
		// `vision` exists in both vocabularies: omp's built-in priority chain must not be shown as
		// the Hermes row's "auto" chain, because here auto means the main model.
		expect(source.defaultRoleChain("vision")).toEqual([])
		expect(source.defaultRoleChain("default")).toEqual([])
	})

	test("maps fallback chains to provider/model selectors and skips incomplete entries", () => {
		// Role rows and `auxiliary.fallback_chains` keys share the hub's selector map.
		expect(source.fallbackChains).toEqual({
			vision: [`${main.provider}/${main.id}`],
			"deepseek/*": ["openrouter/backup"],
		})
	})

	test("returns the gateway cycle order", () => {
		expect(source.cycleOrder).toEqual(["vision", "advisor"])
	})

	test("exposes the gateway preset summary to the hub", () => {
		expect(source.getModelPresets?.()).toEqual({ names: ["fast", "deep"], active: "fast" })
	})

	test("omits the preset strip when the gateway sends no presets payload", () => {
		const withoutPresets = createHermesModelHubSource(fakeRows(rows), Settings.isolated(), { model: main })
		expect(withoutPresets.getModelPresets).toBeUndefined()
	})

	test("uses omp's default-role metadata, the server tag and the row label", () => {
		expect(source.getRoleInfo("default")).toEqual(MODEL_ROLES.default)
		const vision = source.getRoleInfo("vision")
		expect(vision).toMatchObject({ tag: "VISION", name: "Image captions", section: "chat" })
		expect(vision.accepts(main)).toBe(true)
		// An unlabelled row falls back to its id, and an untagged row to the uppercased label.
		expect(source.getRoleInfo("web_tools")).toMatchObject({ tag: "WEB_TOOLS", name: "web_tools" })
	})

	test("renders the imported advisor row with its server tag", () => {
		expect(source.getRoleInfo("advisor")).toMatchObject({
			tag: "ADVISOR",
			name: "Advisor",
			section: "chat",
		})
	})

	test("does not let an unknown @role alias resolve to a bundled model", () => {
		const resolved = source.resolveRoleValue("@smol", [main, aux])
		expect(resolved.model).toBeUndefined()
		expect(source.resolveRoleValue(`@vision`, [main, aux]).model).toBe(aux)
	})
})

interface WriterCall {
	method:
		| "assign"
		| "clear"
		| "setFallbackChain"
		| "setCycleOrder"
		| "savePreset"
		| "applyPreset"
		| "deletePreset"
		| "setModelChain"
		| "refresh"
	args: unknown[]
}

function fakeWriter(calls: WriterCall[], fail?: string): ModelRoleStore {
	const table = [row({ id: "vision", label: "Vision" })]
	return {
		revision: 0,
		rows: table,
		cycleOrder: [],
		presets: undefined,
		modelChains: {},
		byId: (id) => table.find((entry) => entry.id === id),
		async refresh() {
			if (fail) throw new Error(fail)
			calls.push({ method: "refresh", args: [] })
			return table
		},
		async assign(params: ModelRolesSetParams) {
			if (fail) throw new Error(fail)
			calls.push({ method: "assign", args: [params] })
			return row({ id: params.role })
		},
		async clear(role: string) {
			calls.push({ method: "clear", args: [role] })
			return row({ id: role })
		},
		async setFallbackChain(role: string, entries) {
			calls.push({ method: "setFallbackChain", args: [role, entries] })
			return row({ id: role })
		},
		async setCycleOrder(order) {
			calls.push({ method: "setCycleOrder", args: [order] })
			return order
		},
		async savePreset(name) {
			calls.push({ method: "savePreset", args: [name] })
			return name
		},
		async applyPreset(name) {
			calls.push({ method: "applyPreset", args: [name] })
			return table
		},
		async deletePreset(name) {
			calls.push({ method: "deletePreset", args: [name] })
			return name
		},
		async setModelChain(key, entries) {
			calls.push({ method: "setModelChain", args: [key, entries] })
		},
	}
}

function harness(options: { fail?: string } = {}) {
	const calls: WriterCall[] = []
	const writer = fakeWriter(calls, options.fail)
	const statuses: string[] = []
	const errors: string[] = []
	const switched: Model[] = []
	let refreshed = 0
	const callbacks = createModelHubCallbacks({
		roles: writer,
		session: {
			model: main,
			async setModel(model) {
				switched.push(model)
			},
		},
		showStatus: (message) => statuses.push(message),
		showError: (message) => errors.push(message),
		invalidateStatusLine: () => {},
		requestRender: () => {},
		refreshHub: () => {
			refreshed++
		},
		close: () => {},
	})
	return { callbacks, calls, statuses, errors, switched, refreshed: () => refreshed }
}

/** A real `GatewayRolesClient` over a fake JSON-RPC request recorder. */
function gatewayHarness(options: { failApply?: string } = {}) {
	const requests: Array<{ method: string; params: unknown }> = []
	const snapshot = {
		roles: [row({ id: "vision", label: "Vision", tag: "VISION" })],
		cycle_order: ["vision"],
		presets: { names: ["base"], active: "" } satisfies ModelPresetsSummary,
		model_chains: {},
	}
	const client = {
		async request(method: string, params: unknown) {
			requests.push({ method, params })
			switch (method) {
				case "model.roles.get":
					return snapshot
				case "model.roles.set": {
					const value = params as ModelRolesSetParams
					return { role: row({ id: value.role }) }
				}
				case "model.cycle.set":
					return { order: (params as ModelCycleSetParams).order }
				case "model.presets.save":
					return { name: (params as ModelPresetParams).name }
				case "model.presets.apply":
					if (options.failApply) throw new Error(options.failApply)
					return { roles: snapshot.roles, presets: { names: ["base"], active: "base" }, cycle_order: ["vision"] }
				case "model.presets.delete":
					return { name: (params as ModelPresetParams).name }
				default:
					return {}
			}
		},
	} as never
	const roles = new GatewayRolesClient(client)
	const statuses: string[] = []
	const errors: string[] = []
	let refreshed = 0
	const callbacks = createModelHubCallbacks({
		roles,
		session: {
			model: main,
			async setModel() {},
		},
		showStatus: (message) => statuses.push(message),
		showError: (message) => errors.push(message),
		invalidateStatusLine: () => {},
		requestRender: () => {},
		refreshHub: () => {
			refreshed++
		},
		close: () => {},
	})
	return { callbacks, roles, requests, statuses, errors, refreshed: () => refreshed }
}

describe("model hub assignment callbacks", () => {
	test("assigns an auxiliary role through model.roles.set", async () => {
		const view = harness()
		await view.callbacks.onAssign(aux, "vision", ThinkingLevel.High, `${aux.provider}/${aux.id}`)
		expect(view.calls).toEqual([
			{
				method: "assign",
				args: [
					{
						role: "vision",
						provider: aux.provider,
						model: aux.id,
						reasoning_effort: "high",
					},
				],
			},
		])
		expect(view.switched).toEqual([])
		expect(view.refreshed()).toBe(1)
	})

	test("maps auto and off thinking levels onto the wire vocabulary", async () => {
		const view = harness()
		await view.callbacks.onAssign(aux, "vision", "auto", `${aux.provider}/${aux.id}`)
		await view.callbacks.onAssign(aux, "vision", ThinkingLevel.Off, `${aux.provider}/${aux.id}`)
		expect(view.calls.map((call) => (call.args[0] as ModelRolesSetParams).reasoning_effort)).toEqual([
			"inherit",
			"none",
		])
	})

	test("assigns the imported advisor with inherit and no chain or effort", async () => {
		const view = harness()
		await view.callbacks.onAssign(aux, "advisor", undefined, `${aux.provider}/${aux.id}`)
		expect(view.calls).toEqual([
			{
				method: "assign",
				args: [
					{
						role: "advisor",
						provider: aux.provider,
						model: aux.id,
						reasoning_effort: "inherit",
					},
				],
			},
		])
	})

	test("routes a default assignment to the session and writes no role", async () => {
		const view = harness()
		await view.callbacks.onAssign(main, "default", undefined, `${main.provider}/${main.id}`)
		expect(view.switched).toEqual([main])
		expect(view.calls).toEqual([])
		expect(view.statuses.at(-1)).toBe(`Model: ${main.provider}/${main.id}`)
	})

	test("clears an auxiliary role but only points the default role at /model", async () => {
		const view = harness()
		view.callbacks.onUnassign("default")
		expect(view.statuses.at(-1)).toContain("/model")
		expect(view.calls).toEqual([])

		view.callbacks.onUnassign("vision")
		await flush()
		expect(view.calls).toEqual([{ method: "clear", args: ["vision"] }])
		expect(view.refreshed()).toBe(1)
	})

	test("replaces a fallback chain with provider/model entries and skips malformed selectors", async () => {
		const view = harness()
		view.callbacks.onFallbackChainChange?.("vision", [`${main.provider}/${main.id}`, "missing-slash", "a/"])
		await flush()
		expect(view.calls).toEqual([
			{
				method: "setFallbackChain",
				args: ["vision", [{ provider: main.provider, model: main.id }]],
			},
		])
	})

	test("routes a model-keyed chain through setModelChain", async () => {
		const view = harness()
		view.callbacks.onFallbackChainChange?.("deepseek/*", [`${main.provider}/${main.id}`])
		await flush()
		expect(view.calls).toEqual([
			{
				method: "setModelChain",
				args: ["deepseek/*", [{ provider: main.provider, model: main.id }]],
			},
		])
	})

	test("ignores the default role's chain", async () => {
		const view = harness()
		view.callbacks.onFallbackChainChange?.("default", [`${main.provider}/${main.id}`])
		await flush()
		expect(view.calls).toEqual([])
	})

	test("surfaces a gateway failure instead of throwing into the overlay", async () => {
		const view = harness({ fail: "role locked" })
		await expect(
			view.callbacks.onAssign(aux, "vision", ThinkingLevel.Low, `${aux.provider}/${aux.id}`),
		).resolves.toBeUndefined()
		expect(view.errors).toEqual(["role locked"])
	})
})

describe("model hub preset and cycle callbacks", () => {
	test("saves a preset then re-reads the role table", async () => {
		const view = gatewayHarness()
		view.callbacks.onSavePreset?.("work")
		await flush()
		expect(view.requests).toContainEqual({ method: "model.presets.save", params: { name: "work" } })
		expect(view.requests.map((entry) => entry.method)).toContain("model.roles.get")
		expect(view.statuses.at(-1)).toBe("Preset saved: work")
		expect(view.refreshed()).toBe(1)
	})

	test("applies a preset and reports the resulting role count", async () => {
		const view = gatewayHarness()
		await view.callbacks.onSwitchPreset?.("base")
		expect(view.requests).toContainEqual({ method: "model.presets.apply", params: { name: "base" } })
		expect(view.statuses.at(-1)).toBe("Preset applied: base (1 role)")
		expect(view.refreshed()).toBe(1)
	})

	test("surfaces a refused preset switch instead of throwing into the overlay", async () => {
		const view = gatewayHarness({ failApply: "unknown preset: gone" })
		await expect(view.callbacks.onSwitchPreset?.("gone")).resolves.toBeUndefined()
		expect(view.errors).toEqual(["unknown preset: gone"])
	})

	test("persists the cycle order through model.cycle.set", async () => {
		const view = gatewayHarness()
		view.callbacks.onCycleOrderChange?.(["vision", "advisor"])
		await flush()
		expect(view.requests).toContainEqual({
			method: "model.cycle.set",
			params: { order: ["vision", "advisor"] },
		})
		expect(view.statuses.at(-1)).toBe("Model cycle: vision → advisor")
		expect(view.refreshed()).toBe(1)
	})
})

describe("session model cycle", () => {
	const routes = [
		{ role: "vision", provider: aux.provider, model: aux.id },
		{ role: "task", provider: main.provider, model: main.id },
	]

	test("walks the configured role order in both directions", async () => {
		const find = (provider: string, id: string) =>
			[main, aux].find((model) => model.provider === provider && model.id === id)
		const forward = await cycleSessionModel(routes, find, main, "forward", async () => undefined)
		expect(forward).toEqual({ role: "vision", model: aux })
		const backward = await cycleSessionModel(routes, find, aux, "backward", async () => undefined)
		expect(backward).toEqual({ role: "task", model: main })
	})

	test("drops auto and unresolvable roles from the gateway order", () => {
		const roleRows = fakeRows(
			[
				row({ id: "vision", provider: aux.provider, model: aux.id }),
				row({ id: "web_tools", provider: "auto", model: "auto" }),
				row({ id: "advisor", provider: "missing", model: "model" }),
			],
			{ cycleOrder: ["vision", "web_tools", "advisor", "unknown"] },
		)
		expect(roleCycleRoutes(roleRows)).toEqual([
			{ role: "vision", provider: aux.provider, model: aux.id },
			{ role: "advisor", provider: "missing", model: "model" },
		])
	})

	test("falls back to the catalog pool when no role is pinned", async () => {
		let catalogCalls = 0
		const next = await cycleSessionModel([], () => undefined, main, "forward", async () => {
			catalogCalls++
			return { model: aux }
		})
		expect(next).toEqual({ model: aux })
		expect(catalogCalls).toBe(1)
	})
})

describe("gateway roles client", () => {
	test("caches the snapshot and adopts a set result", async () => {
		const requests: Array<{ method: string; params: unknown }> = []
		const client = {
			async request(method: string, params: unknown) {
				requests.push({ method, params })
				if (method === "model.roles.get") {
					return {
						roles: [row({ id: "vision", label: "Vision" })],
						cycle_order: ["vision"],
						presets: { names: ["base"], active: "base" },
						model_chains: { "deepseek/*": [{ provider: "openrouter", model: "backup" }] },
					}
				}
				const paramsValue = params as ModelRolesSetParams
				return { role: row({ id: paramsValue.role, label: "Updated" }) }
			},
		} as never
		const roles = new GatewayRolesClient(client)
		await roles.refresh()
		expect(requests[0]).toEqual({ method: "model.roles.get", params: {} })
		expect(roles.rows.map((entry) => entry.id)).toEqual(["vision"])
		expect(roles.byId("vision")?.label).toBe("Vision")
		expect(roles.cycleOrder).toEqual(["vision"])
		expect(roles.presets).toEqual({ names: ["base"], active: "base" })
		expect(roles.modelChains).toEqual({ "deepseek/*": [{ provider: "openrouter", model: "backup" }] })
		expect(roles.revision).toBe(1)

		await roles.clear("vision")
		expect(requests[1]).toEqual({ method: "model.roles.set", params: { role: "vision", clear: true } })
		expect(roles.byId("vision")?.label).toBe("Updated")
		expect(roles.revision).toBe(2)
	})

	test("sends a replacement chain, and an empty list clears it", async () => {
		const requests: Array<{ method: string; params: unknown }> = []
		const client = {
			async request(method: string, params: unknown) {
				requests.push({ method, params })
				return method === "model.roles.get" ? { roles: [] } : { role: row({ id: "vision" }) }
			},
		} as never
		const roles = new GatewayRolesClient(client)
		await roles.setFallbackChain("vision", [{ provider: "deepseek", model: "chat" }])
		await roles.setFallbackChain("vision", [])
		expect(requests.map((entry) => entry.params)).toEqual([
			{ role: "vision", fallback_chain: [{ provider: "deepseek", model: "chat" }] },
			{ role: "vision", fallback_chain: [] },
		])
	})

	test("writes a model-keyed chain through model.roles.set and keeps it out of the role table", async () => {
		const requests: Array<{ method: string; params: unknown }> = []
		const client = {
			async request(method: string, params: unknown) {
				requests.push({ method, params })
				if (method === "model.roles.get") return { roles: [] }
				return {
					role: row({ id: "deepseek/*", label: "deepseek/*", group: "chain", target: "chain" }),
				}
			},
		} as never
		const roles = new GatewayRolesClient(client)
		await roles.refresh()
		await roles.setModelChain("deepseek/*", [{ provider: "openrouter", model: "backup" }])
		expect(requests[1]).toEqual({
			method: "model.roles.set",
			params: { role: "deepseek/*", fallback_chain: [{ provider: "openrouter", model: "backup" }] },
		})
		expect(roles.rows).toEqual([])
		expect(roles.modelChains).toEqual({ "deepseek/*": [{ provider: "openrouter", model: "backup" }] })

		await roles.setModelChain("deepseek/*", [])
		expect(roles.modelChains).toEqual({})
	})

	test("adopts an applied preset's rows, summary and cycle order", async () => {
		const client = {
			async request(method: string) {
				if (method === "model.roles.get") return { roles: [] }
				if (method === "model.presets.apply") {
					return {
						roles: [row({ id: "vision", label: "Restored" })],
						presets: { names: ["base"], active: "base" },
						cycle_order: ["vision"],
					}
				}
				return {}
			},
		} as never
		const roles = new GatewayRolesClient(client)
		const applied = await roles.applyPreset("base")
		expect(applied.map((entry) => entry.label)).toEqual(["Restored"])
		expect(roles.presets).toEqual({ names: ["base"], active: "base" })
		expect(roles.cycleOrder).toEqual(["vision"])
		expect(roles.revision).toBe(1)
	})

	test("rethrows gateway failures as Errors", async () => {
		const client = {
			async request() {
				throw "unknown role"
			},
		} as never
		await expect(new GatewayRolesClient(client).refresh()).rejects.toThrow("unknown role")
	})
})
