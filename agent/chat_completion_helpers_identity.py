"""Cached-prompt identity rewrite after a provider/model switch (ADR-19 D7.3)."""

from __future__ import annotations

import re


def rewrite_prompt_model_identity(agent, model: str, provider: str) -> None:
    """Rewrite the cached prompt's ``Model:``/``Provider:`` lines after a provider switch.

    Not persisted: the stored row keeps the primary's labels so a restored primary replays a
    byte-identical prompt (prefix cache intact). The labels live in the persisted
    ``<workstation>`` block; a legacy prompt keeps them in the trailer. Only that block is
    touched — memory snapshots and context files may hold ``Model:`` lines of their own."""
    sp = getattr(agent, "_cached_system_prompt", None)
    if not isinstance(sp, str) or not sp:
        return
    values = ((label, value) for label, value in (("Model", model), ("Provider", provider)) if value)
    tail_start = sp.rfind("<project-context>")
    workstation = re.search(r"<workstation>\n.*?</workstation>", sp[max(tail_start, 0):], re.DOTALL)
    if workstation:
        block = workstation.group(0)
        for label, value in values:
            block = re.sub(rf"(?m)^- {label}: .*$", lambda _m, l=label, v=value: f"- {l}: {v}", block)
        offset = max(tail_start, 0)
        sp = f"{sp[:offset + workstation.start()]}{block}{sp[offset + workstation.end():]}"
    else:
        for label, value in values:
            matches = list(re.finditer(rf"(?m)^{label}: .*$", sp))
            if matches:
                last = matches[-1]
                sp = f"{sp[:last.start()]}{label}: {value}{sp[last.end():]}"
    agent._cached_system_prompt = sp
