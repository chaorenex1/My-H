"""Model/provider-keyed auxiliary fallback chains (``auxiliary.fallback_chains``).

Resolution order, most specific first:

    fallback_chains[provider/model] -> fallback_chains[provider/*] -> auxiliary.<task>.fallback_chain

The map is the Hermes equivalent of omp's ``retry.fallbackChains``: it keys a rescue chain by the
route that failed, so one map entry covers every auxiliary task that lands on that model.
"""

import logging
from unittest.mock import MagicMock

import pytest

from agent import auxiliary_client as ac
from hermes_cli.aux_fallback import (
    normalize_model_chain_key,
    normalize_model_fallback_chains,
)


@pytest.fixture(autouse=True)
def _fresh_health():
    ac._reset_aux_unhealthy_cache()
    yield
    ac._reset_aux_unhealthy_cache()


def _route(provider: str, model: str) -> dict[str, str]:
    return {"provider": provider, "model": model}


def _wire(monkeypatch, *, model_chains=None, task_chain=None) -> None:
    """Point the resolver at an in-memory aux section and per-task chain."""
    monkeypatch.setattr(
        "agent.auxiliary_client._get_auxiliary_task_config",
        lambda task: {"fallback_chain": list(task_chain or [])},
    )
    section: dict = {}
    if model_chains is not None:
        section["fallback_chains"] = model_chains
    monkeypatch.setattr("agent.auxiliary_fallback_chains.auxiliary_section", lambda: section)


def _resolve_to_clients(monkeypatch) -> dict:
    """Fake provider resolution: one MagicMock client per resolved model id."""
    clients: dict = {}

    def fake(entry):
        if not entry.get("provider") or not entry.get("model"):
            return None, None
        client = MagicMock(name=f"client:{entry['model']}")
        clients[entry["model"]] = client
        return client, entry["model"]

    monkeypatch.setattr("agent.auxiliary_client._resolve_fallback_entry", fake)
    return clients


def test_exact_model_key_beats_wildcard(monkeypatch):
    _wire(monkeypatch, model_chains={
        "openai/gpt-5": [_route("nous", "exact-model")],
        "openai/*": [_route("openrouter", "wild-model")],
    })
    clients = _resolve_to_clients(monkeypatch)

    client, model, label = ac._try_configured_fallback_chain(
        task="title_generation", failed_provider="openai", failed_model="gpt-5")

    assert model == "exact-model"
    assert client is clients["exact-model"]
    assert label == "fallback_chains[openai/gpt-5][0](nous)"
    assert "wild-model" not in clients


def test_wildcard_key_beats_the_task_chain(monkeypatch):
    _wire(monkeypatch, model_chains={
        "openai/*": [_route("nous", "wild-model")],
    }, task_chain=[_route("anthropic", "task-model")])
    clients = _resolve_to_clients(monkeypatch)

    client, model, label = ac._try_configured_fallback_chain(
        task="title_generation", failed_provider="openai", failed_model="gpt-5")

    assert model == "wild-model"
    assert client is clients["wild-model"]
    assert label == "fallback_chains[openai/*][0](nous)"
    assert "task-model" not in clients


def test_task_chain_wins_when_no_model_key_matches(monkeypatch):
    _wire(monkeypatch, model_chains={
        "other/provider": [_route("nous", "unrelated-model")],
    }, task_chain=[_route("anthropic", "task-model")])
    clients = _resolve_to_clients(monkeypatch)

    client, model, label = ac._try_configured_fallback_chain(
        task="title_generation", failed_provider="openai", failed_model="gpt-5")

    assert model == "task-model"
    assert client is clients["task-model"]
    assert label == "fallback_chain[0](anthropic)"
    assert "unrelated-model" not in clients


def test_missing_failed_model_uses_only_the_provider_wildcard(monkeypatch):
    _wire(monkeypatch, model_chains={
        "openai/gpt-5": [_route("nous", "exact-model")],
        "openai/*": [_route("openrouter", "wild-model")],
    })
    clients = _resolve_to_clients(monkeypatch)

    _client, model, label = ac._try_configured_fallback_chain(
        task="title_generation", failed_provider="openai", failed_model=None)

    assert model == "wild-model"
    assert label == "fallback_chains[openai/*][0](openrouter)"
    assert "exact-model" not in clients


