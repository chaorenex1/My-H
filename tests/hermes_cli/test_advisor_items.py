"""An advisor row keeps one label per note, so the CLI can paint each note by severity."""

from hermes_cli.cli_agent_setup_mixin import _advisor_items


def test_advisor_items_keep_per_note_labels():
    content = "\n".join(
        [
            "[advisor — advisory only]",
            "- [concern · reviewer] read the import first",
            "- [blocker · reviewer] the test never ran",
            "[end advisor]",
        ]
    )

    assert _advisor_items(content) == [
        ("concern · reviewer", "read the import first"),
        ("blocker · reviewer", "the test never ran"),
    ]


def test_advisor_items_skip_the_bracketed_chrome():
    content = "[advisor — advisory only]\nplain advice from an older host\n[end advisor]"

    assert _advisor_items(content) == []
