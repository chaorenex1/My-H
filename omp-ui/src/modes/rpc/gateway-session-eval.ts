/**
 * ADR-4 D12: one decoded `execute_code` result (live or resumed) → the vendored eval card's
 * `EvalToolDetails`.
 *
 * Kept out of `gateway-session.ts` (file-size ratchet) and split into plan → cell → assembly so
 * each phase stays reviewable: language/status/exit mapping, per-cell display, truncation meta and
 * the background-job handle. `undefined` means "not a recognizable eval result" and the caller
 * keeps the plain text card; nothing here guesses a status or invents an exit code.
 */

import type { ImageContent } from "../../vendor/ai/src/index.ts"
import type {
	EvalCellResult,
	EvalLanguage,
	EvalStatusEvent,
	EvalToolDetails,
	EvalViewEntry
} from "@oh-my-pi/pi-tui/tools/eval"
import type { OutputMeta } from "@oh-my-pi/pi-tui/tools/output-meta"

const asRecord = (value: unknown): Record<string, unknown> | undefined =>
	typeof value === "object" && value !== null && !Array.isArray(value)
		? (value as Record<string, unknown>)
		: undefined

const asString = (value: unknown): string | undefined => (typeof value === "string" ? value : undefined)

const asNumber = (value: unknown): number | undefined =>
	typeof value === "number" && Number.isFinite(value) ? value : undefined

export interface ExecuteCodeDetailsOptions {
	isError?: boolean
	/** D15's native truncation notice names the archive read path. */
	toolCallId?: string
	cardTitle?: string
	cellTitles?: readonly (string | null | undefined)[]
	/** Durable display block (resume) when the result JSON no longer carries one. */
	display?: Record<string, unknown>
}

const EVAL_LANGUAGES: Readonly<Record<string, EvalLanguage>> = {
	py: "python",
	python: "python",
	js: "js",
	javascript: "js"
}

const EVAL_STATUS: Readonly<Record<string, EvalCellResult["status"]>> = {
	pending: "pending",
	running: "running",
	success: "complete",
	complete: "complete",
	ok: "complete",
	error: "error",
	timeout: "error",
	interrupted: "error",
	cancelled: "error",
	failed: "error"
}

const evalLanguage = (value: unknown): EvalLanguage | undefined =>
	typeof value === "string" ? EVAL_LANGUAGES[value.toLowerCase()] : undefined

/** The real head/tail marker `_truncate_stdout_text` puts in the output (D15 keeps one warning). */
const TRUNCATION_MARKER = /\n\n\.\.\. \[OUTPUT TRUNCATED - [\d,]+ bytes omitted out of [\d,]+ total\] \.\.\.\n\n/g

const stripTruncationMarker = (text: string): string => text.replace(TRUNCATION_MARKER, "\n").trimEnd()

const records = (value: unknown): Record<string, unknown>[] =>
	Array.isArray(value) ? value.filter((entry): entry is Record<string, unknown> => asRecord(entry) !== undefined) : []

const evalViews = (value: unknown): EvalViewEntry[] | undefined => {
	if (!Array.isArray(value)) return undefined
	const entries = value.filter((entry): entry is EvalViewEntry => {
		const record = asRecord(entry)
		return record !== undefined && typeof record.text === "string" && asRecord(record.view) !== undefined
	})
	return entries.length > 0 ? entries : undefined
}

const evalStatusEvents = (value: unknown): EvalStatusEvent[] | undefined => {
	const entries = records(value).filter((entry): entry is EvalStatusEvent => entry !== undefined)
	return entries.length > 0 ? entries : undefined
}

const evalImages = (value: unknown): ImageContent[] | undefined => {
	if (!Array.isArray(value)) return undefined
	const entries = value.filter(
		(entry): entry is ImageContent =>
			asRecord(entry)?.type === "image" &&
			typeof asRecord(entry)?.data === "string" &&
			typeof asRecord(entry)?.mimeType === "string"
	)
	return entries.length > 0 ? entries : undefined
}

interface EvalPlan {
	result?: Record<string, unknown>
	arg: Record<string, unknown>
	resultCells: Record<string, unknown>[]
	argCells: Record<string, unknown>[]
	inputs: Record<string, unknown>[]
	singleCode?: string
	topDisplay?: Record<string, unknown>
}

