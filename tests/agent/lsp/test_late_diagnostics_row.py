"""ADR-9 P1 invariants: a late LSP verdict becomes one durable custom row.

The facts tested here are the ones a client or the model would notice: the pi-tui line shape, the
``details.files`` payload, the developer projection, the turn-end row, and the single live event.
"""
from __future__ import annotations

import json
import os
import re
from types import SimpleNamespace

import pytest

from agent.custom_messages import project_for_api
from agent.lsp import deferred
from agent.lsp import delivery as lsp_delivery
from agent.lsp.display import DisplayPolicy, display_policy
from agent.lsp.reporter import (
    MAX_TOTAL_CHARS,
    cap_display_files,
    file_entry,
    merge_file_entries,
    model_text,
)

# pi-tui's ``render-utils.ts::parseDiagnosticMessage`` (18.14.1).
_PARSE = re.compile(
    r"^(.+?):(\d+):(\d+)\s+\[(\w+)\]\s+(?:\[([^\]]+)\]\s+)?(.+?)(?:\s+\(([^)]+)\))?$")

_WINDOWS_PATH = r"C:\repo\src\app.ts"


def _diag(severity: int, line: int, col: int, message: str, *, source: str = "typescript",
          code: str | None = None) -> dict:
    diag = {"severity": severity, "message": message, "source": source,
            "range": {"start": {"line": line, "character": col}}}
    if code:
        diag["code"] = code
    return diag


def _files(*entries) -> tuple:
    return tuple(entries)


# ── reporter: the client payload ────────────────────────────────────────────────────────────────

def test_display_lines_parse_like_pi_tui_on_windows_paths():
    entry = file_entry(_WINDOWS_PATH, [
        _diag(1, 11, 4, "Type 'string' is not assignable to type 'number'.", code="2322"),
        _diag(2, 17, 0, "Unused variable.", source="ruff", code="F841"),
    ], severities=frozenset({1, 2}))
    assert entry is not None
    assert entry["path"] == _WINDOWS_PATH
    assert entry["errored"] is True
    assert entry["summary"] == "1 error(s), 1 warning(s)"
    tones = []
    for line in entry["messages"]:
        parsed = _PARSE.match(line)
        assert parsed is not None, f"pi-tui cannot parse {line!r}"
        assert parsed.group(1) == _WINDOWS_PATH
        assert parsed.group(5) in {"typescript", "ruff"}
        tones.append(parsed.group(4))
    # The component maps these two lowercase tokens to the error/warning tones; no other spelling.
    assert tones == ["error", "warning"]


def test_summary_and_errored_follow_the_filtered_set():
    diagnostics = [_diag(1, 0, 0, "boom"), _diag(2, 1, 0, "hmm")]
    errors_only = file_entry("a.py", diagnostics, severities=frozenset({1}))
    warnings_only = file_entry("a.py", diagnostics, severities=frozenset({2}))
    assert errors_only is not None and warnings_only is not None
    assert errors_only["summary"] == "1 error(s)" and errors_only["errored"] is True
    assert len(errors_only["messages"]) == 1
    assert warnings_only["summary"] == "1 warning(s)" and warnings_only["errored"] is False


def test_display_caps_drop_whole_lines_and_keep_the_summary():
    entry = file_entry("a.py", [_diag(1, index, 0, f"err{index}") for index in range(25)])
    assert entry is not None
    capped = cap_display_files([entry], per_file=3, total_chars=10_000)
    assert len(capped[0]["messages"]) == 3
    assert capped[0]["summary"] == "25 error(s)"
    assert all(_PARSE.match(line) for line in capped[0]["messages"])

    tight = cap_display_files([entry], per_file=25, total_chars=len(entry["messages"][0]) + 5)
    assert tight[0]["messages"] == [entry["messages"][0]]  # the line that would overflow is dropped
    assert tight[0]["summary"] == "25 error(s)"


def test_display_cap_spans_files_and_keeps_each_summary():
    first = file_entry("a.py", [_diag(1, 0, 0, "first")])
    second = file_entry("b.py", [_diag(1, 0, 0, "second")])
    assert first is not None and second is not None
    room = len(first["messages"][0]) + len(second["messages"][0]) - 1
    capped = cap_display_files([first, second], total_chars=room)
    assert capped[0]["messages"] == first["messages"]  # fits whole
    assert capped[1]["messages"] == []                # the next line would overflow
    assert capped[1]["summary"] == "1 error(s)"


