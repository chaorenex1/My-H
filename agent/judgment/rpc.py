"""Host ops for sandbox/eval callers (ADR-17 D6; ADR-4 D11.5 seam).

``serve_rpc`` dispatches a line-JSON request either to a tool or, for these reserved names,
to the judgment host. Both ops share the service schema, budget enforcement, accounting, and
error types with the Python API; they are not model tools and never enter the transcript.

Success responses are the v1 result object; failures are ``{"error": {type, message}}`` so a
script can branch without parsing a transport error.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Mapping

from agent.judgment.types import JudgmentError, JudgmentValidationError

logger = logging.getLogger(__name__)

#: Names a sandbox may send over the tool RPC; checked without importing the service.
HOST_OP_NAMES = frozenset({"__judge__", "__judge_batch__"})


def _enabled() -> bool:
    try:
        from agent.judgment.config import load_judgment_settings

        return load_judgment_settings().tools_enabled != "off"
    except Exception:  # health: allow BLE001 -- a broken config must not expose the op
        logger.debug("judgment host op availability check failed", exc_info=True)
        return False


def _error(exc: BaseException) -> str:
    return json.dumps(
        {"error": {"type": type(exc).__name__, "message": str(exc)}},
        ensure_ascii=False)


def _judge_payload(params: Any) -> Mapping[str, Any]:
    if not isinstance(params, Mapping):
        raise JudgmentValidationError("judgment RPC params must be an object")
    return params


def _judge_op(params: Any) -> str:
    try:
        if not _enabled():
            raise JudgmentValidationError("judgment tools are disabled (judgment.tools.enabled=off)")
        from agent.judgment.service import judge, result_to_json

        result = judge(_judge_payload(params))
        return json.dumps(result_to_json(result), ensure_ascii=False)
    except JudgmentError as exc:
        return _error(exc)


def _judge_batch_op(params: Any) -> str:
    try:
        if not _enabled():
            raise JudgmentValidationError("judgment tools are disabled (judgment.tools.enabled=off)")
        from agent.judgment.service import batch_to_json, judge_batch

        result = judge_batch(_judge_payload(params))
        return json.dumps(batch_to_json(result), ensure_ascii=False)
    except JudgmentError as exc:
        return _error(exc)


HOST_OPS: dict[str, Callable[[Any], str]] = {
    "__judge__": _judge_op,
    "__judge_batch__": _judge_batch_op,
}


def host_op(name: str) -> Callable[[Any], str] | None:
    """The host op named ``name``, or ``None`` when it is not a judgment op."""
    return HOST_OPS.get(name)
