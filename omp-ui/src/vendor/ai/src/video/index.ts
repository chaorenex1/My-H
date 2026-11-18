import type { Api, Model } from "../../../catalog/src/types.ts";
import * as AIError from "../error/index.ts";
import {
	downloadOpenRouterVideo,
	pollOpenRouterVideo,
	submitOpenRouterVideo,
	type VideoOptions,
} from "./openrouter-video.ts";
import type { VideoContent, VideoGenerationRequest, VideoJob } from "./types.ts";

export * from "./openrouter-video.ts";
export * from "./types.ts";

/** Submit an asynchronous video generation job through the model's transport. */
export function submitVideo(
	model: Model<Api>,
	request: VideoGenerationRequest,
	options: VideoOptions,
): Promise<VideoJob> {
	if (model.api === "openrouter-video") return submitOpenRouterVideo(model, request, options);
	throw new AIError.ConfigurationError(`Unsupported video API: ${model.api}`);
}

/** Poll an asynchronous video generation job through the model's transport. */
export function pollVideo(model: Model<Api>, jobId: string, options: VideoOptions): Promise<VideoJob> {
	if (model.api === "openrouter-video") return pollOpenRouterVideo(model, jobId, options);
	throw new AIError.ConfigurationError(`Unsupported video API: ${model.api}`);
}

/** Stream generated video content through the model's transport. */
export function downloadVideo(model: Model<Api>, jobId: string, options: VideoOptions): Promise<VideoContent> {
	if (model.api === "openrouter-video") return downloadOpenRouterVideo(model, jobId, options);
	throw new AIError.ConfigurationError(`Unsupported video API: ${model.api}`);
}
