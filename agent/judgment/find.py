"""``judgrg``: judgment-routed, read-only search over a workspace (ADR-11 D14).

One restricted choice question decides which strategy answers the query, then the
engine runs an external matcher:

* ``files``   — ``rg --files``
* ``path``    — ``rg --files -g``
* ``content`` — ``rg`` (JSON matches)
* ``ast``     — ``ast-grep`` (structural/code patterns)

The caller cannot pass questions or arbitrary state, so the judgment engine never
becomes a general classifier. Everything here is read-only: no writes, no git, no
credentials/binaries in the results, and every subprocess is wall-clock bounded.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from agent.judgment.chain import has_native_judge, resolve_judge
from agent.judgment.config import load_judgment_settings
from agent.judgment.decision import IntentPlan, load_decision
from agent.judgment.types import (
    ChoiceAnswer,
    ChoiceQuestion,
    Judge,
    JudgmentAborted,
    JudgmentError,
    JudgmentRequest,
    JudgmentState,
    JudgmentUnavailable,
)
from agent.redact import redact_sensitive_text

logger = logging.getLogger(__name__)

RG_TIMEOUT_S = 20.0
AST_GREP_TIMEOUT_S = 20.0
MAX_PER_FILE = 20
MAX_RANGES = 20
SNIPPET_CHARS = 200

TASK = (
    "Read-only search over a source tree: choose which search strategy answers the "
    "plain-language query in the state."
)

_IGNORED_DIRS = frozenset({
    ".git", ".hg", ".svn", ".bzr", "node_modules", "__pycache__", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".cache", ".tox", ".nox",
    ".idea", ".vscode", ".next", ".nuxt", ".turbo", ".parcel-cache",
})
_CREDENTIAL_NAMES = frozenset({
    ".env", ".netrc", ".npmrc", ".pypirc", ".git-credentials", "id_rsa", "id_dsa",
    "id_ecdsa", "id_ed25519", "credentials", "secrets",
})
_CREDENTIAL_PATTERNS = (
    ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.jks",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", "credentials*.json", "secrets.*",
)
_STOPWORDS = frozenset("""
a an and are as at be been being between but by can could did do does for from had has have
how if in into is it its just like made make makes more most new not of off on one only or
other our out over per run runs should so some such than that the their them then there these
they thing things this through to two up use used user using via was way we were what when
where which will with within without would you your
""".split())

_STRUCTURAL_RE = re.compile(r"\$[A-Za-z_]\w*|\$\$\$|=>|->|::|[(){}\[\];]")


class FindBackendUnavailable(RuntimeError):
    """The chosen strategy's executable is missing or failed to run."""


# ── query → terms, globs, patterns ──────────────────────────────────────────


def _split_quoted(query: str) -> tuple[str, list[str]]:
    """Quoted phrases whole, plus the text outside the quotes."""
    phrases: list[str] = []
    rest: list[str] = []
    chars = list(query)
    index = 0
    while index < len(chars):
        char = chars[index]
        if char not in "\"'":
            rest.append(char)
            index += 1
            continue
        index += 1
        phrase = ""
        closed = False
        while index < len(chars):
            if chars[index] == char:
                closed = True
                index += 1
                break
            phrase += chars[index]
            index += 1
        phrase = phrase.strip()
        if closed and len(phrase) >= 2:
            phrases.append(phrase)
            rest.append(" ")
        else:
            rest.append(f"{phrase} ")
    return "".join(rest), phrases


def search_terms(query: str) -> list[str]:
    """Quoted phrases whole, then word tokens minus stopwords; case preserved."""
    rest, terms = _split_quoted(query)
    seen = {term.lower() for term in terms}
    for token in re.split(r"[^\w]+", rest, flags=re.UNICODE):
        lower = token.lower()
        if len(token) < 2 or token.isdigit() or lower in _STOPWORDS or lower in seen:
            continue
        seen.add(lower)
        terms.append(token)
    return terms


EXPAND_TERMS_LIMIT = 5
_EXPAND_PROMPT = (
    "Expand this code-search request into additional search terms. Reply with ONLY a "
    "JSON array of at most five short literal terms (identifiers, words, or quoted "
    "phrases) that are likely to appear in the code the request describes. "
    "No commentary."
)


