"""Session-title cards as a ``sessions.title`` string sub-form (ADR-21 D11).

One boundary table for the three emitted forms — the fork prompt's ``<title emoji=… code=…>`` tag,
``<icon> <CODE>: title`` and the icon-less ``CODE: title`` — plus the card reply of the top-up
request. A malformed card always degrades to the unchanged plain title; nothing here raises.
"""

from __future__ import annotations

import pytest

from agent.title_card import (
    MAX_CARD_CODE_CHARS,
    MAX_CARD_ICON_CHARS,
    keep_title_card,
    parse_card_reply,
    split_card_title,
)

TITLE = "Fix flaky park tests"


class TestSplitCardTitle:
    """AS-1: three accepted forms, code/icon bounds, and the degrade-to-plain contract."""

    def test_fork_tag_yields_the_card_and_the_plain_title(self):
        raw = (
            "<title emoji='🅿️' code='PARK' nf='nf-md-parking'>"
            f"{TITLE}</title>"
        )

        card, plain = split_card_title(raw)

        assert card == "🅿️ PARK"
        assert plain == TITLE
        assert "nf" not in card and "nf-" not in f"{card}{plain}"

    def test_tag_accepts_double_quotes_and_any_attribute_order(self):
        raw = f'<title code="PARK" nf="nf-md-parking" emoji="🅿️">{TITLE}</title>'

        assert split_card_title(raw) == ("🅿️ PARK", TITLE)

    def test_icon_and_code_prefix_form(self):
        assert split_card_title(f"🅿️ PARK: {TITLE}") == ("🅿️ PARK", TITLE)

    def test_icon_less_prefix_form(self):
        assert split_card_title(f"PARK: {TITLE}") == ("PARK", TITLE)

    def test_first_nonempty_line_decides(self):
        assert split_card_title(f"\n\n  🅿️ PARK: {TITLE}\nignored") == ("🅿️ PARK", TITLE)

    def test_plain_title_has_no_card(self):
        assert split_card_title(TITLE) == (None, TITLE)
        assert split_card_title(f"<title>{TITLE}</title>") == (None, TITLE)

    def test_missing_emoji_attribute_degrades_to_the_inner_title(self):
        assert split_card_title(f"<title code='PARK'>{TITLE}</title>") == (None, TITLE)

    @pytest.mark.parametrize("code", ["park", "PARKING", "ABC-1", ""])
    def test_invalid_codes_never_become_a_card(self, code):
        raw = f"<title emoji='🅿️' code='{code}'>{TITLE}</title>"

        assert split_card_title(raw) == (None, TITLE)
        assert split_card_title(f"{code}: {TITLE}") == (None, f"{code}: {TITLE}")

    def test_code_bounds_are_ascii_one_to_six(self):
        assert split_card_title(f"Z: {TITLE}") == ("Z", TITLE)
        assert split_card_title(f"ABC123: {TITLE}") == ("ABC123", TITLE)
        assert split_card_title(f"ABC1234: {TITLE}") == (None, f"ABC1234: {TITLE}")
        assert MAX_CARD_CODE_CHARS == 6

    def test_ascii_icon_is_not_a_card(self):
        raw = f"A PARK: {TITLE}"

        assert split_card_title(raw) == (None, raw)
        assert split_card_title(f"<title emoji='A' code='PARK'>{TITLE}</title>") == (None, TITLE)

    def test_icon_bounds_are_code_points(self):
        icon_one = "🌲"
        icon_eight = "❄" * MAX_CARD_ICON_CHARS
        icon_nine = "❄" * (MAX_CARD_ICON_CHARS + 1)
        # U+1F17F + U+FE0F: a variation selector counts, so the parking emoji is 2 code points.
        assert len("🅿️") == 2

        assert split_card_title(f"{icon_one} PARK: {TITLE}") == (f"{icon_one} PARK", TITLE)
        assert split_card_title(f"{icon_eight} PARK: {TITLE}") == (f"{icon_eight} PARK", TITLE)
        assert split_card_title(f"{icon_nine} PARK: {TITLE}") == (None, f"{icon_nine} PARK: {TITLE}")
        assert split_card_title("🌲x PARK: " + TITLE) == (None, "🌲x PARK: " + TITLE)

    def test_empty_text_is_an_empty_plain_title(self):
        assert split_card_title("") == (None, "")
        assert split_card_title("   \n ") == (None, "")

    def test_a_title_that_merely_has_a_colon_is_kept(self):
        raw = "Fix the bug: retries drop jobs"

        assert split_card_title(raw) == (None, raw)


class TestKeepTitleCard:
    """AS-6: a regeneration keeps the stored card unless the new title carries its own."""

    def test_plain_new_title_keeps_the_current_card(self):
        assert keep_title_card(
            "Fix flaky auth tests", f"🅿️ PARK: {TITLE}"
        ) == "🅿️ PARK: Fix flaky auth tests"

    def test_new_card_wins(self):
        assert keep_title_card(
            "🐳 DOCKER: Speed up builds", f"🅿️ PARK: {TITLE}"
        ) == "🐳 DOCKER: Speed up builds"

    def test_plain_current_title_leaves_the_new_title_alone(self):
        assert keep_title_card("Fix flaky auth tests", TITLE) == "Fix flaky auth tests"

    def test_keep_is_idempotent(self):
        once = keep_title_card("Fix flaky auth tests", f"PARK: {TITLE}")

        assert keep_title_card(once, f"PARK: {TITLE}") == once


class TestParseCardReply:
    """AS-4: the top-up reply only contributes ``[emoji] CODE``; trailing words are ignored."""

    @pytest.mark.parametrize(
        "reply",
        [
            "<title>🅿️ PARK</title>",
            "<title>🅿️ PARK: extra words</title>",
            "🅿️ PARK: extra words",
            "🅿️ PARK",
        ],
    )
    def test_card_reply_takes_icon_and_code_only(self, reply):
        assert parse_card_reply(reply) == "🅿️ PARK"

    def test_card_reply_accepts_the_tag_attribute_form(self):
        assert parse_card_reply(
            "<title emoji='🅿️' code='PARK'>Fix flaky park tests</title>"
        ) == "🅿️ PARK"

    def test_icon_less_card_reply(self):
        assert parse_card_reply("PARK") == "PARK"

    @pytest.mark.parametrize(
        "reply",
        ["", "   ", "Fix flaky park tests", "A PARK", "park", "PARKING", "🌲x PARK"],
    )
    def test_unusable_replies_yield_no_card(self, reply):
        assert parse_card_reply(reply) is None
