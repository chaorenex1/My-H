"""Acceptance tests for ``agent.title_router`` (sprint-2 title-model-plan, ADR-21 D3).

Contract boundary: every case writes a real ``config.yaml`` under a temporary ``HERMES_HOME`` and
lets the real config loader, the real ``auxiliary.<task>`` readers and the real gates run. The only
fake is ``agent.title_router.call_llm`` (a recorder that answers from a script), so ordering,
dedupe, gating and the executor's call shape are observed exactly as production produces them.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import title_router
from agent.title_router import run_title_plan, title_model_plan
from hermes_yaml import safe_dump

_MAIN_RUNTIME = {
    "provider": "openrouter",
    "model": "main-x",
    "base_url": "https://openrouter.ai/api/v1",
}
_MESSAGES = [{"role": "user", "content": "fix the login button"}]

_DEFAULT_TITLE = {
    "enabled": True,
    "model_upgrade_enabled": True,
    "provider": "auto",
    "model": "",
    "base_url": "",
    "api_key": "",
    "timeout": 31,
}
_UNPINNED_ROLE = {"provider": "auto", "model": "", "base_url": ""}


def _write_config(home: Path, auxiliary: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        safe_dump({"auxiliary": auxiliary}), encoding="utf-8"
    )


def _use_home(monkeypatch, tmp_path: Path, name: str, auxiliary: dict) -> Path:
    """Point the real config loader at a fresh home; a unique path keeps the loader cache honest."""
    home = tmp_path / name
    _write_config(home, auxiliary)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


class _CallLLM:
    """Stand-in for ``agent.title_router.call_llm``: records each call, answers from ``reply_for``."""

    def __init__(self, reply_for=None):
        self.calls: list[dict] = []
        self._reply_for = reply_for or (lambda call: "<title>Fix login button</title>")

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        result = self._reply_for(kwargs)
        if isinstance(result, BaseException):
            raise result
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=result))]
        )

    @property
    def tasks(self) -> list[str]:
        return [call["task"] for call in self.calls]

    @property
    def timeouts(self) -> list[float]:
        return [call["timeout"] for call in self.calls]


def _install(monkeypatch, reply_for=None) -> _CallLLM:
    fake = _CallLLM(reply_for)
    monkeypatch.setattr("agent.title_router.call_llm", fake)
    return fake


def test_as1_default_route_titles_with_the_session_model_once(monkeypatch, tmp_path):
    """AS-1: no pins and no chains → exactly the session model, one call, task-level timeout."""
    _use_home(
        monkeypatch,
        tmp_path,
        "as1",
        {
            "title_generation": _DEFAULT_TITLE,
            "tiny": dict(_UNPINNED_ROLE),
            "commit_message": dict(_UNPINNED_ROLE),
            "smol": dict(_UNPINNED_ROLE),
        },
    )

    plan = title_model_plan(dict(_MAIN_RUNTIME))

    assert len(plan) == 1
    candidate = plan[0]
    assert (
        candidate.task,
        candidate.provider,
        candidate.model,
        candidate.base_url,
    ) == ("title_generation", "openrouter", "main-x", "https://openrouter.ai/api/v1")
    assert candidate.timeout == 31

    fake = _install(monkeypatch)

    assert run_title_plan(plan, _MESSAGES) == "Fix login button"
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["provider"] == "openrouter"
    assert call["model"] == "main-x"
    assert call["base_url"] == "https://openrouter.ai/api/v1"
    assert call["task"] == "title_generation"
    assert call["timeout"] == 31
    assert call["fallback"] is False
    assert call["messages"] == _MESSAGES


def test_as2_pinned_roles_plan_in_tiny_commit_smol_current_order(monkeypatch, tmp_path):
    """AS-2: pinned roles run cheap→capable, each with its own task timeout, current model last."""
    _use_home(
        monkeypatch,
        tmp_path,
        "as2",
        {
            "title_generation": _DEFAULT_TITLE,
            "tiny": {"provider": "openrouter", "model": "tiny-a", "timeout": 11},
            "commit_message": {"provider": "nous", "model": "commit-b", "timeout": 12},
            "smol": {"provider": "openrouter", "model": "smol-c", "timeout": 13},
        },
    )

    plan = title_model_plan(_MAIN_RUNTIME)

    assert [(c.task, c.provider, c.model, c.timeout) for c in plan] == [
        ("tiny", "openrouter", "tiny-a", 11),
        ("commit_message", "nous", "commit-b", 12),
        ("smol", "openrouter", "smol-c", 13),
        ("title_generation", "openrouter", "main-x", 31),
    ]
    assert [c.base_url for c in plan] == [
        None,
        None,
        None,
        "https://openrouter.ai/api/v1",
    ]
    # The current session model is planned once and lands at the tail.
    assert sum(c.task == "title_generation" for c in plan) == 1


def test_as2_auto_provider_role_is_unconfigured_even_with_a_model(
    monkeypatch, tmp_path
):
    """AS-2: ``provider: auto`` on a role means unconfigured, even when its model is filled in."""
    _use_home(
        monkeypatch,
        tmp_path,
        "as2-auto-smol",
        {
            "title_generation": _DEFAULT_TITLE,
            "tiny": {"provider": "openrouter", "model": "tiny-a", "timeout": 11},
            "commit_message": {"provider": "nous", "model": "commit-b", "timeout": 12},
            "smol": {"provider": "auto", "model": "smol-c", "timeout": 13},
        },
    )

    plan = title_model_plan(_MAIN_RUNTIME)

    assert [c.task for c in plan] == ["tiny", "commit_message", "title_generation"]
    assert [c.timeout for c in plan] == [11, 12, 31]


def test_as3_explicit_title_route_leads_the_plan(monkeypatch, tmp_path):
    """AS-3: a pinned ``title_generation`` route is candidate 0, ahead of every role."""
    _use_home(
        monkeypatch,
        tmp_path,
        "as3-explicit",
        {
            "title_generation": {
                "provider": "openrouter",
                "model": "explicit-t",
                "base_url": "https://ex.example/v1",
                "timeout": 20,
            },
            "tiny": {"provider": "openrouter", "model": "tiny-a", "timeout": 11},
            "commit_message": {"provider": "nous", "model": "commit-b", "timeout": 12},
            "smol": {"provider": "openrouter", "model": "smol-c", "timeout": 13},
        },
    )

    plan = title_model_plan(_MAIN_RUNTIME)

    assert [(c.task, c.provider, c.model, c.base_url, c.timeout) for c in plan] == [
        ("title_generation", "openrouter", "explicit-t", "https://ex.example/v1", 20),
        ("tiny", "openrouter", "tiny-a", None, 11),
        ("commit_message", "nous", "commit-b", None, 12),
        ("smol", "openrouter", "smol-c", None, 13),
        (
            "title_generation",
            "openrouter",
            "main-x",
            "https://openrouter.ai/api/v1",
            20,
        ),
    ]


def test_as3_default_title_route_adds_no_leading_candidate(monkeypatch, tmp_path):
    """AS-3: reverting the slot to default drops candidate 0; the roles lead and current trails."""
    home = _use_home(
        monkeypatch,
        tmp_path,
        "as3-default",
        {
            "title_generation": {
                "provider": "openrouter",
                "model": "explicit-t",
                "base_url": "https://ex.example/v1",
                "timeout": 20,
            },
            "tiny": {"provider": "openrouter", "model": "tiny-a", "timeout": 11},
            "commit_message": {"provider": "nous", "model": "commit-b", "timeout": 12},
            "smol": {"provider": "openrouter", "model": "smol-c", "timeout": 13},
        },
    )
    _write_config(
        home,
        {
            "title_generation": {
                "enabled": True,
                "model_upgrade_enabled": True,
                "provider": "auto",
                "model": "",
                "base_url": "",
                "api_key": "",
                "timeout": 20,
            },
            "tiny": {"provider": "openrouter", "model": "tiny-a", "timeout": 11},
            "commit_message": {"provider": "nous", "model": "commit-b", "timeout": 12},
            "smol": {"provider": "openrouter", "model": "smol-c", "timeout": 13},
        },
    )

    plan = title_model_plan(_MAIN_RUNTIME)

    assert [c.task for c in plan] == [
        "tiny",
        "commit_message",
        "smol",
        "title_generation",
    ]
    assert plan[-1].provider == "openrouter"
    assert plan[-1].model == "main-x"
    assert plan[-1].timeout == 20


def test_as4_chains_expand_depth_first_without_repeats_or_cycles(monkeypatch, tmp_path):
    """AS-4: exact key → wildcard → per-task chain, depth-first; cycles and duplicates collapse."""
    _use_home(
        monkeypatch,
        tmp_path,
        "as4",
        {
            "title_generation": _DEFAULT_TITLE,
            "tiny": {
                "provider": "openrouter",
                "model": "tiny-a",
                "timeout": 11,
                "fallback_chain": [
                    {"provider": "nous", "model": "tiny-b"},
                    {"provider": "nous", "model": "tiny-b"},
                ],
            },
            "commit_message": dict(_UNPINNED_ROLE),
            "smol": dict(_UNPINNED_ROLE),
            "fallback_chains": {
                "openrouter/tiny-a": [{"provider": "nous", "model": "tiny-c"}],
                "nous/*": [{"provider": "openrouter", "model": "tiny-a"}],
            },
        },
    )

    plan = title_model_plan(_MAIN_RUNTIME)

    assert [(c.provider, c.model) for c in plan] == [
        ("openrouter", "tiny-a"),
        ("nous", "tiny-c"),
        ("nous", "tiny-b"),
        ("openrouter", "main-x"),
    ]
    # Chain routes keep the owning task and its timeout; only the current model owns title_generation.
    assert [c.task for c in plan] == ["tiny", "tiny", "tiny", "title_generation"]
    assert [c.timeout for c in plan] == [11, 11, 11, 31]
    assert [c.base_url for c in plan] == [
        None,
        None,
        None,
        "https://openrouter.ai/api/v1",
    ]


def test_as4_role_route_matching_the_session_model_is_merged(monkeypatch, tmp_path):
    """AS-4: a role pinned to the session's own route keeps the earlier candidate, no tail repeat."""
    _use_home(
        monkeypatch,
        tmp_path,
        "as4-merged",
        {
            "title_generation": _DEFAULT_TITLE,
            "tiny": {
                "provider": "openrouter",
                "model": "main-x",
                "base_url": "https://openrouter.ai/api/v1",
                "timeout": 11,
            },
            "commit_message": dict(_UNPINNED_ROLE),
            "smol": dict(_UNPINNED_ROLE),
        },
    )

    plan = title_model_plan(_MAIN_RUNTIME)

    assert [(c.task, c.provider, c.model) for c in plan] == [
        ("tiny", "openrouter", "main-x")
    ]
    assert plan[0].timeout == 11


