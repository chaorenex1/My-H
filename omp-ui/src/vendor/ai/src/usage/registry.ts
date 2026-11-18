import type { Provider } from "../types.ts";
import type { CredentialRankingStrategy, UsageProvider } from "../usage.ts";
import { alibabaTokenPlanRankingStrategy, alibabaTokenPlanUsageProvider } from "./alibaba-token-plan.ts";
import { charmHyperUsageProvider } from "./charm-hyper.ts";
import { claudeRankingStrategy, claudeUsageProvider } from "./claude.ts";
import { clinePassUsageProvider } from "./cline-pass.ts";
import { commandCodeRankingStrategy, commandCodeUsageProvider } from "./commandcode.ts";
import { cursorRankingStrategy, cursorUsageProvider } from "./cursor.ts";
import { devinUsageProvider } from "./devin.ts";
import { factoryDroidRankingStrategy, factoryDroidUsageProvider } from "./factory-droid.ts";
import { googleGeminiCliUsageProvider } from "./gemini.ts";
import { githubCopilotUsageProvider } from "./github-copilot.ts";
import { antigravityRankingStrategy, antigravityUsageProvider } from "./google-antigravity.ts";
import { kimiRankingStrategy, kimiUsageProvider } from "./kimi.ts";
import { museCodeUsageProvider } from "./muse-code.ts";
import { minimaxCodeUsageProvider } from "./minimax-code.ts";
import { ollamaCloudUsageProvider, ollamaUsageProvider } from "./ollama.ts";
import { codexRankingStrategy, openaiCodexUsageProvider } from "./openai-codex.ts";
import { opencodeGoRankingStrategy, opencodeGoUsageProvider } from "./opencode-go.ts";
import { syntheticUsageProvider } from "./synthetic.ts";
import { umansUsageProvider } from "./umans.ts";
import { xaiOauthRankingStrategy, xaiOauthUsageProvider } from "./xai-oauth.ts";
import { zaiRankingStrategy, zaiUsageProvider } from "./zai.ts";

/** Resolves the usage-based ranking strategy for a provider. */
export type RankingStrategyResolver = (provider: Provider) => CredentialRankingStrategy | undefined;

/** Built-in usage providers, in probe order. */
export const DEFAULT_USAGE_PROVIDERS: readonly UsageProvider[] = [
	alibabaTokenPlanUsageProvider,
	openaiCodexUsageProvider,
	kimiUsageProvider,
	minimaxCodeUsageProvider,
	museCodeUsageProvider,
	antigravityUsageProvider,
	googleGeminiCliUsageProvider,
	ollamaUsageProvider,
	factoryDroidUsageProvider,
	ollamaCloudUsageProvider,
	claudeUsageProvider,
	clinePassUsageProvider,
	zaiUsageProvider,
	umansUsageProvider,
	opencodeGoUsageProvider,
	githubCopilotUsageProvider,
	cursorUsageProvider,
	syntheticUsageProvider,
	xaiOauthUsageProvider,
	devinUsageProvider,
	charmHyperUsageProvider,
	commandCodeUsageProvider,
];

const DEFAULT_USAGE_PROVIDER_MAP = new Map<Provider, UsageProvider>(
	DEFAULT_USAGE_PROVIDERS.map(provider => [provider.id, provider]),
);

/** Built-in usage provider for `provider`. */
export function defaultUsageProvider(provider: Provider): UsageProvider | undefined {
	return DEFAULT_USAGE_PROVIDER_MAP.get(provider);
}

const DEFAULT_RANKING_STRATEGIES = new Map<Provider, CredentialRankingStrategy>([
	["alibaba-token-plan", alibabaTokenPlanRankingStrategy],
	["openai-codex", codexRankingStrategy],
	["anthropic", claudeRankingStrategy],
	["cursor", cursorRankingStrategy],
	["google-antigravity", antigravityRankingStrategy],
	["factory-droid", factoryDroidRankingStrategy],
	["kimi-code", kimiRankingStrategy],
	["zai", zaiRankingStrategy],
	["opencode-go", opencodeGoRankingStrategy],
	["xai-oauth", xaiOauthRankingStrategy],
	["commandcode", commandCodeRankingStrategy],
]);

/** Built-in ranking strategy for `provider`. */
export function defaultRankingStrategy(provider: Provider): CredentialRankingStrategy | undefined {
	return DEFAULT_RANKING_STRATEGIES.get(provider);
}
