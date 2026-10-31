"""Wire-protocol constants shared by both gateway transports and by the clients that negotiate them.

The gateway speaks JSON-RPC 2.0 on both transports: one JSON object per stdio line and one JSON
object per WebSocket text frame. omp's transport adds an optional framing layer (``rpc_chunk``) for
logical frames larger than one transport frame. This gateway does not implement that layer: it never
emits ``rpc_chunk`` and every inbound frame is a whole JSON object. It therefore advertises
``protocol_version = 1`` and only ``[1]`` — claiming 2 would promise reassembly the backend does not
perform — and ``max_frame_bytes = max_reassembled_frame_bytes = 0`` means "no frame budget is
enforced" (the WebSocket server has its own large attachment ceiling; stdio is unbounded).

Both numbers travel in ``gateway.ready`` and are re-declared by ``client.capabilities`` (see
``docs/appendix-omp-ui-realtime-rpc.md`` A.3.1 and ``docs/appendix-omp-ui-business-rpc.md`` B.3.1).
They are transport constants, never configuration.
"""

from __future__ import annotations

PROTOCOL_VERSION = 1
SUPPORTED_PROTOCOL_VERSIONS = (1,)
MAX_FRAME_BYTES = 0
MAX_REASSEMBLED_FRAME_BYTES = 0


def ready_protocol_fields() -> dict:
    """The negotiation keys both ``gateway.ready`` emitters add to their payload."""
    return {
        "protocol_version": PROTOCOL_VERSION,
        "supported_protocol_versions": list(SUPPORTED_PROTOCOL_VERSIONS),
        "max_frame_bytes": MAX_FRAME_BYTES,
        "max_reassembled_frame_bytes": MAX_REASSEMBLED_FRAME_BYTES,
    }


def ready_payload(*, skin: dict, replay_epoch: str, heartbeat: bool | None = None,
                  session_authority: bool | None = None) -> dict:
    """``gateway.ready``'s payload, so both transports declare the same transport budget.

    ``heartbeat`` is WebSocket-only and ``session_authority`` only when the socket bound an
    authority; both stay absent (not ``null``) on a stdio gateway, matching the existing frame.
    """
    payload = {"skin": skin, "change_events": True, "replay_epoch": replay_epoch, **ready_protocol_fields()}
    if heartbeat is not None:
        payload["heartbeat"] = heartbeat
    if session_authority is not None:
        payload["session_authority"] = session_authority
    return payload
