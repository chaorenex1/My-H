"""Post-turn incremental refresh (ADR-8 D16).

``run_conversation`` calls :func:`schedule_codegraph_post_turn` once per
top-level turn. Enqueueing is zero-I/O: it reads the loaded config, the profile
home and the workspace, and never runs ``git status`` or opens the graph. The
background task owns all probing, is single-flight per profile/workspace, and
only refreshes files whose content hash changed. Failures never affect the turn.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_INFLIGHT: set[tuple[str, str]] = set()
_INFLIGHT_LOCK = threading.Lock()
SYNC_BUDGET_SECONDS = 2.0
SYNC_MAX_FILES = 20
BACKGROUND_BUDGET_SECONDS = 10.0
BACKGROUND_MAX_FILES = 50
_GIT_TIMEOUT_S = 10


def schedule_codegraph_post_turn(agent: Any, result: Any, *, turn_id: str = "") -> dict[str, Any]:
    """Queue (or synchronously flush) a refresh after a delivered final answer."""
    if not _eligible(agent, result):
        return {"status": "skipped", "reason": "not_a_top_level_delivered_turn"}
    try:
        from .config import CodeGraphLimits

        limits = CodeGraphLimits.from_config()
    except Exception as exc:
        logger.debug("code_intel config unreadable; post-turn skipped", exc_info=True)
        return {"status": "skipped", "reason": str(exc)[:200]}
    if not limits.enabled or not limits.auto_update:
        return {"status": "skipped", "reason": "disabled"}
    try:
        from pm import extras

        if not extras.available("graph") or not extras.available("codegraph"):
            return {"status": "skipped", "reason": "extras_missing"}
    except Exception:
        logger.debug("pm extras probe failed during scheduling", exc_info=True)
        return {"status": "skipped", "reason": "pm_unavailable"}
    try:
        from hermes_constants import get_hermes_home

        home = str(get_hermes_home())
    except Exception:
        logger.debug("profile home unavailable for post-turn refresh", exc_info=True)
        return {"status": "skipped", "reason": "no_profile_home"}
    workspace = _workspace(agent)
    if not workspace:
        return {"status": "skipped", "reason": "no_workspace"}
    try:
        from agent.oneshot_footprint import is_single_query_session

        oneshot = is_single_query_session()
    except Exception:
        logger.debug("oneshot footprint probe failed", exc_info=True)
        oneshot = False
    if oneshot:
        return _run_update(
            home,
            workspace,
            turn_id=turn_id,
            max_files=SYNC_MAX_FILES,
            budget_seconds=SYNC_BUDGET_SECONDS,
            synchronous=True,
        )
    return _enqueue(home, workspace, turn_id)


def _eligible(agent: Any, result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    if not str(result.get("final_response") or "").strip():
        return False
    if result.get("failed") or result.get("error"):
        return False
    try:
        from agent.delegation_context import is_delegated_child_context

        if is_delegated_child_context():
            return False
    except Exception:
        logger.debug("delegation context probe failed", exc_info=True)
    if getattr(agent, "_turn_origin", None):
        return False
    if getattr(agent, "_delegate_depth", 0):
        return False
    if getattr(agent, "_memory_write_origin", "assistant_tool") != "assistant_tool":
        return False
    return True


def _workspace(agent: Any) -> str:
    configured = getattr(agent, "session_cwd", None) or getattr(agent, "_cwd", None)
    if configured:
        return str(configured)
    try:
        from agent.runtime_cwd import resolve_agent_cwd

        return str(resolve_agent_cwd())
    except Exception:
        logger.debug("agent cwd resolution failed", exc_info=True)
        try:
            return os.getcwd()
        except OSError:
            return ""


def _enqueue(home: str, workspace: str, turn_id: str) -> dict[str, Any]:
    key = (home, workspace)
    with _INFLIGHT_LOCK:
        if key in _INFLIGHT:
            return {"status": "merged"}
        _INFLIGHT.add(key)

    def worker() -> None:
        try:
            _run_update(
                home,
                workspace,
                turn_id=turn_id,
                max_files=BACKGROUND_MAX_FILES,
                budget_seconds=BACKGROUND_BUDGET_SECONDS,
                synchronous=False,
            )
        finally:
            with _INFLIGHT_LOCK:
                _INFLIGHT.discard(key)

    from agent.memory_provider import spawn_context_thread

    thread = spawn_context_thread(
        worker, name="codegraph-post-turn", daemon=True
    )
    thread.start()
    return {"status": "queued"}


def _run_update(
    home: str,
    workspace: str,
    *,
    turn_id: str,
    max_files: int,
    budget_seconds: float,
    synchronous: bool,
) -> dict[str, Any]:
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(home)
    try:
        return _refresh(
            workspace,
            turn_id=turn_id,
            max_files=max_files,
            budget_seconds=budget_seconds,
            synchronous=synchronous,
        )
    except Exception as exc:
        logger.debug("post-turn code refresh failed", exc_info=True)
        return {"status": "failed", "reason": f"{type(exc).__name__}: {exc}"[:500]}
    finally:
        reset_hermes_home_override(token)


def _refresh(
    workspace: str,
    *,
    turn_id: str,
    max_files: int,
    budget_seconds: float,
    synchronous: bool,
) -> dict[str, Any]:
    try:
        from pm import extras

        if not extras.available("graph") or not extras.available("codegraph"):
            return {"status": "skipped", "reason": "extras_missing"}
    except Exception:
        logger.debug("pm extras probe failed for post-turn refresh", exc_info=True)
        return {"status": "skipped", "reason": "pm_unavailable"}
    from hermes_graph import GraphClient
    from hermes_graph.exceptions import GraphLockedError

    from .api import CodeIntelligence
    from .config import CodeGraphLimits
    from .languages import default_registry
    from .scanner import resolve_repository

    try:
        repository = resolve_repository(workspace)
    except Exception:
        logger.debug("workspace is not a repository", exc_info=True)
        return {"status": "skipped", "reason": "no_repository"}
    try:
        from hermes_constants import get_hermes_home

        database = get_hermes_home() / "graph" / "graph.lbdb"
        if not database.exists():
            return {"status": "skipped", "reason": "not_indexed"}
    except Exception:
        logger.debug("graph database probe failed", exc_info=True)
        return {"status": "skipped", "reason": "not_indexed"}
    try:
        client = GraphClient.open(read_only=False)
    except GraphLockedError:
        return {"status": "skipped", "reason": "graph_locked"}
    from hermes_graph.serialization import derived_id

    if client.nodes.get(derived_id("CodeRepository", repository.key)) is None:
        return {"status": "skipped", "reason": "not_indexed"}
    limits = CodeGraphLimits.from_config()
    ci = CodeIntelligence(client, repository, limits, default_registry())
    changed = _changed_paths(repository.root)
    if not changed:
        return {"status": "no_changes"}
    deadline = time.monotonic() + budget_seconds
    updated = 0
    failed = 0
    truncated = False
    for path in changed:
        if updated + failed >= max_files or time.monotonic() > deadline:
            truncated = True
            break
        if not (repository.root / path).exists():
            try:
                from .update import delete_file

                delete_file(client, repository, ci.registry, limits, path)
                updated += 1
            except GraphLockedError:
                truncated = True
                break
            except Exception:
                logger.debug("post-turn delete failed for %s", path, exc_info=True)
                failed += 1
            continue
        if not _needs_refresh(client, repository, path):
            continue
        try:
            ci.refresh_file(path, semantic=limits.semantic, semgrep=False)
            updated += 1
        except GraphLockedError:
            truncated = True
            break
        except Exception:
            logger.debug("post-turn refresh failed for %s", path, exc_info=True)
            failed += 1
    status = "truncated" if truncated else ("completed" if not failed else "partial")
    _write_run(ci, turn_id=turn_id, status=status, files=len(changed),
               updated=updated, failed=failed)
    return {
        "status": status,
        "updated": updated,
        "failed": failed,
        "truncated": truncated,
        "files": len(changed),
    }


def _needs_refresh(client: Any, repository: Any, path: str) -> bool:
    import hashlib

    from hermes_graph.serialization import derived_id

    node = client.nodes.get(
        derived_id("CodeFile", f"{repository.key}@{path}")
    )
    if node is None:
        return True
    try:
        data = (repository.root / path).read_bytes()
    except OSError:
        return True
    digest = hashlib.sha256(data).hexdigest()
    props = node.properties
    if digest == props.get("indexed_hash") and props.get("parse_status") in (
        "success", "partial"
    ):
        return False
    return True


def _changed_paths(root: Path) -> list[str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "-z"],
            capture_output=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    paths: list[str] = []
    for raw in completed.stdout.split(b"\x00"):
        if not raw:
            continue
        text = os.fsdecode(raw)
        if len(text) > 3 and text[2] == " ":
            text = text[3:]
        text = text.replace("\\", "/")
        if text and text not in paths:
            paths.append(text)
    return paths


def _write_run(
    ci: Any, *, turn_id: str, status: str, files: int, updated: int, failed: int
) -> None:
    from datetime import UTC, datetime

    from hermes_graph.serialization import new_node_id

    run_key = f"run_{new_node_id()}"
    try:
        with ci._client.transaction.begin() as tx:
            tx.nodes.upsert(
                "CodeAnalysisRun",
                run_key,
                {
                    "repository_id": ci.repository.repository_id,
                    "kind": "post_turn",
                    "status": status,
                    "started_at": datetime.now(UTC).isoformat(),
                    "finished_at": datetime.now(UTC).isoformat(),
                    "target_revision": ci.repository.revision_key,
                    "snapshot_hash": "",
                    "files_total": files,
                    "files_indexed": updated,
                    "files_skipped": max(files - updated - failed, 0),
                    "files_failed": failed,
                    "error": "",
                    "turn_id": turn_id,
                },
            )
    except Exception:
        logger.debug("post-turn run record failed", exc_info=True)
