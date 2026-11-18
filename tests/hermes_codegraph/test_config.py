"""``code_intel`` is the only code-graph configuration domain."""

from __future__ import annotations

import pytest

from hermes_codegraph.config import CodeGraphLimits
from hermes_cli.config_defaults import DEFAULT_CONFIG


def test_defaults_expose_code_intel_and_no_codegraph_domain():
    assert "codegraph" not in DEFAULT_CONFIG
    section = DEFAULT_CONFIG["code_intel"]
    assert isinstance(section, dict)
    assert section["enabled"] is True
    assert section["auto_install"] is True
    assert section["semantic"] is False
    assert section["semgrep"] == {"enabled": False, "mode": "auto"}


def test_from_config_reads_code_intel_only():
    limits = CodeGraphLimits.from_config(
        {
            "code_intel": {
                "enabled": False,
                "semantic": True,
                "languages": ["java"],
                "semgrep": {"enabled": True, "mode": "custom_dir"},
            },
            "codegraph": {"enabled": True, "semantic": False},
        }
    )
    assert limits.enabled is False
    assert limits.semantic is True
    assert limits.languages == ("java",)
    assert limits.semgrep_enabled is True
    assert limits.semgrep_mode == "custom_dir"


def test_unknown_semgrep_mode_is_rejected_at_config_load():
    with pytest.raises(ValueError, match="semgrep.mode"):
        CodeGraphLimits.from_config(
            {"code_intel": {"semgrep": {"mode": "remote"}}}
        )


def test_missing_semgrep_block_uses_defaults():
    limits = CodeGraphLimits.from_config({"code_intel": {}})
    assert limits.semgrep_enabled is False
    assert limits.semgrep_mode == "auto"


def test_auto_install_defaults_on_and_can_be_disabled():
    assert CodeGraphLimits.from_config({"code_intel": {}}).auto_install is True
    assert (
        CodeGraphLimits.from_config({"code_intel": {"auto_install": False}}).auto_install
        is False
    )
