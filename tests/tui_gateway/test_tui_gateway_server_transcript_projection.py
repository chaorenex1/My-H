"""ADR-10 P5: the transcript projection's custom/synthetic rows.

Split out of ``test_tui_gateway_server`` (file-line ratchet): these four tests pin the wire shape
after ``display_kind``/``display_metadata`` were removed.
"""

from tui_gateway import server


def test_history_to_messages_types_the_failed_turn_boundary_for_resume():
    """ADR-10 P5: the wire marker is ``synthetic_kind`` (a room poller must not post the boundary
    as the member's reply). Untyped legacy rows are no longer prefix-sniffed."""
    from agent.turn_failure_copy import FAILED_TURN_DISPLAY_KIND, FAILED_TURN_NOTICE, PARTIAL_FAILED_TURN_NOTICE

    history = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": FAILED_TURN_NOTICE, "display_kind": FAILED_TURN_DISPLAY_KIND},
        {"role": "user", "content": "b"},
        {"role": "assistant", "content": PARTIAL_FAILED_TURN_NOTICE},  # legacy untyped row
        {"role": "user", "content": "c"},
        {"role": "assistant", "content": f"Quoting Hermes: {FAILED_TURN_NOTICE}"},  # a real reply
    ]

    assert [m.get("synthetic_kind") for m in server._history_to_messages(history)] == [
        None, FAILED_TURN_DISPLAY_KIND, None, None, None, None,
    ]


def test_history_to_messages_projects_a_skill_turn_to_its_invocation():
    # A /skill invocation is persisted EXPANDED: the activation note plus the
    # entire skill body. That payload is model-facing scaffolding -- this
    # projection is the single display source every client reads, so it must
    # hand back the invocation the user typed and never the body. Without it a
    # chat bubble renders the whole skill as if the user had written it.
    scaffolded = (
        '[IMPORTANT: The user has invoked the "work" skill, indicating they '
        "want you to follow its instructions. The full skill content is "
        "loaded below.]\n\n"
        "# /work\n\nSPIN UP A WORKTREE, never the primary checkout.\n\n"
        "The user has provided the following instruction alongside the skill "
        "invocation: fix the title leak"
    )

    history = [
        {"role": "user", "content": scaffolded},
        {"role": "assistant", "content": "on it"},
    ]

    projected = server._history_to_messages(history)
    assert projected[0]["role"] == "custom"
    assert projected[0]["custom_type"] == "skill-prompt"
    assert projected[0]["text"] == "/work fix the title leak"
    assert projected[0]["details"]["name"] == "work"
    assert projected[0]["details"]["args"] == "fix the title leak"
    assert projected[0]["details"]["prompt"] == scaffolded
    assert "display_kind" not in projected[0]
    assert projected[1] == {"role": "assistant", "text": "on it"}


def test_history_to_messages_projects_a_bare_skill_turn_to_the_command():
    scaffolded = (
        '[IMPORTANT: The user has invoked the "work" skill, indicating they '
        "want you to follow its instructions. The full skill content is "
        "loaded below.]\n\n# /work\n\nSPIN UP A WORKTREE."
    )

    projected = server._history_to_messages([{"role": "user", "content": scaffolded}])
    assert len(projected) == 1
    assert projected[0]["role"] == "custom"
    assert projected[0]["custom_type"] == "skill-prompt"
    assert projected[0]["text"] == "/work"
    assert projected[0]["details"]["name"] == "work"
    assert projected[0]["details"]["prompt"] == scaffolded


def test_history_to_messages_leaves_an_untyped_auto_continue_row_plain():
    # ADR-10 P5 removed the prefix sniff. A crash-interrupted row persisted before turn-start
    # typing landed now projects as ordinary user text; only rows typed at persist time carry
    # ``synthetic_kind``.
    history = [
        {"role": "user", "content": "keep going"},
        {"role": "user", "content": server._auto_continue_note("keep going")},
    ]

    projected = server._history_to_messages(history)

    assert projected == [
        {"role": "user", "text": "keep going"},
        {"role": "user", "text": server._auto_continue_note("keep going")},
    ]