def test_as5_gates_and_empty_plans_never_call_the_model(monkeypatch, tmp_path):
    """AS-5: either gate off, or nothing pinned and no runtime → empty plan, zero model calls."""
    pinned_roles = {
        "title_generation": {**_DEFAULT_TITLE, "enabled": False},
        "tiny": {"provider": "openrouter", "model": "tiny-a", "timeout": 11},
    }
    _use_home(monkeypatch, tmp_path, "as5-disabled", pinned_roles)
    fake = _install(monkeypatch)

    plan = title_model_plan(_MAIN_RUNTIME)
    assert plan == []
    assert run_title_plan(plan, _MESSAGES) is None
    assert fake.calls == []

    _use_home(
        monkeypatch,
        tmp_path,
        "as5-no-upgrade",
        {
            "title_generation": {**_DEFAULT_TITLE, "model_upgrade_enabled": False},
            "tiny": {"provider": "openrouter", "model": "tiny-a", "timeout": 11},
        },
    )
    plan = title_model_plan(_MAIN_RUNTIME)
    assert plan == []
    assert run_title_plan(plan, _MESSAGES) is None
    assert fake.calls == []

    _use_home(
        monkeypatch,
        tmp_path,
        "as5-empty",
        {
            "title_generation": dict(_DEFAULT_TITLE),
            "tiny": dict(_UNPINNED_ROLE),
            "commit_message": dict(_UNPINNED_ROLE),
            "smol": dict(_UNPINNED_ROLE),
        },
    )
    plan = title_model_plan(None)
    assert plan == []
    assert run_title_plan(plan, _MESSAGES) is None
    assert fake.calls == []


