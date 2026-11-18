"""Judge-driven selective staging (ADR-17 D7 "git staging").

``hermes git stage "<instruction>"`` and the restricted ``git_stage`` model tool
score every unstaged unit — one hunk of a modified file, the head of a deleted
or untracked file, a whole binary file — against the instruction with the
bundled ``git-stage`` question file, verify the strongest candidates with one
``pick``, then stage only the accepted units.

Index only: an accepted hunk set is rebuilt as ``file header + hunk`` and piped
to ``git apply --cached --recount -``; an accepted whole file goes through
``git add``. No commit is created and the worktree is never written.

Unit enumeration reuses ``hermes_cli.web_git`` (``review_list`` / ``review_diff``)
so the CLI and the desktop review pane agree on what "changed" means.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Head-truncation bound for a unit's change text (omp ``UNIT_CHARS``).
UNIT_CHARS = 2_400
#: ``belongs`` level "part of it" probability at or above which a unit is staged.
STAGE_THRESHOLD = 0.6
#: Level index of "part of it" in the bundled ``belongs`` score question.
PART_OF_IT = "2"
#: Strongest candidates re-checked together by ``pick``.
VERIFY_CANDIDATES = 8
#: Per-candidate change text bound in the verification pick.
VERIFY_CHARS = 600
#: ``none`` probability at or above which the whole run stages nothing.
NONE_THRESHOLD = 0.5
#: Choice key for "the instruction describes none of the candidates".
NONE = "none"


@dataclass(frozen=True)
class Hunk:
    """One ``@@`` section of a single-file unified diff."""

    header: str
    body: tuple[str, ...]

    @property
    def raw(self) -> str:
        return "\n".join((self.header, *self.body))

    @property
    def change(self) -> str:
        """Only the +/- lines: unchanged context reads as part of the change."""
        return "\n".join(line for line in self.body if line[:1] in ("+", "-"))


@dataclass(frozen=True)
class ParsedDiff:
    """A single-file unified diff split into its file preamble and hunks."""

    header: str
    hunks: tuple[Hunk, ...]
    binary: bool
    new_file: bool
    deleted_file: bool


@dataclass(frozen=True)
class Unit:
    """One independently judged piece of the unstaged tree."""

    path: str
    #: ``hunk`` | ``deleted file`` | ``new file`` | ``binary``.
    kind: str
    #: 1-based hunk index within the file diff; absent for units staged whole.
    hunk: int | None = None
    #: Head-bounded +/- lines or the file head; absent for binaries.
    change: str | None = None
    #: File preamble of the unified diff (``diff --git`` … ``+++`` lines).
    header: str = ""
    #: The hunk (header line included) to replay for this unit.
    raw: str = ""


def parse_unified_diff(text: str) -> ParsedDiff:
    """Split one file's unified diff into its preamble and hunks (no third-party deps)."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    header: list[str] = []
    hunks: list[Hunk] = []
    for line in lines:
        if line.startswith("@@"):
            hunks.append(Hunk(line, ()))
            continue
        if hunks:
            last = hunks[-1]
            hunks[-1] = Hunk(last.header, (*last.body, line))
        else:
            header.append(line)
    return ParsedDiff(
        header="\n".join(header),
        hunks=tuple(hunks),
        binary=any(line.startswith("Binary files ") for line in header)
        or "GIT binary patch" in text,
        new_file=any(line.startswith("new file mode") for line in header),
        deleted_file=any(line.startswith("deleted file mode") for line in header),
    )


def bound(text: str, limit: int) -> str:
    """Head-truncate ``text`` to ``limit`` characters with an ellipsis marker."""
    return text if len(text) <= limit else f"{text[:limit]}\n…"


def judge_available() -> bool:
    """``check_fn``: the judgment-tool policy allows a judge here (off/auto/on), no network call."""
    try:
        from agent.judgment.find import judgment_tools_available

        return judgment_tools_available()
    except Exception:  # health: allow BLE001 -- a broken route must hide the tool, not crash
        return False