def test_model_text_caps_lines_per_file_and_the_total():
    entry = file_entry("a.py", [_diag(1, index, 0, f"boom{index}") for index in range(25)])
    assert entry is not None
    text = model_text([entry], per_file=2, total_chars=10_000)
    assert "boom0" in text and "boom1" in text and "boom2" not in text
    assert model_text([entry], total_chars=200) != ""
    assert len(model_text([entry], total_chars=200)) <= 200
    assert len(model_text([entry])) <= MAX_TOTAL_CHARS


def test_merge_keeps_source_order_per_path():
    """ADR-9 D2/D8: pi-tui sorts inside a file; the shell only merges, never re-sorts."""
    both = frozenset({1, 2})
    lsp = file_entry("a.py", [_diag(1, 39, 0, "late-line")], severities=both)
    semgrep = file_entry("a.py", [{"severity": 2, "message": "early-line", "source": "semgrep",
                                   "code": "r",
                                   "range": {"start": {"line": 2, "character": 0}}}],
                         severities=both)
    assert lsp is not None and semgrep is not None
    (merged,) = merge_file_entries([lsp], [semgrep])
    assert merged["messages"] == lsp["messages"] + semgrep["messages"]
    assert merged["summary"] == "1 error(s), 1 warning(s)"


def test_clean_verdict_produces_no_entry():
    assert file_entry("a.py", [], severities=frozenset({1})) is None
    assert file_entry("a.py", [_diag(2, 0, 0, "warn")], severities=frozenset({1})) is None


# ── config policy ───────────────────────────────────────────────────────────────────────────────

def test_display_policy_defaults_match_the_pre_adr_behaviour(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", dict)
    policy = display_policy()
    assert policy.lsp.mode == "both" and policy.lsp.severities == ("error",)
    assert policy.semgrep.mode == "both" and policy.semgrep.severities == ("error", "warning")
    assert policy.lsp.inline and policy.lsp.late


@pytest.mark.parametrize("mode, inline, late", [
    ("off", False, False), ("inline", True, False), ("late", False, True), ("both", True, True)])
def test_display_policy_modes(monkeypatch, mode, inline, late):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly",
                        lambda: {"lsp": {"display": {"lsp": {"mode": mode, "severities": ["warning"]}}}})
    source = display_policy().lsp
    assert (source.inline, source.late) == (inline, late)
    assert source.severities == ("warning",)


def test_invalid_display_values_fall_back_per_field(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {"lsp": {"display": {
        "lsp": {"mode": "sometimes", "severities": ["info", "warning", 3]},
        "semgrep": {"mode": 7, "severities": []},
    }}})
    policy = DisplayPolicy.from_config()
    assert policy.lsp.mode == "both"            # unknown mode → default
    assert policy.lsp.severities == ("warning",)  # valid entry kept, invalid dropped
    assert policy.semgrep.mode == "both"
    assert policy.semgrep.severities == ("error", "warning")  # empty → that source's default


def test_display_policy_survives_a_broken_config(monkeypatch):
    def boom():
        raise RuntimeError("broken yaml")

    monkeypatch.setattr("hermes_cli.config.load_config_readonly", boom)
    assert display_policy().lsp.mode == "both"


def test_lsp_status_prints_the_resolved_display_policy(monkeypatch, capsys):
    from agent.lsp import cli as lsp_cli

    monkeypatch.setattr("agent.lsp.get_service", lambda: None)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {"lsp": {"display": {
        "lsp": {"mode": "late", "severities": ["warning", "error"]},
        "semgrep": {"mode": "off"},
    }}})
    assert lsp_cli._cmd_status(emit_json=False) == 0
    out = capsys.readouterr().out
    assert "display.lsp:     mode=late severities=error,warning" in out
    assert "display.semgrep: mode=off severities=error,warning" in out
    assert "semgrep master:  enabled=False" in out  # display.mode=off did not touch the scanner switch


def test_shipped_defaults_keep_the_pre_adr9_surface():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["lsp"]["display"] == {
        "lsp": {"mode": "both", "severities": ["error"]},
        "semgrep": {"mode": "both", "severities": ["error", "warning"]},
    }
    assert DEFAULT_CONFIG["lsp"]["enabled"] is True  # the subsystem switch is untouched
    assert DEFAULT_CONFIG["code_intel"]["semgrep"]["enabled"] is False


# ── delivery: the row and its model projection ─────────────────────────────────────────────────

def _note(path: str, diagnostics: list[dict], *, severities=frozenset({1})):
    entry = file_entry(path, diagnostics, severities=severities)
    assert entry is not None
    return deferred.Note(text="<legacy model block>", baseline_text=None, files=_files(entry))


