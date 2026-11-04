"""Idle recap: one ephemeral side turn that says where the session stands.

omp's ``IdleMaintenance`` asks its own session model for a <=40-word "goal, current task, one next
action" line after a stretch of activity and paints it as a transient ``RecapNotice``. This module
is the generation half: the prompt is the upstream ``recap-user.md`` text, the turn is a cache-parity
fork of the session's resident agent, and nothing is persisted (the client owns delivery, ADR-5 D9).

The client owns the idle timer and the suppression inputs only it knows (composer draft, current
view); this module runs only when the gateway handler has already decided the session is idle.
"""

import logging

logger = logging.getLogger(__name__)

RECAP_TASK = "recap"
RECAP_MAX_ITERATIONS = 1
RECAP_MAX_OUTPUT_TOKENS = 256
RECAP_MAX_BYTES = 4096

# omp ``prompts/system/side-channel-no-tools.md`` — the reminder riding the request, verbatim.
_NO_TOOLS_REMINDER = (
    "<system-reminder>\n"
    "Ephemeral side-channel turn; reuses current conversation context.\n"
    "Tool catalog attached only to keep prompt cache warm; tools NOT available this turn.\n"
    "Do NOT emit tool calls; reply plain text only. Tool calls discarded without execution.\n"
    "</system-reminder>"
)

# omp ``prompts/system/recap-user.md`` — the instruction, verbatim (one header line).
_RECAP_HEADER = (
    "<recap>\n"
    "User stepped away; returning. Recap: <40 words, 1–2 plain sentences, no markdown. Lead: "
    "overall goal, current task; then one next action. Skip: root-cause narrative, fix internals, "
    "secondary to-dos, em-dash tangents."
)

_DENY_MESSAGE = "Idle recap denied tool call: {tool_name}. Tools are disabled here — reply in plain text."


def build_recap_prompt(goal: str = "", task: str = "") -> str:
    """The rendered ``recap-user.md`` body: the optional anchors are their own lines, in order."""
    lines = [_RECAP_HEADER]
    if (goal := (goal or "").strip()):
        lines.append(f"Overall goal: {goal}")
    if (task := (task or "").strip()):
        lines.append(f"Active task: {task}")
    lines.append("</recap>")
    return "\n".join(lines)


def dedupe_recap_reply(text: str, max_bytes: int = RECAP_MAX_BYTES) -> str:
    """omp's ``dedupeEphemeralReply``: collapse runs over three identical lines to ``[…N×]``,
    then cut to *max_bytes* on a UTF-8 character boundary with a ``[…truncated]`` suffix."""
    if not text:
        return text
    lines = text.split("\n")
    out: list[str] = []
    index = 0
    while index < len(lines):
        end = index + 1
        while end < len(lines) and lines[end] == lines[index]:
            end += 1
        run = end - index
        if run > 3:
            out.extend((lines[index], f"[…{run}×]"))
        else:
            out.extend([lines[index]] * run)
        index = end
    result = "\n".join(out)
    encoded = result.encode("utf-8")
    if len(encoded) <= max_bytes:
        return result
    suffix = "\n[…truncated]"
    cut = max(0, max_bytes - len(suffix.encode("utf-8")))
    while cut > 0 and (encoded[cut] & 0xC0) == 0x80:
        cut -= 1
    return encoded[:cut].decode("utf-8", "ignore") + suffix


def generate_recap(agent: object, *, goal: str = "", task: str = "") -> str:
    """One cache-parity side turn over *agent*'s conversation; ``""`` on any failure.

    The fork shares the parent's system prompt/``tools[]`` (cache hit) and is detached from
    persistence; the parent's messages are never modified. Failure is silent to the UI (omp logs
    debug and shows nothing), so the next idle stretch may retry.
    """
    from agent.ephemeral_prompt import run_ephemeral_prompt

    prompt = f"{_NO_TOOLS_REMINDER}\n\n{build_recap_prompt(goal, task)}"
    try:
        text = run_ephemeral_prompt(
            agent, prompt, task=RECAP_TASK, max_iterations=RECAP_MAX_ITERATIONS,
            max_output_tokens=RECAP_MAX_OUTPUT_TOKENS, deny_message=_DENY_MESSAGE)
    except Exception:  # the recap is optional: any failure is a silent skip (ADR-5 acceptance 11)
        logger.debug("idle recap turn failed", exc_info=True)
        return ""
    return dedupe_recap_reply(text.strip()).strip()