def _parse_expanded_terms(text: str) -> list[str]:
    """Terms from a JSON array, else bullet/line output; validated and deduped."""
    terms: list[str] = []
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, list):
        terms = [str(item) for item in payload if isinstance(item, str)]
    else:
        for line in str(text or "").splitlines():
            term = line.strip().strip("-*•`\"', ")
            if term and len(term.split()) <= 4:
                terms.append(term)
    out: list[str] = []
    seen: set[str] = set()
    for term in terms:
        term = term.strip()
        key = term.lower()
        if not term or len(term) > 64 or key in seen:
            continue
        seen.add(key)
        out.append(term)
        if len(out) >= EXPAND_TERMS_LIMIT:
            break
    return out


def _aux_terms(query: str) -> list[str]:
    """One aux-LLM call for extra literal search terms (ADR-18 D9)."""
    from agent.auxiliary_client import call_llm
    from agent.judgment.chat import response_text

    response = call_llm(
        task="judge",
        messages=[{"role": "system", "content": _EXPAND_PROMPT},
                  {"role": "user", "content": str(query)}],
        temperature=0.0, max_tokens=80, timeout=4.0, fallback=False)
    return _parse_expanded_terms(response_text(response))


def _expand_terms(query: str) -> list[str]:
    """Extra terms, or ``[]`` on any failure so callers keep ``search_terms()``."""
    try:
        return _aux_terms(query)[:EXPAND_TERMS_LIMIT]
    except Exception as exc:  # health: allow BLE001 -- lexical terms are the fallback
        logger.debug("judgrg term expansion failed (%s); using lexical terms", exc)
        return []


def _query_terms(query: str) -> list[str]:
    """Original terms first (room reserved for two expansions), capped at five."""
    originals = search_terms(query)
    merged: list[str] = []
    seen: set[str] = set()
    for term in [*originals[:3], *_expand_terms(query), *originals[3:]]:
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append(term)
        if len(merged) >= EXPAND_TERMS_LIMIT:
            break
    return merged


def _glob_term(term: str) -> str:
    """Drop glob metacharacters: a backslash escape is a path separator on Windows."""
    return re.sub(r"[{}*?\[\],!\\:/]", "", term)


def path_glob(query: str, terms: Sequence[str]) -> str | None:
    """A name glob for ``rg --files -g``: the query's own glob, else ``*{terms}*``."""
    raw = query.strip()
    if raw and " " not in raw and any(char in raw for char in "*?["):
        return raw
    if not terms:
        return None
    if len(terms) == 1:
        return f"**/*{_glob_term(terms[0])}*"
    return "**/*{" + ",".join(_glob_term(term) for term in terms) + "}*"


def content_pattern(terms: Sequence[str]) -> str | None:
    """One case-insensitive alternation over the literal query terms."""
    ordered: list[str] = sorted(terms, key=len, reverse=True)
    if not ordered:
        return None
    return "|".join(re.escape(term) for term in ordered)


def looks_structural(text: str) -> bool:
    """True for a code-shaped query (``foo($X)``, ``Path.cwd()``), not prose."""
    body = str(text or "").strip()
    if not body:
        return False
    if "$" in body:
        return True
    return not any(char.isspace() for char in body) and bool(_STRUCTURAL_RE.search(body))


# ── scope and safety ────────────────────────────────────────────────────────


def _display_path(path: Path) -> str:
    try:
        return Path(os.path.relpath(path, Path.cwd())).as_posix()
    except ValueError:  # different drive on Windows
        return path.as_posix()


def _scope(raw: str | None) -> Path:
    path = Path(str(raw)).expanduser() if raw else Path.cwd()
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError(f"path not found: {raw or path}") from None


def _check_glob(pattern: str) -> None:
    if pattern.count("[") != pattern.count("]") or pattern.count("{") != pattern.count("}"):
        raise ValueError(f"invalid glob pattern: {pattern}")


def validate_scope(path: str | None, glob_pattern: str | None = None) -> None:
    """Reject a missing path or malformed glob before a judge is resolved."""
    _scope(path)
    if glob_pattern:
        _check_glob(glob_pattern)


def _credentials_file(name: str) -> bool:
    lower = name.lower()
    if lower in _CREDENTIAL_NAMES:
        return True
    return any(fnmatch.fnmatchcase(lower, pattern) for pattern in _CREDENTIAL_PATTERNS)


def _binary_name(name: str) -> bool:
    from tools.binary_extensions import has_binary_extension

    return has_binary_extension(name)


def _dir_exclusions() -> list[str]:
    return [f"!{name}/**" for name in sorted(_IGNORED_DIRS)]


def _credential_globs() -> list[str]:
    """Case-insensitive globs: a tracked ``.ENV``/``ID_RSA`` is still a credential file."""
    return ["!.env", *(f"!{pattern}" for pattern in _CREDENTIAL_PATTERNS)]