/** Gather the cell sources: the result's own cells, else the call's, else one `code` cell. */
function evalPlan(args: unknown, decoded: unknown): EvalPlan | undefined {
	const result = asRecord(decoded)
	const arg = asRecord(args) ?? {}
	const resultCells = records(result?.cells)
	const argCells = records(arg.cells)
	const singleCode = typeof arg.code === "string" ? arg.code : undefined
	// Bad JSON / an unrecognized shape must fall back to text: never synthesize a `complete` cell
	// from args alone (acceptance 19).
	const recognizable = result !== undefined &&
		(resultCells.length > 0 || (typeof result.status === "string" && typeof result.output === "string"))
	if (!recognizable) return undefined
	const inputs =
		resultCells.length > 0 ? resultCells : argCells.length > 0 ? argCells : [{ code: singleCode, title: arg.title }]
	return {
		result,
		arg,
		resultCells,
		argCells,
		inputs,
		singleCode,
		topDisplay: asRecord(result?.display)
	}
}

function evalCellStatus(raw: string | undefined, isError: boolean | undefined): EvalCellResult["status"] {
	// A multi-cell result is `error` as a whole while its other cells succeeded: each cell keeps its
	// own status, and only a cell without one inherits the call's error verdict.
	if (raw) return EVAL_STATUS[raw] ?? (isError ? "error" : "complete")
	return isError ? "error" : "complete"
}

function evalCellTitle(
	input: Record<string, unknown>,
	index: number,
	count: number,
	options: ExecuteCodeDetailsOptions
): string | undefined {
	const explicit = asString(input.title)
	if (explicit) return explicit
	const generated = options.cellTitles?.[index]
	if (generated) return generated
	return count === 1 ? options.cardTitle : undefined
}

function evalCellExit(rawStatus: string | undefined, exitCode: number | undefined): number | undefined {
	if (exitCode === undefined || exitCode < 0) return undefined
	if (rawStatus === "timeout" || rawStatus === "interrupted" || rawStatus === "cancelled") return undefined
	return exitCode
}

function evalCellDisplay(input: Record<string, unknown>, index: number, top?: Record<string, unknown>) {
	const own = asRecord(input.display)
	const source = own ?? (index === 0 ? top : undefined)
	return {
		views: evalViews(input.views) ?? evalViews(source?.views),
		statusEvents: evalStatusEvents(input.statusEvents) ?? evalStatusEvents(source?.statusEvents)
	}
}

function buildEvalCell(
	plan: EvalPlan,
	index: number,
	options: ExecuteCodeDetailsOptions
): EvalCellResult | undefined {
	const input = plan.inputs[index]!
	const argCell = plan.argCells[index] ?? {}
	const code = asString(input.code) ?? asString(argCell.code) ?? (index === 0 ? plan.singleCode : undefined)
	if (code === undefined) return undefined
	const rawStatus = asString(input.status) ?? (index === 0 ? asString(plan.result?.status) : undefined)
	const language =
		evalLanguage(input.language) ?? evalLanguage(argCell.language) ?? evalLanguage(plan.arg.language) ?? "python"
	const { views, statusEvents } = evalCellDisplay(input, index, plan.topDisplay)
	const cell: EvalCellResult = {
		index,
		code,
		language,
		output: stripTruncationMarker(
			asString(input.output) ?? (index === 0 ? asString(plan.result?.output) : undefined) ?? ""
		),
		status: evalCellStatus(rawStatus, options.isError)
	}
	const title = evalCellTitle(input, index, plan.inputs.length, options)
	if (title) cell.title = title
	if (views) cell.views = views
	if (statusEvents) cell.statusEvents = statusEvents
	const duration = asNumber(input.duration_seconds)
	if (duration !== undefined) cell.durationMs = Math.round(duration * 1000)
	const exitCode = evalCellExit(rawStatus, asNumber(input.exit_code))
	if (exitCode !== undefined) cell.exitCode = exitCode
	return cell
}

