/**
 * Projects one compiled auth policy (`rules/auth/<id>.kdl`) plus the
 * provider's optional TypeScript transport hooks into the
 * {@link ProviderDefinition} the rest of `@oh-my-pi/pi-ai` consumes.
 */
import type { CompiledAuthProvider } from "../../../catalog/src/compat/types.ts";
import { $pickenv } from "../../../utils/src/index.ts";
import * as AIError from "../error/index.ts";
import { createApiKeyLogin } from "./engine/api-key.ts";
import { loadLoginHook } from "./engine/common.ts";
import { createDeviceCodeLogin } from "./engine/device-code.ts";
import { createOAuthCodeLogin } from "./engine/oauth-code.ts";
import { createRefresh } from "./engine/refresh.ts";
import { HOOKS } from "./hooks/index.ts";
import type { OAuthController } from "./oauth/types.ts";
import type { KeyResolver, ProviderDefinition } from "./types.ts";

/** Request/model shaping a provider implements in TypeScript beside its KDL auth policy. */
export type ProviderTransport = Pick<
	ProviderDefinition,
	"prepareModel" | "prepareRequest" | "mapSimpleOptions" | "prepareModelDiscovery"
>;

function envResolver(env: CompiledAuthProvider["env"]): KeyResolver | undefined {
	if (!env) return undefined;
	if ("hook" in env) {
		const hook = HOOKS.env[env.hook];
		if (!hook) throw new AIError.ConfigurationError(`auth rules reference unknown env hook "${env.hook}"`);
		return hook;
	}
	return env.vars.length === 1 ? env.vars[0] : () => $pickenv(...env.vars);
}

function loginFor(policy: CompiledAuthProvider): ProviderDefinition["login"] {
	const rule = policy.login;
	if (!rule) return undefined;
	switch (rule.kind) {
		case "api-key":
			return createApiKeyLogin(rule, policy.name);
		case "oauth-code": {
			const login = createOAuthCodeLogin(rule, policy);
			return policy.result === "api-key" ? async (ctrl: OAuthController) => (await login(ctrl)).access : login;
		}
		case "device-code":
			return createDeviceCodeLogin(rule, policy.id);
		case "custom":
			return async (callbacks: OAuthController) => (await loadLoginHook(rule.hook))(callbacks);
	}
}

/** Materializes the registry definition for one compiled policy. */
export function buildProviderDefinition(
	policy: CompiledAuthProvider,
	transport?: ProviderTransport,
): ProviderDefinition {
	const envKeys = envResolver(policy.env);
	const login = loginFor(policy);
	const refreshToken = createRefresh(policy);
	return {
		id: policy.id,
		name: policy.name,
		...transport,
		...(envKeys !== undefined ? { envKeys } : {}),
		...(policy.allowsMissingApiKey !== undefined ? { allowsMissingApiKey: policy.allowsMissingApiKey } : {}),
		...(policy.nativeAuthApis !== undefined ? { nativeAuthApis: policy.nativeAuthApis } : {}),
		...(policy.available !== undefined ? { available: policy.available } : {}),
		...(policy.showInLoginList !== undefined ? { showInLoginList: policy.showInLoginList } : {}),
		...(policy.storeAs !== undefined ? { storeCredentialsAs: policy.storeAs } : {}),
		...(policy.callbackPort !== undefined ? { callbackPort: policy.callbackPort } : {}),
		...(policy.pasteCode ? { pasteCodeFlow: true } : {}),
		...(login ? { login } : {}),
		...(refreshToken ? { refreshToken } : {}),
	};
}
