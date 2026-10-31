/**
 * Hermes entry (`hermes omp` runs `src/main.ts`, which delegates here).
 *
 * The main screen is a **client of the Hermes gateway**, not another local agent: one session is
 * created (or resumed) over the paired WebSocket, and `InteractiveMode` renders the gateway's event
 * stream through the ordinary controller layer. omp's own launch graph (`runRootCommand` → local
 * model registry/auth/session creation/extension discovery) is not on this path; `src/cli.ts`
 * remains only as the worker-host fallback for spawned workers.
 *
 * This file lives outside the byte-identical snapshot, so `scripts/sync-upstream.py` only restores
 * `main.ts`'s short delegator and never has to reproduce this composition.
 */

import * as fsSync from "node:fs";

import { ensureTheme, initTheme } from "@oh-my-pi/pi-tui/theme";

import {
	cfgColorBlindMode,
	cfgStartupChangelogMode,
	cfgStartupQuiet,
	cfgStartupShowSplash,
	cfgSymbolPreset,
	cfgThemeDark,
	cfgThemeLight,
} from "../../modes/settings.ts";
import { shouldShowStartupSplash } from "../../startup-splash.ts";
import { getChangelogPath, resolveStartupChangelogForDisplay } from "../../utils/changelog.ts";
import { APP_NAME, VERSION } from "../../vendor/utils/src/dirs.ts";
import { $env } from "../../vendor/utils/src/env.ts";
import type { SessionManager as SessionManagerType } from "../../session/session-manager";

/** Interactive-only graph boundary; the renderer loads on first boot, never for `--version`. */
async function loadInteractiveModeConstructor() {
	return (await import("../../modes/interactive-mode")).InteractiveMode;
}

