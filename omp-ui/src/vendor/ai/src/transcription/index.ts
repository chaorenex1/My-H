import type { Api, Model } from "../../../catalog/src/types.ts";
import * as AIError from "../error/index.ts";
import { transcribeOpenAI, type TranscriptionOptions } from "./openai-transcriptions.ts";
import type { TranscriptionRequest, TranscriptionResult } from "./types.ts";

export * from "./openai-transcriptions.ts";
export * from "./types.ts";

/** Dispatch an audio transcription through the transport selected by the catalog model. */
export function transcribeAudio(
	model: Model<Api>,
	request: TranscriptionRequest,
	options: TranscriptionOptions,
): Promise<TranscriptionResult> {
	if (model.api === "openai-transcriptions") return transcribeOpenAI(model, request, options);
	throw new AIError.ConfigurationError(`Unsupported transcription API: ${model.api}`);
}