# ── judging ─────────────────────────────────────────────────────────────────


class _RunStats:
    """Counters for one judgrg call: one intent request, then the matcher."""

    def __init__(self) -> None:
        self.requests = 0
        self.failed = 0
        self.uncalibrated = False

    def record(self, *, kind: str = "", failed: bool = False) -> None:
        self.requests += 1
        if failed:
            self.failed += 1
        if kind and kind != "native":
            self.uncalibrated = True

    def snapshot(self) -> dict[str, int]:
        return {"requests": self.requests, "failed": self.failed}


def _ask(judge: Judge, request: JudgmentRequest, *, purpose: str,
         stats: _RunStats) -> tuple[Any, str]:
    try:
        result = judge.judge(request, purpose=purpose)
    except JudgmentAborted:
        raise
    except (JudgmentUnavailable, JudgmentError):
        stats.record(failed=True)
        return None, ""
    api = str(getattr(result, "api", ""))
    stats.record(kind=str(getattr(result, "kind", "")))
    return result, api


def _intent_request(query: str) -> JudgmentRequest:
    """One restricted choice question whose criteria come from the packaged table."""
    table = load_decision()
    state: JudgmentState = {"task": TASK, "query": query}
    question = ChoiceQuestion(table.instructions, table.criteria)
    return JudgmentRequest(state=state, questions={"intent": question})


def _guess_intent(query: str) -> str:
    """Deterministic intent when the judge answers with an unusable choice."""
    return "pattern" if looks_structural(query) else "unknown_implementation"


def choose_intent(judge: Judge, query: str, stats: _RunStats) -> str | None:
    """One restricted choice question; ``None`` when the judge could not answer."""
    result, _api = _ask(judge, _intent_request(query), purpose="find", stats=stats)
    if result is None:
        return None
    answer = result.answers.get("intent")
    labels = load_decision().criteria
    if isinstance(answer, ChoiceAnswer) and answer.choice in labels:
        return answer.choice
    return _guess_intent(query)


# ── planning ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Plan:
    mode: str
    expression: str | None = None
    fallback: str = ""
    ast_pattern: str | None = None


def _single_identifier(terms: Sequence[str]) -> str | None:
    """The one identifier-like term a natural-language query named, if any."""
    if len(terms) != 1:
        return None
    term = terms[0].strip()
    return term if re.fullmatch(r"[A-Za-z_]\w*", term) else None


def plan_search(intent: str, query: str, scope: Path,
                glob_pattern: str | None,
                terms: Sequence[str] | None = None) -> _Plan:
    """Map the judged intent through the JSON table to a runnable plan."""
    entry: IntentPlan = load_decision().plan_for(intent)
    terms = list(terms) if terms is not None else search_terms(query)
    fallback = ""
    mode = entry.mode
    ast_pattern = query.strip() if looks_structural(query) else None
    if mode == "ast" and ast_pattern is None:
        term = _single_identifier(terms)
        if intent == "call" and term:
            ast_pattern = f"{term}($$$)"  # portable call template
        else:
            fallback = "the query is not a code pattern; searched contents instead"
            mode = "content"
    if mode == "ast" and not resolve_ast_grep():
        fallback = "ast-grep is not installed (hermes pm install ast-grep)"
        mode = "content"
        ast_pattern = None
    if mode == "ast":
        return _Plan("ast", ast_pattern, fallback)
    if mode == "hybrid":
        ast_pattern = ast_pattern or _single_identifier(terms)
        if ast_pattern is None:
            fallback = fallback or "no code-shaped pattern in the query; searched contents only"
        elif not resolve_ast_grep():
            fallback = "ast-grep is not installed; searched contents only"
            ast_pattern = None
    if mode in ("content", "hybrid"):
        raw_glob = path_glob(query, ())
        if raw_glob and mode == "content":
            return _Plan("path", raw_glob, fallback)
        expression = content_pattern(terms)
        if expression:
            return _Plan(mode, expression, fallback, ast_pattern)
        mode = "path" if path_glob(query, terms) else "files"
        fallback = fallback or "the query has no searchable terms"
    if mode == "path":
        expression = path_glob(query, terms)
        if expression:
            return _Plan("path", expression, fallback)
        fallback = fallback or "no name pattern could be derived from the query"
    return _Plan("files", None, fallback)


# ── execution ───────────────────────────────────────────────────────────────


