"""``agent_out``: transcript, bundle artifacts, ast summary and inbox (ADR-16 P2/D5)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import run_agent

from agent import agent_loop_service, run_artifacts, run_transcript
from tools import agent_out_tool, agent_tool, read_summary
from tools.registry import registry


class _FakeAgent:
    """Stands in for ``run_agent.AIAgent``: answers deterministically and closes."""

    instances: list[_FakeAgent] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.tools: list = []
        self.history = None
        _FakeAgent.instances.append(self)

    def run_conversation(self, message, system_message=None, conversation_history=None,
                         task_id=None):
        self.history = conversation_history
        prompt = message.split("Complete assignment thoroughly:\n\n", 1)[-1]
        prompt = prompt.split("\n\n--- context ---")[0]
        return {
            "final_response": f"answered: {prompt}",
            "api_calls": 1,
            "messages": [{"role": "user", "content": prompt},
                         {"role": "assistant", "content": f"answered: {prompt}"}],
        }

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _fake_route_and_agent(monkeypatch):
    from agent import agent_run

    _FakeAgent.instances.clear()
    monkeypatch.setattr(agent_run, "_resolve_route",
                        lambda model, provider, base_url="", api_key="": (
                            "fake-model", {"provider": "fake", "api_key": "k"}))
    monkeypatch.setattr(run_agent, "AIAgent", _FakeAgent)
    agent_loop_service.reset_service()
    yield
    agent_loop_service.reset_service()


def _run(**args) -> dict:
    raw = registry.dispatch("agent", args, session_id="main")
    return json.loads(raw) if isinstance(raw, str) else raw


def _read(**args) -> dict:
    raw = registry.dispatch("agent_out", args)
    return json.loads(raw) if isinstance(raw, str) else raw


def _row(agent_session_id: str) -> dict:
    from hermes_state_registry import acquire, release_or_close

    db = acquire()
    try:
        row = db.get_agent_session(agent_session_id)
    finally:
        release_or_close(db)
    assert row is not None
    return row


def _python_source(body_lines: int = 140) -> str:
    body = "\n".join(f"    value_{i} = {i}" for i in range(body_lines))
    return ('"""Module docstring."""\nimport os\n\ndef compute(value):\n'
            f"{body}\n    return value_0\n")


def test_history_is_the_default_mode_and_reads_that_session_only():
    first = _run(agent_name="researcher", user_prompts="first", background=False)
    second = _run(agent_name="researcher", user_prompts="second", background=False)

    first_events = _read(agent_session_id=first["agent_session_id"])
    second_events = _read(mode="history", agent_session_id=second["agent_session_id"])

    first_text = [event.get("text") for event in first_events["events"]]
    second_text = [event.get("text") for event in second_events["events"]]
    assert first_events["mode"] == "history"
    assert "first" in first_text and "second" not in first_text
    assert "second" in second_text and "first" not in second_text
    assert first_events["event_count"] == len(first_events["events"])
    assert first_events["truncated"] is False


def test_settle_writes_the_report_verbatim_and_an_ast_summary():
    run = _run(agent_name="researcher", user_prompts="Find the cause.", background=False,
               subject="cause analysis")

    row = _row(run["agent_session_id"])
    manifest = row["artifact_manifest"]
    report = Path(manifest["report_path"]).read_text(encoding="utf-8")
    assert report == row["final_response"] == "answered: Find the cause."

    ast = json.loads(Path(manifest["ast_summary_path"]).read_text(encoding="utf-8"))
    assert ast["agent_session_id"] == run["agent_session_id"]
    assert ast["subject"] == "cause analysis"
    assert ast["sources"] == [] and ast["reason"] == "no_structural_sources"
    assert Path(manifest["bundle_dir"]) == Path(run["transcript_path"]).parent
    assert Path(manifest["tool_artifacts_dir"]).name == "cause_analysis"


def test_ast_summary_is_written_even_when_read_summarize_is_disabled():
    run = _run(agent_name="researcher", user_prompts="go", background=False)

    data = _read(mode="ast_summary", agent_session_id=run["agent_session_id"])

    assert data["ast_summary"]["version"] == 1
    assert data["ast_summary"]["sources"] == []
    assert data["ast_summary"]["reason"] == "no_structural_sources"
    assert data["path"] == data["ast_summary_path"]


def test_artifact_reports_the_bundle_files():
    run = _run(agent_name="researcher", user_prompts="go", background=False)

    identity = _read(mode="history", agent_session_id=run["agent_session_id"])
    tool_dir = Path(identity["tool_artifacts_dir"])
    (tool_dir / "1.terminal.log").write_text("FULL\n", encoding="utf-8")
    (tool_dir / "1.terminal-original.log").write_text("FULL\nMORE\n", encoding="utf-8")

    report = _read(mode="artifact", agent_session_id=run["agent_session_id"])
    assert report["kind"] == "report" and report["content"] == "answered: go"

    tool = _read(mode="artifact", agent_session_id=run["agent_session_id"], kind="tool",
                 step=1, tool_name="terminal")
    assert tool["content"] == "FULL\n"
    assert tool["original_content"] == "FULL\nMORE\n" and tool["truncated"] is True
    assert tool["path"] == str(tool_dir / "1.terminal.log")

    missing = _read(mode="artifact", agent_session_id=run["agent_session_id"], kind="tool",
                    step=99, tool_name="terminal")
    assert missing["content"] is None and "no archived output" in missing["note"]

    listed = _read(mode="artifact", agent_session_id=run["agent_session_id"], kind="all")
    kinds = {entry["kind"] for entry in listed["files"]}
    assert {"report", "ast_summary", "transcript", "tool"} <= kinds


def test_artifact_tool_requires_step_and_tool_name():
    run = _run(agent_name="researcher", user_prompts="go", background=False)

    no_name = _read(mode="artifact", agent_session_id=run["agent_session_id"], kind="tool",
                    step=1)
    no_step = _read(mode="artifact", agent_session_id=run["agent_session_id"], kind="tool",
                    tool_name="terminal")

    assert "tool_name" in no_name["error"]
    assert "step" in no_step["error"]


def test_a_conversation_session_is_refused_by_artifact_mode():
    from hermes_state_registry import acquire, release_or_close

    db = acquire()
    try:
        db.create_session(session_id="main-conversation", source="cli")
    finally:
        release_or_close(db)

    data = _read(mode="artifact", agent_session_id="main-conversation")

    assert "conversation" in data["error"] and "named agent sessions" in data["error"]


def test_a_legacy_transcript_path_is_still_readable():
    from hermes_state_registry import acquire, release_or_close

    legacy = run_transcript.path_for("researcher", "legacy-run")
    legacy.parent.mkdir(parents=True, exist_ok=True)
    run_transcript.record("researcher", "legacy-run",
                          [{"role": "user", "content": "old question"}], path=legacy)
    db = acquire()
    try:
        db.begin_agent_session(agent_session_id="legacy-run", agent_name="researcher",
                               subject="old run", transcript_path=str(legacy))
    finally:
        release_or_close(db)

    history = _read(mode="history", agent_session_id="legacy-run")
    artifact = _read(mode="artifact", agent_session_id="legacy-run")

    assert [event["text"] for event in history["events"] if event.get("kind") == "msg"] \
        == ["old question"]
    assert artifact["content"] is None and "no report" in artifact["note"]


def test_conversation_tool_artifact_reads_the_recorded_archive(tmp_path):
    """ADR-4 D15: a main conversation's tool output is read from display_metadata, never guessed."""
    import json as _json

    from hermes_state_registry import acquire, release_or_close

    log = tmp_path / "1.execute_code.log"
    original = tmp_path / "1.execute_code-original.log"
    log.write_text("shown head\ntail\n", encoding="utf-8")
    original.write_text("FULL original output\n", encoding="utf-8")
    db = acquire()
    try:
        db.create_session(session_id="conv-artifacts", source="cli")
        db.append_messages_batch("conv-artifacts", [{
            "role": "tool", "tool_name": "execute_code", "tool_call_id": "call-1",
            "content": _json.dumps({"status": "success", "output": "shown head\ntail\n"}),
            "display_metadata": {"tool_output_artifacts": {
                "step": 1, "tool": "execute_code", "artifact_path": str(log),
                "artifact_original_path": str(original), "truncated": True,
            }},
        }])
    finally:
        release_or_close(db)

    data = _read(mode="artifact", kind="tool", session_id="conv-artifacts",
                 tool_call_id="call-1")
    assert data["content"] == "shown head\ntail\n"
    assert data["original_content"] == "FULL original output\n"
    assert data["truncated"] is True and data["path"] == str(log)

    by_step = _read(mode="artifact", kind="tool", session_id="conv-artifacts",
                    step=1, tool_name="execute_code")
    assert by_step["tool_call_id"] == "call-1"


