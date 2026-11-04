import { describe, expect, test } from "bun:test"
import type { Component } from "@oh-my-pi/pi-tui"
import { CompactionSummaryMessageComponent } from "@oh-my-pi/pi-tui/chat/compaction-summary-message"
import { CustomMessageComponent } from "@oh-my-pi/pi-tui/chat/custom-message"
import { LateDiagnosticsMessageComponent } from "@oh-my-pi/pi-tui/chat/late-diagnostics-message"
import { StrippedToolCallsPlaceholder } from "@oh-my-pi/pi-tui/chat/stripped-tool-calls-placeholder"
import { UserMessageComponent } from "@oh-my-pi/pi-tui/chat/user-message"
import { initThemeSync } from "@oh-my-pi/pi-tui/theme"

import type { AgentMessage } from "../../vendor/agent/src/index.ts"
import { Settings } from "../../config/settings"
import { compactedTranscriptStart, UiHelpers } from "./ui-helpers"

describe("transcript replay", () => {
	test("a stripped-tool-calls block mounts the elided placeholder", () => {
		initThemeSync()
		const added: unknown[] = []
		const helper = new UiHelpers({
			chatContainer: {
				addChild: (component: unknown) => {
					added.push(component)
					return component
				}
			},
			hideToolActivity: false
		} as never)
		const components = helper.addMessageToChat({
			role: "custom",
			customType: "stripped-tool-calls",
			content: "",
			display: true,
			details: { count: 2 },
			attribution: "agent",
			timestamp: 1
		})
		expect(components).toHaveLength(1)
		expect(components[0]).toBeInstanceOf(StrippedToolCallsPlaceholder)
		expect(added).toEqual(components)
	})

	test("an lsp-late-diagnostic block mounts the shared diagnostics component", () => {
		initThemeSync()
		const added: unknown[] = []
		const helper = new UiHelpers({
			chatContainer: {
				children: [],
				addChild: (component: unknown) => {
					added.push(component)
					return component
				}
			},
			hideToolActivity: false
		} as never)
		const components = helper.addMessageToChat({
			role: "custom",
			customType: "lsp-late-diagnostic",
			content: "<system-notice>Diagnostics for the files you edited</system-notice>",
			display: true,
			details: {
				files: [
					{
						path: "C:/w/a.py",
						summary: "1 error(s)",
						errored: true,
						messages: ["C:/w/a.py:1:1 [error] [ty] boom"]
					}
				]
			},
			attribution: "agent",
			timestamp: 1
		})
		expect(components).toHaveLength(1)
		expect(components[0]).toBeInstanceOf(LateDiagnosticsMessageComponent)
		expect(added).toEqual(components)
	})

	test("an lsp-late-diagnostic block without files falls back to the generic block", () => {
		initThemeSync()
		const helper = new UiHelpers({
			chatContainer: { children: [], addChild: (component: unknown) => component },
			hideToolActivity: false
		} as never)
		const components = helper.addMessageToChat({
			role: "custom",
			customType: "lsp-late-diagnostic",
			content: "diagnostics",
			display: true,
			attribution: "agent",
			timestamp: 1
		})
		expect(components).toHaveLength(1)
		expect(components[0]).toBeInstanceOf(CustomMessageComponent)
	})

	test("a preserved live tool call is not replayed a second time (#6516)", () => {
		const helper = new UiHelpers({} as never)
		const added = helper.addMessageToChat(
			{
				role: "toolResult",
				toolCallId: "t1",
				toolName: "read",
				content: [],
				isError: false,
				timestamp: 1
			},
			{ preservedLiveToolCallIds: new Set(["t1"]) }
		)
		expect(added).toEqual([])
	})
})

describe("display.collapseCompacted", () => {
	const ledger: AgentMessage[] = [
		{ role: "user", content: "before", timestamp: 1 },
		{
			role: "compactionSummary",
			summary: "summary body",
			tokensBefore: 256_000,
			tokensAfter: 20_000,
			method: "llm_summary",
			timestamp: 2
		},
		{ role: "user", content: "after", timestamp: 3 }
	]

	function replay(messages: readonly AgentMessage[], collapse: boolean): Component[] {
		const children: Component[] = []
		const helper = new UiHelpers({
			settings: Settings.isolated({ "display.collapseCompacted": collapse }),
			chatContainer: {
				children,
				addChild: (component: Component) => {
					children.push(component)
					return component
				}
			},
			transcriptMessageComponents: new WeakMap(),
			pendingTools: new Map(),
			sessionManager: { getMessages: () => messages }
		} as never)
		helper.renderSessionContext()
		return children
	}

	test("the replay starts at the newest compaction boundary", () => {
		expect(compactedTranscriptStart(ledger, true)).toBe(1)
		expect(compactedTranscriptStart(ledger, false)).toBe(0)
		expect(compactedTranscriptStart([{ role: "user", content: "x", timestamp: 1 }], true)).toBe(0)
	})

	test("collapsing renders the divider and the retained tail only", () => {
		initThemeSync()
		const children = replay(ledger, true)
		expect(children.map(child => child.constructor)).toEqual([
			CompactionSummaryMessageComponent,
			UserMessageComponent
		])
		// The omission is render-time only: /copy, rewind and session.history read the full ledger.
		expect(ledger).toHaveLength(3)
	})

	test("inline mode renders every row with the divider in place", () => {
		initThemeSync()
		const children = replay(ledger, false)
		expect(children.map(child => child.constructor)).toEqual([
			UserMessageComponent,
			CompactionSummaryMessageComponent,
			UserMessageComponent
		])
	})
})
