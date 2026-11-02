"""Tests: ``model.roles.get`` / ``model.roles.set`` (``tui_gateway/methods_model_roles.py``).

The omp-ui Models hub's Roles view is backed by the profile's Hermes config
(``auxiliary.<task>``), never the shell's own settings:

- ``get`` lists the picker slots in canonical order with their label/hint and stored route,
  then appends plugin-registered tasks with ``group="plugin"``;
- ``set`` writes through the dashboard assignment chokepoint, so ``config.yaml`` on disk is the
  single truth and the returned row reflects it;
- ``clear`` returns one role to ``provider="auto"`` without disturbing its siblings;
- an unknown role id is refused with the module's validation error code.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import hermes_yaml as yaml

import hermes_cli.plugins as plugins_mod
import tui_gateway.server as srv
from hermes_cli.aux_slots import AUX_TASK_META, aux_picker_keys
from hermes_cli.plugins import PluginManager
from tui_gateway.contracts import METHODS
from tui_gateway.contracts.model_roles import ModelRolesGetResult, ModelRolesSetResult
from tui_gateway.contracts.registry import validate_params

PLUGIN_KEY = "roles_probe_task"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Temp ``HERMES_HOME`` with one enabled plugin that registers an auxiliary task."""
    hermes_home = tmp_path / ".hermes"
    plugin_dir = hermes_home / "plugins" / "roles_probe"
    plugin_dir.mkdir(parents=True)
    # JSON is valid YAML; keeps the test free of a yaml writer.
    (plugin_dir / "plugin.yaml").write_text(
        json.dumps({"name": "roles_probe", "version": "0.1.0", "description": "roles probe"}))
    (plugin_dir / "__init__.py").write_text(
        "def register(ctx):\n"
        f"    ctx.register_auxiliary_task('{PLUGIN_KEY}', display_name='Probe role',\n"
        "                                description='probe task for roles', defaults={'timeout': 5})\n")
    (hermes_home / "config.yaml").write_text(json.dumps({"plugins": {"enabled": ["roles_probe"]}}))

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(plugins_mod, "get_bundled_plugins_dir", lambda: tmp_path / "empty-bundled")
    monkeypatch.setattr(PluginManager, "_scan_entry_points", lambda self: [])
    plugins_mod._reset_plugin_managers_for_tests()
    yield hermes_home
    plugins_mod._reset_plugin_managers_for_tests()


def _result(method: str, params: dict) -> dict:
    envelope = srv._methods[method](1, params)
    assert "error" not in envelope, envelope
    return envelope["result"]


def _error(method: str, params: dict) -> dict:
    envelope = srv._methods[method](1, params)
    assert "error" in envelope, envelope
    return envelope["error"]


def _stored_aux(home: Path) -> dict:
    cfg = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
    return cfg.get("auxiliary") or {}


def test_get_lists_picker_roles_then_plugin_tasks(home):
    result = ModelRolesGetResult.model_validate(_result("model.roles.get", {}))

    builtin_ids = [row.id for row in result.roles if row.group == "builtin"]
    assert builtin_ids == list(aux_picker_keys())
    assert result.roles[-1].id == PLUGIN_KEY
    assert result.roles[-1].group == "plugin"
    assert result.roles[-1].label == "Probe role"
    assert result.roles[-1].hint == "probe task for roles"

    for row in result.roles[: len(builtin_ids)]:
        assert (row.label, row.hint) == AUX_TASK_META[row.id]
        assert row.section == "chat"
        assert row.provider == "auto"
        assert row.model == ""
        assert row.base_url == ""
        assert row.fallback_chain == []
        assert row.inherit_from is None
    # Slots with no shipped override inherit; `voice_chat` ships "none" in DEFAULT_CONFIG.
    assert next(row for row in result.roles if row.id == "vision").reasoning_effort is None

    assert result.default_thinking_level == "medium"
    assert isinstance(result.main.provider, str)
    assert isinstance(result.main.model, str)


