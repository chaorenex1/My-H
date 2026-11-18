"""``voice.record`` answers a capture id so a stale stop cannot cut the live capture.

The handler is driven directly (``server._methods``) with the voice wrapper monkeypatched: no mic,
no ``start_continuous`` process, only the id lifecycle the wire contract now carries.
"""

from types import SimpleNamespace

import pytest

from tui_gateway.contracts.prompt_voice import VoiceRecordParams, VoiceRecordResult


@pytest.fixture
def voice(monkeypatch):
    from tui_gateway import server
    import hermes_cli.voice as voice_impl

    transport = SimpleNamespace(write=lambda frame: True)
    state = {"started": True, "stops": 0}

    monkeypatch.setattr(server, "_caller_transport", lambda: transport)
    monkeypatch.setattr(server, "_wake_owner_snapshot", lambda: (None, None))
    monkeypatch.setattr(server, "_session_voice_enabled", lambda session_id: True)
    monkeypatch.setattr(server, "_tts_stream_stop", lambda **kwargs: None)
    monkeypatch.setattr(server, "_voice_cfg_dict", dict)
    monkeypatch.setattr(server, "_voice_cfg_number", lambda value, default: default)
    monkeypatch.setattr(server, "_resume_voice_wake", lambda: None)
    monkeypatch.setattr(server, "_voice_capture_sid", "")
    monkeypatch.setattr(server, "_voice_capture_id", "")
    monkeypatch.setattr(server, "_voice_event_sid", "")
    monkeypatch.setattr(server, "_voice_event_transport", None)
    monkeypatch.setattr(server, "_voice_wake_owner", None)
    monkeypatch.setattr(voice_impl, "start_continuous", lambda **kwargs: state["started"])
    monkeypatch.setattr(voice_impl, "stop_continuous",
                        lambda **kwargs: state.__setitem__("stops", state["stops"] + 1))
    monkeypatch.setattr(voice_impl, "set_voice_busy_probe", lambda probe: None)

    def call(**params):
        VoiceRecordParams.model_validate(params)
        reply = server._methods["voice.record"](1, params)
        assert "error" not in reply, reply
        VoiceRecordResult.model_validate(reply["result"])
        return reply["result"]

    return call, state, server


def test_start_mints_and_remembers_a_capture_id(voice):
    call, _state, server = voice
    reply = call(action="start", session_id="ui-voice")
    assert reply["status"] == "recording"
    capture_id = reply["capture_id"]
    assert isinstance(capture_id, str) and len(capture_id) == 32
    int(capture_id, 16)  # uuid4 hex
    assert server._voice_capture_id == capture_id
    assert server._voice_capture_sid == "ui-voice"


def test_start_echoes_a_caller_supplied_capture_id(voice):
    call, _state, server = voice
    assert call(action="start", session_id="ui-voice", capture_id="client-cap")["capture_id"] == "client-cap"
    assert server._voice_capture_id == "client-cap"


def test_busy_start_reports_no_capture_id(voice):
    call, state, server = voice
    state["started"] = False
    assert call(action="start", session_id="ui-voice") == {"status": "busy", "capture_id": None}
    assert server._voice_capture_id == ""


def test_stop_returns_the_remembered_id_and_clears_it(voice):
    call, state, server = voice
    started = call(action="start", session_id="ui-voice")
    assert call(action="stop", session_id="ui-voice") == {
        "status": "stopped", "capture_id": started["capture_id"]}
    assert state["stops"] == 1 and server._voice_capture_id == ""
    # Nothing live to name any more.
    assert call(action="stop", session_id="ui-voice") == {"status": "stopped", "capture_id": None}
    assert state["stops"] == 2


def test_stop_with_a_stale_capture_id_leaves_the_live_capture_alone(voice):
    call, state, server = voice
    live = call(action="start", session_id="ui-voice")["capture_id"]
    assert call(action="stop", session_id="ui-voice", capture_id="stale") == {
        "status": "busy", "reason": "capture_mismatch", "capture_id": live}
    assert state["stops"] == 0 and server._voice_capture_id == live
    assert call(action="stop", session_id="ui-voice", capture_id=live)["status"] == "stopped"
    assert state["stops"] == 1
