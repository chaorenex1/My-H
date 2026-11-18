"""ADR-9 P2: Semgrep is an immediate diagnostic source beside LSP.

The P1 invariants (one custom row, one live event, resume === live) are covered in
``test_late_diagnostics_row.py``; these cover the P2 additions: findings map to pi-tui-parseable
lines on the file's absolute path, a write inlines a fast scan and queues a slow one in the same
``deferred`` note as the LSP verdict, and a newer write supersedes stale findings.
"""
from __future__ import annotations

import re

import pytest

from tools.environments.local import LocalEnvironment
from tools.file_operations import ShellFileOperations

# pi-tui's ``render-utils.ts::parseDiagnosticMessage`` (18.14.1).
_PARSE = re.compile(
    r"^(.+?):(\d+):(\d+)\s+\[(\w+)\]\s+(?:\[([^\]]+)\]\s+)?(.+?)(?:\s+\(([^)]+)\))?$")

_CONFIG = {
    "lsp": {"display": {"semgrep": {"mode": "both", "severities": ["error", "warning"]}}},
    "code_intel": {"enabled": True, "semgrep": {"enabled": True, "mode": "auto"}},
}


def _scan_result(path: str, *, severity: str = "error", line: int = 3, col: int = 1,
                 message: str = "Unsafe use of eval.", rule: str = "python.lang.security.audit.eval"):
    return {
        "status": "ok",
        "findings": [{
            "path": path,
            "rule_id": rule,
            "severity": severity,
            "message": message,
            "start_line": line,
            "start_column": col,
            "end_line": line,
            "end_column": col + 2,
        }],
    }


def _lsp_diag(severity: int, line: int, col: int, message: str) -> dict:
    return {"severity": severity, "message": message, "source": "pyright", "code": "reportX",
            "range": {"start": {"line": line, "character": col},
                      "end": {"line": line, "character": col + 1}}}


@pytest.fixture
def semgrep_on(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: _CONFIG)


@pytest.fixture
def generous_budget(monkeypatch):
    """The scan is faked in-process; do not let the 80ms write budget turn it into a race."""
    monkeypatch.setattr("agent.lsp.edit_check.WRITE_BUDGET", 5.0)
    monkeypatch.setattr("agent.lsp.edit_check.INLINE_GRACE", 5.0)


# ── mapping: findings -> the display line pi-tui parses ────────────────────────────────────────

def test_findings_map_to_parseable_lines_on_the_absolute_path(tmp_path):
    from agent.lsp import semgrep_check
    from agent.lsp.reporter import file_entry

    target = str(tmp_path / "a.py")
    diagnostics = semgrep_check.diagnostics_from_scan(
        target, _scan_result("a.py"), severities=frozenset({1, 2}))

    entry = file_entry(target, diagnostics, severities=frozenset({1, 2}))
    assert entry is not None
    assert entry["path"] == target
    assert entry["summary"] == "1 error(s)" and entry["errored"] is True
    assert entry["messages"] == [
        f"{target}:3:1 [error] [semgrep] Unsafe use of eval. (python.lang.security.audit.eval)"]
    parsed = _PARSE.match(entry["messages"][0])
    assert parsed is not None, entry["messages"][0]
    assert parsed.group(1) == target  # filePath is byte-identical to files[].path
    assert parsed.group(3) == "1" and parsed.group(4) == "error"
    assert parsed.group(5) == "semgrep" and parsed.group(7).endswith("eval")


def test_severity_filter_and_foreign_paths_are_dropped(tmp_path):
    from agent.lsp import semgrep_check

    target = str(tmp_path / "a.py")
    warning = semgrep_check.diagnostics_from_scan(
        target, _scan_result("a.py", severity="warning"), severities=frozenset({1}))
    assert warning == []
    foreign = semgrep_check.diagnostics_from_scan(
        target, _scan_result("b.py"), severities=frozenset({1, 2}))
    assert foreign == []
    failed = semgrep_check.diagnostics_from_scan(
        target, {"status": "failed", "findings": []}, severities=frozenset({1, 2}))
    assert failed == []


# ── write path: inline within the budget ───────────────────────────────────────────────────────

