import type { Api, Model } from "../../../catalog/src/types.ts";
import * as AIError from "../error/index.ts";
import { embedOpenAI, type EmbeddingOptions } from "./openai-embeddings.ts";
import type { EmbeddingRequest, EmbeddingResult } from "./types.ts";

export * from "./openai-embeddings.ts";
export * from "./types.ts";

/** Dispatch an embedding request through the transport selected by the catalog model. */
export function embed(
	model: Model<Api>,
	request: EmbeddingRequest,
	options: EmbeddingOptions,
): Promise<EmbeddingResult> {
	if (model.api === "openai-embeddings") return embedOpenAI(model, request, options);
	throw new AIError.ConfigurationError(`Unsupported embeddings API: ${model.api}`);
}
