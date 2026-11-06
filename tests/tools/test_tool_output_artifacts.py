"""``tool_output.artifacts`` defaults, coercion, and ``hermes config`` plumbing (ADR-16 P0/P1)."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import patch

import pytest

from tools import tool_output_artifacts as artifacts


@pytest.fixture(autouse=True)
def _reset_cache():
    artifacts._reset_artifact_settings_cache()
    yield
    artifacts._reset_artifact_settings_cache()


def _settings(config):
    with patch("hermes_cli.config.load_config", return_value=config):
        return artifacts.get_artifact_settings()


class TestDefaults:
    def test_defaults_when_config_load_fails(self):
        with patch("hermes_cli.config.load_config", side_effect=RuntimeError("boom")):
            settings = artifacts.get_artifact_settings()
        assert settings == artifacts.ArtifactSettings(
            enabled=True,
            redact=True,
            exclude_tools=artifacts.DEFAULT_EXCLUDE_TOOLS,
        )

    def test_defaults_when_section_is_missing_or_malformed(self):
        for config in ({}, {"tool_output": None}, {"tool_output": {"artifacts": "off"}}):
            assert _settings(config).enabled is True

    def test_empty_exclude_list_is_a_real_opt_out(self):
        settings = _settings({"tool_output": {"artifacts": {"exclude_tools": []}}})
        assert settings.exclude_tools == ()


class TestOverrides:
    def test_partial_override_keeps_other_defaults(self):
        settings = _settings(
            {"tool_output": {"artifacts": {"enabled": False, "exclude_tools": ["web_search"]}}}
        )
        assert settings.enabled is False
        assert settings.redact is True
        assert settings.exclude_tools == ("web_search",)

    @pytest.mark.parametrize(
        ("value", "expected"),
        [("false", False), ("off", False), ("no", False), (0, False), (False, False),
         ("true", True), ("on", True), (1, True)],
    )
    def test_boolean_words(self, value, expected):
        settings = _settings({"tool_output": {"artifacts": {"enabled": value}}})
        assert settings.enabled is expected

    def test_bare_string_is_one_tool_name(self):
        settings = _settings({"tool_output": {"artifacts": {"exclude_tools": " terminal "}}})
        assert settings.exclude_tools == ("terminal",)

    def test_malformed_entries_are_dropped_and_duplicates_collapse(self):
        settings = _settings(
            {"tool_output": {"artifacts": {"exclude_tools": ["read_file", 3, "", "read_file",
                                                              "todo"]}}}
        )
        assert settings.exclude_tools == ("read_file", "todo")

    def test_non_string_exclude_value_falls_back(self):
        assert _settings(
            {"tool_output": {"artifacts": {"exclude_tools": 7}}}
        ).exclude_tools == artifacts.DEFAULT_EXCLUDE_TOOLS


class TestExclusion:
    def test_excluded_tool_lookup(self):
        settings = artifacts.ArtifactSettings(
            enabled=True, redact=True, exclude_tools=("read_file", "todo")
        )
        assert artifacts.is_excluded_tool("read_file", settings) is True
        assert artifacts.is_excluded_tool("terminal", settings) is False

    def test_lookup_resolves_settings_when_omitted(self):
        with patch("hermes_cli.config.load_config",
                   return_value={"tool_output": {"artifacts": {"exclude_tools": ["x"]}}}):
            assert artifacts.is_excluded_tool("x") is True


class TestCaching:
    def test_second_read_reuses_the_profile_cache_until_reset(self):
        first = {"tool_output": {"artifacts": {"enabled": True}}}
        second = {"tool_output": {"artifacts": {"enabled": False}}}
        with patch("hermes_cli.config.load_config", return_value=first):
            assert artifacts.get_artifact_settings().enabled is True
        with patch("hermes_cli.config.load_config", return_value=second):
            assert artifacts.get_artifact_settings().enabled is True
        artifacts._reset_artifact_settings_cache()
        with patch("hermes_cli.config.load_config", return_value=second):
            assert artifacts.get_artifact_settings().enabled is False


class TestConfigRoundTrip:
    def test_set_then_read_through_a_real_config_file(self):
        from hermes_cli.config import load_config, set_config_value

        set_config_value("tool_output.artifacts.enabled", "false")
        set_config_value("tool_output.artifacts.exclude_tools", '["web_search"]')
        artifacts._reset_artifact_settings_cache()

        settings = artifacts.get_artifact_settings()
        assert settings.enabled is False
        assert settings.exclude_tools == ("web_search",)
        assert settings.redact is True  # untouched leaf keeps the DEFAULT_CONFIG value
        assert load_config()["tool_output"]["artifacts"]["enabled"] is False


class TestDefaultConfigAndCli:
    def test_default_config_matches_the_reader_defaults(self):
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        config = cast(dict[str, Any], DEFAULT_CONFIG)
        tool_output = cast(dict[str, Any], config["tool_output"])
        section = cast(dict[str, Any], tool_output["artifacts"])
        assert section["enabled"] is artifacts.DEFAULT_ENABLED
        assert section["redact"] is artifacts.DEFAULT_REDACT
        assert tuple(section["exclude_tools"]) == artifacts.DEFAULT_EXCLUDE_TOOLS

    def test_config_set_validates_the_new_keys(self):
        from hermes_cli.config import _validate_config_key

        for key in ("tool_output.artifacts.enabled", "tool_output.artifacts.redact",
                    "tool_output.artifacts.exclude_tools"):
            assert _validate_config_key(key) == (True, None)

    def test_config_set_coerces_values_for_the_reader(self):
        from hermes_cli.config import _coerce_config_set_value

        assert _coerce_config_set_value("tool_output.artifacts.enabled", "false") is False
        assert _coerce_config_set_value(
            "tool_output.artifacts.exclude_tools", '["read_file", "todo"]'
        ) == ["read_file", "todo"]