def stage(
    instruction: str,
    *,
    cwd: str | None = None,
    path: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Stage the unstaged changes ``instruction`` describes; return the JSON summary.

    Raises ``RuntimeError`` when there is nothing to filter, git fails, or any
    judgment fails — in which case nothing is staged.
    """
    from hermes_cli import web_git

    if not judge_available():
        raise RuntimeError(
            "judgment tools are unavailable (judgment.tools.enabled=off, or auto without a "
            "native route)")
    instruction = str(instruction or "").strip()
    if not instruction:
        raise RuntimeError("instruction must be a non-empty description of what to stage")
    root = str(cwd or os.getcwd())
    if web_git.repo_status(root) is None:
        raise RuntimeError(f"not a git repository: {root}")
    path_filter = _normalize_filter(path)

    units, files, skipped = _changed_units(root, path_filter)
    if not units:
        raise RuntimeError("no unstaged changes to filter")

    scores = _score_units(units, instruction, files)
    accepted = [score >= STAGE_THRESHOLD for score in scores]
    if any(accepted) and not _verify_candidates(units, scores, instruction):
        accepted = [False] * len(units)

    staged_units = [unit for unit, ok in zip(units, accepted) if ok]
    if staged_units and not dry_run:
        _apply_index(root, staged_units)
    elif staged_units:
        logger.debug("git stage: dry run, would stage %d units", len(staged_units))

    matched_paths = {unit.path for unit in staged_units}
    return {
        "staged": bool(staged_units),
        "whole_files": sum(1 for unit in staged_units if unit.hunk is None),
        "matched_files": len(matched_paths),
        "total_files": len(files),
        "matched_hunks": sum(1 for unit in staged_units if unit.hunk is not None),
        "total_hunks": sum(1 for unit in units if unit.hunk is not None),
        "skipped": skipped,
        "dry_run": bool(dry_run),
    }


# ── unit enumeration ────────────────────────────────────────────────────────


def _normalize_filter(path: str | None) -> str | None:
    if path is None:
        return None
    value = str(path).strip().replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return value.rstrip("/") or None


def _under(file_path: str, path_filter: str | None) -> bool:
    if not path_filter:
        return True
    return file_path == path_filter or file_path.startswith(f"{path_filter}/")


def _file_head(file_path: Path) -> tuple[str | None, bool]:
    """(head text or None, binary): mirrors omp's ``readHead``."""
    try:
        with file_path.open("rb") as handle:
            sample = handle.read(UNIT_CHARS * 4)
    except OSError:
        return None, False
    if b"\0" in sample:
        return None, True
    return bound(sample.decode("utf-8", errors="replace"), UNIT_CHARS), False


def _new_file_units(file_path: Path, path: str) -> list[Unit]:
    change, binary = _file_head(file_path)
    if binary:
        return [Unit(path=path, kind="binary")]
    return [Unit(path=path, kind="new file", change=change)]


def _changed_units(
    cwd: str, path_filter: str | None
) -> tuple[list[Unit], list[str], list[str]]:
    """(units, changed files, skipped conflicted paths) for the unstaged tree."""
    from hermes_cli import web_git

    units: list[Unit] = []
    files: list[str] = []
    skipped: list[str] = []
    listing = web_git.review_list(cwd, "worktree", None)
    for row in listing["files"]:
        path = str(row["path"])
        if not _under(path, path_filter):
            continue
        status = str(row.get("status") or "")
        if status == "U":
            skipped.append(path)
            continue
        if status == "?":
            files.append(path)
            units.extend(_new_file_units(Path(cwd) / path, path))
            continue
        parsed = parse_unified_diff(web_git.review_diff(cwd, path, "worktree", None, False))
        if parsed.new_file:
            # review_diff fabricates an all-add diff when the worktree has no
            # unstaged change (e.g. only the index was modified); a tracked file
            # is never genuinely new, so there is nothing to filter here.
            continue
        files.append(path)
        if parsed.binary:
            units.append(Unit(path=path, kind="binary"))
            continue
        kind = "deleted file" if parsed.deleted_file else "hunk"
        for index, hunk in enumerate(parsed.hunks, start=1):
            change = hunk.change
            if not change:
                continue
            units.append(Unit(
                path=path, kind=kind, hunk=index,
                change=bound(change, UNIT_CHARS),
                header=parsed.header, raw=hunk.raw))
    return units, files, skipped


# ── judgment ────────────────────────────────────────────────────────────────


def _score_units(units: list[Unit], instruction: str, all_changed_files: list[str]) -> list[float]:
    """``belongs`` for every unit in one capped wave; any failed item aborts the run."""
    from agent.judgment import (
        BatchItem,
        ScoreAnswer,
        ScoreQuestion,
        judge_batch,
        load_questions,
    )

    belongs = load_questions("git-stage")["belongs"]
    if not isinstance(belongs, ScoreQuestion):
        raise RuntimeError("git-stage question 'belongs' is not a score question")
    # judge_batch reloads from a source; hand it the already-rendered question's spec
    # so only `belongs` rides along (the verification `pick` is a separate request).
    spec = {"instructions": belongs.instructions, "criteria": list(belongs.criteria)}
    items = [
        BatchItem(key=index, state={
            "instruction": instruction,
            "all_changed_files": all_changed_files,
            "path": unit.path,
            "kind": unit.kind,
            **({"change": unit.change} if unit.change is not None else {}),
        })
        for index, unit in enumerate(units)
    ]
    batch = judge_batch(items, questions={"belongs": spec}, purpose="git-stage")
    scores = [0.0] * len(units)
    for result in batch.results:
        key = result.key
        path = units[key].path if isinstance(key, int) and 0 <= key < len(units) else "a change"
        if not result.ok or result.result is None:
            detail = result.error.message if result.error is not None else "unknown error"
            raise RuntimeError(f"judgment failed for {path}: {detail}")
        answer = result.result.answers.get("belongs")
        if not isinstance(answer, ScoreAnswer):
            raise RuntimeError(f"judgment answer for {path} is not a score")
        scores[int(key)] = float(answer.probabilities.get(PART_OF_IT, 0.0))
    return scores


def _verify_candidates(units: list[Unit], scores: list[float], instruction: str) -> bool:
    """``pick`` over the strongest candidates; False when it names ``none`` of them."""
    from agent.judgment import (
        ChoiceAnswer,
        ChoiceQuestion,
        JudgmentRequest,
        judge,
        load_questions,
    )

    ranked = sorted(
        ((score, index) for index, score in enumerate(scores) if score >= STAGE_THRESHOLD),
        key=lambda item: (-item[0], item[1]),
    )[:VERIFY_CANDIDATES]
    if not ranked:
        return True
    base = load_questions("git-stage")["pick"]
    if not isinstance(base, ChoiceQuestion):
        raise RuntimeError("git-stage question 'pick' is not a choice question")
    criteria: dict[str, str | None] = dict(base.criteria)
    candidates: list[dict[str, Any]] = []
    for key_index, (_, unit_index) in enumerate(ranked):
        key = f"c{key_index}"
        criteria[key] = None
        unit = units[unit_index]
        candidate: dict[str, Any] = {"key": key, "path": unit.path, "kind": unit.kind}
        if unit.change is not None:
            candidate["change"] = bound(unit.change, VERIFY_CHARS)
        candidates.append(candidate)
    request = JudgmentRequest(
        state={"instruction": instruction, "candidates": candidates},
        questions={"pick": ChoiceQuestion(base.instructions, criteria)},
    )
    result = judge(request, purpose="git-stage")
    answer = result.answers.get("pick")
    if not isinstance(answer, ChoiceAnswer):
        raise RuntimeError("git-stage verification answer is not a choice")
    return float(answer.probabilities.get(NONE, 0.0)) < NONE_THRESHOLD


# ── index-only staging ──────────────────────────────────────────────────────


def _apply_index(cwd: str, units: list[Unit]) -> None:
    """Replay accepted units into the index; the worktree is never touched."""
    from hermes_cli import web_git

    hunks_by_file: dict[str, list[Unit]] = {}
    whole: list[str] = []
    for unit in units:
        if unit.hunk is None:
            whole.append(unit.path)
        else:
            hunks_by_file.setdefault(unit.path, []).append(unit)
    for path, file_units in hunks_by_file.items():
        patch = "\n".join([
            file_units[0].header,
            *(unit.raw for unit in file_units),
            "",
        ])
        web_git.stage_index_patch(cwd, patch)
    if whole:
        web_git.stage_index_paths(cwd, sorted(set(whole)))


__all__ = [
    "NONE",
    "NONE_THRESHOLD",
    "PART_OF_IT",
    "STAGE_THRESHOLD",
    "UNIT_CHARS",
    "VERIFY_CANDIDATES",
    "VERIFY_CHARS",
    "Hunk",
    "ParsedDiff",
    "Unit",
    "bound",
    "judge_available",
    "parse_unified_diff",
    "stage",
]