def test_set_persists_to_config_and_returns_the_row(home):
    result = ModelRolesSetResult.model_validate(_result(
        "model.roles.set", {"role": "vision", "provider": "nous", "model": "hermes-4.5-405b"}))

    assert result.role.id == "vision"
    assert result.role.label == "Vision"
    assert result.role.provider == "nous"
    assert result.role.model == "hermes-4.5-405b"
    # Omitted knobs stay untouched.
    assert result.role.reasoning_effort is None
    assert result.role.fallback_chain == []

    vision = _stored_aux(home)["vision"]
    assert vision["provider"] == "nous"
    assert vision["model"] == "hermes-4.5-405b"

    # The file is the single truth: a fresh get answers the same route.
    rows = {row["id"]: row for row in _result("model.roles.get", {})["roles"]}
    assert rows["vision"]["provider"] == "nous"
    assert rows["vision"]["model"] == "hermes-4.5-405b"


def test_clear_resets_one_role_without_touching_siblings(home):
    # Through the wire's own param model, so the chain arrives as contract entries (the real shape).
    params, problem = validate_params(METHODS["model.roles.set"], {
        "role": "vision", "provider": "nous", "model": "hermes-4.5-405b",
        "base_url": "https://example.invalid/v1", "reasoning_effort": "high",
        "fallback_chain": [{"provider": "openrouter", "model": "vendor/model"}],
    })
    assert problem is None
    _result("model.roles.set", params)
    _result("model.roles.set", {"role": "compression", "provider": "openrouter", "model": "vendor/small"})

    result = ModelRolesSetResult.model_validate(
        _result("model.roles.set", {"role": "vision", "clear": True}))

    assert result.role.provider == "auto"
    assert result.role.model == ""
    assert result.role.base_url == ""
    assert result.role.reasoning_effort is None
    assert result.role.fallback_chain == []

    stored = _stored_aux(home)
    vision = stored["vision"]
    # save_config strips values equal to the shipped defaults, so absent means "auto" too.
    assert vision.get("provider", "auto") == "auto"
    assert vision.get("model", "") == ""
    assert not vision.get("base_url")
    assert not vision.get("fallback_chain")
    # The sibling role still holds its assignment.
    assert stored["compression"]["provider"] == "openrouter"
    assert stored["compression"]["model"] == "vendor/small"


def test_unknown_role_is_refused(home):
    error = _error("model.roles.set", {"role": "not_a_role", "provider": "nous", "model": "x"})
    assert error["code"] == 5126
    assert "unknown role" in error["message"]
    assert "not_a_role" not in _stored_aux(home)


def test_invalid_effort_and_fallback_chain_are_refused(home):
    error = _error("model.roles.set", {"role": "vision", "provider": "nous", "reasoning_effort": "hgih"})
    assert error["code"] == 5127
    assert "reasoning_effort" in error["message"]

    error = _error("model.roles.set", {
        "role": "vision", "provider": "nous",
        "fallback_chain": [{"provider": "", "model": ""}],
    })
    assert error["code"] == 5127
    assert "fallback_chain[0]" in error["message"]


def test_wire_dispatch_matches_the_declared_contract(home, monkeypatch):
    """Through admission: params and results validate against the models the generator renders."""
    from tui_gateway.contracts import registry

    monkeypatch.setattr(registry, "STRICT", True)

    resp = srv.handle_request({"jsonrpc": "2.0", "id": "get", "method": "model.roles.get", "params": {}})
    assert "error" not in resp, resp

    resp = srv.handle_request({
        "jsonrpc": "2.0", "id": "set", "method": "model.roles.set",
        "params": {
            "role": "vision", "provider": "nous", "model": "hermes-4.5-405b",
            "reasoning_effort": "inherit",
            "fallback_chain": [{"provider": "openrouter", "model": "vendor/model"}],
        },
    })
    assert "error" not in resp, resp
    assert resp["result"]["role"]["fallback_chain"] == [{"provider": "openrouter", "model": "vendor/model"}]


