import {
	type BedrockMantleOptions,
	createBedrockMantleAuthenticatedFetch,
	prepareBedrockMantleRequest,
} from "../providers/bedrock-mantle.ts";
import type { Model } from "../types.ts";
import { resolveAwsRegion } from "../utils/aws-profile.ts";
import { resolveAwsBearerToken } from "./aws.ts";
import type { ProviderTransport } from "./build.ts";

/** Bedrock Mantle request/discovery shaping; auth policy lives in `rules/auth/bedrock-mantle.kdl`. */
export const bedrockMantleTransport: ProviderTransport = {
	prepareRequest: (model, options) =>
		prepareBedrockMantleRequest(model as Model<"openai-responses">, options as BedrockMantleOptions),
	mapSimpleOptions: options => ({ providerOptions: options.providerOptions }),
	prepareModelDiscovery: config => {
		const bearerToken = resolveAwsBearerToken(config.apiKey);
		if (!bearerToken) {
			return { ...config, apiKey: undefined, authenticated: false };
		}
		const region = resolveAwsRegion();
		return {
			authenticated: true,
			baseUrl: `https://bedrock-mantle.${encodeURIComponent(region)}.api.aws/openai/v1`,
			fetch: createBedrockMantleAuthenticatedFetch({
				fetch: config.fetch,
				providerOptions: { bearerToken, region },
			}),
		};
	},
};
