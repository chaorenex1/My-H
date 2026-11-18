"""Git TUI on the session authority: ``git.*`` runs in the session's own terminal backend.

A remote / sandboxed session must never report the gateway host's repository, so every command
goes through ``session_terminal_command.run`` (``tools/terminal_tool`` with the session's frozen
policy and workspace). Parsing is split into pure helpers so the wire shapes are testable without a
terminal; writable verbs require ``session:control``.
"""

from __future__ import annotations

import re
import shlex
import threading
from functools import partial

from gateway.session_busy_controls import authorize
from gateway.session_terminal_command import run, run_checked
from hermes_state_runtime import RuntimeStoreError

_GIT = "GIT_TERMINAL_PROMPT=0 GIT_PAGER=cat git -c core.quotepath=false"
_DIFF_BYTES = 45000
_DIFF_CONTEXT_MAX = 50
_LOG_LIMIT_MAX = 100
_TRACK_RE = re.compile(r"ahead (\d+)|behind (\d+)")
# Retry-idempotent commits: a repeated ``request_id`` returns the first receipt instead of
# committing again (or failing with "nothing to commit" after the first one succeeded).
_COMMIT_RECEIPTS: dict[tuple[str, str, str], dict] = {}
_RECEIPT_LOCK = threading.Lock()
_RECEIPT_MAX = 64


def handlers(connection):
    return {
        "git.status": partial(status, connection),
        "git.diff": partial(diff, connection),
        "git.log": partial(log, connection),
        "git.branches": partial(branches, connection),
        "git.stage": partial(stage, connection),
        "git.unstage": partial(unstage, connection),
        "git.commit": partial(commit, connection),
    }


def _ref(connection, ref, params, capability):
    if not isinstance(ref.session_id, str) or not ref.session_id:
        raise RuntimeStoreError("invalid_params", field="session_id")
    if ref.session_id not in connection.authority.sessions:
        raise RuntimeStoreError("not_found")
    authorize(connection, ref, params, capability)


def _quote(value: str) -> str:
    return shlex.quote(value)


def _path_list(params):
    paths = params.get("paths")
    if (not isinstance(paths, list) or not paths
            or any(not isinstance(path, str) or not path or "\x00" in path for path in paths)):
        raise RuntimeStoreError("invalid_params", field="paths")
    return paths


def _parse_status(output: str) -> list[dict]:
    """Porcelain v1 ``-z`` output → ``[{path, xy, staged}]``; rename entries keep the new path."""
    files = []
    entries = output.split("\x00")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry or len(entry) < 3:
            continue
        xy = entry[:2]
        path = entry[3:]
        if xy[0] in "RC":  # rename/copy: the next NUL field is the original path
            index += 1
        files.append({"path": path, "xy": xy, "staged": xy[0] not in {" ", "?", "!"}})
    return files


def _parse_track(value: str) -> tuple[int, int]:
    if not value or value.strip() == "gone":
        return 0, 0
    ahead = behind = 0
    for match in _TRACK_RE.finditer(value):
        if match.group(1):
            ahead = int(match.group(1))
        else:
            behind = int(match.group(2))
    return ahead, behind


def _parse_branches(output: str) -> list[dict]:
    rows = []
    for line in output.splitlines():
        if not line:
            continue
        name, _, rest = line.partition("\x00")
        upstream, _, track = rest.partition("\x00")
        ahead, behind = _parse_track(track)
        rows.append({"name": name, "upstream": upstream or None, "ahead": ahead, "behind": behind})
    return rows


def _parse_log(output: str) -> list[dict]:
    commits = []
    for line in output.splitlines():
        if not line:
            continue
        sha, _, rest = line.partition("\x00")
        subject, _, rest = rest.partition("\x00")
        author, _, date = rest.partition("\x00")
        commits.append({"sha": sha, "subject": subject, "author": author, "date": date})
    return commits


async def status(connection, ref, params):
    _ref(connection, ref, params, "session:read")
    branch = (await run(connection, ref, f"{_GIT} branch --show-current")).get("output", "").strip()
    files = _parse_status(await run_checked(
        connection, ref, f"{_GIT} status --porcelain=v1 -z --untracked-files=all",
        field="session_id"))
    tracking = await run(
        connection, ref, f"{_GIT} rev-list --left-right --count '@{{upstream}}...HEAD'", timeout=15)
    ahead = behind = 0
    # ``rev-list`` is "<behind>\\t<ahead>" (left = upstream-only).
    if tracking.get("exit_code") == 0:
        parts = tracking.get("output", "").split()
        if len(parts) == 2 and all(part.isdigit() for part in parts):
            behind, ahead = int(parts[0]), int(parts[1])
    return {"branch": branch, "ahead": ahead, "behind": behind, "files": files}