/** ADR-4 D15: byte/line facts → the vendored renderer's native middle-elision warning. */
function evalOutputMeta(source: Record<string, unknown> | undefined): OutputMeta | undefined {
	if (!source || source.stdout_truncated !== true) return undefined
	const totalBytes = asNumber(source.stdout_bytes_total)
	const outputBytes = asNumber(source.stdout_bytes_captured)
	if (totalBytes === undefined || outputBytes === undefined || totalBytes <= outputBytes) return undefined
	const elidedBytes = asNumber(source.stdout_bytes_omitted) ?? totalBytes - outputBytes
	const totalLines = asNumber(source.stdout_lines_total) ?? 0
	const outputLines = Math.min(asNumber(source.stdout_lines_shown) ?? totalLines, totalLines)
	return {
		truncation: {
			direction: "middle",
			truncatedBy: "bytes",
			totalBytes,
			outputBytes,
			elidedBytes,
			totalLines,
			outputLines
		}
	}
}

function evalMeta(plan: EvalPlan, cells: readonly EvalCellResult[]): OutputMeta | undefined {
	if (plan.resultCells.length > 0) return evalOutputMeta(plan.result)
	if (cells.length === 1) return evalOutputMeta({ ...(plan.result ?? {}), ...plan.inputs[0] })
	return evalOutputMeta(plan.result)
}

/** A multi-cell aggregate keeps `images` on each cell's `display` block, not at the top level. */
function evalAllImages(plan: EvalPlan, topDisplay: Record<string, unknown> | undefined): ImageContent[] | undefined {
	const images: ImageContent[] = []
	const push = (value: unknown) => {
		const found = evalImages(value)
		if (found) images.push(...found)
	}
	push(topDisplay?.images)
	push(plan.result?.images)
	for (const input of plan.inputs) push(asRecord(input.display)?.images)
	return images.length > 0 ? images : undefined
}

function evalLanguages(cells: readonly EvalCellResult[]): EvalLanguage[] {
	const languages: EvalLanguage[] = []
	for (const cell of cells) {
		if (cell.language && !languages.includes(cell.language)) languages.push(cell.language)
	}
	return languages
}

function evalAsync(result: Record<string, unknown> | undefined): EvalToolDetails["async"] {
	const async = asRecord(result?.async)
	if (!async || typeof async.jobId !== "string") return undefined
	const state = asString(async.state)
	return {
		state: state === "completed" || state === "failed" || state === "cancelled" ? state : "running",
		jobId: async.jobId,
		type: "eval"
	}
}

function evalNotice(
	meta: OutputMeta | undefined,
	cells: readonly EvalCellResult[],
	options: ExecuteCodeDetailsOptions
): string | undefined {
	if (!options.toolCallId) return undefined
	if (meta?.truncation) {
		return `Full output: agent_out(mode="artifact", kind="tool", tool_call_id="${options.toolCallId}")`
	}
	if (options.isError && !cells.some(cell => cell.status === "error")) {
		return `Open the transcript row for the full output (tool_call_id="${options.toolCallId}")`
	}
	return undefined
}

/**
 * ADR-4 D12: `execute_code`'s decoded result → the eval renderer's details.
 *
 * Status uses the result JSON's real vocabulary (`success`→`complete`; `error`/`timeout`/
 * `interrupted`→`error`); `is_error` always wins. Timeout/interrupted/negative exits carry no
 * `exitCode`, so the foot cannot attribute a killed call to a cell that finished.
 */
export function executeCodeDetails(
	args: unknown,
	decoded: unknown,
	options: ExecuteCodeDetailsOptions = {}
): EvalToolDetails | undefined {
	const plan = evalPlan(args, decoded)
	if (!plan) return undefined
	// Resume rows can carry the display block only in the durable sidecar.
	if (!plan.topDisplay && options.display) plan.topDisplay = options.display
	const cells: EvalCellResult[] = []
	for (let index = 0; index < plan.inputs.length; index++) {
		const cell = buildEvalCell(plan, index, options)
		if (cell) cells.push(cell)
	}
	if (cells.length === 0) return undefined

	const meta = evalMeta(plan, cells)
	const details: EvalToolDetails = { cells, languages: evalLanguages(cells) }
	if (cells[0]?.language) details.language = cells[0].language
	const topDisplay = plan.topDisplay ?? options.display
	const images = evalAllImages(plan, topDisplay)
	if (images) details.images = images
	const topEvents = evalStatusEvents(topDisplay?.statusEvents)
	if (topEvents && cells.length === 1 && !cells[0]!.statusEvents) cells[0]!.statusEvents = topEvents
	if (meta) details.meta = meta
	const async = evalAsync(plan.result)
	if (async) details.async = async
	const notice = evalNotice(meta, cells, options)
	if (notice) details.notice = notice
	if (options.isError) details.isError = true
	return details
}
