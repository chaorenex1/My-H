"""The omp rule funnel: dropped / TTSR / always-apply / rulebook, plus the rule_view set."""

from __future__ import annotations

from pathlib import Path

from agent import rule_buckets
from agent.rule_utils import parse_rule_text
from agent.ttsr_runtime import TtsrSettings


def _rule(frontmatter: str, *, rule_id: str = "x", body: str = "body",
          source: str | None = None) -> dict:
    text = f"---\n{frontmatter.strip()}\n---\n\n{body}\n"
    return parse_rule_text(text, source=source or f"/project/rules/{rule_id}.md", rule_id=rule_id)


def test_the_funnel_sends_each_rule_to_one_exit():
    conditional = _rule("description: Runtime.\ncondition: 'x'", rule_id="conditional")
    always = _rule("description: Always.\nalwaysApply: true", rule_id="always")
    listed = _rule("description: Listed.", rule_id="listed")
    bare = _rule("condition: 'y'", rule_id="bare")

    buckets = rule_buckets.bucket_rules([conditional, always, listed, bare], TtsrSettings())
    assert [rule["rule_id"] for rule in buckets.rulebook] == ["conditional", "listed"]
    assert [rule["rule_id"] for rule in buckets.always_apply] == ["always"]
    assert buckets.ttsr == ()

    registered = rule_buckets.bucket_rules(
        [conditional, always, listed, bare], TtsrSettings(), register_ttsr=True)
    assert [rule["rule_id"] for rule in registered.ttsr] == ["conditional", "bare"]
    assert [rule["rule_id"] for rule in registered.rulebook] == ["listed"]
    assert [rule["rule_id"] for rule in registered.always_apply] == ["always"]


def test_always_apply_wins_over_a_description():
    always = _rule("description: Always.\nalwaysApply: true")
    buckets = rule_buckets.bucket_rules([always], TtsrSettings())
    assert buckets.always_apply and not buckets.rulebook


def test_disabled_builtin_and_agent_gates_drop_every_exit():
    install = Path(__file__).resolve().parents[2] / "rules" / "ts-no-any.md"
    shipped = _rule("description: Shipped.\ncondition: 'x'", rule_id="shipped",
                    source=str(install))
    local = _rule("description: Local.\ncondition: 'x'", rule_id="local",
                  source="/project/rules/local.md")
    gated = _rule("description: Scout only.\nagents: scout", rule_id="gated",
                  source="/project/rules/gated.md")

    disabled = rule_buckets.bucket_rules(
        [shipped, local, gated], TtsrSettings(disabled_rules=("local",)),
        agent_name="main", register_ttsr=True)
    assert [rule["rule_id"] for rule in disabled.ttsr] == ["shipped"]
    assert disabled.rulebook == ()

    no_builtin = rule_buckets.bucket_rules(
        [shipped, local], TtsrSettings(builtin_rules=False), register_ttsr=True)
    assert [rule["rule_id"] for rule in no_builtin.ttsr] == ["local"]

    scout = rule_buckets.bucket_rules([gated], TtsrSettings(), agent_name="scout")
    assert [rule["rule_id"] for rule in scout.rulebook] == ["gated"]
    main = rule_buckets.bucket_rules([gated], TtsrSettings(), agent_name="main")
    assert not main.rulebook and not main.always_apply and not main.ttsr


def test_first_rule_for_a_name_wins():
    first = _rule("description: First.", rule_id="dup")
    second = _rule("description: Second.", rule_id="dup")
    buckets = rule_buckets.bucket_rules([first, second], TtsrSettings())
    assert [rule["description"] for rule in buckets.rulebook] == ["First."]


def test_addressable_rules_are_the_bucket_union(monkeypatch):
    monkeypatch.setattr(rule_buckets, "ttsr_session_active", lambda: False)
    rows = [
        _rule("description: Listed.", rule_id="listed"),
        _rule("description: Always.\nalwaysApply: true", rule_id="always"),
        _rule("condition: 'x'", rule_id="bare"),
        _rule("description: Off.", rule_id="off"),
    ]
    catalog = rule_buckets.addressable_rules(
        rows, TtsrSettings(disabled_rules=("off",)), agent_name="main")
    assert [rule["rule_id"] for rule in catalog] == ["listed", "always"]

    monkeypatch.setattr(rule_buckets, "ttsr_session_active", lambda: True)
    catalog = rule_buckets.addressable_rules(
        rows, TtsrSettings(disabled_rules=("off",)), agent_name="main")
    assert [rule["rule_id"] for rule in catalog] == ["listed", "always", "bare"]
