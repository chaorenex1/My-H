import { describe, expect, test } from "bun:test"

import { filterToGatewayProviders, installGatewayModelCatalog, planGatewayProviders } from "./gateway-models"
import type { ModelOptionsResult } from "./gateway-contract.generated"
import type { ProviderConfigInput } from "../../config/model-registry"

const options = {
	provider: "deepseek",
	model: "deepseek-flash",
	providers: [
		{ slug: "deepseek", name: "DeepSeek", authenticated: true, models: ["deepseek-flash"] },
		{ slug: "anthropic", name: "Anthropic", authenticated: false, models: ["claude-sonnet-5"] },
		{ slug: "lmstudio", name: "LM Studio", authenticated: true, models: [] }
	],
	models: [
		{
			id: "deepseek-flash",
			name: "DeepSeek Flash",
			provider: "deepseek",
			reasoning: true,
			input: ["text", "image"],
			context_window: 128_000,
			max_tokens: 8_192,
			cost: { input: 0.1, output: 0.2, cache_read: 0.01, cache_write: null },
			thinking: { levels: ["none", "low", "high", "turbo"] }
		},
		{
			id: "claude-sonnet-5",
			name: "",
			provider: "anthropic",
			input: [],
			context_window: null,
			max_tokens: null,
			cost: null
		}
	]
} as unknown as ModelOptionsResult

describe("gateway model catalog", () => {
	test("groups rows by provider and keeps the authority's auth verdict", () => {
		const registrations = planGatewayProviders(options)
		expect(registrations.map((row) => [row.provider, row.authenticated])).toEqual([
			["deepseek", true],
			["anthropic", false]
		])
	})

	test("projects display fields and drops unpublished effort levels", () => {
		const [deepseek, anthropic] = planGatewayProviders(options)
		expect(deepseek?.models[0]).toMatchObject({
			id: "deepseek-flash",
			name: "DeepSeek Flash",
			reasoning: true,
			input: ["text", "image"],
			contextWindow: 128_000,
			maxTokens: 8_192,
			cost: { input: 0.1, output: 0.2, cacheRead: 0.01, cacheWrite: 0 },
			thinking: { mode: "effort", efforts: ["low", "high"] }
		})
		// Nulls become display defaults; an empty name falls back to the id; empty inputs mean text.
		expect(anthropic?.models[0]).toMatchObject({
			id: "claude-sonnet-5",
			name: "claude-sonnet-5",
			reasoning: false,
			input: ["text"],
			contextWindow: 200_000,
			maxTokens: 32_768
		})
		expect(anthropic?.models[0]?.thinking).toBeUndefined()
	})

	test("a provider with no rows is not registered", () => {
		expect(planGatewayProviders(options).map((row) => row.provider)).not.toContain("lmstudio")
	})

	test("registration carries a key only for authenticated providers", () => {
		const calls: Array<[string, ProviderConfigInput, string | undefined]> = []
		const exclusive: string[][] = []
		installGatewayModelCatalog(
			{
				registerProvider: (provider, config, sourceId) => calls.push([provider, config, sourceId]),
				setGatewayProviders: (providers) => exclusive.push([...providers])
			},
			options
		)
		expect(calls.map(([provider]) => provider)).toEqual(["deepseek", "anthropic"])
		expect(calls[0]?.[1]).toMatchObject({ apiKey: "hermes-gateway" })
		expect(calls[1]?.[1]).not.toHaveProperty("apiKey")
		expect(calls.every(([, , sourceId]) => sourceId === "hermes-gateway")).toBe(true)
		// The registry's published lists become exactly the advertised providers.
		expect(exclusive).toEqual([["deepseek", "anthropic"]])
	})

	test("filter keeps only advertised provider rows, case-insensitively", () => {
		const rows = [
			{ provider: "deepseek", id: "deepseek-flash" },
			{ provider: "local", id: "llama.cpp" },
			{ provider: "web", id: "search" },
			{ provider: "OPENROUTER", id: "x" }
		]
		expect(filterToGatewayProviders(rows, new Set(["deepseek", "openrouter"]))).toEqual([rows[0], rows[3]])
		// An installed catalog with no providers publishes nothing (pre-install, the registry keeps
		// behaving as a plain catalog because its provider set is still undefined).
		expect(filterToGatewayProviders(rows, new Set())).toEqual([])
	})
})
