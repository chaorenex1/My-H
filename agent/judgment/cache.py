"""Native-answer cache plus run audit in one DELETE-journal SQLite database.

Only native answers are cached: text judgments are one-hot, and ``call_llm`` has
its own provider fallback, so caching one chat model's guess would freeze it.
The session ledger stays the only billing source; ``runs`` serves the CLI and
the P5 dashboard panel and stores hashes/counts, never state or answer bodies.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from hermes_constants import get_hermes_home, hermes_home_key

from agent.judgment.types import (
    Answer,
    BoxQuestion,
    JudgmentImage,
    JudgmentValidationError,
    Question,
    answer_from_json,
    answer_to_json,
    image_bytes,
    question_type,
)
from agent.usage_pricing import CanonicalUsage

logger = logging.getLogger(__name__)

SCHEMA = """
PRAGMA journal_mode=DELETE;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS states (
  id         TEXT PRIMARY KEY,
  state      TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS answers (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  state       TEXT NOT NULL REFERENCES states(id) ON DELETE CASCADE,
  model       TEXT NOT NULL,
  name        TEXT NOT NULL,
  type        TEXT NOT NULL,
  instruction TEXT NOT NULL,
  criteria    TEXT NOT NULL,
  answer      TEXT NOT NULL,
  created_at  INTEGER NOT NULL,
  UNIQUE (state, model, name, type, instruction, criteria)
);
CREATE INDEX IF NOT EXISTS idx_answers_state ON answers(state);

CREATE TABLE IF NOT EXISTS runs (
  id            TEXT PRIMARY KEY,
  created_at    INTEGER NOT NULL,
  session_id    TEXT NOT NULL,
  execution_id  TEXT NOT NULL,
  request_id    TEXT NOT NULL,
  purpose       TEXT NOT NULL,
  mode          TEXT NOT NULL,
  api           TEXT NOT NULL,
  provider      TEXT NOT NULL,
  model         TEXT NOT NULL,
  questions     INTEGER NOT NULL,
  cached        INTEGER NOT NULL,
  input_tokens  INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  cost_usd      REAL NOT NULL,
  latency_ms    INTEGER NOT NULL,
  status        TEXT NOT NULL,
  error         TEXT NOT NULL,
  state_sha     TEXT NOT NULL,
  answers_sha   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at);
CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_runs_purpose ON runs(purpose, created_at);

-- ADR-17 D5: one row per billed native HTTP attempt. Text/local judgments bill the
-- session ledger only; cache hits write no row.
CREATE TABLE IF NOT EXISTS usage (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at         INTEGER NOT NULL,
  api                TEXT NOT NULL,
  provider           TEXT NOT NULL,
  model              TEXT NOT NULL,
  input_tokens       INTEGER NOT NULL,
  output_tokens      INTEGER NOT NULL,
  cache_read_tokens  INTEGER NOT NULL,
  cache_write_tokens INTEGER NOT NULL,
  reasoning_tokens   INTEGER NOT NULL,
  cost_usd           REAL
);
CREATE INDEX IF NOT EXISTS idx_usage_created ON usage(created_at);
CREATE INDEX IF NOT EXISTS idx_usage_model ON usage(provider, model, created_at);
"""


def judgment_home() -> Path:
    """``$HERMES_HOME/judgment`` for the active profile."""
    return get_hermes_home() / "judgment"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def state_identity(
    state: Any, images: Sequence[JudgmentImage] = (), *,
    ignores_image_detail: bool = False,
) -> tuple[str, str]:
    """``(cache state id, canonical state)``.

    Without images the id is ``sha256(canonical state)``. With images it is the
    ``multimodal-judgment-cache-v1:`` namespace over the state plus each image's
    effective MIME, decoded-bytes digest, and detail, so padded/unpadded or
    re-encoded base64 of the same bytes share one identity. A wire that drops
    detail (TypeSafe) must also drop it here, or the same request would miss.
    """
    if not images:
        canonical = canonical_json(state)
        return sha256_text(canonical), canonical
    canonical = canonical_json({
        "state": state,
        "images": [
            {
                "mime_type": image.mime_type.strip().lower(),
                "digest": hashlib.sha256(image_bytes(image)).hexdigest(),
                "detail": None if ignores_image_detail else image.detail,
            }
            for image in images
        ],
    })
    return f"multimodal-judgment-cache-v1:{sha256_text(canonical)}", canonical


def _criteria_key(question: Question) -> str:
    if isinstance(question, BoxQuestion):
        # Two box questions can share instructions and differ only by target image.
        return canonical_json({"image": question.image})
    criteria = getattr(question, "criteria", None)
    if isinstance(criteria, Mapping):
        criteria = dict(criteria)
    elif isinstance(criteria, tuple):
        criteria = list(criteria)
    return canonical_json(None if criteria is None else criteria)


class JudgmentCache:
    """Process-wide-on-demand cache/audit store. Every method is best-effort."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else judgment_home() / "judgment.db"
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None
        self._available = True
        self._disabled_reason = ""
        self._open()

    @property
    def available(self) -> bool:
        return self._available and self._db is not None

    @property
    def disabled_reason(self) -> str:
        return self._disabled_reason

    def _open(self) -> None:
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(str(self.db_path), check_same_thread=False)
            db.executescript(SCHEMA)
            self._db = db
        except (OSError, sqlite3.Error) as exc:
            self._disable(f"cannot open {self.db_path}: {exc}")

    def _disable(self, reason: str) -> None:
        if self._available:
            logger.warning("judgment store unavailable; continuing without it: %s", reason)
        self._available = False
        self._disabled_reason = reason
        db, self._db = self._db, None
        if db is not None:
            try:
                db.close()
            except sqlite3.Error:
                logger.debug("judgment store close failed", exc_info=True)

    def close(self) -> None:
        with self._lock:
            db, self._db = self._db, None
            if db is not None:
                try:
                    db.close()
                except sqlite3.Error:
                    logger.debug("judgment store close failed", exc_info=True)

    # ── answers ────────────────────────────────────────────────────────────

    def lookup(self, model_key: str, state: Any, questions: Mapping[str, Question],
               images: Sequence[JudgmentImage] = (), *,
               ignores_image_detail: bool = False) -> dict[str, Answer]:
        if not self.available:
            return {}
        state_id, _canonical = state_identity(
            state, images, ignores_image_detail=ignores_image_detail)
        found: dict[str, Answer] = {}
        try:
            with self._lock:
                db = self._db
                if db is None:
                    return {}
                for question_id, question in questions.items():
                    row = db.execute(
                        "SELECT answer FROM answers WHERE state=? AND model=? AND name=? "
                        "AND type=? AND instruction=? AND criteria=?",
                        (state_id, model_key, question_id, question_type(question),
                         question.instructions, _criteria_key(question)),
                    ).fetchone()
                    if row is None:
                        continue
                    try:
                        found[question_id] = answer_from_json(json.loads(row[0]))
                    except (ValueError, KeyError, TypeError, JudgmentValidationError):
                        logger.debug("ignoring malformed cached answer for %s/%s", model_key,
                                     question_id, exc_info=True)
        except sqlite3.Error as exc:
            self._disable(f"cache lookup failed: {exc}")
            return {}
        return found

    def record(self, model_key: str, state: Any, questions: Mapping[str, Question],
               answers: Mapping[str, Answer], *, max_states: int = 50_000,
               images: Sequence[JudgmentImage] = (),
               ignores_image_detail: bool = False) -> None:
        if not self.available or not answers:
            return
        state_id, canonical = state_identity(
            state, images, ignores_image_detail=ignores_image_detail)
        now = int(time.time())
        try:
            with self._lock:
                db = self._db
                if db is None:
                    return
                with db:
                    db.execute(
                        "INSERT OR IGNORE INTO states (id, state, created_at) VALUES (?, ?, ?)",
                        (state_id, canonical, now),
                    )
                    for question_id, answer in answers.items():
                        question = questions[question_id]
                        db.execute(
                            "INSERT INTO answers "
                            "(state, model, name, type, instruction, criteria, answer, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                            "ON CONFLICT (state, model, name, type, instruction, criteria) "
                            "DO UPDATE SET answer=excluded.answer, created_at=excluded.created_at",
                            (state_id, model_key, question_id, question_type(question),
                             question.instructions, _criteria_key(question),
                             canonical_json(answer_to_json(answer)), now),
                        )
                    count = db.execute("SELECT COUNT(*) FROM states").fetchone()[0]
                    if count > max_states:
                        db.execute(
                            "DELETE FROM states WHERE id IN ("
                            "SELECT id FROM states ORDER BY created_at ASC, rowid ASC LIMIT ?)",
                            (count - max_states,),
                        )
        except sqlite3.Error as exc:
            logger.warning("judgment cache write failed: %s", exc)

    # ── native usage ───────────────────────────────────────────────────────

    def record_usage(
        self, *, api: str, provider: str, model: str, usage: CanonicalUsage,
        cost_usd: float | None = None,
    ) -> None:
        """One row per billed native HTTP attempt; cache hits and text/local never write."""
        if not self.available:
            return
        try:
            with self._lock:
                db = self._db
                if db is None:
                    return
                with db:
                    db.execute(
                        "INSERT INTO usage (created_at, api, provider, model, input_tokens, "
                        " output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens, "
                        " cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            int(time.time()), str(api or ""), str(provider or ""),
                            str(model or ""), int(usage.input_tokens),
                            int(usage.output_tokens), int(usage.cache_read_tokens),
                            int(usage.cache_write_tokens), int(usage.reasoning_tokens),
                            None if cost_usd is None else float(cost_usd),
                        ),
                    )
        except sqlite3.Error as exc:
            logger.warning("judgment usage write failed: %s", exc)

    def recent_usage(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Newest billed native attempts first (P5 panel/doctor read this)."""
        if not self.available:
            return []
        try:
            with self._lock:
                db = self._db
                if db is None:
                    return []
                rows = db.execute(
                    "SELECT created_at, api, provider, model, input_tokens, output_tokens, "
                    "cache_read_tokens, cache_write_tokens, reasoning_tokens, cost_usd "
                    "FROM usage ORDER BY created_at DESC, id DESC LIMIT ?",
                    (max(1, int(limit)),),
                ).fetchall()
        except sqlite3.Error:
            return []
        keys = ("created_at", "api", "provider", "model", "input_tokens", "output_tokens",
                "cache_read_tokens", "cache_write_tokens", "reasoning_tokens", "cost_usd")
        return [dict(zip(keys, row)) for row in rows]

    def usage_count(self) -> int:
        if not self.available:
            return 0
        try:
            with self._lock:
                db = self._db
                if db is None:
                    return 0
                return int(db.execute("SELECT COUNT(*) FROM usage").fetchone()[0])
        except sqlite3.Error:
            return 0

    # ── run audit ──────────────────────────────────────────────────────────

    def record_run(self, run: Mapping[str, Any], *, max_runs: int = 20_000) -> None:
        if not self.available or not run:
            return
        try:
            with self._lock:
                db = self._db
                if db is None:
                    return
                with db:
                    db.execute(
                        "INSERT OR REPLACE INTO runs "
                        "(id, created_at, session_id, execution_id, request_id, purpose, mode, api, "
                        " provider, model, questions, cached, input_tokens, output_tokens, cost_usd, "
                        " latency_ms, status, error, state_sha, answers_sha) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            str(run.get("id") or ""), int(run.get("created_at") or time.time()),
                            str(run.get("session_id") or ""), str(run.get("execution_id") or ""),
                            str(run.get("request_id") or ""), str(run.get("purpose") or ""),
                            str(run.get("mode") or ""), str(run.get("api") or ""),
                            str(run.get("provider") or ""), str(run.get("model") or ""),
                            int(run.get("questions") or 0), int(run.get("cached") or 0),
                            int(run.get("input_tokens") or 0), int(run.get("output_tokens") or 0),
                            float(run.get("cost_usd") or 0.0), int(run.get("latency_ms") or 0),
                            str(run.get("status") or ""), str(run.get("error") or "")[:1024],
                            str(run.get("state_sha") or ""), str(run.get("answers_sha") or ""),
                        ),
                    )
                    count = db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
                    if count > max_runs:
                        db.execute(
                            "DELETE FROM runs WHERE id IN ("
                            "SELECT id FROM runs ORDER BY created_at ASC, rowid ASC LIMIT ?)",
                            (count - max_runs,),
                        )
        except sqlite3.Error as exc:
            logger.warning("judgment run audit write failed: %s", exc)

    def recent_runs(self, *, limit: int = 50, purpose: str = "", status: str = "",
                    session_id: str = "", execution_id: str = "") -> list[dict[str, Any]]:
        if not self.available:
            return []
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (("purpose", purpose), ("status", status),
                              ("session_id", session_id), ("execution_id", execution_id)):
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        params.append(max(1, int(limit)))
        try:
            with self._lock:
                db = self._db
                if db is None:
                    return []
                rows = db.execute(
                    "SELECT created_at, session_id, execution_id, request_id, purpose, mode, api, "
                    "provider, model, questions, cached, input_tokens, output_tokens, cost_usd, "
                    "latency_ms, status, error, state_sha, answers_sha FROM runs "
                    f"{where}ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    params,
                ).fetchall()
        except sqlite3.Error as exc:
            self._disable(f"run query failed: {exc}")
            return []
        keys = ("created_at", "session_id", "execution_id", "request_id", "purpose", "mode",
                "api", "provider", "model", "questions", "cached", "input_tokens",
                "output_tokens", "cost_usd", "latency_ms", "status", "error",
                "state_sha", "answers_sha")
        return [dict(zip(keys, row)) for row in rows]

    def run_count(self) -> int:
        if not self.available:
            return 0
        try:
            with self._lock:
                db = self._db
                if db is None:
                    return 0
                return int(db.execute("SELECT COUNT(*) FROM runs").fetchone()[0])
        except sqlite3.Error:
            return 0


_shared: dict[str, JudgmentCache | None] = {}
_shared_lock = threading.Lock()


def shared_cache() -> JudgmentCache | None:
    """Open each profile's store once per process; ``None`` when it cannot open.

    A multiplexed gateway serves many profiles from one process, so the singleton is
    keyed by ``hermes_home_key()`` rather than a module-wide connection (root HX002).
    """
    home = get_hermes_home()
    key = hermes_home_key(home)
    with _shared_lock:
        if key in _shared:
            return _shared[key]
        try:
            cache = JudgmentCache(home / "judgment" / "judgment.db")
            _shared[key] = cache if cache.available else None
            return _shared[key]
        except Exception as exc:  # never let the store break a judgment
            logger.warning("judgment store unavailable: %s", exc, exc_info=True)
            _shared[key] = None
            return None


def reset_shared_cache() -> None:
    """Test hook: forget every profile's process cache (closing them)."""
    with _shared_lock:
        caches = list(_shared.values())
        _shared.clear()
    for cache in caches:
        if isinstance(cache, JudgmentCache):
            cache.close()
