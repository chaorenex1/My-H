"""Plan panel: ``session.plan.read|save|approve`` over the session workspace's ``.hermes/plans``.

Plans live in the workspace (the ``/plan`` prompt tells the agent to write there), and the
workspace may be a remote / sandboxed backend — so every file operation runs through
``session_terminal_command`` and never the gateway host. ``read`` returns the newest plan; ``save``
overwrites it (or creates a timestamped one); approval is a sibling ``<plan>.status`` file, so the
markdown itself is never rewritten and approval cannot resume execution on its own.
"""

from __future__ import annotations

import shlex
from datetime import UTC, datetime
from functools import partial

from gateway.session_busy_controls import authorize
from gateway.session_terminal_command import run, run_checked
from hermes_state_runtime import RuntimeStoreError

PLANS_DIR = ".hermes/plans"
_MAX_PLAN_CHARS = 24000
_STATUSES = frozenset({"draft", "approved"})


def handlers(connection):
    return {
        "session.plan.read": partial(read_plan, connection),
        "session.plan.save": partial(save_plan, connection),
        "session.plan.approve": partial(approve_plan, connection),
    }


def _ref(connection, ref, params, capability):
    if not isinstance(ref.session_id, str) or not ref.session_id:
        raise RuntimeStoreError("invalid_params", field="session_id")
    if ref.session_id not in connection.authority.sessions:
        raise RuntimeStoreError("not_found")
    authorize(connection, ref, params, capability)


def _quote(value: str) -> str:
    return shlex.quote(value)


def _newest_plan_command() -> str:
    return (f"if [ -d {PLANS_DIR} ]; then "
            f"ls -1t -- {PLANS_DIR}/*.md 2>/dev/null | head -n 1; fi")


def _stat_command(path: str) -> str:
    quoted = _quote(path)
    return (f"stat -c %Y '{quoted}' 2>/dev/null || stat -f %m '{quoted}' 2>/dev/null || echo 0")


async def _newest_plan(connection, ref) -> str:
    return (await run(connection, ref, _newest_plan_command())).get("output", "").strip()


async def _meta(connection, ref, path: str) -> tuple[float | None, str]:
    quoted = _quote(path)
    command = (f"printf '%s\\n' \"$({_stat_command(path)})\" "
               f"\"$(cat '{quoted}.status' 2>/dev/null || echo draft)\"")
    output = await run(connection, ref, command)
    lines = output.get("output", "").splitlines()
    updated = None
    if lines:
        try:
            value = float(lines[0].strip())
            updated = value if value > 0 else None
        except ValueError:
            updated = None
    status = lines[1].strip().lower() if len(lines) > 1 else "draft"
    return updated, status if status in _STATUSES else "draft"


async def read_plan(connection, ref, params):
    _ref(connection, ref, params, "session:read")
    path = await _newest_plan(connection, ref)
    if not path:
        return {"path": None, "text": "", "status": "none", "updated_at": None}
    text = (await run_checked(connection, ref, f"cat -- {_quote(path)}", field="path")).strip("\n")
    updated, status = await _meta(connection, ref, path)
    return {"path": path, "text": text, "status": status, "updated_at": updated}


def _validate_write(params, *, with_text: bool):
    for key in ("profile",):
        if params.get(key) is not None and not isinstance(params[key], str):
            raise RuntimeStoreError("invalid_params", field=key)
    request_id = params.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        raise RuntimeStoreError("invalid_params", field="request_id")
    if with_text:
        text = params.get("text")
        if not isinstance(text, str) or "\x00" in text:
            raise RuntimeStoreError("invalid_params", field="text")
        if len(text) > _MAX_PLAN_CHARS:
            raise RuntimeStoreError("invalid_params", field="text",
                                    detail=f"plan is over the {_MAX_PLAN_CHARS}-character cap")
        return text
    return None


def _heredoc(text: str) -> str:
    for _ in range(5):
        delimiter = f"HERMES_PLAN_{datetime.now(UTC).strftime('%H%M%S%f')}"
        if delimiter not in text:
            return f"cat > {{target}} <<'{delimiter}'\n{text}\n{delimiter}\n"
    raise RuntimeStoreError("invalid_params", field="text", detail="plan text cannot be delimited")


async def save_plan(connection, ref, params):
    _ref(connection, ref, params, "session:control")
    text = _validate_write(params, with_text=True)
    path = await _newest_plan(connection, ref)
    if not path:
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        path = f"{PLANS_DIR}/{stamp}-plan.md"
    template = _heredoc(text)
    quoted = _quote(path)
    body = template.replace("{target}", f"'{quoted}'", 1)
    # Two commands: a heredoc terminator ends the current command line, so a ``&&`` after it
    # would start a line with ``&&`` (a bash syntax error). Write first, then stamp the status.
    await run_checked(connection, ref, f"mkdir -p {PLANS_DIR} && {body}", field="text")
    await run_checked(connection, ref, f"printf draft > '{quoted}.status'", field="text")
    updated, _status = await _meta(connection, ref, path)
    return {"path": path, "updated_at": updated or 0.0}


async def approve_plan(connection, ref, params):
    _ref(connection, ref, params, "session:control")
    _validate_write(params, with_text=False)
    path = await _newest_plan(connection, ref)
    if not path:
        raise RuntimeStoreError("invalid_params", field="path", detail="no plan to approve")
    await run_checked(connection, ref, f"printf approved > {_quote(path + '.status')}", field="path")
    return {"status": "approved"}
