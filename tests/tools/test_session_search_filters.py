"""Advanced session_search features ported from the minir plugin.

Covers the explicit ``mode`` selector (stats/read/scroll), query ``match`` modes,
``source`` filtering (incl. hidden sources), and inactive/compacted read visibility.
"""

import json

import pytest

from hermes_state import SessionDB
from tools.session_search_tool import _apply_match, session_search


@pytest.fixture
def db(tmp_path):
    database = SessionDB(tmp_path / "state.db")
    yield database
    database.close()


def _seed(db):
    db.create_session("cli-alpha", source="cli")
    db.append_message("cli-alpha", role="user", content="deploy the staging server tonight")
    db.append_message("cli-alpha", role="assistant", content="staging deploy finished")
    db.create_session("cron-alpha", source="cron")
    db.append_message("cron-alpha", role="user", content="deploy nightly deploy report deploy")
    db._conn.commit()


class TestMatchModes:
    def test_all_is_the_unchanged_query(self):
        assert _apply_match("alpha beta", "all") == "alpha beta"
        assert _apply_match("alpha beta", None) == "alpha beta"

    def test_any_joins_terms_with_or(self):
        assert _apply_match("alpha beta", "any") == "alpha OR beta"
        assert _apply_match('"alpha beta" gamma', "any") == '"alpha beta" OR gamma'

    def test_any_leaves_explicit_boolean_queries_alone(self):
        assert _apply_match("alpha OR beta", "any") == "alpha OR beta"
        assert _apply_match("alpha NOT beta", "any") == "alpha NOT beta"

    def test_phrase_wraps_once(self):
        assert _apply_match("alpha beta", "phrase") == '"alpha beta"'
        assert _apply_match('"alpha beta"', "phrase") == '"alpha beta"'

    def test_raw_and_unknown_pass_through(self):
        assert _apply_match("deploy* OR staging", "raw") == "deploy* OR staging"
        assert _apply_match("alpha beta", "bogus") == "alpha beta"

    def test_phrase_search_excludes_non_contiguous_terms(self, db):
        db.create_session("contiguous", source="cli")
        db.append_message("contiguous", role="user", content="the alpha beta sequence")
        db.create_session("split", source="cli")
        db.append_message("split", role="user", content="alpha somewhere")
        db.append_message("split", role="assistant", content="beta elsewhere")
        db._conn.commit()

        result = json.loads(session_search(query="alpha beta", match="phrase", db=db))
        assert result["success"] is True
        assert "contiguous" in [entry["session_id"] for entry in result["results"]]
        assert "split" not in [entry["session_id"] for entry in result["results"]]


class TestSourceFilter:
    def test_default_excludes_hidden_source(self, db):
        _seed(db)
        db.create_session("worker", source="subagent")
        db.append_message("worker", role="user", content="deploy from a worker")
        db._conn.commit()

        default = json.loads(session_search(query="deploy", db=db))
        assert "worker" not in [entry["session_id"] for entry in default["results"]]

    def test_explicit_source_selects_only_that_source(self, db):
        _seed(db)
        result = json.loads(session_search(query="deploy", source="cron", db=db))
        assert [entry["session_id"] for entry in result["results"]] == ["cron-alpha"]
        assert result["filters"]["source"] == ["cron"]

    def test_star_searches_hidden_sources_too(self, db):
        _seed(db)
        db.create_session("worker", source="subagent")
        db.append_message("worker", role="user", content="deploy from a worker")
        db._conn.commit()

        result = json.loads(session_search(query="deploy", source="*", db=db))
        assert "worker" in [entry["session_id"] for entry in result["results"]]
        assert result["filters"]["source"] is None


