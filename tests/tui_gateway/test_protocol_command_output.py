"""``command.dispatch`` emits ``command.output`` from the result envelope."""


def test_command_dispatch_emits_command_output_from_the_result_envelope(protocol_server, monkeypatch):
    """The stage returns a JSON-RPC envelope; the output text lives under ``result``."""
    server = protocol_server
    sid = "test-session"
    server._sessions[sid] = {"session_key": sid}
    events = []
    monkeypatch.setattr(server, "_emit",
                        lambda event, event_sid, payload=None: events.append((event, event_sid, payload)))
    monkeypatch.setattr(
        server, "_dispatch_quick",
        lambda rid, params, session, name, arg: server._ok(rid, {"type": "exec", "output": "hello"}))

    resp = server.handle_request({
        "id": "r1", "method": "command.dispatch",
        "params": {"name": "quick", "arg": "", "session_id": sid}})

    assert resp["result"]["output"] == "hello"
    assert ("command.output", sid, {"text": "hello", "name": "quick"}) in events
