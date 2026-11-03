import { describe, expect, test } from "bun:test"

import { UiHelpers } from "./ui-helpers"

describe("transcript replay", () => {
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
