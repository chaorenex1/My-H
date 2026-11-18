import type { TodoPhase } from "@oh-my-pi/pi-tui/tools/todo";
import { formatPhaseDisplayName } from "@oh-my-pi/pi-tui/tools/todo";
import { theme } from "@oh-my-pi/pi-tui/theme";
import type { InteractiveModeContext } from "../types";

const USAGE = [
	"Usage: /todo [verb]",
	"  /todo             Show the session's todos",
	"  /todo expand      Show every phase and task in the HUD",
	"  /todo collapse    Restore the bounded HUD preview",
].join("\n");

/** One task line: the status glyph plus the task's text, colored by state. */
function formatTaskLine(phase: TodoPhase, index: number): string {
	const task = phase.tasks[index]!;
	const box = task.status === "completed" ? theme.checkbox.checked : theme.checkbox.unchecked;
	switch (task.status) {
		case "completed":
			return theme.fg("success", `  ${box} ${task.content}`);
		case "in_progress":
			return theme.fg("accent", `  ${box} ${task.content}`);
		case "abandoned":
			return theme.fg("error", `  ${box} ${task.content}`);
		case "blocked":
			return theme.fg("warning", `  ${box} ${task.content} (blocked)`);
		default:
			return theme.fg("dim", `  ${box} ${task.content}`);
	}
}

/** The authority's todo snapshot as transcript text. */
export function formatTodoPhases(phases: readonly TodoPhase[]): string {
	const lines: string[] = [];
	for (const [index, phase] of phases.entries()) {
		lines.push(theme.bold(formatPhaseDisplayName(phase.name, index + 1)));
		for (let task = 0; task < phase.tasks.length; task++) lines.push(formatTaskLine(phase, task));
	}
	return lines.join("\n");
}

/**
 * `/todo` over the authority's todo state.
 *
 * The gateway owns the list (`todo.updated`, `session.create`'s `todo_state`), so
 * this controller only displays it: every verb that used to mutate a local list —
 * append/start/done/drop/rm, editor round-trips, file import/export — has no
 * source to write back to and is gone.
 */
export class TodoCommandController {
	constructor(private readonly ctx: InteractiveModeContext) {}

	async handleTodoCommand(args: string): Promise<void> {
		const trimmed = args.trim();
		if (!trimmed) {
			this.#showCurrent();
			return;
		}

		const spaceIndex = trimmed.search(/\s/);
		const verb = (spaceIndex === -1 ? trimmed : trimmed.slice(0, spaceIndex)).toLowerCase();

		switch (verb) {
			case "expand":
				this.ctx.setTodoExpanded(true);
				return;
			case "collapse":
				if (this.ctx.todoExpanded) this.ctx.toggleTodoExpansion();
				return;
			case "help":
			case "?":
				this.ctx.showStatus(USAGE);
				return;
			default:
				this.ctx.showError(`Unknown /todo verb "${verb}".\n${USAGE}`);
		}
	}

	#showCurrent(): void {
		const phases = this.ctx.session.getTodoPhases();
		if (phases.length === 0) {
			this.ctx.showStatus("No todos in this session.");
			return;
		}
		this.ctx.showStatus(formatTodoPhases(phases));
	}
}