/** `hermes omp` / `omp-ui/src/main.ts`: argv → gateway session → `InteractiveMode`. */
export async function runHermesOmp(args: string[]): Promise<void> {
	const resume = parseResumeArg(args);
	// The launcher labels the shell with this install's version; the renderer's own version is the fallback.
	const version = process.env.HERMES_VERSION?.trim() || VERSION;
	if (args.includes("--version") || args.includes("-v")) {
		process.stdout.write(`${version}\n`);
		return;
	}
	if (args.includes("--help") || args.includes("-h")) {
		process.stdout.write(
			`${APP_NAME} ${version} (Hermes gateway client)\n\n` +
				"Usage: hermes omp [--resume SESSION] [--version]\n" +
				"  --resume, -r SESSION  resume a Hermes session by id or title\n",
		);
		return;
	}
	const { installGlobalProxyFetch } = await import("../../vendor/ai/src/utils/proxy.ts");
	installGlobalProxyFetch();
	const cwd = process.cwd();
	const [{ Settings }, { AuthStorage, SqliteAuthCredentialStore }, { GatewayModelRegistry }, { SessionManager }, { Agent }, { AgentSession }, { Database }] =
		await Promise.all([
			import("../../config/settings"),
			import("../../session/auth-storage"),
			import("./gateway-models"),
			import("../../session/session-manager"),
			import("../../vendor/agent/src/index.ts"),
			import("../../session/agent-session"),
			import("bun:sqlite"),
		]);
	const settings = await Settings.init({ cwd });
	await ensureTheme();
	await initTheme(
		process.stdin.isTTY === true,
		cfgSymbolPreset.get(settings),
		cfgColorBlindMode.get(settings),
		cfgThemeDark.get(settings),
		cfgThemeLight.get(settings),
	);
	// Startup chrome is omp's, unchanged (plan §2.3.1): a resume suppresses the welcome intro and
	// skips What's New, while the changelog is keyed to the *shell* version whose CHANGELOG.md it
	// renders — the launcher's `HERMES_VERSION` only relabels the lockup.
	const resuming = resume !== undefined;
	const showStartupSplash = shouldShowStartupSplash({
		configured: cfgStartupShowSplash.get(settings),
		isInteractive: process.stdin.isTTY === true && process.stdout.isTTY === true,
		resuming,
		quiet: cfgStartupQuiet.get(settings),
		timing: Boolean($env.PI_TIMING),
		stdinIsTTY: process.stdin.isTTY,
		stdoutIsTTY: process.stdout.isTTY,
	});
	// The vendored shell has no package-root CHANGELOG.md; `undefined` selects the bundled asset the
	// changelog reader already falls back to (an existing-but-empty path would parse as "no entries").
	const changelogPath = getChangelogPath();
	const startupChangelog = resuming
		? undefined
		: await resolveStartupChangelogForDisplay({
				mode: cfgStartupChangelogMode.get(settings),
				currentVersion: VERSION,
				changelogPath: changelogPath && fsSync.existsSync(changelogPath) ? changelogPath : undefined,
			});
	const authStorage = new AuthStorage(new SqliteAuthCredentialStore(new Database(":memory:")));
	const modelRegistry = new GatewayModelRegistry(authStorage, undefined, {
		ignoreLocalModelConfig: true,
		settings,
	});
	const sessionManager = SessionManager.inMemory(cwd);
	const agent = new Agent();
	const session = new AgentSession({
		agent,
		sessionManager,
		settings,
		modelRegistry,
		memoryEnabled: false,
		interactivePrompts: true,
	});
	const { connectGatewayOmpSession } = await import("./gateway-session");
	let gateway: Awaited<ReturnType<typeof connectGatewayOmpSession>>;
	try {
		gateway = await connectGatewayOmpSession({
			cwd,
			...(resume ? { resumeSessionId: resume } : {}),
			onError: error => session.emitNotice("error", error.message, "gateway"),
		});
	} catch (error) {
		process.stderr.write(
			`omp: cannot open the Hermes session${resume ? ` ${resume}` : ""}: ${error instanceof Error ? error.message : String(error)}\n`,
		);
		process.exitCode = 1;
		return;
	}
	// The authority owns credentials and routing; the shell's pickers render its inventory and the
	// only local artifact is an in-memory availability marker (nothing is persisted to the omp DB).
	const { installGatewayModelCatalog } = await import("./gateway-models");
	try {
		installGatewayModelCatalog(modelRegistry, await gateway.modelOptions());
	} catch (error) {
		session.emitNotice("warning", `Model catalog unavailable: ${error instanceof Error ? error.message : String(error)}`, "gateway");
	}
	// Seed the shell's transcript from the gateway snapshot before the first render; `attachGateway`
	// mirrors the same messages onto the local agent for token/context accounting.
	for (const message of gateway.initialMessages()) {
		sessionManager.appendMessage(message as Parameters<SessionManagerType["appendMessage"]>[0]);
	}
	session.attachGateway(gateway);
	await gateway.refresh();
	const InteractiveModeConstructor = await loadInteractiveModeConstructor();
	const mode = new InteractiveModeConstructor(session, version, startupChangelog);
	await mode.init({
		suppressWelcomeIntro: resuming || showStartupSplash,
		clearInitialTerminalHistory: true,
		autoStartCollab: false,
	});
	if (showStartupSplash) {
		const { runStartupSplash } = await import("../../modes/setup");
		await runStartupSplash(mode);
	}
	await mode.renderInitialMessages({ preserveExistingChat: true });
	const { submitInteractiveInput } = await import("../../main.ts");
	while (true) {
		const input = await mode.getUserInput();
		await submitInteractiveInput(mode, session, input);
	}
}

/** `--resume SESSION` / `--resume=SESSION` / `-r SESSION`; anything else is ignored by this entry. */
function parseResumeArg(args: string[]): string | undefined {
	for (let index = 0; index < args.length; index += 1) {
		const arg = args[index]!;
		if (arg === "--resume" || arg === "-r") return args[index + 1];
		if (arg.startsWith("--resume=")) return arg.slice("--resume=".length) || undefined;
		if (arg.startsWith("-r=")) return arg.slice(3) || undefined;
	}
	return undefined;
}
