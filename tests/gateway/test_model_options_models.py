"""``model.options``'s flat ``models`` rows are projected from the provider payload.

The projection is cache-only: ``get_model_info`` must be called with ``allow_network=False``, and a
missing entry or a raising lookup degrades to the provider-only fields instead of failing.
"""
from types import SimpleNamespace

import agent.models_dev
import agent.reasoning_effort

from hermes_cli.inventory import project_model_infos


def _payload(models):
    return {"providers": [{"slug": "anthropic", "name": "Anthropic", "api_url": "https://api.example",
                           "models": models,
                           "capabilities": {m: {"reasoning": m != "free"} for m in models}}],
            "model": "claude", "provider": "anthropic"}


def test_projection_flattens_catalog_facts_and_keeps_providers_untouched(monkeypatch):
    calls = []

    def fake_info(provider_id, model_id, *, allow_network=False):
        calls.append((provider_id, model_id, allow_network))
        if model_id == "unknown":
            return None
        return SimpleNamespace(name="Friendly", input_modalities=("text", "image"),
                               context_window=200000, max_output=64000,
                               cost_input=3.0, cost_output=15.0,
                               cost_cache_read=0.3, cost_cache_write=None)

    monkeypatch.setattr(agent.models_dev, "get_model_info", fake_info)
    monkeypatch.setattr(agent.reasoning_effort, "route_supported_efforts",
                        lambda provider, model: ("low", "high"))
    payload = _payload(["claude", "unknown", "free"])
    infos = project_model_infos(payload)

    assert [i["id"] for i in infos] == ["claude", "unknown", "free"]
    first = infos[0]
    assert first["name"] == "Friendly"
    assert (first["provider"], first["provider_name"]) == ("anthropic", "Anthropic")
    assert first["base_url"] == "https://api.example"
    assert first["reasoning"] is True
    assert first["input"] == ["text", "image"]
    assert (first["context_window"], first["max_tokens"]) == (200000, 64000)
    assert first["cost"] == {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": None}
    assert first["thinking"] == {"levels": ["low", "high"]}

    assert infos[1]["name"] == "unknown"
    assert infos[1]["cost"] is None and infos[1]["input"] == [] and infos[1]["context_window"] is None
    # reasoning False on the capability row: no thinking block.
    assert infos[2]["reasoning"] is False and infos[2]["thinking"] is None

    assert all(call[2] is False for call in calls)
    assert payload["providers"][0]["models"] == ["claude", "unknown", "free"]


def test_zero_and_absent_prices_are_none(monkeypatch):
    monkeypatch.setattr(agent.models_dev, "get_model_info", lambda *a, **k: SimpleNamespace(
        name="", input_modalities=(), context_window=0, max_output=0,
        cost_input=0.0, cost_output=0.0, cost_cache_read=0.0, cost_cache_write=None))
    [info] = project_model_infos(_payload(["free"]))
    assert info["cost"] == {"input": None, "output": None, "cache_read": None, "cache_write": None}


def test_a_raising_catalog_lookup_degrades_to_provider_fields(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("catalog down")

    monkeypatch.setattr(agent.models_dev, "get_model_info", boom)
    [info] = project_model_infos(_payload(["claude"]))
    assert info["name"] == "claude" and info["cost"] is None
    assert info["context_window"] is None and info["max_tokens"] is None
    assert info["reasoning"] is True
    assert info["thinking"] == {"levels": list(
        agent.reasoning_effort.route_supported_efforts("anthropic", "claude"))}
