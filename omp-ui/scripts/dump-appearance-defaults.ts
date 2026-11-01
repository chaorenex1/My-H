/**
 * Print omp's Appearance defaults as a flat JSON object (`setting id → current default`).
 *
 * `hermes omp` stores the panel's values in the profile's Hermes config (`omp.appearance`) and
 * Hermes needs the shell's own defaults for that section, so `scripts/gen_omp_appearance_defaults.py`
 * writes this output to `hermes_cli/data/omp_appearance_defaults.json`. The registry is the single
 * source of truth; `gateway-appearance.test.ts` fails when the committed copy drifts.
 *
 * Run from the repo root: `bun omp-ui/scripts/dump-appearance-defaults.ts`.
 */

import "../src/config/all-settings";
import { all } from "../src/config/registry";

const entries = all()
	.filter(setting => setting.ui?.tab === "appearance")
	.sort((left, right) => left.id.localeCompare(right.id))
	.map(setting => [setting.id, setting.default] as const);

process.stdout.write(`${JSON.stringify(Object.fromEntries(entries), null, 2)}\n`);