def test_write_file_inlines_semgrep_with_its_own_result_field(tmp_path, semgrep_on, generous_budget,
                                                              monkeypatch):
    fops = ShellFileOperations(LocalEnvironment(cwd=str(tmp_path)))
    target = tmp_path / "x.py"
    monkeypatch.setattr(fops, "_maybe_lsp_diagnostics", lambda *a, **k: "")
    monkeypatch.setattr("hermes_codegraph.analysis.semgrep_scan",
                        lambda *a, **k: _scan_result("x.py"))

    result = fops.write_file(str(target), "x = eval('1')\n")

    assert result.error is None
    assert result.lsp_diagnostics is None
    assert result.semgrep_diagnostics is not None
    assert "[semgrep]" in result.semgrep_diagnostics
    assert "Unsafe use of eval." in result.semgrep_diagnostics
    assert "lsp_diagnostics" not in result.to_dict()


def test_relative_write_resolves_against_the_backend_cwd(tmp_path, semgrep_on, monkeypatch):
    """A gateway session's cwd is not the process cwd; the scan identity must be the file written."""
    from agent.lsp import semgrep_check

    fops = ShellFileOperations(LocalEnvironment(cwd=str(tmp_path)))
    monkeypatch.setattr(fops, "_maybe_lsp_diagnostics", lambda *a, **k: "")
    seen = []
    monkeypatch.setattr(semgrep_check, "start_scan",
                        lambda abs_path, digest=None: seen.append(abs_path) or None)

    result = fops.write_file("sub/x.py", "x = 1\n")

    assert result.error is None
    assert seen == [str(tmp_path / "sub" / "x.py")]


def test_patch_replace_propagates_semgrep(tmp_path, semgrep_on, generous_budget, monkeypatch):
    fops = ShellFileOperations(LocalEnvironment(cwd=str(tmp_path)))
    target = tmp_path / "x.py"
    target.write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(fops, "_maybe_lsp_diagnostics", lambda *a, **k: "")
    monkeypatch.setattr("hermes_codegraph.analysis.semgrep_scan",
                        lambda *a, **k: _scan_result("x.py"))

    result = fops.patch_replace(str(target), "x = 1", "x = eval('1')")

    assert result.success is True
    assert result.semgrep_diagnostics is not None
    assert "[semgrep]" in result.semgrep_diagnostics
    assert "semgrep_diagnostics" in result.to_dict()


def test_a_slow_scan_lands_in_the_deferred_queue(tmp_path, semgrep_on, monkeypatch):
    """Beyond the shared budget the verdict is not dropped: it is queued under the editing session
    and reaches the next delivery point (ADR-9 D8)."""
    import threading

    from agent.lsp import deferred
    from tests.waiting import wait_until

    fops = ShellFileOperations(LocalEnvironment(cwd=str(tmp_path)))
    monkeypatch.setattr(fops, "_maybe_lsp_diagnostics", lambda *a, **k: "")
    monkeypatch.setattr("agent.lsp.edit_check.WRITE_BUDGET", 0.01)
    monkeypatch.setattr("agent.lsp.edit_check.INLINE_GRACE", 0.01)
    release = threading.Event()

    def slow_scan(*_args, **_kwargs):
        release.wait(10)
        return _scan_result("x.py")

    monkeypatch.setattr("hermes_codegraph.analysis.semgrep_scan", slow_scan)

    with deferred.bind_owner("S-late"):
        result = fops.write_file(str(tmp_path / "x.py"), "x = eval('1')\n")

    assert result.semgrep_diagnostics is None
    release.set()
    (path, note), = wait_until(lambda: deferred.pop_notes("S-late"), timeout=10,
                               what="the late Semgrep verdict")
    assert path == str(tmp_path / "x.py")
    assert "[semgrep]" in note.semgrep_text and "earlier edit" in note.semgrep_text
    assert note.semgrep_files and "[semgrep]" in note.semgrep_files[0]["messages"][0]


