import { describe, expect, test } from "bun:test"

import type { SessionListRow, SessionTranscriptFile } from "../rpc/gateway-contract.generated"
import { pickerRows, previewFromTranscript, toResumeEntry } from "./selector-controller"

const row = (overrides: Partial<SessionListRow> = {}): SessionListRow => ({
	id: "stored-1",
	title: "",
	preview: "fix the build",
	started_at: 1_700_000_000,
	message_count: 4,
	source: "cli",
	archived: false,
	restore_required: false,
	...overrides,
})

const bundle = (overrides: Partial<SessionTranscriptFile> = {}): SessionTranscriptFile => ({
	session_id: "stored-1",
	project: "-tmp-workspace",
	path: "/home/sessions/-tmp-workspace/2026-01-01T00-00-00-000Z_stored-1.jsonl",
	size_bytes: 2048,
	message_count: 4,
	modified_at: 1_700_000_500,
	offloaded: false,
	...overrides,
})

describe("toResumeEntry", () => {
	test("a mirrored row keeps its preview and takes the bundle's file metadata", () => {
		const entry = toResumeEntry(row(), { mirrored: true, archived: false, transcript: bundle() })
		expect(entry.id).toBe("stored-1")
		expect(entry.firstMessage).toBe("fix the build")
		expect(entry.size).toBe(2048)
		expect(entry.modified.getTime()).toBe(1_700_000_500_000)
	})

	test("an unmirrored row carries the not mirrored note", () => {
		const entry = toResumeEntry(row(), { mirrored: false, archived: false })
		expect(entry.note).toBe("not mirrored")
		expect(entry.firstMessage).toBe("fix the build")
	})

	test("an archived row is noted archived and shows its bundle preview", () => {
		const entry = toResumeEntry(row({ preview: "" }), {
			mirrored: true,
			archived: true,
			transcript: bundle({ offloaded: true }),
			preview: "where were we",
		})
		expect(entry.note).toBe("archived")
		expect(entry.firstMessage).toBe("where were we")
	})

	test("an archived row with an unreadable bundle still says archived", () => {
		const entry = toResumeEntry(row({ preview: "" }), {
			mirrored: true,
			archived: true,
			transcript: bundle({ offloaded: true }),
		})
		expect(entry.note).toBe("archived")
	})

	test("a mirrored row carries no note", () => {
		expect(toResumeEntry(row(), { mirrored: true, archived: false }).note).toBeUndefined()
	})

	test("without transcript metadata the picker claims no storage state", () => {
		const entry = toResumeEntry(row())
		expect(entry.note).toBeUndefined()
		expect(entry.size).toBe(0)
	})

	test("a compression tip row is addressed by its resolved id", () => {
		const entry = toResumeEntry(row({ resolved_id: "tip-2" }), { mirrored: true, archived: false })
		expect(entry.path).toBe("tip-2")
		expect(entry.id).toBe("tip-2")
	})
})

describe("previewFromTranscript", () => {
	test("prefers the first user turn", () => {
		expect(
			previewFromTranscript([
				{ role: "system", content: "you are a bot" },
				{ role: "user", content: "the first question" },
				{ role: "assistant", content: "an answer" },
			])
		).toBe("the first question")
	})

	test("joins multipart text and squashes whitespace", () => {
		expect(
			previewFromTranscript([
				{ role: "user", content: [{ type: "text", text: "hello\n  world" }, { type: "image" }] },
			])
		).toBe("hello world")
	})

	test("falls back to the first non-empty message", () => {
		expect(previewFromTranscript([{ role: "assistant", content: "only an answer" }])).toBe("only an answer")
	})

	test("an empty page previews as empty", () => {
		expect(previewFromTranscript([])).toBe("")
	})
})

describe("pickerRows", () => {
	test("drops delegate runs from the human resume list", () => {
		const rows = [row({ id: "human" }), row({ id: "child", source: "subagent" }), row({ id: "shout", source: "SUBAGENT" })]
		expect(pickerRows(rows).map(entry => entry.id)).toEqual(["human"])
	})

	test("keeps a row whose source the listing did not fill in", () => {
		expect(pickerRows([row({ source: "" })]).length).toBe(1)
	})
})
