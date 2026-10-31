/**
 * Hooks referenced by API-key style rules: `headers-hook` resolvers for
 * `validate "models-endpoint"` probes needing computed headers, and
 * `login "custom"` flows that paste keys but need region/endpoint prompts.
 */
import type { HeadersHook, Lazy, LoginHook } from "./types.ts";

export const API_KEY_HEADERS_HOOKS: Record<string, Lazy<HeadersHook>> = {
	"coreweave-project": () => import("../oauth/coreweave.ts").then(m => m.requireCoreWeaveProjectHeaders),
	"cline-pass-client": () => import("../../../../catalog/src/wire/cline-pass.ts").then(m => m.clinePassClientHeaders),
};

export const API_KEY_LOGIN_HOOKS: Record<string, Lazy<LoginHook>> = {
	"alibaba-coding-plan": () => import("../oauth/alibaba-coding-plan.ts").then(m => m.loginAlibabaCodingPlan),
	"alibaba-token-plan": () => import("../oauth/alibaba-token-plan.ts").then(m => m.loginAlibabaTokenPlan),
	"cloudflare-ai-gateway": () => import("../oauth/cloudflare-ai-gateway.ts").then(m => m.loginCloudflareAiGateway),
	"cloudflare-workers-ai": () => import("../oauth/cloudflare-workers-ai.ts").then(m => m.loginCloudflareWorkersAi),
	kilo: () => import("../oauth/kilo.ts").then(m => m.loginKilo),
	xiaomi: () => import("../oauth/xiaomi.ts").then(m => m.loginXiaomi),
};
