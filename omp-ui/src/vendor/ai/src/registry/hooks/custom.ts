/**
 * Whole-flow hooks: `login "custom" hook=…` and `refresh hook=…` for providers
 * whose flow is not expressible in the declarative grammar.
 */
import type { Lazy, LoginHook, RefreshHook } from "./types.ts";

export const CUSTOM_LOGIN_HOOKS: Record<string, Lazy<LoginHook>> = {
	"github-copilot": () => import("../oauth/github-copilot.ts").then(module => module.loginGitHubCopilotHook),
	cursor: () => import("../oauth/cursor.ts").then(module => module.loginCursorHook),
	perplexity: () => import("../oauth/perplexity.ts").then(module => module.loginPerplexity),
	snowflake: () => import("../oauth/snowflake.ts").then(module => module.loginSnowflake),
};
export const CUSTOM_REFRESH_HOOKS: Record<string, Lazy<RefreshHook>> = {
	"github-copilot": () => import("../oauth/github-copilot.ts").then(module => module.refreshGitHubCopilotHook),
	cursor: () => import("../oauth/cursor.ts").then(module => module.refreshCursorHook),
	snowflake: () => import("../oauth/snowflake.ts").then(module => module.refreshSnowflakeToken),
};