def test_build_row_carries_the_custom_shape_and_a_source_neutral_model_text():
    note = _note(_WINDOWS_PATH, [_diag(1, 11, 4, "boom", code="2322")])
    row = lsp_delivery.build_row([(_WINDOWS_PATH, note)])
    assert row is not None
    assert row["role"] == "custom" and row["custom_type"] == "lsp-late-diagnostic"
    assert row["display"] is True and row["display_only"] is False
    assert row["attribution"] == "agent" and row["legacy_role"] == "user"
    assert row["content"].startswith("<system-notice>\nDiagnostics for the files you edited:")
    assert "Fix them if they are real" in row["content"]
    assert "MOCK" not in row["content"]  # no fabricated header text
    (entry,) = row["details"]["files"]
    assert set(entry) == {"path", "summary", "errored", "messages"}
    assert entry["path"] == _WINDOWS_PATH


def test_model_content_escapes_but_details_stay_plain():
    note = _note("a.py", [_diag(1, 0, 0, "<script>&alert", code="x<y")])
    row = lsp_delivery.build_row([("a.py", note)])
    assert row is not None
    assert "&lt;script&gt;&amp;alert" in row["content"]
    assert "&lt;script&gt;" not in row["details"]["files"][0]["messages"][0]
    assert "<script>" in row["details"]["files"][0]["messages"][0]


def test_one_row_carries_every_path_once_in_pop_order():
    semgrep_only = file_entry("b.py", [_diag(2, 1, 0, "b-boom", source="semgrep")],
                              severities=frozenset({1, 2}))
    assert semgrep_only is not None
    notes = [("b.py", deferred.Note(text="", baseline_text=None,
                                    semgrep_text="<semgrep>", semgrep_files=(semgrep_only,))),
             ("a.py", _note("a.py", [_diag(1, 2, 0, "a-boom")]))]
    row = lsp_delivery.build_row(notes)
    assert row is not None
    # A Semgrep-only path keeps its pop position; it is not pushed behind every LSP path.
    assert [entry["path"] for entry in row["details"]["files"]] == ["b.py", "a.py"]
    assert row["content"].count("Diagnostics for the files you edited") == 1
    assert "b-boom" in row["content"] and "a-boom" in row["content"]


def test_build_row_is_empty_when_the_filter_left_nothing():
    empty = deferred.Note(text="", baseline_text=None, files=())
    assert lsp_delivery.build_row([("a.py", empty)]) is None
    # A verdict with only filtered-out severities never reaches a Note, but a malformed entry must
    # still not crash a turn.
    assert lsp_delivery.build_row([("a.py", deferred.Note(text="x", baseline_text=None,
                                                          files=({"path": "a.py"},)))]) is None


def test_row_projects_to_a_developer_message_without_private_fields():
    row = lsp_delivery.build_row([(_WINDOWS_PATH, _note(_WINDOWS_PATH, [_diag(1, 0, 0, "boom")]))])
    assert row is not None
    (projected,) = project_for_api(row, custom_messages=True)
    assert projected["role"] == "developer"
    assert projected["content"] == row["content"]
    for key in ("custom_type", "details", "display", "display_only", "attribution", "legacy_role"):
        assert key not in projected


def test_the_developer_row_survives_both_provider_shapes_without_private_keys():
    row = lsp_delivery.build_row([(_WINDOWS_PATH, _note(_WINDOWS_PATH, [_diag(1, 0, 0, "boom")]))])
    assert row is not None
    (projected,) = project_for_api(row, custom_messages=True)
    assert projected["role"] == "developer"

    from agent.transports.chat_completions import _normalize_developer_roles

    assert _normalize_developer_roles([projected], "gpt-5.2")[0]["role"] == "developer"
    assert _normalize_developer_roles([projected], "glm-4.7")[0]["role"] == "system"

    from agent.anthropic_message_convert import convert_messages_to_anthropic

    _system, converted = convert_messages_to_anthropic([projected])
    assert converted[0]["role"] == "user"
    wire = json.dumps(converted)
    for key in ("custom_type", "display_only", "attribution", "legacy_role", "details", "_row_id"):
        assert f'"{key}"' not in wire


def test_model_text_keeps_whole_parseable_lines():
    note = _note("a.py", [_diag(1, index, 0, f"err{index}") for index in range(3)])
    entry = note.files[0]
    text = model_text(cap_display_files([entry]))
    assert "a.py — 3 error(s)" in text
    for line in entry["messages"]:
        assert line in text


