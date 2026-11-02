/**
 * Hermes `model.roles.*` client: the profile's auxiliary-task role assignments.
 *
 * Hermes owns the role table (`auxiliary.<task>` in `config.yaml`); this shell only reads it for
 * the `/models` Roles view and writes one row when the user assigns, clears or re-chains a role.
 * The client keeps the last snapshot, so the hub's synchronous getters (`getModelRole`,
 * `fallbackChains`, …) never wait on the wire; `revision` moves whenever the snapshot does so the
 * source's derived caches invalidate.
 *
 * Wire shapes are `ModelRolesGetResult` / `ModelRolesSetParams` (generated contract). A rejected
 * request is rethrown as an `Error` the controller can hand to `showError`.
 */

import type { GatewayClient } from "./gateway-client"
import type {
	ModelCycleSetParams,
	ModelCycleSetResult,
	ModelPresetParams,
	ModelPresetsApplyResult,
	ModelPresetsDeleteResult,
	ModelPresetsSaveResult,
	ModelPresetsSummary,
	ModelRoleFallbackEntry,
	ModelRoleRow,
	ModelRolesGetResult,
	ModelRolesSetParams,
	ModelRolesSetResult
} from "./gateway-contract.generated"

/** The reasoning values `model.roles.set` accepts ("auto" is shell-only and maps to "inherit"). */
export type RolesThinkingLevel = NonNullable<ModelRolesSetParams["reasoning_effort"]>

/** The snapshot surface the model-hub source reads (a fake satisfies it in tests). */
export interface ModelRoleRows {
	readonly revision: number
	readonly rows: readonly ModelRoleRow[]
	readonly cycleOrder: readonly string[]
	readonly presets: ModelPresetsSummary | undefined
	readonly modelChains: Readonly<Record<string, readonly ModelRoleFallbackEntry[]>>
	byId(id: string): ModelRoleRow | undefined
}

/** The write surface the model-hub callbacks use. */
export interface ModelRoleWriter {
	assign(params: ModelRolesSetParams): Promise<ModelRoleRow>
	clear(role: string): Promise<ModelRoleRow>
	setFallbackChain(role: string, entries: readonly ModelRoleFallbackEntry[]): Promise<ModelRoleRow>
	setCycleOrder(order: readonly string[]): Promise<readonly string[]>
	savePreset(name: string): Promise<string>
	applyPreset(name: string): Promise<readonly ModelRoleRow[]>
	deletePreset(name: string): Promise<string>
	setModelChain(key: string, entries: readonly ModelRoleFallbackEntry[]): Promise<void>
}

/** The read+write surface the callbacks use (a preset save re-reads the role table). */
export interface ModelRoleStore extends ModelRoleRows, ModelRoleWriter {
	refresh(): Promise<readonly ModelRoleRow[]>
}

export class GatewayRolesClient implements ModelRoleStore {
	#client: GatewayClient
	#snapshot: ModelRolesGetResult = {}
	#revision = 0
	#loaded = false

	constructor(client: GatewayClient) {
		this.#client = client
	}

	get revision(): number {
		return this.#revision
	}

	get rows(): readonly ModelRoleRow[] {
		return this.#snapshot.roles ?? []
	}

	get cycleOrder(): readonly string[] {
		return this.#snapshot.cycle_order ?? []
	}

	get presets(): ModelPresetsSummary | undefined {
		return this.#snapshot.presets
	}

	get modelChains(): Readonly<Record<string, readonly ModelRoleFallbackEntry[]>> {
		return this.#snapshot.model_chains ?? {}
	}

	/** True once a full `model.roles.get` snapshot was adopted (partial writes do not count). */
	get loaded(): boolean {
		return this.#loaded
	}

	byId(id: string): ModelRoleRow | undefined {
		return this.rows.find((row) => row.id === id)
	}

	/** Fetch the profile's role table; the snapshot is replaced only after a successful read. */
	async refresh(): Promise<readonly ModelRoleRow[]> {
		let result: ModelRolesGetResult
		try {
			result = await this.#client.request<ModelRolesGetResult>("model.roles.get", {})
		} catch (error) {
			throw error instanceof Error ? error : new Error(String(error))
		}
		this.#snapshot = result
		this.#loaded = true
		this.#revision++
		return this.rows
	}

	/** Write one role row; the gateway's updated row replaces the cached one. */
	async assign(params: ModelRolesSetParams): Promise<ModelRoleRow> {
		let result: ModelRolesSetResult
		try {
			result = await this.#client.request<ModelRolesSetResult>("model.roles.set", params)
		} catch (error) {
			throw error instanceof Error ? error : new Error(String(error))
		}
		this.#adopt(result.role)
		return result.role
	}

