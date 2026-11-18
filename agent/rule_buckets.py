"""Rule bucketing: omp's ``capability/rule-buckets.ts`` funnel, ported to Hermes.

Every visible rule ends in exactly one of three exits, decided in this order:

1. dropped — ``ttsr.disabled_rules`` names it, the built-in switch is off, or its
   ``agents`` glob excludes this agent;
2. TTSR — it has a runtime trigger (``condition`` / ``astCondition`` / ``question``,
   or Hermes' glob-only passive reminder) and registration succeeds;
3. always-apply — ``alwaysApply: true``, body injected in full under ``<generic-rules>``;
4. rulebook — it has a ``description``, listed as name + globs + description under
   ``<domain-rules>``.

A rule with no description, no ``alwaysApply`` and no successful TTSR registration is
unaddressable: it appears in no prompt block and ``rule_view`` cannot resolve it
(the ``rule_view`` set is rulebook + always-apply + registered TTSR).

The prompt uses ``register_ttsr=False`` unless the session is an omp-ui local viewer
(TTSR is installed per turn by ``agent.ttsr_runtime.install_coordinator``), so a rule is
still listed on CLI/Desktop/messaging sessions that never run the matcher.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from agent.rule_utils import is_builtin_rule_source, rule_applies_to_agent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuleBuckets:
    """One funnel result; rows keep their discovery order inside each bucket."""

    rulebook: tuple[Mapping[str, Any], ...] = ()
    always_apply: tuple[Mapping[str, Any], ...] = ()
    ttsr: tuple[Mapping[str, Any], ...] = ()
    unsupported: tuple[tuple[str, str], ...] = ()


def rule_name(rule: Mapping[str, Any]) -> str:
    return str(rule.get("name") or rule.get("rule_id") or "").strip()


def ttsr_session_active() -> bool:
    """Whether this session's turns install the TTSR coordinator.

    ``install_coordinator`` requires a local omp-ui viewer; reading the same client label
    keeps the prompt/tool view aligned with the runtime without importing the run loop.
    """
    try:
        from hermes_cli.plugin_session import session_client

        client = session_client() or {}
        return client.get("name") == "omp-ui"
    except Exception:  # health: allow BLE001 -- prompt/tool rendering must not depend on gateway state
        logger.debug("rule buckets: TTSR session probe failed", exc_info=True)
        return False


def ttsr_ast_available(rules: Sequence[Mapping[str, Any]]) -> bool:
    """Resolve ast-grep only when some rule needs it; ``select_rules`` rejects AST-only rules otherwise."""
    needs_ast = any(
        (rule.get("metadata") or {}).get("ast_condition") for rule in rules
        if isinstance(rule, Mapping))
    if not needs_ast:
        return True
    from agent.ttsr_ast import resolve_ast_grep

    return resolve_ast_grep() is not None


def bucket_rules(
    rules: Sequence[Mapping[str, Any]],
    settings: Any | None = None,
    *,
    agent_name: str = "",
    register_ttsr: bool = False,
    ast_available: bool = False,
) -> RuleBuckets:
    """Split *rules* into the three exits after applying the config/agent gates.

    *settings* is an ``agent.ttsr_runtime.TtsrSettings``; omitted means the dataclass defaults,
    so a caller that wants config.yaml must pass ``load_ttsr_settings()``.
    ``register_ttsr`` asks the real selector to register candidates; a rejected rule
    (unsupported mode, missing AST backend, invalid regex) falls through to the static
    buckets instead of disappearing.
    """
    from agent.ttsr_runtime import TtsrSettings, select_rules

    settings = settings if settings is not None else TtsrSettings()
    disabled = set(settings.disabled_rules)
    eligible: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for rule in rules:
        name = rule_name(rule)
        if not name or name in seen:
            continue
        seen.add(name)
        metadata = rule.get("metadata") or {}
        if not metadata.get("enabled", True):
            continue
        if name in disabled:
            continue
        if not settings.builtin_rules and is_builtin_rule_source(str(rule.get("source") or "")):
            continue
        if not rule_applies_to_agent(rule, agent_name):
            continue
        eligible.append(rule)

    ttsr_names: set[str] = set()
    unsupported: tuple[tuple[str, str], ...] = ()
    if register_ttsr:
        active, rejected = select_rules(eligible, settings, ast_available=ast_available)
        ttsr_names = {rule.name for rule in active}
        unsupported = tuple(rejected)

    rulebook: list[Mapping[str, Any]] = []
    always_apply: list[Mapping[str, Any]] = []
    ttsr: list[Mapping[str, Any]] = []
    for rule in eligible:
        if rule_name(rule) in ttsr_names:
            ttsr.append(rule)
        elif (rule.get("metadata") or {}).get("always_apply"):
            always_apply.append(rule)
        elif str(rule.get("description") or "").strip():
            rulebook.append(rule)
    return RuleBuckets(tuple(rulebook), tuple(always_apply), tuple(ttsr), unsupported)


def addressable_rules(
    rules: Sequence[Mapping[str, Any]],
    settings: Any | None = None,
    *,
    agent_name: str = "",
) -> list[Mapping[str, Any]]:
    """The ``rule_view`` set: rulebook + always-apply + registered TTSR, in discovery order."""
    from agent.ttsr_runtime import TtsrSettings

    settings = settings if settings is not None else TtsrSettings()
    buckets = bucket_rules(
        rules,
        settings,
        agent_name=agent_name,
        register_ttsr=bool(settings.enabled and ttsr_session_active()),
        ast_available=ttsr_ast_available(rules),
    )
    order: dict[str, int] = {}
    for index, rule in enumerate(rules):
        order.setdefault(rule_name(rule), index)
    merged = [*buckets.rulebook, *buckets.always_apply, *buckets.ttsr]
    return sorted(merged, key=lambda rule: order[rule_name(rule)])


__all__ = [
    "RuleBuckets",
    "addressable_rules",
    "bucket_rules",
    "rule_name",
    "ttsr_ast_available",
    "ttsr_session_active",
]
