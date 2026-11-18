"""Defaults for the judgment consumers added by ADR-17 P5.

Pure data only: the classifier knobs live under ``agent`` and the one-shot model
handoff is a top-level section. Every consumer treats a missing key as these
values, so an older config.yaml keeps the conservative behavior.
"""

from __future__ import annotations


def agent_consumer_defaults() -> dict:
    """Auto-thinking and semantic-stop classifiers."""
    return {
        # Per-user-turn reasoning classifier (ADR-17 P5). "off" keeps the configured effort;
        # "auto" only trusts a native judge (calibrated); "on" also allows the text route.
        # The resolved level is fixed for the whole turn so the prompt prefix stays cached.
        "reasoning_auto": "off",  # off | auto | on
        "reasoning_auto_max_effort": "xhigh",  # xhigh | max
        "reasoning_auto_timeout_ms": 4000,
        # Semantic stop classifier (ADR-17 P5): "mechanical" keeps the shape-based recovery
        # only, "smart" also judges a text answer that announces work it never started.
        "unexpected_stop_detection": "mechanical",  # none | mechanical | smart
    }


def top_level_consumer_defaults() -> dict:
    """One-shot prewalk handoff settings (off by default: a handoff costs a cache rebuild)."""
    return {
        "prewalk": {
            "enabled": False,
            "from": "@default",
            "into": "@smol",
            "auto": "off",  # off | suggest | on
            "auto_target": "@smol",
        },
    }
