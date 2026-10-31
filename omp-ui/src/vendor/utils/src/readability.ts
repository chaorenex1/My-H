/** Behavior-compatible reimplementation of @mozilla/readability's used surface. */

export { Readability } from "./readability/readability.ts";
export { isProbablyReaderable, type ReaderableOptions } from "./readability/readerable.ts";
export type {
	ReadabilityArticle,
	ReadabilityDocument,
	ReadabilityElement,
	ReadabilityNode,
	ReadabilityOptions,
} from "./readability/types.ts";
