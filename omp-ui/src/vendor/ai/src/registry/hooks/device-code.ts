/**
 * Hooks referenced by `login "device-code"` rules: fingerprint headers,
 * discovered endpoints and post-exchange identity steps.
 */
import type { AfterExchangeHook, HeadersHook, Lazy, ValueHook } from "./types.ts";

export const DEVICE_CODE_HEADERS_HOOKS: Record<string, Lazy<HeadersHook>> = {
	"kimi-fingerprint": () => import("../oauth/kimi.ts").then(module => module.getKimiCommonHeaders),
};
export const DEVICE_CODE_VALUE_HOOKS: Record<string, Lazy<ValueHook>> = {
	"xai-token-endpoint": () => import("../oauth/xai-oauth.ts").then(module => module.getXAITokenEndpoint),
};
export const DEVICE_CODE_AFTER_EXCHANGE_HOOKS: Record<string, Lazy<AfterExchangeHook>> = {
	"muse-code-key": () => import("../oauth/muse-code.ts").then(module => module.attachMuseCodeApiKey),
	"factory-droid-region": () => import("../oauth/factory-droid.ts").then(module => module.attachFactoryDroidRegion),
};