def test_concurrent_scans_are_bounded(monkeypatch, tmp_path, semgrep_on):
    """A batch of edits must not spawn one Semgrep process per write: the scan slots cap what runs
    at once, and queued scans still resolve their own futures."""
    import threading

    from agent.lsp import semgrep_check

    active = 0
    peak = 0
    lock = threading.Lock()
    release = threading.Event()
    two_running = threading.Event()

    def fake_scan(*_args, **_kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active >= semgrep_check._MAX_CONCURRENT_SCANS:
                two_running.set()
        release.wait(10)
        with lock:
            active -= 1
        return {"status": "ok", "findings": []}

    monkeypatch.setattr("hermes_codegraph.analysis.semgrep_scan", fake_scan)
    target = str(tmp_path / "x.py")
    futures = [semgrep_check.start_scan(target) for _ in range(4)]
    assert all(future is not None for future in futures)
    assert two_running.wait(5), "the scan slots never admitted the first scans"
    assert peak == semgrep_check._MAX_CONCURRENT_SCANS
    release.set()
    for future in futures:
        future.result(timeout=10)
    assert peak == semgrep_check._MAX_CONCURRENT_SCANS


def test_verdict_for_bytes_another_writer_replaced_is_dropped(tmp_path, semgrep_on, generous_budget,
                                                              monkeypatch):
    """The scan ran after another session rewrote the file: its findings are not this edit's."""
    fops = ShellFileOperations(LocalEnvironment(cwd=str(tmp_path)))
    target = tmp_path / "x.py"
    monkeypatch.setattr(fops, "_maybe_lsp_diagnostics", lambda *a, **k: "")

    def scan(*_args, **_kwargs):
        target.write_text("x = 2\n", encoding="utf-8")  # someone else wins the race
        return _scan_result("x.py")

    monkeypatch.setattr("hermes_codegraph.analysis.semgrep_scan", scan)

    result = fops.write_file(str(target), "x = 1\n")

    assert result.error is None
    assert result.semgrep_diagnostics is None


def test_late_verdict_for_bytes_another_writer_replaced_is_dropped(tmp_path, semgrep_on, monkeypatch):
    import threading

    from agent.lsp import deferred
    from tests.waiting import wait_until

    fops = ShellFileOperations(LocalEnvironment(cwd=str(tmp_path)))
    target = tmp_path / "x.py"
    monkeypatch.setattr(fops, "_maybe_lsp_diagnostics", lambda *a, **k: "")
    monkeypatch.setattr("agent.lsp.edit_check.WRITE_BUDGET", 0.01)
    monkeypatch.setattr("agent.lsp.edit_check.INLINE_GRACE", 0.01)
    release = threading.Event()

    def slow_scan(*_args, **_kwargs):
        release.wait(10)
        target.write_text("x = 2\n", encoding="utf-8")
        return _scan_result("x.py")

    monkeypatch.setattr("hermes_codegraph.analysis.semgrep_scan", slow_scan)

    with deferred.bind_owner("S-stale"):
        result = fops.write_file(str(target), "x = 1\n")
    assert result.semgrep_diagnostics is None
    release.set()
    wait_until(lambda: not deferred._PENDING.get("S-stale"), timeout=10,
               what="the stale scan to settle")
    assert deferred.pop_notes("S-stale") == []


def test_display_mode_off_never_scans(tmp_path, monkeypatch):
    config = {
        "lsp": {"display": {"semgrep": {"mode": "off"}}},
        "code_intel": {"enabled": True, "semgrep": {"enabled": True, "mode": "auto"}},
    }
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: config)
    called = []
    monkeypatch.setattr("hermes_codegraph.analysis.semgrep_scan",
                        lambda *a, **k: called.append(1) or _scan_result("x.py"))
    fops = ShellFileOperations(LocalEnvironment(cwd=str(tmp_path)))
    monkeypatch.setattr(fops, "_maybe_lsp_diagnostics", lambda *a, **k: "")

    result = fops.write_file(str(tmp_path / "x.py"), "x = eval('1')\n")

    assert called == []
    assert result.semgrep_diagnostics is None


# ── late path: one note per path, two source slots ────────────────────────────────────────────

