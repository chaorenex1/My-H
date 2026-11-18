"""One canonical auxiliary-slot registry (``hermes_cli.aux_slots``) feeds every aux surface.

Before it, ``DEFAULT_CONFIG["auxiliary"]``, the CLI picker (``main_provider_setup._AUX_TASKS``),
the dashboard REST allowlist (``web_server_config._AUX_TASK_SLOTS``) and the plugin reservation
list were hand-copied — and the copies had already drifted (goal_judge/monitor/background_review
were config-only, and a plugin could register a key colliding with a MoA slot).
"""

from __future__ import annotations

import pytest

from hermes_cli.aux_slots import (
    AUX_PICKER_EXCLUDED,
    AUX_TASK_KEYS,
    AUX_TASK_META,
    aux_picker_keys,
)
from hermes_cli.config_defaults import DEFAULT_CONFIG


#: ``auxiliary`` also carries non-slot maps (route-keyed policy); they are not picker tasks.
_NON_SLOT_MAP_KEYS = frozenset({"fallback_chains"})


def _config_task_keys() -> tuple[str, ...]:
    """Every ``auxiliary`` key that holds a per-task block, in config order."""
    return tuple(
        key for key, value in DEFAULT_CONFIG["auxiliary"].items()
        if isinstance(value, dict) and key not in _NON_SLOT_MAP_KEYS
    )


def test_config_auxiliary_tasks_and_canonical_keys_are_the_same_list_in_the_same_order():
    assert _config_task_keys() == AUX_TASK_KEYS


def test_the_three_new_slots_sit_at_the_contracted_positions():
    keys = list(AUX_TASK_KEYS)
    assert keys[keys.index("title_generation") + 1] == "commit_message"
    assert keys[keys.index("kanban_decomposer") + 1] == "kanban_estimator"
    assert keys[keys.index("review") + 1] == "side_question"


def test_every_slot_has_a_label_and_a_hint():
    assert set(AUX_TASK_META) == set(AUX_TASK_KEYS)
    assert all(label.strip() and hint.strip() for label, hint in AUX_TASK_META.values())


def test_the_picker_excludes_only_the_moa_slots():
    assert AUX_PICKER_EXCLUDED == frozenset({"moa_reference", "moa_aggregator"})
    assert aux_picker_keys() == tuple(key for key in AUX_TASK_KEYS if key not in AUX_PICKER_EXCLUDED)
    assert "side_question" in aux_picker_keys() and "moa_aggregator" not in aux_picker_keys()


def test_cli_picker_and_dashboard_allowlist_derive_from_the_registry():
    from hermes_cli.main_provider_setup import _AUX_TASKS
    from hermes_cli.web_server_config import _AUX_TASK_SLOTS

    assert tuple(_AUX_TASK_SLOTS) == aux_picker_keys()
    assert _AUX_TASKS == [(key, *AUX_TASK_META[key]) for key in aux_picker_keys()]


@pytest.mark.parametrize("key", ["goal_judge", "monitor", "background_review", "moa_reference",
                                 "side_question", "commit_message", "kanban_estimator"])
def test_a_plugin_cannot_register_a_key_a_builtin_owns(key):
    from hermes_cli.plugins_aux_tasks import build_auxiliary_task_entry

    with pytest.raises(ValueError, match="reserved for a built-in task"):
        build_auxiliary_task_entry(
            "probe", "probe", key, display_name="Probe", description="", defaults=None,
            inherit_from=None, registered={})


@pytest.mark.parametrize("key", ["goal_judge", "monitor", "background_review", "moa_aggregator"])
def test_plugin_llm_trust_gate_knows_every_builtin_slot(key, monkeypatch):
    from agent.plugin_llm import PluginLlmTrustError, _TrustPolicy, _check_task

    monkeypatch.setattr("hermes_cli.plugins.get_plugin_auxiliary_tasks", list)
    with pytest.raises(PluginLlmTrustError):
        _check_task(_TrustPolicy("probe"), plugin_id="probe", requested_task=key)