# ── turn end: a durable row, not a user bubble ─────────────────────────────────────────────────

def test_turn_end_appends_a_custom_row_and_continues(monkeypatch):
    from agent.turn_stop_gates import apply_stop_gates

    path = r"C:\repo\c.py"
    notes = [(path, _note(path, [_diag(1, 4, 0, "MOCK001")]))]
    monkeypatch.setattr(deferred, "settle_for_agent", lambda agent, **_: list(notes))
    monkeypatch.setattr(lsp_delivery, "custom_messages_enabled", lambda: True)
    notified = []
    monkeypatch.setattr("agent.plugin_events.notify_plugin_message", lambda agent, row: notified.append(row))

    agent = SimpleNamespace(
        session_id="S-end", _current_task_id="t-S-end", _lsp_turn_end_notes=0,
        _interim_content_was_streamed=lambda _text: False,
        _emit_interim_assistant_message=lambda _msg: None,
    )

    def flush(messages, _history=None):
        for index, message in enumerate(messages):
            message.setdefault("_row_id", 1000 + index)

    agent._flush_messages_to_session_db = flush
    messages = [{"role": "user", "content": "edit c.py"}]
    verdict = apply_stop_gates(
        agent, {"role": "assistant", "content": "done"}, final_response="done", messages=messages,
        conversation_history=[], pending_verification_response=None,
        pending_verification_response_previewed=False)

    assert verdict.continue_turn is True
    row = messages[-1]
    assert row["role"] == "custom" and row["custom_type"] == "lsp-late-diagnostic"
    assert "MOCK001" in row["content"]
    assert not any(m["role"] == "user" for m in messages[1:]), messages
    assert notified == [row]
    assert agent._session_messages is messages


def test_turn_end_merges_every_pending_path_into_one_row(monkeypatch):
    from agent.turn_stop_gates import apply_stop_gates

    notes = [(path, _note(path, [_diag(1, index, 0, f"boom-{path}")]))
             for index, path in enumerate(("a.py", "b.py"))]
    monkeypatch.setattr(deferred, "settle_for_agent", lambda agent, **_: list(notes))
    monkeypatch.setattr(lsp_delivery, "custom_messages_enabled", lambda: True)
    monkeypatch.setattr("agent.plugin_events.notify_plugin_message", lambda agent, row: None)
    agent = SimpleNamespace(
        session_id="S-multi", _current_task_id=None, _lsp_turn_end_notes=0,
        _interim_content_was_streamed=lambda _text: False,
        _emit_interim_assistant_message=lambda _msg: None,
        _flush_messages_to_session_db=lambda *_a: None,
    )
    messages = [{"role": "user", "content": "edit both"}]

    verdict = apply_stop_gates(
        agent, {"role": "assistant", "content": "done"}, final_response="done", messages=messages,
        conversation_history=[], pending_verification_response=None,
        pending_verification_response_previewed=False)

    assert verdict.continue_turn is True
    rows = [message for message in messages if message.get("custom_type") == "lsp-late-diagnostic"]
    assert len(rows) == 1
    assert [entry["path"] for entry in rows[0]["details"]["files"]] == ["a.py", "b.py"]
    assert agent._lsp_turn_end_notes == 1


def test_a_turn_start_row_is_announced_only_after_its_flush(monkeypatch):
    notified = []
    monkeypatch.setattr("agent.plugin_events.notify_plugin_message", lambda agent, row: notified.append(row))
    agent = SimpleNamespace()
    messages = [{"role": "user", "content": "edit a.py"}]
    row = lsp_delivery.insert_row(
        agent, messages, [("a.py", _note("a.py", [_diag(1, 0, 0, "boom")]))], 1)
    assert row is not None and messages[-1] is row
    lsp_delivery.notify_ready(agent)
    assert notified == []  # no durable id yet: never synthesise one
    for index, message in enumerate(messages):
        message.setdefault("_row_id", 100 + index)
    lsp_delivery.notify_ready(agent)
    assert notified == [row]


def test_mode_late_budget_hit_note_does_not_claim_it_arrived_late(monkeypatch):
    """D12: a verdict the policy moved to the late surface in time must not say it answered after."""
    import concurrent.futures

    from agent.lsp.display import SourceDisplay
    from agent.lsp.edit_check import CheckResult
    from tools.file_operations_lint import _queue_late_verdict

    def verdict():
        future = concurrent.futures.Future()
        future.set_result(CheckResult(diagnostics=[_diag(1, 0, 0, "boom")], baseline_text=None))
        return future

    _queue_late_verdict("S-budget", "/w/a.py", verdict(), (), SourceDisplay(), budget_hit=True)
    note = deferred.take("S-budget", "/w/a.py")
    assert note is not None and "answered after" not in note.text
    assert "introduced by your edit of /w/a.py" in note.text

    _queue_late_verdict("S-budget", "/w/a.py", verdict(), (), SourceDisplay())
    late_note = deferred.take("S-budget", "/w/a.py")
    assert late_note is not None and "answered after" in late_note.text