def _as6_home(monkeypatch, tmp_path, name: str) -> None:
    _use_home(
        monkeypatch,
        tmp_path,
        name,
        {
            "title_generation": _DEFAULT_TITLE,
            "tiny": {"provider": "local", "model": "tiny-local", "timeout": 11},
            "commit_message": {"provider": "nous", "model": "commit-b", "timeout": 12},
            "smol": {"provider": "openrouter", "model": "smol-c", "timeout": 13},
        },
    )


def test_as6_failed_candidate_advances_and_the_first_success_stops(
    monkeypatch, tmp_path
):
    """AS-6: an exception on one candidate continues the plan; the first title ends it."""
    _as6_home(monkeypatch, tmp_path, "as6-failure")
    plan = title_model_plan(_MAIN_RUNTIME)
    assert [c.task for c in plan] == [
        "tiny",
        "commit_message",
        "smol",
        "title_generation",
    ]
    assert len({(c.provider, c.model) for c in plan}) == 4

    def reply_for(call):
        if call["task"] == "tiny":
            raise RuntimeError("local down")
        return "<title>Fix flaky park tests</title>"

    fake = _install(monkeypatch, reply_for)

    assert run_title_plan(plan, _MESSAGES) == "Fix flaky park tests"
    assert fake.tasks == ["tiny", "commit_message"]
    assert fake.timeouts == [11, 12]
    assert [call["provider"] for call in fake.calls] == ["local", "nous"]
    assert all(call["fallback"] is False for call in fake.calls)


