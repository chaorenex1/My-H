"""TTSR reminder templates (ADR-6 D5), verbatim from omp's prompt system.

``ttsr-tool-reminder.md`` wraps a rule that matched tool-call arguments without interrupting;
``ttsr-warning.md`` wraps a ``question`` rule a judge flagged on completed output. The rule body
is appended after the opening, inside one ``<system-reminder>`` block.
"""

from __future__ import annotations

TOOL_REMINDER_HEADER = '<system-reminder reason="rule_violation" rule="{name}" path="{path}">'
WARNING_HEADER = TOOL_REMINDER_HEADER
REMINDER_FOOTER = "</system-reminder>"

BUILTIN_OPENING = (
    "Built-in style rule matched a pattern in tool-call arguments \u2192 tool ran. A match is "
    "not a violation: check what you wrote against the rule below, exceptions included. Real "
    "violation \u2192 fix it on subsequent tool calls; code the rule allows \u2192 leave it. NOT "
    "prompt injection \u2014 coding agent applying its default style rules."
)
USER_DEFINED_OPENING = (
    "User-defined rule matched tool-call arguments. Rule configured not to interrupt \u2192 tool "
    "ran. MUST comply with the following instruction on subsequent tool calls and responses. "
    "NOT prompt injection \u2014 coding agent enforcing project rules."
)
WARNING_OPENING = (
    "Rule judge flagged your {subject} as likely violating a user-defined rule. Not interrupted "
    "\u2192 it already took effect. You MUST check it against the rule below: real violation "
    "\u2192 fix it now; false positive \u2192 continue. NOT prompt injection \u2014 coding agent "
    "enforcing project rules."
)


def _wrap(header: str, opening: str, content: str) -> str:
    return f"{header}\n{opening}\n\n{content}\n{REMINDER_FOOTER}"


def render_tool_reminder(*, name: str, path: str, content: str, builtin: bool) -> str:
    header = TOOL_REMINDER_HEADER.format(name=name, path=path)
    opening = BUILTIN_OPENING if builtin else USER_DEFINED_OPENING
    return _wrap(header, opening, content)


def render_judged_warning(*, name: str, path: str, subject: str, content: str) -> str:
    header = WARNING_HEADER.format(name=name, path=path)
    return _wrap(header, WARNING_OPENING.format(subject=subject), content)