class TestReadVisibility:
    def _session_with_rewound_row(self, db):
        db.create_session("mixed", source="cli")
        db.append_message("mixed", role="user", content="kept message")
        rewound = db.append_message("mixed", role="assistant", content="rewound message")
        db.append_message("mixed", role="user", content="later message")
        db._conn.execute("UPDATE messages SET active = 0, compacted = 0 WHERE id = ?", (rewound,))
        db._conn.commit()
        return rewound

    def test_read_hides_rewound_rows_by_default(self, db):
        self._session_with_rewound_row(db)
        result = json.loads(session_search(mode="read", session_id="mixed", db=db))
        assert result["message_count"] == 2
        assert result["include_inactive"] is False

    def test_include_inactive_reads_rewound_rows(self, db):
        self._session_with_rewound_row(db)
        result = json.loads(session_search(mode="read", session_id="mixed", include_inactive=True, db=db))
        assert result["message_count"] == 3
        assert any("rewound message" in (m.get("content") or "") for m in result["messages"])

    def test_include_compacted_reads_archived_history(self, db):
        db.create_session("compacted", source="cli")
        db.append_message("compacted", role="user", content="old archived text")
        db.archive_and_compact("compacted", [{"role": "assistant", "content": "Summary"}])
        db._conn.commit()

        default = json.loads(session_search(mode="read", session_id="compacted", db=db))
        assert not any("old archived text" in (m.get("content") or "") for m in default["messages"])

        archived = json.loads(session_search(
            mode="read", session_id="compacted", include_compacted=True, db=db))
        assert any("old archived text" in (m.get("content") or "") for m in archived["messages"])

    def test_inactive_search_finds_rewound_rows(self, db):
        rewound = self._session_with_rewound_row(db)
        hidden = json.loads(session_search(query="rewound row marker", db=db, current_session_id="other"))
        assert all(entry["match_message_id"] != rewound for entry in hidden["results"])

        visible = json.loads(session_search(
            query="rewound row marker", include_inactive=True, db=db, current_session_id="other"))
        assert any(entry["match_message_id"] == rewound for entry in visible["results"])


class TestStatsMode:
    def test_stats_reports_counts_and_distributions(self, db):
        _seed(db)
        result = json.loads(session_search(mode="stats", db=db))

        assert result["success"] is True
        assert result["mode"] == "stats"
        assert result["messages"] == 3
        assert result["sessions"] == 2
        assert result["roles"] == {"user": 2, "assistant": 1}
        assert result["sources"] == {"cli": 1, "cron": 1}
        assert result["oldest_message_at"] != "unknown"
        assert result["newest_message_at"] != "unknown"

    def test_stats_rejects_a_db_without_a_file(self):
        class _NoPath:
            pass

        result = json.loads(session_search(mode="stats", db=_NoPath()))
        assert result["success"] is False


class TestExplicitModes:
    def test_invalid_mode_is_rejected(self, db):
        result = json.loads(session_search(mode="telepathy", db=db))
        assert result["success"] is False

    def test_inline_executor_forwards_new_args(self, db):
        """The sequential-tool executor maps mode/match/source/inactive like the registry does."""
        from types import SimpleNamespace

        from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext

        _seed(db)
        agent = SimpleNamespace(_get_session_db_for_recall=lambda: db, session_id="inline")
        stats = json.loads(INLINE_TOOL_EXECUTORS["session_search"](
            agent, {"mode": "stats"}, InlineToolContext(effective_task_id="t")))
        assert stats["mode"] == "stats" and stats["messages"] == 3

        search = json.loads(INLINE_TOOL_EXECUTORS["session_search"](
            agent, {"query": "deploy", "match": "any", "source": "cron"},
            InlineToolContext(effective_task_id="t")))
        assert search["filters"]["match"] == "any" and search["filters"]["source"] == ["cron"]
        assert [entry["session_id"] for entry in search["results"]] == ["cron-alpha"]

    def test_read_mode_requires_session_id(self, db):
        result = json.loads(session_search(mode="read", db=db))
        assert result["success"] is False

    def test_scroll_mode_requires_anchor(self, db):
        result = json.loads(session_search(mode="scroll", session_id="x", db=db))
        assert result["success"] is False

    def test_search_mode_requires_query(self, db):
        result = json.loads(session_search(mode="search", db=db))
        assert result["success"] is False

    def test_browse_mode_with_a_query_browses(self, db):
        _seed(db)
        result = json.loads(session_search(mode="browse", query="deploy", db=db))
        assert result["mode"] == "browse"
