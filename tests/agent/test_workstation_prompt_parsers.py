"""Persisted-prompt parsers anchored in ``<workstation>`` (ADR-19 D7.3)."""

from __future__ import annotations

from agent.chat_completion_helpers_identity import rewrite_prompt_model_identity
from agent.surface_switch import identity_line_value, runtime_host_value, workstation_value
from types import SimpleNamespace


PROMPT = """§ Role
Active Hermes profile: default.

§ Runtime
> Model: decoy-in-runtime

§ Delivery
<contract>
- Model: decoy-in-delivery
</contract>

§ Memories
<memories>
<workstation>
- Model: decoy-workstation
- Current working directory: /decoy
</workstation>
</memories>

<project-context>
<workstation>
- OS: linux 6.8
- Arch: x64
- Host: linux (6.8)
- User home directory: /home/u
- Current working directory: /repo/app
- Model: primary-model
- Provider: openrouter
- Platform: cli
</workstation>
</project-context>"""


def test_workstation_value_reads_bulleted_identity_lines():
    assert workstation_value(PROMPT, "Model") == "primary-model"
    assert identity_line_value(PROMPT, "Provider") == "openrouter"
    assert identity_line_value(PROMPT, "Platform") == "cli"
    assert runtime_host_value(PROMPT, "Current working directory") == "/repo/app"


def test_identity_lines_never_come_from_earlier_prose():
    """A memory/context/runtime block quoting ``Model:`` or ``<workstation>`` is never identity."""
    assert identity_line_value(PROMPT, "Model") == "primary-model"
    assert runtime_host_value(PROMPT, "Current working directory") == "/repo/app"


def test_legacy_prompt_falls_back_to_the_trailer():
    legacy = "SYSTEM BODY\n\nModel: old-model\nProvider: old-provider\nPlatform: discord"
    assert identity_line_value(legacy, "Model") == "old-model"
    assert identity_line_value(legacy, "Platform") == "discord"


def test_legacy_runtime_block_still_yields_the_cwd():
    legacy = (
        "SYSTEM\n\n# Hermes runtime environment\n\nHost: Example\nUser home directory: /u\n"
        "Current working directory: /legacy/app\n\n<!-- End Hermes runtime environment -->"
    )
    assert runtime_host_value(legacy, "Current working directory") == "/legacy/app"


def test_model_identity_rewrite_touches_only_the_workstation():
    agent = SimpleNamespace(_cached_system_prompt=PROMPT)
    rewrite_prompt_model_identity(agent, "failover-model", "anthropic")
    rewritten = agent._cached_system_prompt
    assert "- Model: failover-model" in rewritten
    assert "- Provider: anthropic" in rewritten
    assert "> Model: decoy-in-runtime" in rewritten
    assert "- Model: decoy-in-delivery" in rewritten
    assert "- Model: decoy-workstation" in rewritten
    assert identity_line_value(rewritten, "Model") == "failover-model"


def test_model_identity_rewrite_keeps_the_legacy_trailer_shape():
    legacy = "SYSTEM BODY\n\nModel: old-model\nProvider: old-provider\nPlatform: cli"
    agent = SimpleNamespace(_cached_system_prompt=legacy)
    rewrite_prompt_model_identity(agent, "new-model", "new-provider")
    assert agent._cached_system_prompt == (
        "SYSTEM BODY\n\nModel: new-model\nProvider: new-provider\nPlatform: cli"
    )
