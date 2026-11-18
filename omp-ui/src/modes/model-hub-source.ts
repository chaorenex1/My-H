/**
 * Project Hermes' auxiliary-task roles onto the omp `/models` hub.
 *
 * The hub's browser half (provider order, disabled providers, tier labels) is omp's own
 * presentation state and keeps coming from {@link createModelBrowserSource}. The role half is not:
 * Hermes owns `auxiliary.<task>` in the profile's `config.yaml`, so `knownRoleIds`, role values,
 * fallback chains (roles and `auxiliary.fallback_chains` keys), role metadata, cycle order and
 * presets come from `model.roles.get` / `model.roles.set` through {@link ModelRoleRows}. The
 * profile has one config, so every role is global; the `default` row is the live session model
 * (read from the session, not persisted here).
 */

import type { Model } from "../vendor/ai/src/index.ts"
import { modelKind } from "../vendor/catalog/src/types.ts"
import type { ModelBrowserRoleInfo } from "@oh-my-pi/pi-tui/overlays/model-browser"
import type { ModelHubSource } from "@oh-my-pi/pi-tui/overlays/model-hub"
import { MODEL_ROLES } from "../config/model-roles"
import { resolveModelRoleValue } from "../config/model-resolver"
import type { Settings } from "../config/settings"
import type { ModelRoleFallbackEntry, ModelRoleRow } from "./rpc/gateway-contract.generated"
import type { ModelRoleRows } from "./rpc/gateway-roles"
import { createModelBrowserSource } from "./model-browser-source"

/** Distinct hues for auxiliary rows, reused in gateway order so a role keeps its colour. */
const AUX_ROLE_COLORS = ["accent", "success", "warning", "error", "muted", "dim"] as const

/** The live model the `default` role resolves from. */
export interface ModelHubSessionView {
	readonly model: Model | undefined
}

/** `provider/model` for a pinned row; auto rows carry no selector. */
function rowSelector(row: ModelRoleRow | undefined): string | undefined {
	if (!row) return undefined
	const provider = row.provider?.trim()
	const model = row.model?.trim()
	if (!provider || !model || provider === "auto") return undefined
	return `${provider}/${model}`
}

/** A gateway fallback chain as `provider/model` selectors; incomplete routes are skipped. */
function chainSelectors(entries: readonly ModelRoleFallbackEntry[] | undefined): string[] {
	return (entries ?? []).flatMap((entry) => {
		const provider = entry.provider?.trim()
		const model = entry.model?.trim()
		return provider && model ? [`${provider}/${model}`] : []
	})
}

/**
 * The role id a value refers to (`*`, `@role`, `pi/role`, optional `:level`), or undefined for a
 * literal selector. Bare role names are literals in omp's grammar and are left alone.
 */
function roleAliasOf(value: string | undefined): string | undefined {
	const trimmed = value?.trim()
	if (!trimmed) return undefined
	if (trimmed === "*") return "default"
	const match = /^(?:@|pi\/)([^:]+)(?::.*)?$/.exec(trimmed)
	return match?.[1]
}

export function createHermesModelHubSource(
	roles: ModelRoleRows,
	settings: Settings,
	session: ModelHubSessionView,
): ModelHubSource {
	const browser = createModelBrowserSource(settings)
	const knownRoleIds = (): string[] => [...new Set(["default", ...roles.rows.map((row) => row.id)])]
	const knownRoleId = (role: string): boolean => role === "default" || roles.byId(role) !== undefined
	const roleValue = (role: string): string | undefined =>
		role === "default"
			? session.model
				? `${session.model.provider}/${session.model.id}`
				: undefined
			: rowSelector(roles.byId(role))

	return {
		// Both inputs are monotonic: settings edits and role snapshots each move it forward.
		get revision() {
			return settings.revision + roles.revision
		},
		get defaultThinkingLevel() {
			return browser.defaultThinkingLevel
		},
		get modelProviderOrder() {
			return browser.modelProviderOrder
		},
		get disabledProviders() {
			return browser.disabledProviders
		},
		get knownRoleIds() {
			return knownRoleIds()
		},
		get mruOrder() {
			return []
		},
		get modelPerf() {
			return new Map()
		},
		serviceTierFor: (model) => browser.serviceTierFor?.(model),
		get modelRoleStorage(): "global" {
			return "global"
		},
		get cycleOrder() {
			return roles.cycleOrder
		},
		get fallbackChains() {
			const chains: Record<string, string[]> = {}
			for (const row of roles.rows) {
				const chain = chainSelectors(row.fallback_chain)
				if (chain.length > 0) chains[row.id] = chain
			}
			// Model-keyed chains (`provider/model`, `provider/*`) are rows in the hub's Roles view,
			// so they share the same selector map as role ids.
			for (const [key, entries] of Object.entries(roles.modelChains)) {
				const chain = chainSelectors(entries)
				if (chain.length > 0) chains[key] = chain
			}
			return chains
		},
		...(roles.presets === undefined
			? {}
			: {
					getModelPresets: () => ({
						names: roles.presets?.names ?? [],
						active: roles.presets?.active || undefined,
					}),
				}),
		getModelRole: roleValue,
		getProjectModelRole: () => undefined,
		getGlobalModelRole: roleValue,
		getModelRoleSource: (role) => (role !== "default" && roleValue(role) ? "global" : "default"),
		getRoleInfo: (role) => {
			if (role === "default") return MODEL_ROLES.default
			const index = roles.rows.findIndex((row) => row.id === role)
			const entry = roles.rows[index]
			const label = entry?.label?.trim() || role
			return {
				// The gateway owns omp's tags (`aux_slots.omp_role_tags`); unlisted rows fall back
				// to the label, exactly like the server does.
				tag: entry?.tag?.trim() || label.toUpperCase(),
				name: label,
				color: AUX_ROLE_COLORS[(index >= 0 ? index : 0) % AUX_ROLE_COLORS.length],
				section: "chat",
				accepts: (model: Model) => modelKind(model) === "chat",
			} satisfies ModelBrowserRoleInfo
		},
		// Hermes has no per-role auto-selection chain: an unassigned row means "the main model",
		// never omp's built-in provider priorities (which would mislabel Hermes' `vision` task
		// with omp's vision chain). Only the gateway-sent `fallbackChains` are real.
		defaultRoleChain: () => [],
		resolveRoleValue: (value, models, roleLookup) => {
			// omp's grammar accepts every built-in role alias; Hermes only knows the rows it sent.
			const alias = roleAliasOf(value)
			if (alias !== undefined && !knownRoleId(alias)) {
				return { model: undefined, thinkingLevel: undefined, explicitThinkingLevel: false, warning: undefined }
			}
			return resolveModelRoleValue(value, models, {
				settings,
				roleLookup: roleLookup ?? { getModelRole: roleValue },
			})
		},
	}
}