async def diff(connection, ref, params):
    _ref(connection, ref, params, "session:read")
    context = params.get("context")
    if context is not None and (not isinstance(context, int) or isinstance(context, bool) or context < 0):
        raise RuntimeStoreError("invalid_params", field="context")
    context = min(context if context is not None else 3, _DIFF_CONTEXT_MAX)
    staged = params.get("staged", False)
    if not isinstance(staged, bool):
        raise RuntimeStoreError("invalid_params", field="staged")
    path = params.get("path")
    if path is not None and (not isinstance(path, str) or "\x00" in path):
        raise RuntimeStoreError("invalid_params", field="path")
    command = f"set -o pipefail; {_GIT} diff --no-color -U{context}"
    if staged:
        command += " --cached"
    if path:
        command += f" -- {_quote(path)}"
    # ``pipefail`` keeps git's failure (not-a-repo, bad path) instead of ``head``'s zero; the
    # trailing ``cat`` drains the rest so a diff over the cap never SIGPIPEs git into a false error.
    command += f" | {{ head -c {_DIFF_BYTES + 1}; cat >/dev/null; }}"
    output = await run_checked(connection, ref, command, field="session_id")
    truncated = len(output) > _DIFF_BYTES
    return {"diff": output[:_DIFF_BYTES] if truncated else output, "truncated": truncated}


async def log(connection, ref, params):
    _ref(connection, ref, params, "session:read")
    limit = params.get("limit")
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 1):
        raise RuntimeStoreError("invalid_params", field="limit")
    limit = min(limit or 20, _LOG_LIMIT_MAX)
    output = await run_checked(
        connection, ref,
        f"{_GIT} log -n {limit} --format='%H%x00%s%x00%an%x00%cI'", field="session_id")
    return {"commits": _parse_log(output)}


async def branches(connection, ref, params):
    _ref(connection, ref, params, "session:read")
    current = (await run(connection, ref, f"{_GIT} branch --show-current")).get("output", "").strip()
    output = await run_checked(
        connection, ref,
        f"{_GIT} for-each-ref --format='%(refname:short)%00%(upstream:short)%00%(upstream:track,nobracket)' "
        "refs/heads", field="session_id")
    return {"current": current, "branches": _parse_branches(output)}


async def _staged_paths(connection, ref):
    output = await run(connection, ref, f"{_GIT} diff --cached --name-only -z")
    return [path for path in output.get("output", "").split("\x00") if path]


async def stage(connection, ref, params):
    _ref(connection, ref, params, "session:control")
    paths = _path_list(params)
    await run_checked(connection, ref, f"{_GIT} add -- {' '.join(_quote(path) for path in paths)}",
                      field="paths")
    return {"staged": await _staged_paths(connection, ref)}


async def unstage(connection, ref, params):
    _ref(connection, ref, params, "session:control")
    paths = _path_list(params)
    quoted = " ".join(_quote(path) for path in paths)
    result = await run(connection, ref, f"{_GIT} restore --staged -- {quoted}")
    if result["exit_code"] != 0:
        await run_checked(connection, ref,
                          f"{_GIT} rm --cached -r --ignore-unmatch -- {quoted}", field="paths")
    return {"staged": await _staged_paths(connection, ref)}


def _receipt(key):
    with _RECEIPT_LOCK:
        return dict(_COMMIT_RECEIPTS[key]) if key in _COMMIT_RECEIPTS else None


def _remember(key, receipt):
    with _RECEIPT_LOCK:
        while len(_COMMIT_RECEIPTS) >= _RECEIPT_MAX:
            _COMMIT_RECEIPTS.pop(next(iter(_COMMIT_RECEIPTS)))
        _COMMIT_RECEIPTS[key] = dict(receipt)


async def commit(connection, ref, params):
    _ref(connection, ref, params, "session:control")
    message = params.get("message")
    if not isinstance(message, str) or not message.strip() or "\x00" in message:
        raise RuntimeStoreError("invalid_params", field="message", detail="message must be non-empty")
    sign = params.get("sign", False)
    if not isinstance(sign, bool):
        raise RuntimeStoreError("invalid_params", field="sign")
    request_id = params.get("request_id")
    if request_id is not None and (not isinstance(request_id, str) or not request_id):
        raise RuntimeStoreError("invalid_params", field="request_id")
    key = (connection.authority.profile_id, ref.session_id, request_id) if request_id else None
    if key is not None:
        cached = _receipt(key)
        if cached is not None:
            return cached
    command = f"{_GIT} commit"
    if sign:
        command += " -S"
    command += f" -m {_quote(message)}"
    await run_checked(connection, ref, command, field="message", timeout=120)
    sha = (await run_checked(connection, ref, f"{_GIT} rev-parse HEAD", field="session_id")).strip()
    summary = (await run(connection, ref, f"{_GIT} log -1 --format=%s")).get("output", "").strip()
    receipt = {"sha": sha, "summary": summary}
    if key is not None:
        _remember(key, receipt)
    return receipt
