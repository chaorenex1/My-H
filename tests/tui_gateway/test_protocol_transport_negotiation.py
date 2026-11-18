"""Transport negotiation on ``client.capabilities`` and ``gateway.ready``."""

from tests.tui_gateway.test_protocol import _silent_ws


def test_client_capabilities_negotiates_the_transport_protocol(protocol_server):
    """The omp wire negotiates version + frame limits on the same capability call (A.3.1/B.3.1):
    the params are accepted (not a 4000 unknown-key refusal) and the result re-declares them."""
    server = protocol_server
    from tui_gateway.contracts.protocol import (
        MAX_FRAME_BYTES,
        MAX_REASSEMBLED_FRAME_BYTES,
        PROTOCOL_VERSION,
        SUPPORTED_PROTOCOL_VERSIONS,
    )
    from tui_gateway.transport import bind_transport, reset_transport

    peer = _silent_ws()
    token = bind_transport(peer)
    try:
        response = server.handle_request({"id": 1, "method": "client.capabilities", "params": {
            "server_requests": True, "protocol_version": 2,
            "supported_protocol_versions": [1, 2], "max_frame_bytes": 65536,
            "max_reassembled_frame_bytes": 262144}})
    finally:
        reset_transport(token)
    result = response["result"]
    assert result["protocol_version"] == PROTOCOL_VERSION
    assert result["supported_protocol_versions"] == list(SUPPORTED_PROTOCOL_VERSIONS)
    assert result["max_frame_bytes"] == MAX_FRAME_BYTES
    assert result["max_reassembled_frame_bytes"] == MAX_REASSEMBLED_FRAME_BYTES


def test_gateway_ready_payload_declares_transport_negotiation(protocol_server):
    """Both transports build ``gateway.ready`` from the same helper: its payload validates against
    the contract, carries the transport budget, and omits the WS-only keys on stdio."""
    from tui_gateway.contracts.events import GatewayReadyPayload
    from tui_gateway.contracts.protocol import (
        MAX_FRAME_BYTES,
        MAX_REASSEMBLED_FRAME_BYTES,
        PROTOCOL_VERSION,
        SUPPORTED_PROTOCOL_VERSIONS,
        ready_payload,
    )

    stdio = GatewayReadyPayload.model_validate(
        ready_payload(skin={}, replay_epoch="r"))
    assert stdio.protocol_version == PROTOCOL_VERSION
    assert stdio.supported_protocol_versions == list(SUPPORTED_PROTOCOL_VERSIONS)
    assert stdio.max_frame_bytes == MAX_FRAME_BYTES
    assert stdio.max_reassembled_frame_bytes == MAX_REASSEMBLED_FRAME_BYTES
    assert stdio.heartbeat is None and stdio.session_authority is None

    websocket = GatewayReadyPayload.model_validate(
        ready_payload(skin={}, replay_epoch="r", heartbeat=True, session_authority=True))
    assert websocket.heartbeat is True and websocket.session_authority is True
