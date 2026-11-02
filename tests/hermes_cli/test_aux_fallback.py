"""``auxiliary.<task>.fallback_chain`` validation and the Config-page schema it must stay out of.

The runtime chain reader (``agent/auxiliary_client._try_configured_fallback_chain``) skips
entries without a provider or model, so a half-filled route is dead weight the write path refuses
up front — naming the offending index rather than failing silently at the next aux call.
"""

from __future__ import annotations

import pytest

from hermes_cli.aux_fallback import (
    MAX_FALLBACK_CHAIN_ENTRIES,
    normalize_fallback_chain,
    public_fallback_chain,
)
from hermes_cli.web_models import AuxFallbackRoute


def test_entries_are_stripped_and_base_url_is_kept_only_when_set():
    assert normalize_fallback_chain([
        {"provider": " openrouter ", "model": " google/gemini-3.6-flash ", "base_url": "   "},
        {"provider": "nous", "model": "hermes-4", "base_url": " https://edge.example/v1 "},
    ]) == [
        {"provider": "openrouter", "model": "google/gemini-3.6-flash"},
        {"provider": "nous", "model": "hermes-4", "base_url": "https://edge.example/v1"},
    ]


def test_an_empty_chain_or_none_clears():
    assert normalize_fallback_chain([]) == []
    assert normalize_fallback_chain(None) == []


def test_a_route_without_a_model_is_rejected_by_index():
    with pytest.raises(ValueError, match=r"fallback_chain\[1\] requires a model"):
        normalize_fallback_chain([
            {"provider": "nous", "model": "hermes-4"},
            {"provider": "openrouter", "model": "  "},
        ])


def test_a_route_without_a_provider_is_rejected_by_index():
    with pytest.raises(ValueError, match=r"fallback_chain\[0\] requires a provider"):
        normalize_fallback_chain([{"provider": "", "model": "m"}])


def test_a_non_list_and_a_non_object_entry_are_rejected():
    with pytest.raises(ValueError, match="must be a list"):
        normalize_fallback_chain("openrouter/x")
    with pytest.raises(ValueError, match=r"fallback_chain\[0\] must be an object"):
        normalize_fallback_chain(["openrouter/x"])


def test_the_chain_is_capped():
    entries = [{"provider": "nous", "model": f"m{i}"} for i in range(MAX_FALLBACK_CHAIN_ENTRIES + 1)]
    with pytest.raises(ValueError, match=f"at most {MAX_FALLBACK_CHAIN_ENTRIES} entries"):
        normalize_fallback_chain(entries)
    assert len(normalize_fallback_chain(entries[:-1])) == MAX_FALLBACK_CHAIN_ENTRIES


def test_a_rest_body_route_is_accepted():
    assert normalize_fallback_chain([AuxFallbackRoute(provider="nous", model="hermes-4")]) == [
        {"provider": "nous", "model": "hermes-4"}]


def test_the_read_view_keeps_usable_entries_and_drops_the_rest():
    stored = [{"provider": "nous", "model": "hermes-4"}, {"provider": ""}, "junk",
              {"provider": "openrouter", "model": "m", "base_url": "https://edge/v1"}]
    assert public_fallback_chain(stored) == [
        {"provider": "nous", "model": "hermes-4"},
        {"provider": "openrouter", "model": "m", "base_url": "https://edge/v1"},
    ]
    assert public_fallback_chain(None) == [] and public_fallback_chain({"provider": "x"}) == []


def test_the_config_page_never_offers_a_chain_editable_as_a_flat_list():
    from hermes_cli.web_server_config import CONFIG_SCHEMA

    generated = [key for key in CONFIG_SCHEMA if key.endswith("fallback_chain")]
    assert generated == []
    # Everything else under an aux task block still surfaces.
    assert "auxiliary.vision.timeout" in CONFIG_SCHEMA
    assert "auxiliary.commit_message.timeout" in CONFIG_SCHEMA
