import type { Api, Model } from "../../../catalog/src/types.ts";
import * as AIError from "../error/index.ts";
import { rerankOpenRouter, type RerankOptions } from "./openrouter-rerank.ts";
import type { RerankRequest, RerankResult } from "./types.ts";

export * from "./openrouter-rerank.ts";
export * from "./types.ts";

/** Dispatch reranking through the transport selected by the catalog model. */
export function rerank(model: Model<Api>, request: RerankRequest, options: RerankOptions): Promise<RerankResult> {
	if (model.api === "openrouter-rerank") return rerankOpenRouter(model, request, options);
	throw new AIError.ConfigurationError(`Unsupported rerank API: ${model.api}`);
}