def _resolve_binary(name: str, package: str) -> str | None:
    """The PM-managed executable, else one on PATH; never installs anything."""
    env_path = None
    try:
        from pm.install import env_for

        env = env_for(package)
        env_path = env.get("PATH") if isinstance(env, dict) else None
    except Exception:  # health: allow BLE001 -- optional backend must not fail a find
        logger.debug("judgrg: PM lookup failed for %s", name, exc_info=True)
    try:
        from hermes_platform.resolver import LookupContext, locate_command
    except Exception:  # health: allow BLE001 -- imported lazily for test isolation
        return None
    try:
        resolution = locate_command(
            name, LookupContext(path=env_path) if env_path else None)
    except Exception:  # health: allow BLE001 -- optional backend must not fail a find
        logger.debug("judgrg: %s resolution failed", name, exc_info=True)
        return None
    return resolution.command[0] if resolution.command else None


def resolve_rg() -> str | None:
    return _resolve_binary("rg", "ripgrep")


def resolve_ast_grep() -> str | None:
    return _resolve_binary("ast-grep", "ast-grep")


@dataclass(frozen=True)
class _Output:
    stdout: str
    stderr: str
    code: int
    timed_out: bool = False


def _run(argv: Sequence[str], *, cwd: Path, timeout: float) -> _Output:
    try:
        completed = subprocess.run(
            list(argv), cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, check=False)
    except FileNotFoundError:
        raise FindBackendUnavailable(f"{argv[0]} not found") from None
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        return _Output(stdout or "", "", 124, True)
    except OSError as exc:
        raise FindBackendUnavailable(f"{argv[0]} failed to start: {exc}") from None
    return _Output(completed.stdout or "", completed.stderr or "", completed.returncode)


def _root(scope: Path) -> tuple[Path, str]:
    return (scope.parent, scope.name) if scope.is_file() else (scope, ".")


def _hit_path(scope: Path, rel: str) -> str:
    if scope.is_file():
        return _display_path(scope)
    return _display_path(scope / rel.removeprefix("./"))


def _snippet_breakdown(text: str) -> str:
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return redact_sensitive_text(stripped[:SNIPPET_CHARS], file_read=True)
    return ""