# ── wire v2: imported omp roles, omp tags, cycle order, presets ───────────────────────────────

def _stored_cfg(home: Path) -> dict:
    return yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}


def _write_cfg(home: Path, cfg: dict) -> None:
    (home / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")


def _rows(result: dict) -> dict[str, dict]:
    return {row["id"]: row for row in result["roles"]}


def test_rows_carry_omp_tags_imported_roles_and_targets(home):
    from tui_gateway.contracts.model_roles import ModelRolesGetResult

    result = ModelRolesGetResult.model_validate(_result("model.roles.get", {}))
    rows = {row.id: row for row in result.roles}

    assert result.cycle_order == []
    assert result.presets.names == [] and result.presets.active == ""

    for slot in aux_picker_keys():
        assert rows[slot].group == "builtin"
        assert rows[slot].target == "auxiliary"
        assert rows[slot].tag
    assert rows["vision"].tag == "VISION"
    assert rows["title_generation"].tag == "TINY"
    assert rows["background_review"].tag == "MEMORY"
    assert rows["goal_judge"].tag == "JUDGE"
    assert rows["commit_message"].tag == "COMMIT"
    assert rows["compression"].tag == "COMPRESSION"
    assert rows[PLUGIN_KEY].tag == "PROBE ROLE"

    assert [row.id for row in result.roles if row.group == "imported"] == ["task", "advisor"]
    task, advisor = rows["task"], rows["advisor"]
    assert (task.label, task.target, task.tag) == ("Task", "delegation", "TASK")
    assert (advisor.label, advisor.target, advisor.tag) == ("Advisor", "adviser", "ADVISOR")
    # "auto" for an imported role is the empty provider (inherit the parent route).
    assert (task.provider, task.model) == ("", "")
    assert (advisor.provider, advisor.model) == ("", "")


def test_imported_task_role_writes_delegation_and_clear_restores_auto(home):
    from tui_gateway.contracts.model_roles import ModelRolesSetResult

    result = ModelRolesSetResult.model_validate(_result("model.roles.set", {
        "role": "task", "provider": "openrouter", "model": "vendor/child",
        "base_url": "https://example.invalid/v1", "api_key": "sk-test",
        "reasoning_effort": "high",
        "fallback_chain": [{"provider": "nous", "model": "hermes-4.5-405b"}],
    }))
    assert (result.role.provider, result.role.model) == ("openrouter", "vendor/child")
    assert result.role.base_url == "https://example.invalid/v1"
    assert result.role.reasoning_effort == "high"
    assert result.role.fallback_chain[0].provider == "nous"
    assert (result.role.group, result.role.target) == ("imported", "delegation")

    delegation = _stored_cfg(home)["delegation"]
    assert (delegation["provider"], delegation["model"]) == ("openrouter", "vendor/child")
    assert delegation["reasoning_effort"] == "high"
    assert delegation["fallback_providers"] == [{"provider": "nous", "model": "hermes-4.5-405b"}]

    cleared = ModelRolesSetResult.model_validate(
        _result("model.roles.set", {"role": "task", "clear": True})).role
    assert (cleared.provider, cleared.model, cleared.base_url) == ("", "", "")
    assert cleared.reasoning_effort is None and cleared.fallback_chain == []

    delegation = _stored_cfg(home)["delegation"]
    assert (delegation.get("provider", ""), delegation.get("model", "")) == ("", "")
    assert not delegation.get("base_url") and not delegation.get("api_key")
    # ``null`` = inherit the parent chain, the "auto" value for this role.
    assert delegation.get("fallback_providers") is None


def test_imported_advisor_writes_adviser_and_refuses_chain_and_effort(home):
    from tui_gateway.contracts.model_roles import ModelRolesSetResult

    result = ModelRolesSetResult.model_validate(_result("model.roles.set", {
        "role": "advisor", "provider": "nous", "model": "hermes-4.5-405b",
    })).role
    assert (result.provider, result.model) == ("nous", "hermes-4.5-405b")
    assert (result.group, result.target, result.tag) == ("imported", "adviser", "ADVISOR")
    assert _stored_cfg(home)["adviser"] == {"provider": "nous", "model": "hermes-4.5-405b"}

    error = _error("model.roles.set", {
        "role": "advisor", "provider": "nous", "reasoning_effort": "high"})
    assert error["code"] == 5127
    assert "reasoning_effort" in error["message"]

    error = _error("model.roles.set", {
        "role": "advisor", "provider": "nous",
        "fallback_chain": [{"provider": "openrouter", "model": "vendor/model"}]})
    assert error["code"] == 5127
    assert "fallback chain" in error["message"]
    assert _stored_cfg(home)["adviser"] == {"provider": "nous", "model": "hermes-4.5-405b"}

    # The hub always submits its thinking selection ("inherit" = no override) and an empty chain
    # for a role that has neither; that must assign the model instead of failing every time.
    hub_shaped = ModelRolesSetResult.model_validate(_result("model.roles.set", {
        "role": "advisor", "provider": "nous", "model": "hermes-4.5-405b",
        "reasoning_effort": "inherit", "fallback_chain": [],
    })).role
    assert (hub_shaped.provider, hub_shaped.model) == ("nous", "hermes-4.5-405b")
    assert _stored_cfg(home)["adviser"] == {"provider": "nous", "model": "hermes-4.5-405b"}

    cleared = ModelRolesSetResult.model_validate(
        _result("model.roles.set", {"role": "advisor", "clear": True})).role
    assert (cleared.provider, cleared.model) == ("", "")


def test_cycle_set_validates_each_id_and_persists_omp_cycle_order(home):
    from tui_gateway.contracts.model_roles import ModelCycleSetResult, ModelRolesGetResult

    order = ["vision", "task", "advisor", "compression"]
    assert ModelCycleSetResult.model_validate(
        _result("model.cycle.set", {"order": order})).order == order
    assert _stored_cfg(home)["omp"]["cycle_order"] == order
    assert ModelRolesGetResult.model_validate(
        _result("model.roles.get", {})).cycle_order == order

    error = _error("model.cycle.set", {"order": ["vision", "not_a_role"]})
    assert error["code"] == 5126
    assert "not_a_role" in error["message"]
    # A refused order leaves the persisted one untouched.
    assert _stored_cfg(home)["omp"]["cycle_order"] == order


def test_preset_save_apply_restores_every_route_and_resets_absent_roles(home):
    from tui_gateway.contracts.model_roles import ModelPresetsApplyResult

    _result("model.roles.set", {
        "role": "vision", "provider": "nous", "model": "hermes-4.5-405b",
        "reasoning_effort": "high",
        "fallback_chain": [{"provider": "openrouter", "model": "vendor/vision"}],
    })
    _result("model.roles.set", {"role": "compression", "provider": "openrouter", "model": "vendor/small"})
    _result("model.roles.set", {"role": "task", "provider": "nous", "model": "hermes-4.5-405b"})
    _result("model.roles.set", {"role": "advisor", "provider": "nous", "model": "hermes-mini"})
    _result("model.cycle.set", {"order": ["vision", "compression"]})

    assert _result("model.presets.save", {"name": "work"}) == {"name": "work"}
    stored = _stored_cfg(home)["model_presets"]["work"]
    assert set(stored) == set(aux_picker_keys()) | {"task", "advisor"}
    assert stored["vision"]["reasoning_effort"] == "high"
    assert stored["vision"]["fallback_chain"] == [{"provider": "openrouter", "model": "vendor/vision"}]

    # Move everything away, then drop the compression route from the snapshot on disk so apply
    # must reset it (omp: a preset describes the complete state).
    _result("model.roles.set", {"role": "vision", "clear": True})
    _result("model.roles.set", {"role": "compression", "provider": "openrouter", "model": "vendor/other"})
    _result("model.roles.set", {"role": "task", "clear": True})
    _result("model.roles.set", {"role": "advisor", "provider": "nous", "model": "other"})
    cfg = _stored_cfg(home)
    cfg["model_presets"]["work"].pop("compression")
    _write_cfg(home, cfg)

    result = ModelPresetsApplyResult.model_validate(_result("model.presets.apply", {"name": "work"}))
    rows = {row.id: row for row in result.roles}
    assert (rows["vision"].provider, rows["vision"].model) == ("nous", "hermes-4.5-405b")
    assert rows["vision"].reasoning_effort == "high"
    assert [entry.model for entry in rows["vision"].fallback_chain] == ["vendor/vision"]
    assert rows["task"].model == "hermes-4.5-405b"
    assert rows["advisor"].model == "hermes-mini"
    assert rows["compression"].provider == "auto" and rows["compression"].model == ""
    assert result.presets.names == ["work"] and result.presets.active == "work"
    assert result.cycle_order == ["vision", "compression"]

    assert _result("model.presets.delete", {"name": "work"}) == {"name": "work"}
    assert _result("model.roles.get", {})["presets"] == {"names": [], "active": ""}
    assert _error("model.presets.delete", {"name": "work"})["code"] == 5126
    assert _error("model.presets.apply", {"name": "work"})["code"] == 5126


def test_preset_names_are_validated(home):
    for name in ("", "has space", "x" * 65, "../escape"):
        error = _error("model.presets.save", {"name": name})
        assert error["code"] == 5127, name
        assert "1-64 characters" in error["message"]
    assert _stored_cfg(home).get("model_presets", {}) == {}
    assert _error("model.presets.apply", {"name": "bad name"})["code"] == 5127


def test_wire_dispatch_matches_the_v2_contracts(home, monkeypatch):
    """State-mutating v2 methods, through admission with strict result validation."""
    from tui_gateway.contracts import registry

    monkeypatch.setattr(registry, "STRICT", True)

    for method, params in (
        ("model.cycle.set", {"order": ["vision", "task"]}),
        ("model.presets.save", {"name": "wire"}),
        ("model.presets.apply", {"name": "wire"}),
        ("model.presets.delete", {"name": "wire"}),
        ("model.roles.set", {"role": "openrouter/*",
                             "fallback_chain": [{"provider": "nous", "model": "hermes-4.5-405b"}]}),
    ):
        resp = srv.handle_request({"jsonrpc": "2.0", "id": method, "method": method, "params": params})
        assert "error" not in resp, resp

    resp = srv.handle_request({"jsonrpc": "2.0", "id": "get", "method": "model.roles.get", "params": {}})
    assert "error" not in resp, resp
    assert resp["result"]["cycle_order"] == ["vision", "task"]
    assert resp["result"]["model_chains"] == {
        "openrouter/*": [{"provider": "nous", "model": "hermes-4.5-405b"}]}


def test_preset_with_an_advisor_chain_is_refused_before_any_write(home):
    cfg = _stored_cfg(home)
    cfg["model_presets"] = {"bad": {
        "vision": {"provider": "nous", "model": "hermes-4.5-405b"},
        "advisor": {"provider": "nous", "model": "hermes-mini",
                    "fallback_chain": [{"provider": "nous", "model": "backup"}]},
    }}
    _write_cfg(home, cfg)

    error = _error("model.presets.apply", {"name": "bad"})
    assert error["code"] == 5127
    assert "fallback chain" in error["message"]
    # Routes are validated before the first write, so the good role was not half-applied.
    assert _stored_aux(home).get("vision", {}).get("provider", "auto") == "auto"


def test_preset_with_an_invalid_effort_names_the_role(home):
    cfg = _stored_cfg(home)
    cfg["model_presets"] = {"bad": {"vision": {"provider": "nous", "reasoning_effort": "hgih"}}}
    _write_cfg(home, cfg)

    error = _error("model.presets.apply", {"name": "bad"})
    assert error["code"] == 5127
    assert "vision" in error["message"]


def test_preset_apply_clears_a_task_endpoint_absent_from_the_snapshot(home):
    # Same provider in the snapshot, but the preset's task route has no endpoint: apply is exact.
    delegated = _result("model.roles.set", {
        "role": "task", "provider": "nous", "model": "hermes-4.5-405b",
        "base_url": "https://example.invalid/v1", "api_key": "sk-test",
    })
    assert delegated["role"]["base_url"] == "https://example.invalid/v1"

    _result("model.roles.set", {
        "role": "compression", "provider": "nous", "model": "hermes-mini",
        "base_url": "https://example.invalid/v1", "api_key": "sk-test",
    })

    cfg = _stored_cfg(home)
    cfg["model_presets"] = {"no_endpoint": {
        "task": {"provider": "nous", "model": "hermes-4.5-405b"},
        "compression": {"provider": "nous", "model": "hermes-mini"},
    }}
    _write_cfg(home, cfg)

    rows = _rows(_result("model.presets.apply", {"name": "no_endpoint"}))
    assert rows["task"]["base_url"] == "" and rows["compression"]["base_url"] == ""
    delegation = _stored_cfg(home)["delegation"]
    assert not delegation.get("base_url") and not delegation.get("api_key")
    assert delegation["provider"] == "nous" and delegation["model"] == "hermes-4.5-405b"
    compression = _stored_aux(home)["compression"]
    assert not compression.get("base_url") and not compression.get("api_key")
    assert compression["provider"] == "nous" and compression["model"] == "hermes-mini"



def test_preset_apply_keeps_the_credential_of_an_unchanged_endpoint(home):
    """Snapshots carry routes, never credentials: re-applying the same endpoint must keep its key."""
    _result("model.roles.set", {
        "role": "task", "provider": "nous", "model": "hermes-4.5-405b",
        "base_url": "https://example.invalid/v1", "api_key": "sk-test",
    })
    _result("model.presets.save", {"name": "same_endpoint"})

    cfg = _stored_cfg(home)
    assert cfg["model_presets"]["same_endpoint"]["task"]["base_url"] == "https://example.invalid/v1"
    assert "api_key" not in cfg["model_presets"]["same_endpoint"]["task"]

    _result("model.presets.apply", {"name": "same_endpoint"})
    delegation = _stored_cfg(home)["delegation"]
    assert delegation["base_url"] == "https://example.invalid/v1"
    assert delegation["api_key"] == "sk-test"

    # A different endpoint belongs to a different host, so the old key must not follow it.
    cfg = _stored_cfg(home)
    cfg["model_presets"]["moved"] = {"task": {
        "provider": "nous", "model": "hermes-4.5-405b", "base_url": "https://other.invalid/v1",
    }}
    _write_cfg(home, cfg)
    _result("model.presets.apply", {"name": "moved"})
    delegation = _stored_cfg(home)["delegation"]
    assert delegation["base_url"] == "https://other.invalid/v1"
    assert "api_key" not in delegation

# ── model-keyed fallback chains (``auxiliary.fallback_chains``) ────────────────────────────────

def test_get_reports_model_keyed_fallback_chains(home):
    from tui_gateway.contracts.model_roles import ModelRolesGetResult

    assert ModelRolesGetResult.model_validate(_result("model.roles.get", {})).model_chains == {}

    cfg = _stored_cfg(home)
    cfg["auxiliary"] = {"fallback_chains": {
        "openrouter/*": [{"provider": "nous", "model": "hermes-4.5-405b"}],
        "anthropic/claude-sonnet-4": [{"provider": "openrouter", "model": "vendor/model",
                                       "base_url": "https://example.invalid/v1"}],
        # Hand-edited junk is dropped, exactly like a row's chain.
        "bad/key": [{"provider": "", "model": ""}],
    }}
    _write_cfg(home, cfg)

    result = ModelRolesGetResult.model_validate(_result("model.roles.get", {}))
    assert list(result.model_chains) == ["openrouter/*", "anthropic/claude-sonnet-4", "bad/key"]
    assert result.model_chains["openrouter/*"][0].provider == "nous"
    assert result.model_chains["anthropic/claude-sonnet-4"][0].base_url == "https://example.invalid/v1"
    assert result.model_chains["bad/key"] == []


def test_set_writes_and_removes_one_model_chain_key_without_touching_roles(home):
    from tui_gateway.contracts.model_roles import ModelRolesSetResult

    _result("model.roles.set", {"role": "vision", "provider": "nous", "model": "hermes-4.5-405b"})

    row = ModelRolesSetResult.model_validate(_result("model.roles.set", {
        "role": "openrouter/*",
        "fallback_chain": [{"provider": "nous", "model": "hermes-4.5-405b"}],
    })).role
    assert (row.id, row.label, row.tag) == ("openrouter/*", "openrouter/*", "OPENROUTER/*")
    assert (row.group, row.target) == ("chain", "chain")
    assert (row.provider, row.model) == ("", "")
    assert [entry.model for entry in row.fallback_chain] == ["hermes-4.5-405b"]
    assert _stored_cfg(home)["auxiliary"]["fallback_chains"] == {
        "openrouter/*": [{"provider": "nous", "model": "hermes-4.5-405b"}]}

    _result("model.roles.set", {
        "role": "anthropic/claude-sonnet-4",
        "fallback_chain": [{"provider": "openrouter", "model": "vendor/model"}],
    })
    chains = _stored_cfg(home)["auxiliary"]["fallback_chains"]
    assert set(chains) == {"openrouter/*", "anthropic/claude-sonnet-4"}
    # The role assignment is untouched by chain writes.
    assert _stored_aux(home)["vision"]["model"] == "hermes-4.5-405b"

    cleared = ModelRolesSetResult.model_validate(_result(
        "model.roles.set", {"role": "openrouter/*", "fallback_chain": []})).role
    assert cleared.fallback_chain == []
    chains = _stored_cfg(home)["auxiliary"]["fallback_chains"]
    assert set(chains) == {"anthropic/claude-sonnet-4"}
    assert _stored_aux(home)["vision"]["model"] == "hermes-4.5-405b"
    assert set(_result("model.roles.get", {})["model_chains"]) == {"anthropic/claude-sonnet-4"}


def test_unknown_role_that_is_not_a_chain_key_stays_5126(home):
    error = _error("model.roles.set", {"role": "not_a_key", "fallback_chain": []})
    assert error["code"] == 5126
    assert "not_a_key" in error["message"]

    # A key-shaped role without a chain to write is still an unknown role.
    error = _error("model.roles.set", {"role": "openrouter/*"})
    assert error["code"] == 5126
    assert not _stored_cfg(home).get("auxiliary", {}).get("fallback_chains")


def test_clear_is_refused_for_a_model_chain_key(home):
    _result("model.roles.set", {
        "role": "openrouter/*",
        "fallback_chain": [{"provider": "nous", "model": "hermes-4.5-405b"}],
    })

    error = _error("model.roles.set", {"role": "openrouter/*", "clear": True})
    assert error["code"] == 5127
    assert "clear" in error["message"] and "shortened" in error["message"]
    assert _stored_cfg(home)["auxiliary"]["fallback_chains"] == {
        "openrouter/*": [{"provider": "nous", "model": "hermes-4.5-405b"}]}