def test_late_semgrep_and_lsp_merge_into_one_row():
    from agent.lsp import deferred, delivery
    from agent.lsp.reporter import file_entry

    path = r"C:\repo\src\app.ts"
    lsp_entry = file_entry(path, [_lsp_diag(1, 11, 4, "type mismatch")], severities=frozenset({1}))
    semgrep_entry = file_entry(path, [{"severity": 2, "message": "Unsafe use of eval.",
                                       "source": "semgrep", "code": "x.eval",
                                       "range": {"start": {"line": 39, "character": 8},
                                                 "end": {"line": 39, "character": 12}}}],
                               severities=frozenset({1, 2}))
    assert lsp_entry is not None and semgrep_entry is not None

    token = deferred.begin_semgrep("S-merge", path)
    deferred.post("S-merge", path, "<lsp block>", None, [lsp_entry])
    deferred.post_semgrep("S-merge", path, token, "<semgrep block>", [semgrep_entry])

    notes = deferred.pop_notes("S-merge")
    assert len(notes) == 1
    note = notes[0][1]
    assert note.legacy_text == "<lsp block>\n\n<semgrep block>"
    row = delivery.build_row(notes)
    assert row is not None
    (entry,) = row["details"]["files"]
    assert entry["path"] == path
    assert entry["summary"] == "1 error(s), 1 warning(s)"
    assert entry["errored"] is True
    assert any("[semgrep]" in message for message in entry["messages"])
    assert "Diagnostics for the files you edited" in row["content"]


def test_a_newer_scan_supersedes_the_older_token():
    from agent.lsp import deferred
    from agent.lsp.reporter import file_entry

    path = "a.py"
    stale = file_entry(path, [{"severity": 1, "message": "stale", "source": "semgrep",
                               "code": "r", "range": {"start": {"line": 0, "character": 0},
                                                      "end": {"line": 0, "character": 1}}}])
    fresh = file_entry(path, [{"severity": 1, "message": "fresh", "source": "semgrep",
                               "code": "r", "range": {"start": {"line": 1, "character": 0},
                                                      "end": {"line": 1, "character": 1}}}])
    assert stale is not None and fresh is not None
    old_token = deferred.begin_semgrep("S-token", path)
    new_token = deferred.begin_semgrep("S-token", path)

    deferred.post_semgrep("S-token", path, old_token, "<old>", [stale])
    assert deferred.pop_notes("S-token") == []  # the old scan lost the race, nothing queued
    deferred.post_semgrep("S-token", path, new_token, "<new>", [fresh])
    (queued_path, note), = deferred.pop_notes("S-token")
    assert queued_path == path and note.semgrep_text == "<new>"


def test_restore_of_a_semgrep_only_carry_queues_nothing():
    """The newer LSP check got no verdict; the carried Semgrep finding describes the bytes that
    write replaced. Restoring it would resurrect stale findings, and an empty note must not be
    queued (turn-end treats any note as a reason to continue the turn)."""
    from agent.lsp import deferred
    from agent.lsp.reporter import file_entry

    path = "a.py"
    entry = file_entry(path, [{"severity": 1, "message": "old", "source": "semgrep", "code": "r",
                               "range": {"start": {"line": 0, "character": 0},
                                         "end": {"line": 0, "character": 1}}}])
    assert entry is not None
    token = deferred.begin_semgrep("S-empty", path)
    deferred.post_semgrep("S-empty", path, token, "<semgrep>", [entry])

    carry = deferred.take("S-empty", path)
    assert carry is not None and carry.semgrep_text == "<semgrep>"
    deferred.restore("S-empty", path, (carry,))

    assert deferred.pop_notes("S-empty") == []


def test_a_new_write_drops_the_stale_semgrep_slot_but_keeps_lsp():
    from agent.lsp import deferred
    from agent.lsp.reporter import file_entry

    path = "a.py"
    entry = file_entry(path, [_lsp_diag(1, 0, 0, "boom")], severities=frozenset({1}))
    assert entry is not None
    deferred.post("S-clear", path, "<lsp>", None, [entry])
    deferred.post_semgrep("S-clear", path, deferred.begin_semgrep("S-clear", path), "<semgrep>",
                          [{"path": path, "summary": "1 warning(s)", "errored": False,
                            "messages": [f"{path}:1:1 [warning] [semgrep] w (r)"]}])

    token = deferred.begin_semgrep("S-clear", path)  # a new write of the same file
    note = deferred.pop_notes("S-clear")[0][1]
    assert note.text == "<lsp>" and note.files
    assert note.semgrep_text == "" and note.semgrep_files == ()
    deferred.finish_semgrep("S-clear", path, token)