	/** Clear a role back to auto-selection (`{clear: true}`, not an empty model selector). */
	async clear(role: string): Promise<ModelRoleRow> {
		return await this.assign({ role, clear: true })
	}

	/** Replace a role's fallback chain; an empty chain clears the entry. */
	async setFallbackChain(role: string, entries: readonly ModelRoleFallbackEntry[]): Promise<ModelRoleRow> {
		return await this.assign({ role, fallback_chain: [...entries] })
	}

	/** Persist one role's reasoning override. */
	async setThinking(role: string, level: RolesThinkingLevel | null): Promise<ModelRoleRow> {
		return await this.assign({ role, reasoning_effort: level })
	}

	/** Persist omp's ctrl+p role cycle (`omp.cycle_order`). */
	async setCycleOrder(order: readonly string[]): Promise<readonly string[]> {
		let result: ModelCycleSetResult
		const params: ModelCycleSetParams = { order: [...order] }
		try {
			result = await this.#client.request<ModelCycleSetResult>("model.cycle.set", params)
		} catch (error) {
			throw error instanceof Error ? error : new Error(String(error))
		}
		const next = result.order ?? [...order]
		this.#snapshot = { ...this.#snapshot, cycle_order: [...next] }
		this.#revision++
		return next
	}

	/** Snapshot the current assignments as `name`; the gateway answers with the saved name only. */
	async savePreset(name: string): Promise<string> {
		let result: ModelPresetsSaveResult
		const params: ModelPresetParams = { name }
		try {
			result = await this.#client.request<ModelPresetsSaveResult>("model.presets.save", params)
		} catch (error) {
			throw error instanceof Error ? error : new Error(String(error))
		}
		const presets = this.#snapshot.presets
		if (presets) {
			const names = [...(presets.names ?? []), result.name]
			this.#snapshot = { ...this.#snapshot, presets: { ...presets, names: [...new Set(names)] } }
			this.#revision++
		}
		return result.name
	}

	/** Restore `name`; the gateway sends the resulting rows, presets and cycle order. */
	async applyPreset(name: string): Promise<readonly ModelRoleRow[]> {
		let result: ModelPresetsApplyResult
		const params: ModelPresetParams = { name }
		try {
			result = await this.#client.request<ModelPresetsApplyResult>("model.presets.apply", params)
		} catch (error) {
			throw error instanceof Error ? error : new Error(String(error))
		}
		this.#snapshot = {
			...this.#snapshot,
			roles: [...(result.roles ?? [])],
			presets: result.presets,
			cycle_order: [...(result.cycle_order ?? [])]
		}
		this.#revision++
		return this.rows
	}

	/** Remove `name`; the gateway's answer carries the deleted name. */
	async deletePreset(name: string): Promise<string> {
		let result: ModelPresetsDeleteResult
		const params: ModelPresetParams = { name }
		try {
			result = await this.#client.request<ModelPresetsDeleteResult>("model.presets.delete", params)
		} catch (error) {
			throw error instanceof Error ? error : new Error(String(error))
		}
		const presets = this.#snapshot.presets
		if (presets) {
			const names = (presets.names ?? []).filter((entry) => entry !== result.name)
			const active = presets.active === result.name ? "" : presets.active
			this.#snapshot = { ...this.#snapshot, presets: { ...presets, names, active } }
			this.#revision++
		}
		return result.name
	}

	/**
	 * Replace one model-keyed chain (`provider/model` or `provider/*`); `model.roles.set` treats a
	 * non-role `role` as a chain key and an empty list removes the entry.
	 */
	async setModelChain(key: string, entries: readonly ModelRoleFallbackEntry[]): Promise<void> {
		await this.assign({ role: key, fallback_chain: [...entries] })
		const chains = { ...(this.#snapshot.model_chains ?? {}) }
		if (entries.length > 0) chains[key] = entries.map((entry) => ({ ...entry }))
		else delete chains[key]
		this.#snapshot = { ...this.#snapshot, model_chains: chains }
		this.#revision++
	}

	#adopt(row: ModelRoleRow): void {
		// A chain key answers with a synthetic `group: "chain"` row; it belongs in `model_chains`,
		// never in the role table the hub lists and resolves aliases against.
		if (row.group === "chain" || row.target === "chain") return
		const rows = [...this.rows]
		const at = rows.findIndex((entry) => entry.id === row.id)
		if (at >= 0) rows[at] = row
		else rows.push(row)
		this.#snapshot = { ...this.#snapshot, roles: rows }
		this.#revision++
	}
}