def test_post_merges_the_older_note_and_keeps_the_newer_payload_per_path():
    old = file_entry("a.py", [_diag(1, 0, 0, "old")], severities=frozenset({1}))
    new = file_entry("a.py", [_diag(1, 0, 0, "new")], severities=frozenset({1}))
    assert old is not None and new is not None
    deferred.post("S-merge", "a.py", "<old>", "baseline", [old])
    deferred.post("S-merge", "a.py", "<new>", None, [new])
    note = deferred.take("S-merge", "a.py")
    assert note is not None
    assert note.text == "<old>\n\n<new>" and note.baseline_text == "baseline"
    assert len(note.files) == 1 and "new" in note.files[0]["messages"][0]


@pytest.mark.platforms("windows")
def test_windows_case_variants_share_one_queue_entry():
    deferred.post("S-case", r"C:\Repo\a.py", "<one>", None)
    deferred.post("S-case", r"c:\repo\A.py", "<two>", None)
    (path, note), = deferred.pop_notes("S-case")
    assert path == r"c:\repo\A.py"  # the newest caller's spelling, for card matching
    assert note.text == "<one>\n\n<two>" and note.path == path


@pytest.mark.platforms("windows")
def test_take_matches_a_windows_case_variant():
    deferred.post("S-case-take", r"C:\Repo\a.py", "<one>", None)
    note = deferred.take("S-case-take", r"c:\repo\A.PY")
    assert note is not None and note.path == r"C:\Repo\a.py"
    assert deferred.pop_notes("S-case-take") == []


def test_a_symlink_and_its_target_share_one_queue_entry(tmp_path):
    target = tmp_path / "real.py"
    target.write_text("x = 1\n", encoding="utf-8")
    link = tmp_path / "link.py"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot create symlinks")
    deferred.post("S-link", str(link), "<via link>", None)
    deferred.post("S-link", str(target), "<via target>", None)
    (path, note), = deferred.pop_notes("S-link")
    assert path == str(target) and note.text == "<via link>\n\n<via target>"


def test_custom_rows_gate_keeps_legacy_sessions_on_the_live_note_queue(monkeypatch):
    entry = file_entry("a.py", [_diag(1, 0, 0, "boom")], severities=frozenset({1}))
    assert entry is not None
    agent = SimpleNamespace(session_id="S-take", _current_task_id=None)

    deferred.post("S-take", "a.py", "<legacy>", None, [entry])
    monkeypatch.setattr(lsp_delivery, "custom_messages_enabled", lambda: False)
    assert deferred.custom_rows_enabled() is False
    deferred.forward_to_agent(agent)
    assert "lsp:a.py" in agent._plugin_live_notes  # legacy: rides the next tool result

    deferred.post("S-take", "a.py", "<legacy>", None, [entry])
    monkeypatch.setattr(lsp_delivery, "custom_messages_enabled", lambda: True)
    assert deferred.custom_rows_enabled() is True
    (path, note), = deferred.pop_for_agent(agent)
    assert path == "a.py" and note.files[0]["path"] == "a.py"


def test_custom_row_announces_only_for_omp_ui_with_a_durable_id(monkeypatch):
    from tui_gateway.agent_callbacks import plugin_row_event

    row = lsp_delivery.build_row([("a.py", _note("a.py", [_diag(1, 0, 0, "boom")]))])
    assert row is not None
    row["_row_id"] = 42
    monkeypatch.setattr(lsp_delivery, "live_event_allowed", lambda: True)
    event = plugin_row_event(row)
    assert event is not None
    name, payload = event
    message = payload["message"]
    assert isinstance(message, dict)
    assert name == "transcript.custom"
    assert message["role"] == "custom"
    assert message["row_id"] == 42
    assert message["custom_type"] == "lsp-late-diagnostic"

    monkeypatch.setattr(lsp_delivery, "live_event_allowed", lambda: False)
    assert plugin_row_event(row) is None
    monkeypatch.setattr(lsp_delivery, "live_event_allowed", lambda: True)
    row.pop("_row_id")
    assert plugin_row_event(row) is None
