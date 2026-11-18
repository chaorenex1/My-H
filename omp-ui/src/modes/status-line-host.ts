import type { StatusLineHost, StatusLineSession } from "@oh-my-pi/pi-tui/status-line/host";
import { Settings, settings } from "../config/settings";
import type { AgentSession } from "../session/agent-session";
import { calculateTokensPerSecond } from "../utils/token-rate";

import {
	cfgStatusLineCompactThinkingLevel,
	cfgStatusLineContextLine,
	cfgStatusLineLeftSegments,
	cfgStatusLinePreset,
	cfgStatusLineRightSegments,
	cfgStatusLineSegmentOptions,
	cfgStatusLineSeparator,
	cfgStatusLineSessionAccent,
	cfgStatusLineShowHookStatus,
	cfgStatusLineTransparent,
} from "./settings";

/**
 * Session capabilities the host consults beyond the display subset. Every
 * field is optional so display-only sessions still render; a live `AgentSession`
 * satisfies it structurally.
 */
export type StatusLineHostSession = StatusLineSession & Partial<Pick<AgentSession, "settings" | "sessionId">>;

/**
 * Application policy for the portable status renderer.
 *
 * The renderer's segments read the session for their numbers (`getSessionStats`,
 * `getContextUsage`, model, title), so the host only carries preferences. There is
 * no git probe, no account/OAuth identity and no usage-report source in a gateway
 * client — the authority owns all three — so the segments that would consume them
 * stay dark instead of reporting invented state.
 */
export const statusLineHost: StatusLineHost<StatusLineHostSession> = {
	getSettings: () => ({
		preset: cfgStatusLinePreset.get(settings),
		leftSegments: cfgStatusLineLeftSegments.get(settings),
		rightSegments: cfgStatusLineRightSegments.get(settings),
		separator: cfgStatusLineSeparator.get(settings),
		showHookStatus: cfgStatusLineShowHookStatus.get(settings),
		segmentOptions: cfgStatusLineSegmentOptions.get(settings),
		sessionAccent: cfgStatusLineSessionAccent.get(settings),
		transparent: cfgStatusLineTransparent.get(settings),
		compactThinkingLevel: cfgStatusLineCompactThinkingLevel.get(settings),
		contextLine: cfgStatusLineContextLine.get(settings),
	}),
	// Read per frame: skip the `settings` proxy trap and hit the instance getter directly.
	getSettingsRevision: () => Settings.instance.revision,
	getSessionSettingsIdentity: session => session.settings,
	getSessionSettingsRevision: session => session.settings?.revision ?? 0,
	/** No repository probe: the branch segment is a local-git feature. */
	gitEnabled: () => false,
	/** Reset-expiry fireworks belong to the provider-account segments, which do not exist here. */
	codexResetFireworksEnabled: () => false,
	/** Goals live on the authority; the footer never claims one. */
	goalStatusInFooter: () => false,
	/** Sign-in identity is the authority's; the client holds no account. */
	activeAccount: () => undefined,
	canFetchUsageReports: () => false,
	fetchUsageReports: () => Promise.resolve(null),
	/** No nested-repository workspace selection in a gateway client. */
	resolveActiveRepo: () => null,
	/** No git probe, so the pull-request segment resolves nothing (`exitCode` 1 = no data). */
	lookupPullRequest: () => Promise.resolve({ stdout: "", exitCode: 1 }),
	calculateTokensPerSecond,
	/** No account identity exists, so no report row can be scoped to one. */
	limitMatchesActiveAccount: () => false,
	/** The authority compacts; the client draws no boundary markers. */
	computeCompactionBoundaries: () => null,
};
