import type { Api, Model } from "../../../catalog/src/types.ts";
import * as AIError from "../error/index.ts";
import { synthesizeOpenAiSpeech } from "./openai-speech.ts";
import type { SpeechOptions, SpeechRequest, SpeechResult } from "./types.ts";
import { synthesizeXaiSpeech } from "./xai-tts.ts";

export * from "./openai-speech.ts";
export * from "./transport.ts";
export * from "./types.ts";
export * from "./xai-tts.ts";

/** Catalog APIs {@link synthesizeSpeech} serves; the `tts` tool and the gateway gate on these. */
export const SPEECH_APIS = ["xai-tts", "openai-speech"] as const;
export type SpeechApi = (typeof SPEECH_APIS)[number];

/** Whether a catalog API synthesizes speech through a cloud transport (local inference is tool-only). */
export function isSpeechApi(api: Api): api is SpeechApi {
	return api === "xai-tts" || api === "openai-speech";
}

/** Synthesize speech through the transport selected by the catalog model's `api`. */
export function synthesizeSpeech(
	model: Model<Api>,
	request: SpeechRequest,
	options: SpeechOptions,
): Promise<SpeechResult> {
	switch (model.api) {
		case "xai-tts":
			return synthesizeXaiSpeech(model, request, options);
		case "openai-speech":
			return synthesizeOpenAiSpeech(model, request, options);
		default:
			throw new AIError.ConfigurationError(`Unsupported speech API: ${model.api}`);
	}
}
