"""``<title>`` marker contract for naming replies — sprint-1-title-protocol AS-3/AS-4 (ADR-21 D4).

Parsing (`parse_title_output`) and the shared naming guard (`clean_title`) are tested against the
frozen contract: closed / self-closing / ``none`` markers, unlabeled prose, first-non-empty-line
decides, quote / ``Title:`` / trailing-punctuation stripping, reject-not-truncate bounds, and
``think`` stripping before the parse.
"""

from __future__ import annotations

import pytest

from agent.title_parser import (
    MAX_TITLE_CHARS,
    MAX_TITLE_WORDS,
    TitleParse,
    clean_title,
    parse_title_output,
)


class TestMarkerParsing:
    """AS-3: one ``<title>`` marker contract for every stage-1 naming reply."""

    def test_closed_tag_yields_the_inner_text(self):
        assert parse_title_output("<title>Fix retry queue</title>") == TitleParse("Fix retry queue")

    def test_self_closing_tag_terminates_naming(self):
        parsed = parse_title_output("<title/>")
        assert parsed.title is None and parsed.terminal is True

    def test_none_inner_terminates_naming(self):
        """``<title>none</title>`` is the decline in tag form; case-insensitive like the tags."""
        assert parse_title_output("<title>none</title>") == TitleParse(None, terminal=True)
        assert parse_title_output("<title>NONE</title>") == TitleParse(None, terminal=True)

    def test_markers_are_case_insensitive(self):
        assert parse_title_output("<TITLE>Fix retry queue</TITLE>") == TitleParse("Fix retry queue")
        assert parse_title_output("<Title/>") == TitleParse(None, terminal=True)

    def test_unlabeled_prose_titles_the_session(self):
        """A model that ignored the marker convention still titles the session with its line."""
        assert parse_title_output("Fix retry queue") == TitleParse("Fix retry queue")

    def test_multiline_output_takes_the_first_nonempty_line(self):
        parsed = parse_title_output("\n  \n<title>Fix retry queue</title>\nextra")
        assert parsed == TitleParse("Fix retry queue")
        assert parse_title_output("\n\n First line title \nsecond line") == TitleParse("First line title")

    def test_surrounding_whitespace_is_trimmed(self):
        assert parse_title_output("  <title/>  ") == TitleParse(None, terminal=True)
        assert parse_title_output("\n<title>  Fix retry queue  </title>\n") == TitleParse("Fix retry queue")

    def test_quoted_inner_text_is_unquoted_by_the_shared_guard(self):
        assert parse_title_output('<title>"Fix retry queue"</title>') == TitleParse("Fix retry queue")

    def test_empty_tag_is_unusable_but_not_a_decline(self):
        """No text and no decline marker: unusable now, the naming attempt is not over."""
        assert parse_title_output("<title></title>") == TitleParse(None)

    def test_truncated_marker_degrades_to_its_text(self):
        """A token cap that cut the reply mid-marker must not leak ``<title>`` into a title."""
        assert parse_title_output("<title>Fix retry queue") == TitleParse("Fix retry queue")
        assert parse_title_output("Fix retry queue</title>") == TitleParse("Fix retry queue")

    def test_empty_reply_is_unusable_but_not_a_decline(self):
        assert parse_title_output("") == TitleParse(None)
        assert parse_title_output("  \n  ") == TitleParse(None)

    def test_bare_none_prose_is_rejected_but_not_a_decline(self):
        """The decline needs the tag form; prose "none" is just an unusable guard rejection."""
        parsed = parse_title_output("none")
        assert parsed.title is None and parsed.terminal is False


class TestSharedGuard:
    """AS-4: quotes / ``Title:`` prefix / trailing punctuation stripped; answer-shaped output
    rejected (never truncated); ``think`` blocks stripped before parsing."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ('"Fix retry queue"', "Fix retry queue"),
            ("'Fix retry queue'", "Fix retry queue"),
            ('"Fix \'inner\' queue"', "Fix 'inner' queue"),
            ("Title: Fix retry queue", "Fix retry queue"),
            ("TITLE: Fix retry queue", "Fix retry queue"),
            ('Title: "Fix retry queue."', "Fix retry queue"),
            ("Fix retry queue.", "Fix retry queue"),
            ("Fix retry queue!", "Fix retry queue"),
            ("Fix  retry   queue", "Fix retry queue"),
        ],
    )
    def test_strips_quotes_prefix_and_trailing_punctuation(self, raw, expected):
        assert clean_title(raw) == expected

    def test_eighty_characters_is_the_hard_cap(self):
        """AS-4 boundary: 80 characters stay a title, 81 are rejected — never truncated to 77+``...``."""
        assert clean_title("B" * MAX_TITLE_CHARS) == "B" * MAX_TITLE_CHARS
        over = clean_title("A" * (MAX_TITLE_CHARS + 1))
        assert over is None
        assert "..." not in (over or "")

    def test_twelve_words_fit_thirteen_do_not(self):
        twelve = "one two three four five six seven eight nine ten eleven twelve"
        assert clean_title(twelve) == twelve
        assert clean_title(twelve + " thirteen") is None
        assert clean_title("w" * MAX_TITLE_WORDS) == "w" * MAX_TITLE_WORDS  # 12 single-char words

    def test_zero_words_are_rejected(self):
        for blank in ("", "   ", "!!!", "---", "..."):
            assert clean_title(blank) is None, blank

    def test_non_latin_and_numeric_titles_pass(self):
        assert clean_title("修复登录按钮") == "修复登录按钮"
        assert clean_title("42") == "42"

    def test_rejects_the_bare_decline_word(self):
        assert clean_title("none") is None

    def test_think_blocks_are_stripped_before_the_first_line_decides(self):
        """Reasoning can neither leak into the title nor hide the marker behind it (ADR-21 D4)."""
        wrapped = (
            " <think> thought about the task, maybe the user wants X. </think>\n"
            "<title>Fix retry queue</title>"
        )
        assert parse_title_output(wrapped) == TitleParse("Fix retry queue")

    def test_think_block_alone_leaves_nothing(self):
        assert parse_title_output(" <think> only, the model never answered </think>") == TitleParse(None)

    def test_marker_inside_a_think_block_is_ignored(self):
        parsed = parse_title_output(
            " <think> <title>Pretend task</title> </think>\n<title>Fix retry queue</title>"
        )
        assert parsed == TitleParse("Fix retry queue")

    def test_unterminated_think_block_strips_to_nothing(self):
        assert parse_title_output(" <think> reasoning that never closed") == TitleParse(None)