def test_conversation_tool_artifact_falls_back_to_stdout_spill(tmp_path):
    import json as _json

    from hermes_constants import get_hermes_dir
    from hermes_state_registry import acquire, release_or_close

    spill_dir = get_hermes_dir("cache/exec", "exec_spill")
    spill_dir.mkdir(parents=True, exist_ok=True)
    spill = spill_dir / f"stdout-{tmp_path.name}.txt"
    spill.write_text("full spilled output\n", encoding="utf-8")
    db = acquire()
    try:
        db.create_session(session_id="conv-spill", source="cli")
        db.append_messages_batch("conv-spill", [{
            "role": "tool", "tool_name": "execute_code", "tool_call_id": "call-2",
            "content": _json.dumps({"status": "success", "output": "truncated",
                                    "stdout_spill_path": str(spill)}),
        }])
    finally:
        release_or_close(db)

    data = _read(mode="artifact", kind="tool", session_id="conv-spill",
                 tool_call_id="call-2")
    assert data["path"] == str(spill)
    assert data["content"] == "full spilled output\n"


def test_conversation_spill_fallback_refuses_arbitrary_host_files(tmp_path):
    import json as _json

    from hermes_state_registry import acquire, release_or_close

    secret = tmp_path / "secret.txt"
    secret.write_text("do not read me\n", encoding="utf-8")
    db = acquire()
    try:
        db.create_session(session_id="conv-unsafe", source="cli")
        db.append_messages_batch("conv-unsafe", [{
            "role": "tool", "tool_name": "execute_code", "tool_call_id": "call-3",
            "content": _json.dumps({"status": "success",
                                    "stdout_spill_path": str(secret)}),
        }])
    finally:
        release_or_close(db)

    data = _read(mode="artifact", kind="tool", session_id="conv-unsafe",
                 tool_call_id="call-3")
    assert data["content"] is None
    assert "missing or unreadable" in data["note"]


