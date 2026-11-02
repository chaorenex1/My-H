"""Conservative read-only Cypher allowlist.

This is not a parser: it strips literals/comments, then checks the first
keyword and rejects any write/DDL keyword token. Ambiguity is rejected, never
guessed. Parameters (``$name``) are the only supported way to inject values.
"""

from __future__ import annotations

from ..exceptions import QueryError

_ALLOWED_FIRST = frozenset({"MATCH", "OPTIONAL", "UNWIND", "WITH", "RETURN"})

_FORBIDDEN = frozenset(
    {
        "ALTER",
        "ANALYZE",
        "ATTACH",
        "BEGIN",
        "CALL",
        "CHECKPOINT",
        "COMMENT",
        "COMMIT",
        "COPY",
        "CREATE",
        "DELETE",
        "DETACH",
        "DROP",
        "EXPORT",
        "FOREACH",
        "GRANT",
        "IMPORT",
        "INSERT",
        "INSTALL",
        "LOAD",
        "MERGE",
        "REMOVE",
        "REVOKE",
        "ROLLBACK",
        "SET",
        "UPDATE",
        "USE",
    }
)


def validate_read_only(statement: str) -> None:
    if not isinstance(statement, str) or not statement.strip():
        raise QueryError("Cypher statement is empty")
    scrubbed = _scrub(statement)
    tokens = _tokens(scrubbed)
    if not tokens:
        raise QueryError("Cypher statement has no executable tokens")
    first = tokens[0].upper()
    if first not in _ALLOWED_FIRST:
        raise QueryError(
            f"Cypher must start with one of {sorted(_ALLOWED_FIRST)}; got {first!r}"
        )
    for token in tokens[1:]:
        upper = token.upper()
        if upper in _FORBIDDEN:
            raise QueryError(f"write/DDL keyword {upper!r} is not allowed in read-only Cypher")
    if _has_multiple_statements(scrubbed):
        raise QueryError("multi-statement Cypher is not supported")


def _scrub(statement: str) -> str:
    out: list[str] = []
    index = 0
    length = len(statement)
    while index < length:
        char = statement[index]
        if char in {"'", '"', "`"}:
            index = _skip_quoted(statement, index, char, out)
            continue
        if statement.startswith("//", index) or statement.startswith("/*", index):
            raise QueryError("Cypher comments are not allowed")
        out.append(char)
        index += 1
    return "".join(out)


def _skip_quoted(statement: str, start: int, quote: str, out: list[str]) -> int:
    index = start + 1
    length = len(statement)
    while index < length:
        char = statement[index]
        if char == "\\" and quote != "`":
            index += 2
            continue
        if char == quote:
            if quote != "`" and index + 1 < length and statement[index + 1] == quote:
                index += 2
                continue
            out.append(" ")
            return index + 1
        index += 1
    raise QueryError("unterminated string or quoted identifier in Cypher")


def _tokens(scrubbed: str) -> list[str]:
    tokens: list[str] = []
    current: list[str] = []
    for char in scrubbed:
        if char.isalnum() or char == "_":
            current.append(char)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return tokens


def _has_multiple_statements(scrubbed: str) -> bool:
    stripped = scrubbed.strip()
    if ";" not in stripped:
        return False
    semicolon = stripped.find(";")
    return bool(stripped[semicolon + 1 :].strip())