def _merge_ranges(ranges: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    kept = sorted((dict(item) for item in ranges), key=lambda item: (item["start"], item["end"]))
    merged: list[dict[str, Any]] = []
    for item in kept:
        last = merged[-1] if merged else None
        if last is not None and item["start"] <= last["end"] + 1:
            last["end"] = max(last["end"], item["end"])
            continue
        merged.append(item)
    return merged


def _merge_hit_sets(*sets: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Union of rg and ast-grep hits, merged per path and sorted by path."""
    merged: dict[str, list[dict[str, Any]]] = {}
    for hits in sets:
        for hit in hits:
            merged.setdefault(str(hit["path"]), []).extend(hit.get("ranges", ()))
    out: list[dict[str, Any]] = []
    for path, ranges in merged.items():
        item: dict[str, Any] = {"path": path}
        if ranges:
            item["ranges"] = _merge_ranges(ranges)[:MAX_RANGES]
        out.append(item)
    out.sort(key=lambda hit: hit["path"])
    return out


def _combine_outputs(primary: _Output, secondary: _Output) -> _Output:
    code = primary.code if primary.code not in (0, 1) else secondary.code
    return _Output(
        primary.stdout, primary.stderr or secondary.stderr, code,
        primary.timed_out or secondary.timed_out)


def _file_hits(entries: Sequence[str], *, scope: Path,
               limit: int) -> tuple[list[dict[str, Any]], bool]:
    kept: list[str] = []
    seen: set[str] = set()
    for raw in entries:
        rel = raw.strip()
        if not rel:
            continue
        name = Path(rel).name
        if _credentials_file(name) or _binary_name(name):
            continue
        # One match per real path: rg may list the same file under two roots.
        key = _hit_path(scope, rel)
        if key in seen:
            continue
        seen.add(key)
        kept.append(key)
    return [{"path": path} for path in kept[:limit]], len(kept) > limit


def _search_files(rg: str, scope: Path, *, glob_pattern: str | None,
                  ignore_case: bool = False,
                  hidden: bool, limit: int) -> tuple[list[dict[str, Any]], bool, _Output]:
    cwd, root = _root(scope)
    argv = [rg, "--files", "--color", "never"]
    if hidden:
        argv.append("--hidden")
    if glob_pattern:
        argv += ["--iglob" if ignore_case else "-g", glob_pattern]
    for exclude in _dir_exclusions():
        argv += ["-g", exclude]
    for exclude in _credential_globs():
        argv += ["--iglob", exclude]
    argv += ["--", root]
    output = _run(argv, cwd=cwd, timeout=RG_TIMEOUT_S)
    hits, truncated = _file_hits(
        output.stdout.splitlines(), scope=scope, limit=limit)
    return hits, truncated, output


def _json_matches(stdout: str) -> list[tuple[str, int, str]]:
    rows: list[tuple[str, int, str]] = []
    for line in stdout.splitlines():
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("type") != "match":
            continue
        data = record.get("data") or {}
        path = str((data.get("path") or {}).get("text") or "")
        number = data.get("line_number")
        text = str((data.get("lines") or {}).get("text") or "")
        if path and isinstance(number, int):
            rows.append((path, number, text))
    return rows


def _content_search(rg: str, scope: Path, *, expression: str,
                    glob_pattern: str | None, hidden: bool, limit: int,
                    context_lines: int) -> tuple[list[dict[str, Any]], bool, _Output]:
    cwd, root = _root(scope)
    argv = [rg, "--json", "--color", "never", "-i",
            "--max-count", str(MAX_PER_FILE)]
    if hidden:
        argv.append("--hidden")
    if glob_pattern:
        argv += ["-g", glob_pattern]
    for exclude in _dir_exclusions():
        argv += ["-g", exclude]
    for exclude in _credential_globs():
        argv += ["--iglob", exclude]
    argv += ["--", expression, root]
    output = _run(argv, cwd=cwd, timeout=RG_TIMEOUT_S)
    context = max(0, context_lines)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for rel, number, text in _json_matches(output.stdout):
        if _credentials_file(Path(rel).name):
            continue
        grouped.setdefault(rel, []).append({
            "start": max(1, number - context),
            "end": number + context,
            "snippet": _snippet_breakdown(text),
        })
    hits = [
        {"path": _hit_path(scope, rel), "ranges": _merge_ranges(ranges)[:MAX_RANGES]}
        for rel, ranges in grouped.items()
    ]
    hits.sort(key=lambda hit: hit["path"])
    return hits[:limit], len(hits) > limit, output


def _ast_matches(stdout: str) -> list[tuple[str, int, int, str]]:
    rows: list[tuple[str, int, int, str]] = []
    for line in stdout.splitlines():
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        path = str(record.get("file") or "")
        span = record.get("range") or {}
        start = (span.get("start") or {}).get("line")
        end = (span.get("end") or {}).get("line")
        if not path or not isinstance(start, int) or not isinstance(end, int):
            continue
        rows.append((path, start + 1, end + 1, str(record.get("text") or "")))
    return rows


def _ast_search(sg: str, scope: Path, *, pattern: str, lang: str | None,
                glob_pattern: str | None, limit: int,
                context_lines: int) -> tuple[list[dict[str, Any]], bool, _Output]:
    cwd, root = _root(scope)
    argv = [sg, "run", "--pattern", pattern, "--json=stream", "--color", "never"]
    if lang:
        argv += ["--lang", lang]
    if glob_pattern:
        argv += ["--globs", glob_pattern]
    for exclude in _dir_exclusions() + _credential_globs():
        argv += ["--globs", exclude]
    argv += ["--", root]
    output = _run(argv, cwd=cwd, timeout=AST_GREP_TIMEOUT_S)
    context = max(0, context_lines)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for rel, start, end, text in _ast_matches(output.stdout):
        if _credentials_file(Path(rel).name):
            continue
        grouped.setdefault(rel, []).append({
            "start": max(1, start - context),
            "end": end + context,
            "snippet": _snippet_breakdown(text),
        })
    hits = [
        {"path": _hit_path(scope, rel), "ranges": _merge_ranges(ranges)[:MAX_RANGES]}
        for rel, ranges in grouped.items()
    ]
    hits.sort(key=lambda hit: hit["path"])
    return hits[:limit], len(hits) > limit, output


def _execute(plan: _Plan, query: str, scope: Path, *, glob_pattern: str | None,
             hidden: bool, limit: int, context_lines: int
             ) -> tuple[list[dict[str, Any]], bool, _Output, list[str]]:
    if plan.mode == "ast":
        executable = resolve_ast_grep()
        if not executable:
            raise FindBackendUnavailable(
                "ast-grep is not installed; run `hermes pm install ast-grep` "
                "or use search_files")
        hits, truncated, output = _ast_search(
            executable, scope, pattern=plan.expression or query, lang=None,
            glob_pattern=glob_pattern, limit=limit, context_lines=context_lines)
        return hits, truncated, output, ["ast-grep"]
    executable = resolve_rg()
    if not executable:
        raise FindBackendUnavailable(
            "ripgrep (rg) is not installed; run `hermes pm install ripgrep` "
            "or use search_files")
    if plan.mode in ("content", "hybrid"):
        hits, truncated, output = _content_search(
            executable, scope, expression=plan.expression or query,
            glob_pattern=glob_pattern, hidden=hidden, limit=limit,
            context_lines=context_lines)
        tools = ["rg"]
        ast_executable = resolve_ast_grep() if plan.ast_pattern else None
        if plan.mode == "hybrid" and plan.ast_pattern and ast_executable:
            ast_hits, ast_truncated, ast_output = _ast_search(
                ast_executable, scope, pattern=plan.ast_pattern, lang=None,
                glob_pattern=glob_pattern, limit=limit, context_lines=context_lines)
            merged = _merge_hit_sets(hits, ast_hits)
            truncated = truncated or ast_truncated or len(merged) > limit
            return merged[:limit], truncated, _combine_outputs(output, ast_output), [
                *tools, "ast-grep"]
        return hits, truncated, output, tools
    # Only glob-shaped requests reach this fallback, so the query's own glob is
    # the include filter; credentials/binaries are dropped from the results.
    hits, truncated, output = _search_files(
        executable, scope, glob_pattern=plan.expression,
        ignore_case=True, hidden=hidden, limit=limit)
    return hits, truncated, output, ["rg"]


def judgrg(
    query: str,
    *,
    judge: Judge,
    path: str | None = None,
    glob_pattern: str | None = None,
    limit: int = 50,
    context_lines: int = 0,
    hidden: bool = False,
) -> dict[str, Any]:
    """Judge the query's intent, run the table's strategy, report honest coverage."""
    started = time.monotonic()
    validate_scope(path, glob_pattern)
    scope = _scope(path)
    stats = _RunStats()
    intent = choose_intent(judge, query, stats)
    if intent is None:
        return {
            "query": query,
            "intent": None,
            "mode": "files",
            "tools": [],
            "hits": [],
            "coverage": "unavailable",
            "stats": stats.snapshot(),
        }
    mode = load_decision().plan_for(intent).mode
    terms = _query_terms(query) if mode != "ast" else search_terms(query)
    plan = plan_search(intent, query, scope, glob_pattern, terms=terms)
    hits, truncated, output, tools = _execute(
        plan, query, scope, glob_pattern=glob_pattern, hidden=hidden,
        limit=limit, context_lines=context_lines)
    if output.timed_out:
        coverage = "partial"
    elif output.code not in (0, 1) and not hits:
        detail = output.stderr.strip() or f"exit {output.code}"
        raise FindBackendUnavailable(f"{plan.mode} search failed: {detail}")
    else:
        coverage = "complete" if output.code in (0, 1) else "partial"
    payload: dict[str, Any] = {
        "query": query,
        "intent": intent,
        "mode": plan.mode,
        "tools": tools,
        "hits": hits,
        "coverage": coverage,
        "stats": {
            **stats.snapshot(),
            "matches": len(hits),
            "truncated": truncated,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        },
    }
    if plan.fallback:
        payload["fallback"] = plan.fallback
    if stats.uncalibrated:
        payload["uncalibrated"] = True
    return payload


# ── tool gating ─────────────────────────────────────────────────────────────


def judgment_tools_available() -> bool:
    """``check_fn``: reachability only — reads config/credentials, never a network call."""
    try:
        settings = load_judgment_settings()
    except Exception:  # health: allow BLE001 -- a broken config hides the tool, never crashes a session
        return False
    if settings.tools_enabled == "off":
        return False
    if settings.tools_enabled == "on":
        return True
    try:
        return has_native_judge()
    except Exception:  # health: allow BLE001 -- credential probes are optional; unreachable hides the tool
        return False


def resolve_find_judge(purpose: str) -> Judge:
    """The chain for a find tool; refuses to spend a text model under ``auto``."""
    settings = load_judgment_settings()
    if settings.tools_enabled == "off":
        raise JudgmentUnavailable("judgment tools are off (judgment.tools.enabled=off)")
    if settings.tools_enabled == "auto" and not has_native_judge():
        raise JudgmentUnavailable(
            "no native judgment route is configured (judgment.tools.enabled=auto)")
    return resolve_judge(purpose=purpose)
