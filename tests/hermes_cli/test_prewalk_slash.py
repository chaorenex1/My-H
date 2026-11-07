"""ADR-12 D5: `/prewalk` dispatches to the session coordinator, local surfaces only."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli import cli_prewalk_command as mixin
from hermes_cli.cli_commands_mixin import CLICommandsMixin


@pytest.fixture
def printed(monkeypatch):
    lines: list[str] = []
    monkeypatch.setattr(mixin, "_cp", lambda *parts: lines.extend(parts) or lines)
    monkeypatch.setattr(mixin, "_accent_line", lambda text: text)
    monkeypatch.setattr(mixin, "_dim_line", lambda text: text)
    return lines


def _call(cmd: str, *, platform="cli", agent=object()):
    host = SimpleNamespace(platform=platform, agent=agent)
    return CLICommandsMixin._handle_prewalk_command(host, cmd)


def test_status_arms_and_off_reach_the_coordinator(monkeypatch, printed):
    calls: list[tuple] = []
    monkeypatch.setattr("agent.prewalk.status", lambda agent: {
        "armed": False, "consumed": False, "from": "@default", "into": "@smol",
        "gate": "closed", "auto_mode": "off"},
        raising=False)
    monkeypatch.setattr("agent.prewalk.disarm", lambda agent: calls.append(("off", agent)) or {
        "armed": False, "consumed": False, "from": "@default", "into": "@smol",
        "gate": "closed", "auto_mode": "off"},
        raising=False)

    _call("/prewalk")
    assert any("prewalk: off" in line for line in printed)

    _call("/prewalk off")
    assert calls and calls[0][0] == "off"


def test_restart_and_arm_pass_target_and_source(monkeypatch, printed):
    calls: list[tuple] = []

    def fake(action):
        def run(agent, *, into=None, from_=None):
            calls.append((action, into, from_))
            return {"armed": True, "consumed": False, "from": from_ or "@default",
                    "into": into or "@smol", "gate": "closed", "auto_mode": "on"}
        return run

    monkeypatch.setattr("agent.prewalk.restart", fake("restart"), raising=False)
    monkeypatch.setattr("agent.prewalk.arm", fake("arm"), raising=False)

    _call("/prewalk restart fast-model @default")
    _call("/prewalk fast-model")
    assert calls == [("restart", "fast-model", "@default"), ("arm", "fast-model", None)]
    assert any(line.startswith("prewalk: armed") for line in printed)


def test_remote_platforms_and_missing_agent_are_refused(monkeypatch, printed):
    monkeypatch.setattr("agent.prewalk.disarm", lambda agent: pytest.fail("must not run"), raising=False)
    assert _call("/prewalk off", platform="telegram") is not None
    assert "local-only" in printed[-1]

    assert _call("/prewalk off", agent=None) is not None
    assert "No live agent" in printed[-1]
