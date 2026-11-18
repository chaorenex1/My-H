"""The read-only Cypher allowlist rejects writes, comments, and batches."""

from __future__ import annotations

import pytest

from hermes_graph import QueryError
from hermes_graph.services.cypher_guard import validate_read_only


@pytest.mark.parametrize(
    "statement",
    [
        "MATCH (n:Memory) RETURN n.id AS id",
        "OPTIONAL MATCH (n:Memory) RETURN n",
        "UNWIND $rows AS row RETURN row",
        "WITH 1 AS x RETURN x",
        "RETURN 1 AS one",
        "MATCH (n:Memory) WHERE n.content = 'DELETE ME' RETURN n;",
    ],
)
def test_allowed_statements(statement):
    validate_read_only(statement)


@pytest.mark.parametrize(
    "keyword",
    [
        "CREATE", "MERGE", "SET", "DELETE", "DETACH", "CALL", "LOAD",
        "ATTACH", "COPY", "BEGIN", "COMMIT", "ROLLBACK", "CHECKPOINT",
        "DROP", "ALTER", "INSERT", "REMOVE", "FOREACH",
    ],
)
def test_write_keywords_rejected(keyword):
    with pytest.raises(QueryError):
        validate_read_only(f"MATCH (n:Memory) {keyword} n")


def test_comments_and_batches_rejected():
    with pytest.raises(QueryError):
        validate_read_only("MATCH (n:Memory) RETURN n // comment")
    with pytest.raises(QueryError):
        validate_read_only("MATCH (n:Memory) RETURN n /* comment */")
    with pytest.raises(QueryError):
        validate_read_only("MATCH (n:Memory) RETURN n; MATCH (m:Entity) RETURN m")
    with pytest.raises(QueryError):
        validate_read_only("")


def test_non_read_start_rejected():
    with pytest.raises(QueryError):
        validate_read_only("EXPLAIN MATCH (n:Memory) RETURN n")
    with pytest.raises(QueryError):
        validate_read_only("CALL show_tables() RETURN *")
