"""ADR-16 P0 path contract: project-dir encoding, timestamps, component sanitising,
case-insensitive collision handling, and the Windows component ceiling.

The layout is a cross-process contract (mirror, subagents, advisor, omp-ui), so these tests
pin the *documented* encoding, not the helper's internals.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

import hermes_session_paths as paths


def _resolved(path: Path) -> str:
    absolute = os.path.abspath(str(path))
    return os.path.realpath(absolute) if os.path.exists(absolute) else absolute


def _roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    home = tmp_path / "home"
    temp = home / "Local" / "Temp"  # Windows' temp root nests inside home
    outside = tmp_path / "outside"
    return home, temp, outside


class TestProjectDirEncoding:
    def test_path_inside_home_is_home_relative(self, tmp_path):
        home, temp, _ = _roots(tmp_path)
        assert paths.encode_project_dir(
            home / "work" / "proj", os_home=home, temp_root=temp
        ) == "-work-proj"

    def test_home_itself_is_a_single_dash(self, tmp_path):
        home, temp, _ = _roots(tmp_path)
        assert paths.encode_project_dir(home, os_home=home, temp_root=temp) == "-"

    def test_temp_root_wins_over_home(self, tmp_path):
        home, temp, _ = _roots(tmp_path)
        assert paths.encode_project_dir(
            temp / "work 项目" / "proj", os_home=home, temp_root=temp
        ) == "-tmp-work 项目-proj"

    def test_outside_home_and_temp_gets_double_dash_absolute(self, tmp_path):
        home, temp, outside = _roots(tmp_path)
        cwd = outside / "proj"
        encoded = paths.encode_project_dir(cwd, os_home=home, temp_root=temp)
        expected = "--" + re.sub(r"[/\\:]", "-", _resolved(cwd).lstrip("/\\")) + "--"
        assert encoded == expected
        assert ":" not in encoded
        assert encoded.startswith("--") and encoded.endswith("--")

    @pytest.mark.platforms("windows")
    def test_home_is_case_insensitive_for_containment(self, tmp_path):
        home, temp, _ = _roots(tmp_path)
        cased = Path(str(home).upper()) / "work"
        # Not every filesystem round-trips case; the contract is the classification, and on a
        # host where the path exists the encoding must still be home-relative, not absolute.
        encoded = paths.encode_project_dir(cased, os_home=home, temp_root=temp)
        assert not encoded.startswith("--")

    @pytest.mark.platforms("posix")
    def test_colon_in_a_component_is_encoded(self, tmp_path):
        home, temp, _ = _roots(tmp_path)
        cwd = os.path.join(str(temp), "a:b", "x y", "中文")
        assert paths.encode_project_dir(cwd, os_home=home, temp_root=temp) == \
            "-tmp-a-b-x y-中文"

    def test_over_long_component_is_capped_with_a_stable_hash(self, tmp_path):
        home, temp, _ = _roots(tmp_path)
        cwd = home / ("a" * 200)
        encoded = paths.encode_project_dir(cwd, os_home=home, temp_root=temp)
        full = "-" + "a" * 200
        digest = hashlib.sha256(full.encode("utf-8")).hexdigest()[:8]
        assert len(encoded) == paths.MAX_COMPONENT_CHARS
        assert encoded == full[: paths.MAX_COMPONENT_CHARS - len(digest) - 1] + "-" + digest
        # Distinct long paths do not collapse onto one directory.
        other = paths.encode_project_dir(
            home / ("a" * 199 + "b"), os_home=home, temp_root=temp
        )
        assert other != encoded

    def test_project_dir_is_under_the_sessions_root(self, tmp_path):
        home, temp, _ = _roots(tmp_path)
        hermes_home = tmp_path / "hermes"
        path = paths.project_dir(
            home / "work", hermes_home=hermes_home, os_home=home, temp_root=temp
        )
        assert path == hermes_home / "sessions" / "-work"

    def test_encoded_names_never_collide_with_reserved_entries(self, tmp_path):
        home, temp, _ = _roots(tmp_path)
        for cwd in (home, temp, tmp_path / "outside"):
            name = paths.encode_project_dir(cwd, os_home=home, temp_root=temp)
            assert not name.startswith(".")
            assert name not in paths.RESERVED_SESSIONS_ENTRIES


class TestTimestamp:
    def test_epoch_is_encoded_as_filesystem_safe_utc_iso(self):
        assert paths.file_safe_timestamp(0.0) == "1970-01-01T00-00-00-000Z"
        assert paths.file_safe_timestamp(1762345678.9) == "2025-11-05T12-27-58-900Z"

    def test_iso_string_matches_the_epoch_form(self):
        assert paths.file_safe_timestamp("2025-11-05T12:27:58.900Z") == \
            "2025-11-05T12-27-58-900Z"

    def test_naive_datetime_is_treated_as_utc(self):
        moment = datetime(2025, 11, 5, 12, 27, 58, 900_000)
        assert paths.file_safe_timestamp(moment) == "2025-11-05T12-27-58-900Z"
        aware = datetime(2025, 11, 5, 12, 27, 58, 900_000, tzinfo=UTC)
        assert paths.file_safe_timestamp(aware) == "2025-11-05T12-27-58-900Z"

    def test_unparseable_string_still_has_no_separators(self):
        assert paths.file_safe_timestamp("not:a.time") == "not-a-time"
        hostile = paths.file_safe_timestamp("../../etc/passwd")
        assert "/" not in hostile and "\\" not in hostile
        assert os.sep not in paths.bundle_name("../../etc/passwd", "sid")

    def test_bundle_name_joins_timestamp_and_session_id(self):
        assert paths.bundle_name(1762345678.9, "20251105_122758_abc123") == (
            "2025-11-05T12-27-58-900Z_20251105_122758_abc123"
        )


class TestComponents:
    @pytest.mark.parametrize(
        ("value", "fallback", "expected"),
        [
            ("web.search", "tool", "web_search"),
            ("", "tool", "tool"),
            ("   ", "agent", "agent"),
            ("Agent (2)", "agent", "Agent__2_"),
            ("a" * 80, "tool", "a" * paths.NAME_COMPONENT_CHARS),
        ],
    )
    def test_sanitize(self, value, fallback, expected):
        assert paths.sanitize_component(value, fallback) == expected

    def test_tool_and_topic_fallbacks(self):
        assert paths.tool_component(None) == "tool"
        assert paths.topic_component("") == "agent"


class TestBundleLayout:
    def test_tool_artifact_paths(self, tmp_path):
        archive, original = paths.tool_artifact_paths(tmp_path, 3, "web.search")
        assert archive == tmp_path / "3.web_search.log"
        assert original == tmp_path / "3.web_search-original.log"

    @pytest.mark.parametrize("step", [0, -1])
    def test_tool_step_must_be_one_based(self, tmp_path, step):
        with pytest.raises(ValueError):
            paths.tool_artifact_paths(tmp_path, step, "terminal")

    def test_advisor_paths(self, tmp_path):
        assert paths.advisor_path(tmp_path) == tmp_path / "_advisor.jsonl"
        assert paths.advisor_path(tmp_path, "security-review") == \
            tmp_path / "_advisor.security-review.jsonl"
        assert paths.advisor_path(tmp_path, "a b") == tmp_path / "_advisor.a_b.jsonl"

    def test_subagent_namespace_and_files(self, tmp_path):
        topic = "Agent (2)"
        assert paths.subagent_dir(tmp_path, topic) == tmp_path / "Agent__2_"
        assert paths.transcript_path(tmp_path, topic) == tmp_path / "Agent__2_.jsonl"
        assert paths.report_path(tmp_path, topic) == tmp_path / "Agent__2_.md"
        assert paths.ast_summary_path(tmp_path, topic) == tmp_path / "Agent__2__ast.json"
        assert paths.structured_output_path(tmp_path, topic) == tmp_path / "Agent__2_.json"

    def test_allocated_topic_avoids_a_sanitized_collision(self, tmp_path):
        assert paths.allocate_topic(tmp_path, "a b") == "a_b"
        (tmp_path / "a_b.jsonl").write_text("", encoding="utf-8")
        digest = hashlib.sha256(b"a?b").hexdigest()[:8]
        assert paths.allocate_topic(tmp_path, "a?b") == f"a_b-{digest}"
        assert paths.transcript_path(tmp_path, "a?b", topic="a_b-" + digest).name \
            == f"a_b-{digest}.jsonl"
        # Case-insensitive on purpose: Windows would fold the two names together.
        (tmp_path / "A_B.md").write_text("", encoding="utf-8")
        assert paths.allocate_topic(tmp_path, "a b", discriminator="x") != "a_b"


class TestCaseCollision:
    def _parent(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        home, temp, _ = _roots(tmp_path)
        parent = paths.project_dir(
            home / "work", hermes_home=tmp_path / "hermes", os_home=home, temp_root=temp
        )
        parent.mkdir(parents=True)
        return parent, home, temp

    def test_missing_parent_uses_the_plain_bundle_name(self, tmp_path):
        home, temp, _ = _roots(tmp_path)
        allocated = paths.allocate_bundle_dir(
            home / "work", 1762345678.9, "abc123",
            hermes_home=tmp_path / "hermes", os_home=home, temp_root=temp,
        )
        assert allocated.name.endswith("_abc123")
        assert not allocated.exists()

    def test_same_spelling_is_idempotent(self, tmp_path):
        parent, home, temp = self._parent(tmp_path)
        started, sid = 1762345678.9, "abcdef"
        exact = parent / paths.bundle_name(started, sid)
        exact.mkdir()
        allocated = paths.allocate_bundle_dir(
            home / "work", started, sid,
            hermes_home=tmp_path / "hermes", os_home=home, temp_root=temp,
        )
        assert allocated == exact

    def test_different_case_gets_the_session_id_hash(self, tmp_path):
        parent, home, temp = self._parent(tmp_path)
        started = 1762345678.9
        (parent / paths.bundle_name(started, "abcdef")).mkdir()
        allocated = paths.allocate_bundle_dir(
            home / "work", started, "ABCDEF",
            hermes_home=tmp_path / "hermes", os_home=home, temp_root=temp,
        )
        digest = hashlib.sha256(b"ABCDEF").hexdigest()[:8]
        assert allocated.name == paths.bundle_name(started, "ABCDEF") + "-" + digest


class TestDeriveTopic:
    """AS-5 (ADR-21 D8): the assignment -> artifact-stem pipeline.

    ``derive_topic`` is a composition, not a second naming pipeline: it picks the first meaningful
    line (skipping the task document's structure), hands it to ``derive_title`` for the 48-char
    truncation, then sanitises into the shared component width. A stem with no ASCII alphanumeric
    left keeps a stable digest address instead of an unreadable ``____`` component.
    """

    def test_leading_structure_is_skipped(self):
        assignment = (
            "## Target\n"
            "```python\n"
            "print('not the topic')\n"
            "```\n"
            "<!-- reviewer note -->\n"
            "Investigate pool exhaustion\n"
            "## Acceptance\n"
            "It is fixed.\n"
        )

        assert paths.derive_topic(assignment) == "Investigate_pool_exhaustion"

    def test_an_inline_comment_before_the_prose_does_not_hide_it(self):
        assert paths.derive_topic("<!-- note --> Fix the flaky login test") \
            == "Fix_the_flaky_login_test"

    def test_the_derived_line_is_truncated_at_the_title_boundary(self):
        assignment = "x" * 120

        topic = paths.derive_topic(assignment)

        # derive_title keeps 48 characters plus its ellipsis, which sanitising maps to '_'.
        assert topic.rstrip("_") == "x" * 48
        assert len(topic) == 49
        assert paths.sanitize_component(topic, "agent") == topic

    def test_the_sanitized_component_fits_the_shared_width(self):
        assignment = "a b c d e f g h i j k l m n o p q r s t u v w x y z " * 3

        topic = paths.derive_topic(assignment)

        assert len(topic) <= paths.NAME_COMPONENT_CHARS
        assert re.fullmatch(r"[A-Za-z0-9_-]+", topic)
        assert paths.sanitize_component(topic, "agent") == topic

    def test_a_stem_without_ascii_alphanumerics_gets_a_stable_digest(self):
        assignment = "修复登录按钮的并发问题"

        topic = paths.derive_topic(assignment)

        assert re.fullmatch(r"topic-[0-9a-f]{8}", topic)
        assert topic == paths.derive_topic(assignment)  # same input, same address
        assert topic != paths.derive_topic("修复会话标题的迁移问题")
        digest = hashlib.sha256(assignment.encode("utf-8")).hexdigest()[:8]
        assert topic == f"topic-{digest}"

    def test_allocation_adds_an_eight_hex_suffix_for_a_collision(self, tmp_path):
        topic = paths.derive_topic("Investigate the connection pool exhaustion")
        first = paths.allocate_topic(tmp_path, topic, discriminator="run-1")
        (tmp_path / f"{first}.jsonl").write_text("", encoding="utf-8")
        second = paths.allocate_topic(tmp_path, topic, discriminator="run-2")

        assert first == topic
        assert re.fullmatch(rf"{re.escape(topic)}-[0-9a-f]{{8}}", second)
        assert second != first

    def test_an_empty_assignment_falls_back_to_the_run_name(self):
        assert paths.derive_topic("   ", fallback="researcher") == "researcher"
        assert paths.derive_topic("", fallback="code Reviewer") == "code_Reviewer"
        assert paths.derive_topic("## Only structure\n```\nfenced\n```", fallback="researcher") \
            == "researcher"
