/** GitLab Duo OAuth residue: invalidate direct-access credentials after token rotation. */

import { clearGitLabDuoDirectAccessCache } from "../../providers/gitlab-duo.ts";
import type { AfterExchangeHook } from "../hooks/types.ts";

export const gitLabDuoClearCacheHook: AfterExchangeHook = async credentials => {
	clearGitLabDuoDirectAccessCache();
	return credentials;
};
