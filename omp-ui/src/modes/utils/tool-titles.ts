/**
 * ADR-4 D13 display titles for tool cards.
 *
 * The gateway computes titles with the same deterministic method as a session's instant title;
 * the client only merges them into the render args `getRenderCells` reads. Generated titles never
 * replace a model-supplied `title`, and they never touch the wire args (display-only copies).
 */

import { isRecord } from "../../vendor/utils/src/index.ts"

/** Fill each cell (or the single code cell) title from `cell_titles`; explicit titles win. */
export function mergeEvalCellTitles(
	args: unknown,
	titles: readonly (string | null | undefined)[] | undefined
): unknown {
	if (!titles || titles.length === 0 || !isRecord(args)) return args
	const cells = Array.isArray(args.cells) ? args.cells : undefined
	if (cells) {
		const merged = cells.map((cell, index) => {
			if (!isRecord(cell) || cell.title || !titles[index]) return cell
			return { ...cell, title: titles[index] }
		})
		return { ...args, cells: merged }
	}
	if (typeof args.code === "string" && !args.title && titles[0]) return { ...args, title: titles[0] }
	return args
}

/** Set one cell's title (a live `tool.title` frame); returns the original args when unchanged. */
export function withEvalCellTitle(args: unknown, cellIndex: number, title: string): unknown {
	if (!isRecord(args)) return args
	const cells = Array.isArray(args.cells) ? args.cells : undefined
	if (cells) {
		const cell = cells[cellIndex]
		if (!isRecord(cell) || cell.title === title) return args
		const merged = cells.slice()
		merged[cellIndex] = { ...cell, title }
		return { ...args, cells: merged }
	}
	if (cellIndex === 0 && typeof args.code === "string" && args.title !== title) return { ...args, title }
	return args
}