def test_conversation_artifact_identity_is_mutually_exclusive():
    data = _read(mode="artifact", kind="tool", session_id="conv-artifacts",
                 agent_session_id="agent-1")

    assert "mutually exclusive" in data["error"]


def test_identity_must_be_exactly_one_of_id_and_subject():
    both = _read(mode="history", agent_session_id="a", subject="b")
    neither = _read(mode="history")

    assert "exactly one" in both["error"]
    assert "exactly one" in neither["error"]


def test_an_unknown_identity_is_an_error():
    data = _read(mode="history", agent_session_id="researcher-missing")

    assert "no agent session" in data["error"]


def test_a_repeated_subject_is_disambiguated_and_each_row_stays_addressable():
    first = _run(agent_name="researcher", user_prompts="one", background=False,
                 subject="same topic")
    second = _run(agent_name="researcher", user_prompts="two", background=False,
                  subject="same topic")

    assert second["subject"] == "same topic (2)"
    assert _read(mode="history", subject="same topic")["agent_session_id"] \
        == first["agent_session_id"]
    assert _read(mode="history", subject="same topic (2)")["agent_session_id"] \
        == second["agent_session_id"]
    # Each stored subject names its own artifacts: no file is shared.
    assert Path(second["transcript_path"]).name == "same_topic__2_.jsonl"


def test_a_running_row_has_no_report_or_ast_yet():
    from hermes_state_registry import acquire, release_or_close

    db = acquire()
    try:
        db.begin_agent_session(agent_session_id="researcher-running", agent_name="researcher",
                               subject="still running", transcript_path="nowhere.jsonl")
    finally:
        release_or_close(db)

    report = _read(mode="artifact", agent_session_id="researcher-running")
    ast = _read(mode="ast_summary", agent_session_id="researcher-running")

    assert report["content"] is None and "no report" in report["note"]
    assert ast["ast_summary"] is None and "no ast summary" in ast["note"]


def test_settle_aggregates_collected_summaries_and_legacy_summaries(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    transcript = bundle / "topic.jsonl"
    transcript.write_text("", encoding="utf-8")
    run = SimpleNamespace(agent_name="researcher", agent_session_id="settle-run",
                          subject="topic", bundle=bundle, path=transcript,
                          project_dir=str(tmp_path), output_schema=None)

    legacy_dir = run_transcript.summary_dir("researcher", "settle-run")
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "old.md").write_text("OLD SUMMARY", encoding="utf-8")
    (legacy_dir / "index.json").write_text(json.dumps({"summaries": [{
        "source": "src/old.py", "file": "old.md", "kept_lines": 2,
        "total_lines": 10, "elided_ranges": ["3-10"]}]}), encoding="utf-8")

    read_summary.set_read_summarize("settle-run", True, agent_name="researcher",
                                    agent_session_id="settle-run")
    try:
        summary = read_summary.ReadSummary(content="NEW SUMMARY", total_lines=8,
                                           elided=((2, 8),), kept_lines=1)
        read_summary.persist_summary("settle-run", "src/new.py", summary)
        manifest = run_artifacts.settle_bundle(run, "completed", "the report")
    finally:
        read_summary.clear_read_summarize("settle-run")

    assert manifest is not None and manifest["ast_summary_written"] is True
    ast = json.loads(Path(manifest["ast_summary_path"]).read_text(encoding="utf-8"))
    by_source = {entry["source"]: entry for entry in ast["sources"]}
    assert by_source["src/new.py"]["summary"] == "NEW SUMMARY"
    assert by_source["src/old.py"]["summary"] == "OLD SUMMARY"
    assert "reason" not in ast
    assert not legacy_dir.exists(), "the legacy directory is removed after a verified write"


