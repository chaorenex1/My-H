import { readFile } from "node:fs/promises";
import { join } from "node:path";
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";

// Installed by spec-workflow-init. Inert unless a spec-orchestrate run is
// mid-flight: no marker, a paused marker, a registered wait, or an abandoned
// marker all allow the turn to end.
const MARKER = join(".specs", ".orchestrate-active.json");
const ABANDONED_MS = 4 * 60 * 60 * 1000;

type Marker = { feature?: string; waiting_report?: string | null; paused?: boolean; ts?: string };
type PipelineState = { phase?: string; completed_phases?: string[] };

async function readJson<T>(path: string): Promise<T | undefined> {
	try {
		return JSON.parse(await readFile(path, "utf8")) as T;
	} catch {
		return undefined;
	}
}

export default function (pi: ExtensionAPI): void {
	pi.on("session_stop", async (event, ctx) => {
		// One nudge per turn: the continuation that follows a block stops with
		// stop_hook_active set, so this never loops.
		if (event.stop_hook_active) return;

		const marker = await readJson<Marker>(join(ctx.cwd, MARKER));
		if (!marker || marker.paused === true) return;
		if (marker.waiting_report) return;
		const feature = marker.feature;
		if (!feature) return;
		const ts = marker.ts ? Date.parse(marker.ts) : Number.NaN;
		if (Number.isFinite(ts) && Date.now() - ts > ABANDONED_MS) return;

		const statePath = join(ctx.cwd, ".specs", feature, "pipeline-state.json");
		const state = await readJson<PipelineState>(statePath);
		if (!state) {
			return {
				decision: "block",
				reason: `spec-orchestrate marker for '${feature}' has no readable ${statePath}. Recreate the state, or delete .specs/.orchestrate-active.json to abandon the run.`,
			};
		}
		if ((state.completed_phases ?? []).includes("retrospective")) return;

		const phase = state.phase ?? "unknown";
		return {
			decision: "block",
			reason:
				`spec-orchestrate run '${feature}' is mid-flight at phase '${phase}' with no registered wait. ` +
				`Read .specs/${feature}/pipeline-state.json, run pipeline-state-check.sh .specs/${feature}, ` +
				"then dispatch the next phase with the task tool, or set waiting_report before ending the turn.",
		};
	});
}