@pytest.mark.parametrize("reply", ["<title/>", "<title>none</title>"])
def test_as6_terminal_decline_ends_the_plan(monkeypatch, tmp_path, reply):
    """AS-6: both tag-form declines terminate immediately; later candidates are never called."""
    _as6_home(monkeypatch, tmp_path, "as6-terminal")
    plan = title_model_plan(_MAIN_RUNTIME)
    fake = _install(monkeypatch, lambda call: reply)

    assert run_title_plan(plan, _MESSAGES) is None
    assert fake.tasks == ["tiny"]


def test_as6_empty_reply_counts_as_a_failed_candidate(monkeypatch, tmp_path):
    """AS-6: a blank reply is a candidate failure, not a decline — the next candidate still runs."""
    _as6_home(monkeypatch, tmp_path, "as6-empty")
    plan = title_model_plan(_MAIN_RUNTIME)
    fake = _install(
        monkeypatch,
        lambda call: (
            "   " if call["task"] == "tiny" else "<title>Fix flaky park tests</title>"
        ),
    )

    assert run_title_plan(plan, _MESSAGES) == "Fix flaky park tests"
    assert fake.tasks == ["tiny", "commit_message"]
    assert fake.timeouts == [11, 12]


def test_as6_unusable_reply_counts_as_a_failed_candidate(monkeypatch, tmp_path):
    """AS-6: an answer-shaped reply fails its candidate; the plan moves on without retrying it."""
    _as6_home(monkeypatch, tmp_path, "as6-unusable")
    plan = title_model_plan(_MAIN_RUNTIME)
    answer_shaped = " ".join(["word"] * 13)
    fake = _install(
        monkeypatch,
        lambda call: (
            answer_shaped
            if call["task"] == "tiny"
            else "<title>Fix flaky park tests</title>"
        ),
    )

    assert run_title_plan(plan, _MESSAGES) == "Fix flaky park tests"
    assert fake.tasks == ["tiny", "commit_message"]


def test_as6_exhausted_plan_calls_every_candidate_exactly_once(monkeypatch, tmp_path):
    """AS-6: total failure is one call per candidate — no plan-level retry, no concurrency."""
    _as6_home(monkeypatch, tmp_path, "as6-exhausted")
    plan = title_model_plan(_MAIN_RUNTIME)

    def reply_for(call):
        raise RuntimeError(f"{call['provider']} down")

    fake = _install(monkeypatch, reply_for)

    assert run_title_plan(plan, _MESSAGES) is None
    assert fake.tasks == ["tiny", "commit_message", "smol", "title_generation"]
    assert fake.timeouts == [11, 12, 13, 31]
