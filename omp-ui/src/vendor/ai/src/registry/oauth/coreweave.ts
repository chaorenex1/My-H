import { coreWeaveProjectHeaders } from "../../../../catalog/src/wire/coreweave.ts";
import { $env } from "../../../../utils/src/index.ts";
import * as AIError from "../../error/index.ts";

const PROJECT_PERSIST_INSTRUCTIONS =
	"add export COREWEAVE_PROJECT=<team>/<project> to your shell startup file (for example ~/.zshrc, ~/.bashrc, or your shell's profile/rc file)";

export function requireCoreWeaveProjectHeaders(): Record<string, string> {
	const headers = coreWeaveProjectHeaders($env);
	if (!headers) {
		throw new AIError.ConfigurationError(
			`CoreWeave Serverless Inference requires OpenAI-Project. Set COREWEAVE_PROJECT=<team>/<project> before running /login coreweave. To persist it, ${PROJECT_PERSIST_INSTRUCTIONS}.`,
		);
	}
	return headers;
}