def test_invalid_model_chain_entries_are_skipped_like_the_task_chain(monkeypatch):
    _wire(monkeypatch, model_chains={
        "openai/gpt-5": [
            "not-a-route",
            {"model": "no-provider"},
            {"provider": "nous"},  # provider but no model -> resolution fails
            _route("anthropic", "good-model"),
        ],
    })
    clients = _resolve_to_clients(monkeypatch)

    _client, model, label = ac._try_configured_fallback_chain(
        task="title_generation", failed_provider="openai", failed_model="gpt-5")

    assert model == "good-model"
    assert label == "fallback_chains[openai/gpt-5][3](anthropic)"
    assert list(clients) == ["good-model"]


@pytest.mark.parametrize("model_chains", [None, {}])
def test_map_absent_or_empty_keeps_the_previous_resolution_and_log_lines(
        monkeypatch, caplog, model_chains):
    _wire(monkeypatch, model_chains=model_chains,
          task_chain=[_route("anthropic", "task-model")])
    clients = _resolve_to_clients(monkeypatch)

    with caplog.at_level(logging.INFO, logger=ac.logger.name):
        client, model, label = ac._try_configured_fallback_chain(
            task="title_generation", failed_provider="openai", failed_model="gpt-5")

    assert model == "task-model"
    assert client is clients["task-model"]
    assert label == "fallback_chain[0](anthropic)"
    assert ("Auxiliary title_generation: error on openai — configured fallback to "
            "fallback_chain[0](anthropic) (task-model)") in caplog.text
    assert "fallback_chains" not in caplog.text


def test_exhausted_model_chain_logs_a_debug_line(monkeypatch, caplog):
    _wire(monkeypatch, model_chains={"openai/gpt-5": [_route("nous", "dead-model")]})
    monkeypatch.setattr("agent.auxiliary_client._resolve_fallback_entry",
                        lambda entry: (None, None))

    with caplog.at_level(logging.DEBUG, logger=ac.logger.name):
        result = ac._try_configured_fallback_chain(
            task="title_generation", failed_provider="openai", failed_model="gpt-5")

    assert result == (None, None, "")
    assert ("configured fallback_chains exhausted "
            "(tried: fallback_chains[openai/gpt-5][0](nous))") in caplog.text


def test_model_chain_label_resolves_its_stored_entry(monkeypatch):
    entry = {"provider": "nous", "model": "fallback", "base_url": "https://nous.example/v1",
             "timeout": 240}
    _wire(monkeypatch, model_chains={"openai/gpt-5": [entry]})
    label = "fallback_chains[openai/gpt-5][0](nous)"

    assert ac._fallback_chain_entry("title_generation", label) is entry
    assert ac._fallback_entry_timeout("title_generation", label) == 240.0
    assert ac._fallback_provider_from_label(label) == "nous"


class TestModelChainValidation:
    def test_default_config_ships_an_empty_map(self):
        from hermes_cli.config_defaults import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["auxiliary"]["fallback_chains"] == {}

    def test_key_keeps_slashes_in_the_model(self):
        assert normalize_model_chain_key("anthropic/claude-sonnet-4") == "anthropic/claude-sonnet-4"
        assert normalize_model_chain_key("  openai/gpt-5  ") == "openai/gpt-5"

    def test_wildcard_key_is_valid(self):
        assert normalize_model_chain_key("openai/*") == "openai/*"

    @pytest.mark.parametrize("bad", ["openai", "", "   ", "openai/", "/gpt-5", "/"])
    def test_key_requires_slash_and_both_sides(self, bad):
        with pytest.raises(ValueError, match="fallback_chains key"):
            normalize_model_chain_key(bad)

    def test_map_validates_every_chain_with_the_task_rules(self):
        chains = normalize_model_fallback_chains({
            "openai/gpt-5": [{"provider": "nous", "model": "m", "base_url": "  "}],
        })
        assert chains == {"openai/gpt-5": [{"provider": "nous", "model": "m"}]}

        with pytest.raises(ValueError, match=r"fallback_chains\['openai/gpt-5'\]"):
            normalize_model_fallback_chains({"openai/gpt-5": [{"provider": "nous"}]})

    def test_map_caps_each_chain_at_eight(self):
        entry = {"provider": "nous", "model": "m"}
        with pytest.raises(ValueError, match="at most 8 entries"):
            normalize_model_fallback_chains({"openai/gpt-5": [entry] * 9})

    def test_map_rejects_a_non_mapping(self):
        with pytest.raises(ValueError, match="must be a map"):
            normalize_model_fallback_chains([{"provider": "nous", "model": "m"}])
