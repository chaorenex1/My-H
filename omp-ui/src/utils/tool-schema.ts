import { schemaDefinesProperty } from "../vendor/ai/src/utils/schema/index.ts";
import { INTENT_FIELD } from "../vendor/wire/src/index.ts";

/** Whether a wire schema owns `i` as a tool parameter rather than harness intent. */
export function schemaDeclaresIntentField(schema: unknown): boolean {
	return schemaDefinesProperty(schema, INTENT_FIELD);
}