def test_settle_derives_sources_from_the_transcript_when_none_were_collected(tmp_path):
    source = tmp_path / "sample.py"
    source.write_text(_python_source(), encoding="utf-8")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    transcript = bundle / "topic.jsonl"
    run_transcript.record("researcher", "derived-run", [{
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "t1", "name": "read_file",
                        "args": json.dumps({"path": str(source)})}],
    }], path=transcript)
    run = SimpleNamespace(agent_name="researcher", agent_session_id="derived-run",
                          subject="topic", bundle=bundle, path=transcript,
                          project_dir=str(tmp_path), output_schema=None)

    manifest = run_artifacts.settle_bundle(run, "completed", "report")

    assert manifest is not None
    ast = json.loads(Path(manifest["ast_summary_path"]).read_text(encoding="utf-8"))
    assert [entry["source"] for entry in ast["sources"]] == [str(source)]
    assert "def compute(value):" in ast["sources"][0]["summary"]


def test_settle_writes_the_structured_output_sidecar(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    transcript = bundle / "topic.jsonl"
    transcript.write_text("", encoding="utf-8")
    run = SimpleNamespace(agent_name="researcher", agent_session_id="structured-run",
                          subject="topic", bundle=bundle, path=transcript,
                          project_dir=str(tmp_path),
                          output_schema={"type": "object", "required": ["x"],
                                         "properties": {"x": {"type": "integer"}}})

    manifest = run_artifacts.settle_bundle(run, "completed", 'here: {"x": 3}')

    assert manifest is not None and manifest["structured_output_written"] is True
    assert json.loads(Path(manifest["structured_output_path"]).read_text(encoding="utf-8")) \
        == {"x": 3}


def test_inbox_roster_is_scoped_to_the_spawn_tree(monkeypatch):
    from tools import agent_inbox

    monkeypatch.setattr(agent_inbox, "live_named_sessions", lambda: [
        {"agent_session_id": "sibling", "parent_session_id": "parent"},
        {"agent_session_id": "foreign", "parent_session_id": "other-parent"},
    ])
    monkeypatch.setattr(agent_inbox, "read_inbox", lambda *_a, **_k: [])
    monkeypatch.setattr(agent_inbox, "count_inbox", lambda *_a, **_k: 0)
    monkeypatch.setattr(agent_inbox, "session_lifecycle",
                        lambda _row: {"live": False, "parked": False})

    payload = agent_out_tool._inbox_payload(
        {"agent_session_id": "self", "parent_session_id": "parent"},
        {"agent_session_id": "self"})

    assert [peer["agent_session_id"] for peer in payload["peers"]] == ["sibling"]


# ── AS-6: the manifest owns the artifact identity ────────────────────────────────────────────


def test_as6_identity_for_row_prefers_the_manifest_and_survives_a_label_change():
    run = _run(agent_name="researcher", background=False,
               user_prompts="## Target\nInvestigate pool exhaustion")
    agent_session_id = run["agent_session_id"]
    row = _row(agent_session_id)
    manifest = row["artifact_manifest"]

    identity = run_artifacts.identity_for_row(row)

    assert identity["report_path"] == manifest["report_path"]
    assert identity["ast_summary_path"] == manifest["ast_summary_path"]
    assert identity["structured_output_path"] == manifest["structured_output_path"]
    assert identity["tool_artifacts_dir"] == manifest["tool_artifacts_dir"]
    assert identity["bundle_dir"] == manifest["bundle_dir"]

    from hermes_state_registry import acquire, release_or_close

    db = acquire()
    try:
        assert db.set_agent_session_title(agent_session_id, "Investigate pool exhaustion") is True
    finally:
        release_or_close(db)

    assert run_artifacts.identity_for_row(_row(agent_session_id)) == identity
