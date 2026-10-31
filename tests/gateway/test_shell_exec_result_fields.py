"""``shell.exec`` answers output metadata next to the legacy stdout/stderr/code trio.

Totals describe what the child wrote (pre-redaction, pre-slice); output_* describe what the caller
receives after redaction and the 4000/2000-character tail slices.
"""
from types import SimpleNamespace

from gateway import session_shell


def _fake_run(monkeypatch, *, stdout, stderr, code=0):
    # Redaction is not under test here; keep the emitted strings byte-identical to the child's.
    monkeypatch.setattr("agent.redact.redact_sensitive_text", lambda text, **kwargs: text)
    monkeypatch.setattr(session_shell.subprocess, 'run', lambda *a, **k: SimpleNamespace(
        stdout=stdout, stderr=stderr, returncode=code))


def test_run_reports_totals_and_combined_output(monkeypatch, tmp_path):
    stdout, stderr = 'line one\nline two\n', 'warn\n'
    _fake_run(monkeypatch, stdout=stdout, stderr=stderr, code=0)
    result = session_shell._run('echo hi', str(tmp_path))
    assert result['stdout'] == stdout
    assert result['stderr'] == stderr
    assert result['code'] == 0
    assert result['output'] == stdout + '\n' + stderr
    assert result['total_bytes'] == len(stdout.encode()) + len(stderr.encode())
    assert result['total_lines'] == 3
    assert result['output_bytes'] == len(result['output'].encode())
    assert result['output_lines'] == len(result['output'].splitlines())
    assert result['truncated'] is False
    assert result['working_dir'] == str(tmp_path)
    assert (result['cancelled'], result['timed_out']) == (False, False)
    assert result['artifact_id'] is None and result['artifact_error'] is None
    assert result['images'] == []


def test_run_flags_truncation_from_the_pre_slice_streams(monkeypatch, tmp_path):
    stdout, stderr = 'a' * 4100, 'b' * 2100
    _fake_run(monkeypatch, stdout=stdout, stderr=stderr)
    result = session_shell._run('big', str(tmp_path))
    assert result['truncated'] is True
    assert len(result['stdout']) == 4000
    assert len(result['stderr']) == 2000
    assert result['total_bytes'] == len(stdout) + len(stderr)
    assert result['total_lines'] == 2
    assert result['output_lines'] == 2
    assert result['output_bytes'] == 4000 + 1 + 2000


def test_run_does_not_flag_an_exactly_full_stream(monkeypatch, tmp_path):
    _fake_run(monkeypatch, stdout='a' * 4000, stderr='b' * 2000)
    result = session_shell._run('edge', str(tmp_path))
    assert result['truncated'] is False


def test_run_with_no_output_keeps_an_empty_string(monkeypatch, tmp_path):
    _fake_run(monkeypatch, stdout='', stderr='')
    result = session_shell._run('true', str(tmp_path))
    assert result['output'] == ''
    assert (result['total_lines'], result['total_bytes']) == (0, 0)
    assert (result['output_lines'], result['output_bytes']) == (0, 0)
