"""Auto-generate short session titles from the user's opening message.

Two stages, both off the critical path: an **instant** deterministic title (written before the model
is called, cannot fail), then an **upgrade** from one small-model call (cheap tier, thinking off,
JSON-constrained). Storage enforces provenance ``derived < llm < user``: stage 2 only replaces stage 1
and neither replaces a name the user typed."""

import json
import logging
import os
import re
import threading
import time
import weakref
from contextlib import suppress
from typing import Any, Callable, Optional

from agent.auxiliary_client import call_llm
from agent.context_compressor import LEGACY_SUMMARY_PREFIX
from agent.delegation_context import is_dispatcher_owned_worker_context
from agent.message_content import flatten_message_text

logger = logging.getLogger(__name__)

# In-flight stage-2 upgrade threads. They bill their aux usage to the session from a daemon thread,
# so a process that reads the ledger right before exit (``-z --usage-file``) must be able to join
# them (bounded) instead of racing the write (#112848).
_UPGRADE_THREADS: weakref.WeakSet[threading.Thread] = weakref.WeakSet()


def wait_for_title_upgrades(timeout: float = 10.0) -> None:
    """Bounded join of the auto-title threads still running; never raises."""
    deadline = time.monotonic() + timeout
    for thread in list(_UPGRADE_THREADS):
        thread.join(max(0.0, deadline - time.monotonic()))

# (task_name, exception) -> None; surfaces auxiliary failures so silent drops don't pile up as NULL titles.
FailureCallback = Callable[[str, BaseException], None]
# (title, source) -> None; source is the persisted provenance (``derived`` / ``llm``). Consumers paying a
# rate-limited remote rename per title (Discord thread, Telegram topic) should act on ``llm`` only.
TitleCallback = Callable[[str, str], None]
# () -> bool, called right before the LLM request; False skips (e.g. the user switched models and
# the request would reload one the runtime already evicted).
# Validation callback: () -> bool. See #19027.
RuntimeValidator = Callable[[], bool]

# Text budget handed to the model (Claude Code / OpenClaw converged on 1000).
MAX_TITLE_INPUT_CHARS = 1000
_PASTE_PREVIEW_LABEL = "\n\nPasted content:\n"
_ATTACHMENT_REF_RE = re.compile(r"@(?:file|folder):\S+")
# Footers the @-reference expander appends below the typed text (agent/context_references.py).
_CONTEXT_FOOTER_RE = re.compile(r"\n+--- (?:Context Warnings|Attached Context) ---\n.*", re.DOTALL)
# Cap on the instant derived title; a raw fragment reads worse the longer it runs.
MAX_DERIVED_TITLE_CHARS = 48
# Answer-shaped guard: a tiny model sometimes answers instead of titling; longer is rejected, not truncated.
# Upper bound on accepted title word count. Titling is a 3-7 word task; a small tiny-model sometimes ignores
# the task and answers the user's message instead — that answer must never become the session title (see the
# answer-shaped output guard in generate_title; port of can1357/oh-my-pi#7306). 12 leaves headroom for
# legitimate wordy titles while excluding full-sentence answers.
_MAX_TITLE_WORDS = 12
# Output budget for the title call: room for a fenced/prefixed JSON reply and for a reasoning model that
# thinks despite the thinking-disabled request, without letting a runaway reply burn minutes.
TITLE_MAX_TOKENS = 512

# Title-drift judging (ADR-17 P5, port of omp ``title-drift-classifier.ts`` / ``messages.ts``): the
# judge compares the stored title with the latest conversation, and only a drifted title is
# regenerated. A single turn is capped so one long reply cannot fill the window, and the whole window
# is capped so the judgment stays cheap.
TITLE_DRIFT_CONTEXT_CHARS = 3000
TITLE_DRIFT_TURN_CHARS = 1000
# Yes-probability below which the title no longer fits the conversation (omp's tuned cutoff; keyword
# judges answer exactly 0 or 1).
TITLE_DRIFT_FITS_THRESHOLD = 0.5
TITLE_DRIFT_JUDGE_TIMEOUT_MS = 15_000
# Assistant rows that carry no usable conversation text; ``finish_reason`` mirrors the values the
# session pickers classify as an errored/interrupted turn.
_TITLE_DRIFT_DEAD_FINISH_REASONS = frozenset({
    "aborted", "error", "agent_error", "content_filter", "error_finish", "interrupted",
})

# The example titles shown to the model in the prompt, and the echo-guard
# set: when the opening message carries little topical signal, a small model
# sometimes takes the cheapest schema-valid answer and parrots one of these
# back verbatim — most visibly "Fix login button on mobile" naming sessions
# that have nothing to do with a login button. The prompt's example lines are
# rendered from these constants so the guard set and the prompt cannot drift
# apart. Port of QwenLM/qwen-code#9709.
_PROMPT_GOOD_EXAMPLES = (
    "Fix login button on mobile",
    "Postgres connection pool exhaustion",
    "Friendly greeting",
)
_PROMPT_VAGUE_EXAMPLE = "Code changes"

# "Friendly greeting" is deliberately NOT in the reject set: the prompt
# instructs the model to produce it for bare greetings, so it is a legitimate
# output, not an echo failure. The too-vague example is rejected too — a model
# repeating the counter-example says nothing about the session, and the
# derived title the guard falls back to is strictly more informative.
_EXAMPLE_ECHO_REJECT = frozenset(
    t.lower() for t in _PROMPT_GOOD_EXAMPLES if t != "Friendly greeting"
) | {_PROMPT_VAGUE_EXAMPLE.lower()}

# "Friendly greeting" is what the prompt asks for when the opener has no topic yet, so
# it is the one model title that must NOT settle the session: it is persisted at
# ``derived`` authority (a placeholder, like the instant title) and the next substantive
# turn upgrades it. Kept as a prompt example on purpose — a predictable placeholder is
# detectable, an improvised one ("Casual check-in chat") would lock the title as ``llm``.
_PROVISIONAL_GREETING_TITLE = "friendly greeting"


_TITLE_PROMPT_TEMPLATE = (
    "You name chat sessions. Given the user's opening message, write a title "
    "that lets them find this conversation again in a list.\n\n"
    "Rules:\n"
    "- 3 to 7 words, sentence case (capitalize only the first word and proper nouns).\n"
    "- Name what the user wants DONE, not that they asked a question.\n"
    "- Keep technical terms, filenames, numbers, and error codes exact.\n"
    "- Drop filler words: the, this, my, a, an.\n"
    "- No trailing punctuation, no quotes, no tool names, no 'Title:' prefix.\n"
    "- Never answer the message. Name it.\n"
    "- Always produce something, even for a bare greeting.\n"
    "__LANGUAGE_RULE__\n"
    + "".join(f'Good: {{"title": "{t}"}}\n' for t in _PROMPT_GOOD_EXAMPLES)
    + f'Too vague: {{"title": "{_PROMPT_VAGUE_EXAMPLE}"}}\n'
    'Too long: {"title": "Investigate and fix the issue where the login button '
    'does not respond on mobile devices"}\n\n'
    'Reply with JSON only: {"title": "..."}'
)

_LANGUAGE_RULE_MATCH_USER = "- Write the title in the same language as the user's message."
_LANGUAGE_RULE_PINNED = "- Write the title in {language}."

# Constrains the response to a single title field ("model answered instead of titling" failure class).
_TITLE_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "session_title", "strict": True, "schema": {
        "type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"], "additionalProperties": False}},
}

# Control-tag wrappers around machine-authored content inside a nominal "user" message (Codex CLI's
# RECOGNIZED_CONTROL_WRAPPERS): stripped, titling continues on what remains.
_CONTROL_WRAPPERS = tuple(
    (f"<{tag}>", f"</{tag}>")
    for tag in ("command-message", "command-name", "command-args", "local-command-caveat", "local-command-stderr",
                "local-command-stdout", "task-notification", "system-reminder", "ide_opened_file", "ide_selection")
)

# Hermes' own machine-authored openers: a compaction handoff or resumed session must not be titled after them.
_MACHINE_PREFIXES = (
    "[CONTEXT COMPACTION", LEGACY_SUMMARY_PREFIX, "[Runtime note:", "[System note:", "[SYSTEM]",
    # tui_gateway.server._MODEL_SWITCH_MARKER_PREFIX (keep in sync); persisted as role="user" because
    # strict providers reject a non-first system message.
    # Model-switch marker from tui_gateway.server._append_model_switch_marker. It is persisted with
    # role="user" (strict OpenAI-compatible providers reject a system message that is not first — #48338),
    # so without this entry it looks like a real opening turn: switching models before the first real
    # message titled the session "[System: The active model for this chat has…" instead of the user's actual
    # question.
    "[System: The active model for this chat has changed to ",
)


def _title_config() -> dict:
    """``auxiliary.title_generation`` (lazy read-only import: no hermes_cli cycle, no migration writes)."""
    from hermes_cli.config import load_config_readonly
    return ((load_config_readonly() or {}).get("auxiliary") or {}).get("title_generation") or {}


def _title_language() -> str:
    """Configured title language, or "" to match the user."""
    try:
        return str(_title_config().get("language", "")).strip()
    except Exception:
        return ""


def _auto_title_enabled() -> bool:
    try:
        from utils import is_truthy_value
        return is_truthy_value(_title_config().get("enabled"), default=True)
    except Exception:
        logger.debug("Failed to read title_generation.enabled", exc_info=True)
        return True


def _model_title_upgrade_enabled() -> bool:
    """Distinct from ``enabled``: keep the instant derived title, skip the background model call (#85194)."""
    try:
        from utils import is_truthy_value
        return is_truthy_value(_title_config().get("model_upgrade_enabled"), default=True)
    except Exception:
        logger.debug("Failed to read title_generation.model_upgrade_enabled", exc_info=True)
        return True


def _title_refresh_after_prompts() -> int:
    """``auxiliary.title_generation.refresh_after_prompts``; 0 (the default) disables drift checks."""
    try:
        raw = _title_config().get("refresh_after_prompts", 0)
        if isinstance(raw, bool):  # bool is an int subclass; ``true`` is not an interval
            return 0
        return max(0, int(raw))
    except Exception:
        logger.debug("Failed to read title_generation.refresh_after_prompts", exc_info=True)
        return 0


def title_upgrade_must_wait_for_turn(main_runtime: Optional[dict]) -> bool:
    """True when the model title call would hit the SAME self-hosted endpoint as the turn's own request.

    A self-hosted main route (``_is_self_hosted_provider``: custom, lmstudio, local and their aliases)
    whose ``auxiliary.title_generation`` is not pinned elsewhere (a pin naming the same custom route —
    ``custom:<name>``, bare ``<name>`` or its display name — is not "elsewhere", #120558)
    shares one local server between the streaming main request and the
    concurrent ``response_format: json_schema`` title request. Single-slot servers then serve the
    title grammar/completion into the main turn: the user's reply arrives as ``{"title": ...}``, is
    persisted as a genuine assistant row and replayed, and the model adopts the format (#117296).
    Running the title call after the turn settles keeps the two requests off the wire at once.
    Hosted providers multiplex requests independently and keep the turn-start timing.
    """
    provider = str((main_runtime or {}).get("provider") or "").strip().lower()
    if not _is_self_hosted_provider(provider):
        return False
    try:
        cfg = _title_config()
        pinned_provider = str(cfg.get("provider") or "").strip().lower()
        main_base_url = str((main_runtime or {}).get("base_url") or "").strip().rstrip("/")
        if pinned_provider not in ("", "auto") and not _title_pin_may_share_endpoint(
                pinned_provider, provider, main_base_url):
            return False
    except Exception:
        return True
    pinned_base_url = str(cfg.get("base_url") or "").strip().rstrip("/")
    return not pinned_base_url or pinned_base_url == main_base_url


def _is_self_hosted_provider(provider: str) -> bool:
    """``custom``, a named ``custom:<name>`` route, LM Studio or ``local`` (vllm/llama.cpp): one local server per route.

    Normalised here so the main route and the title pin resolve aliases (``ollama``, ``lm-studio``…) the same way.
    """
    from hermes_cli.providers import normalize_provider
    provider = normalize_provider(provider)
    return provider in ("custom", "lmstudio", "local") or provider.startswith("custom:")


def _title_pin_may_share_endpoint(pinned_provider: str, main_provider: str, main_base_url: str) -> bool:
    """A title pin that can land on the turn's own self-hosted server (only its ``base_url`` can prove otherwise).

    Hosted pins (``openrouter``…) multiplex and never share the slot. A pin to ``custom``/``lmstudio``/``local``/any
    ``custom:<name>`` is assumed to share until the caller compares ``base_url``, and a bare ``<name>`` /
    display-name pin is the same endpoint when it aliases the main ``custom:<name>`` route
    (``hermes_cli.providers.custom_provider_aliases`` — the resolver's own identity set) or resolves to a
    configured custom entry serving ``main_base_url`` (a keyed ``providers:`` entry's display name does not
    alias its ``custom:<key>`` id).
    """
    from hermes_cli.config import get_compatible_custom_providers, load_config_readonly
    from hermes_cli.providers import custom_provider_aliases, resolve_custom_provider
    if _is_self_hosted_provider(pinned_provider):
        return True
    if custom_provider_aliases(pinned_provider) & custom_provider_aliases(main_provider):
        return True
    pdef = resolve_custom_provider(pinned_provider, get_compatible_custom_providers(load_config_readonly()))
    return bool(pdef and main_base_url and pdef.base_url.strip().rstrip("/") == main_base_url)


def _start_title_thread(thread: Optional[threading.Thread]) -> None:
    """Start a title-related daemon thread and make it joinable at process exit; no-op once started."""
    if thread is None or thread.ident is not None:
        return
    _UPGRADE_THREADS.add(thread)
    thread.start()


def start_title_upgrade(upgrade: Optional[threading.Thread]) -> None:
    """Start a (deferred) title upgrade thread; joinable via ``wait_for_title_upgrades`` only once started."""
    _start_title_thread(upgrade)


def strip_control_wrappers(text: str) -> str:
    """Remove leading control wrappers (nested too) so a slash-command turn reduces to the prose the user typed."""
    current = (text or "").strip()
    for _ in range(len(_CONTROL_WRAPPERS) * 2):  # bounded: each pass must remove a wrapper or we stop
        stripped = _strip_one_wrapper(current)
        if stripped == current:
            break
        current = stripped
    return current


def _strip_one_wrapper(text: str) -> str:
    lowered = text.lower()
    for open_tag, close_tag in _CONTROL_WRAPPERS:
        if not lowered.startswith(open_tag):
            continue
        end = lowered.find(close_tag)
        if end == -1:  # unterminated wrapper: drop the opening tag and keep the body
            return text[len(open_tag):].strip()
        # Prefer trailing prose; otherwise the wrapper body is all we have.
        return (text[end + len(close_tag):].strip() or text[len(open_tag):end].strip()).strip()
    return text


# Matches an ``@file:``/``@folder:`` context reference the way the canonical
# ``agent.context_references`` reference pattern does: an unquoted ``\S+``
# value, or a backtick/double/single-quoted value (a space-bearing path is
# written backtick-quoted). Kept local so the titler doesn't import the
# context-reference machinery (circularity / startup cost) just to detect the
# attachment-only shape (#92068).
_QUOTED = r"(?:`[^`\n]+`|\"[^\"\n]+\"|'[^'\n]+')"
# Mirrors the canonical agent.context_references value shape: a quoted
# (space-bearing) path may carry a ``:start[-end]`` line-range suffix, and a
# bare path swallows its own range via ``\S+``.
_CONTEXT_REFERENCE_TOKEN_RE = re.compile(
    rf"(?<![\w/])@(?:file|folder):(?:{_QUOTED}(?::\d+(?:-\d+)?)?|\S+)"
)


def _attachment_only_opener(message: str) -> bool:
    """True when nothing but context references (and their expansion footer)
    remain — a manual-attach opener with no typed request and no paste
    preview has no topic of its own to title (#92068)."""
    residual = _CONTEXT_REFERENCE_TOKEN_RE.sub("", _CONTEXT_FOOTER_RE.sub("", message))
    return not residual.strip()


def _summarize_user_message(user_message: str) -> str:
    """Text worth titling: describe a ``/skill`` invocation (it embeds the whole skill body), then strip wrappers."""
    if not user_message:
        return ""
    described = None
    try:
        from agent.skill_commands import describe_skill_invocation
        described = describe_skill_invocation(user_message)
    except Exception:
        logger.debug("Skill-scaffolding summary failed; titling raw", exc_info=True)
    return strip_control_wrappers(user_message if described is None else described)


def build_title_input(user_message: str, title_preview: str | None = None, *,
                      max_chars: int = MAX_TITLE_INPUT_CHARS) -> str:
    """Combine the opening text with a bounded Desktop-generated paste preview.

    ``title_preview`` is deliberately an explicit, auxiliary-only value: ordinary
    attachments never populate it, and it is never returned to the main turn.
    Keep enough of a separately typed request to preserve a useful instruction,
    then spend the remaining title budget on the beginning of the pasted topic.
    ``max_chars`` widens the budget for a conversation context (title drift), which is
    already bounded per turn and in total by its builder.
    """
    message = _summarize_user_message(user_message)
    preview = title_preview.strip() if isinstance(title_preview, str) else ""
    if not preview:
        return message[:max_chars]
    # The titler sees the message AFTER @-reference expansion, so the generated ref drags a
    # warnings/attached-context footer along; the preview already carries the topic, so drop it.
    message = _CONTEXT_FOOTER_RE.sub("", message).strip()
    # A paste-only opener is just the generated `@file:` ref: the preview IS the topic, so it
    # leads (derive_title takes the first line, and a file path is not a title).
    if not _ATTACHMENT_REF_RE.sub("", message).strip():
        return preview[:max_chars]
    message_budget = min(len(message), max_chars // 2)
    preview_budget = max_chars - message_budget - len(_PASTE_PREVIEW_LABEL)
    if preview_budget <= 0:
        return message[:max_chars]
    return message[:message_budget] + _PASTE_PREVIEW_LABEL + preview[:preview_budget]


def is_titleable_user_message(user_message: str) -> bool:
    """False for machine-authored openers and turns that reduce to nothing once scaffolding is stripped."""
    return (isinstance(user_message, str) and bool(user_message.strip()) and not user_message.lstrip().startswith(_MACHINE_PREFIXES)
            and bool(_summarize_user_message(user_message).strip())
            # An attachment-only opener (manual attach, no paste preview) is a
            # file drop, not a request: deriving its "title" from the message
            # names the session after the truncated file path (#92068).
            and not _attachment_only_opener(user_message))


def derive_title(user_message: str, title_preview: str | None = None) -> Optional[str]:
    """Instant title: first meaningful line trimmed to a word boundary. No model, never fails."""
    # Attachment-only opener, no paste preview: a file drop has no topic —
    # refuse rather than name the session after the truncated path (#92068).
    if not title_preview and _attachment_only_opener(user_message):
        return None
    line = " ".join(_first_line(build_title_input(user_message, title_preview)).split())
    if len(line) > MAX_DERIVED_TITLE_CHARS:
        cut = line[:MAX_DERIVED_TITLE_CHARS]
        space = cut.rfind(" ")
        line = (cut[:space] if space > MAX_DERIVED_TITLE_CHARS // 2 else cut).rstrip(" ,.;:—-") + "…"
    return line or None


def _strip_title_prefix(text: str) -> str:
    return text[6:].strip() if text.lower().startswith("title:") else text


def _first_line(text: str) -> str:
    return next((ln.strip() for ln in text.splitlines() if ln.strip()), "")


def _extract_json_title(raw: str) -> Optional[str]:
    """Title from a ``{"title": ...}`` payload — strict parse, then a loose ``"title": "..."`` scan; None when absent."""
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and isinstance(parsed.get("title"), str):
            return parsed["title"].strip()
    except (ValueError, TypeError):
        pass
    match = re.search(r'"title\"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
    if match:
        with suppress(ValueError):
            return json.loads(f'"{match.group(1)}"').strip()
        return match.group(1).strip()
    return None


def _is_truncated_structured_output(raw: str) -> bool:
    """Structured output the token cap cut before its closing quote/brace/fence (``{"title``, a bare fence opener).

    Checked only after the JSON paths failed, and on structure alone (a JSON-shaped opener, a fence
    opener that is never closed) so quoted, *emphasized* or ``[WIP]``-prefixed prose titles are
    untouched (#83903)."""
    return raw.startswith(('{"', '["', "[{")) or (raw.startswith("```") and raw.count("```") % 2 == 1)


def _extract_title_text(content: str) -> str:
    """Strict JSON, then a loose JSON scan, then first-line prose (a provider ignoring ``response_format`` still titles).

    A truncated structured payload is dropped rather than handed to the prose fallback: the fragment
    would otherwise be persisted as the session title."""
    if not content:
        return ""
    raw = content.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", raw, re.DOTALL)
    if fenced:
        raw = fenced.group(1).strip()
    title = _extract_json_title(raw)
    if title is not None:
        return title
    if _is_truncated_structured_output(raw):
        return ""
    # Prose fallback: scrub <think> blocks so reasoning can't leak into a title.
    try:
        from agent.agent_runtime_helpers import strip_think_blocks
        raw = strip_think_blocks(None, raw).strip()
    except Exception:
        logger.debug("strip_think_blocks unavailable for title output", exc_info=True)
    return _strip_title_prefix(_first_line(raw)).strip("\"'").strip()


def _title_from_reasoning(message: Any) -> str:
    """The ``{"title": ...}`` payload when a reasoning model put it in ``reasoning_content`` / ``reasoning``
    and left ``content`` empty (glm-5 / minimax under ``json_schema``, #82291). Structured extraction only:
    chain-of-thought prose is never a title, so there is no prose fallback here."""
    for field in ("reasoning_content", "reasoning"):
        text = getattr(message, field, None)
        if isinstance(text, str) and text.strip():
            title = _extract_json_title(text.strip())
            if title:
                return title
    return ""


def _clean_title(text: str) -> Optional[str]:
    """Normalize a model-produced title, or None when nothing usable remains."""
    title = _strip_title_prefix(" ".join((text or "").split()).strip("\"'").strip()).rstrip(".!,;:")
    if len(title) > 80:
        title = title[:77].rstrip() + "..."
    return title or None


def _safe_callback(callback: Optional[Callable], args: tuple, log_fmt: str, label: str) -> None:
    """Invoke an optional consumer callback, never raising."""
    try:
        if callback is not None:
            callback(*args)
    except Exception:
        logger.debug(log_fmt, label, exc_info=True)


def _report_failure(failure_callback: Optional[FailureCallback], exc: BaseException, label: str) -> None:
    _safe_callback(failure_callback, ("title generation", exc), "%s failure_callback raised", label)


def _notify_title(title_callback: Optional[TitleCallback], title: str, source: str, label: str) -> None:
    _safe_callback(title_callback, (title, source), "%s callback failed", label)


def _is_provisional_greeting_title(title: str) -> bool:
    """The prompt's greeting placeholder (also "Friendly greeting in chat" and quoted/bracketed variants)."""
    normalized = re.sub(r"^[\W_]+|[\W_]+$", "", title.strip(), flags=re.UNICODE).lower()
    return normalized in (_PROVISIONAL_GREETING_TITLE, _PROVISIONAL_GREETING_TITLE + " in chat")


def _is_prompt_example_echo(title: str) -> bool:
    """Return True when *title* is one of the prompt's own example titles.

    Comparison is case-insensitive after stripping any leading/trailing run of
    non-letter/non-digit characters, so bracket/quote wrappers cannot bypass
    the guard — while ``_clean_title`` keeps brackets for real titles like
    "(WIP) Fix build". Unicode-aware so full-width wrappers are covered too.
    """
    normalized = re.sub(r"^[\W_]+|[\W_]+$", "", title.strip(), flags=re.UNICODE).lower()
    return normalized in _EXAMPLE_ECHO_REJECT


def generate_title(
    user_message: str,
    timeout: Optional[float] = None,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: dict | None = None,
    runtime_validator: Optional[RuntimeValidator] = None,
    title_preview: str | None = None,
    max_input_chars: int = MAX_TITLE_INPUT_CHARS,
) -> Optional[str]:
    """Title from the opening message alone (waiting for the assistant made this slow and bought
    nothing). ``runtime_validator`` runs right before the request; False skips silently.

    If it returns False (e.g. the user's model was switched since the background thread captured its runtime
    snapshot), the call is skipped silently — no request is sent, so a stale title request can't reload a
    model the runtime already unloaded (#19027). ``max_input_chars`` lets a caller pass an already bounded
    conversation context (title drift) without the opening-message budget cutting its latest turns.
    """
    if not _auto_title_enabled():
        logger.debug("Auto-title skipped: auxiliary.title_generation.enabled=false")
        return None
    try:
        if runtime_validator is not None and not runtime_validator():
            logger.debug("Title generation skipped: runtime validator returned False")
            return None
    except Exception:  # fail open: a broken validator must not disable titling
        logger.debug("Title runtime validator raised; proceeding", exc_info=True)
    user_snippet = build_title_input(user_message, title_preview, max_chars=max_input_chars)
    if not user_snippet.strip() or (
        # An attachment-only opener (manual attach, no paste preview) reaches
        # here via auto_title_session, which lacks the instant-title guard:
        # refuse it rather than titling the session after the file path (#92068).
        not title_preview and _attachment_only_opener(user_snippet)
    ):
        return None
    language = _title_language()
    # str.replace, not str.format: the prompt embeds literal JSON braces.
    prompt = _TITLE_PROMPT_TEMPLATE.replace(
        "__LANGUAGE_RULE__", _LANGUAGE_RULE_PINNED.format(language=language) if language else _LANGUAGE_RULE_MATCH_USER,
    )
    try:
        # Use the provider's default temperature instead of forcing 0.3.
        # Some models (e.g. GPT-5.6) only accept their server-side default
        # and reject explicit temperature values, causing the daemon title
        # thread to fail with "Unsupported value: 'temperature'".
        # See: #72351, #51083, #51157
        response = call_llm(
            task="title_generation",
            messages=[{"role": "system", "content": prompt}, {"role": "user", "content": user_snippet}],
            # A title is a handful of tokens, but 64 was cut mid-JSON by fenced/prefixed replies and by
            # reasoning models whose thinking survives the disable below (#83903, #82291). A model that
            # honours the JSON contract stops after ~15 tokens regardless, so the ceiling only costs on
            # replies that would have been garbage anyway. temperature=None: omitted from the wire so
            # default-only reasoning models accept the first request (#72351).
            max_tokens=TITLE_MAX_TOKENS, temperature=None, timeout=timeout, main_runtime=main_runtime,
            extra_body={"response_format": _TITLE_RESPONSE_FORMAT},
            # The module contract above promises thinking-disabled operation,
            # but nothing enforced it: with the aux default reasoning_effort
            # "" (provider default), Gemini enables internal thinking and
            # bills thought tokens against max_tokens=64 — the JSON payload
            # never lands, and the prose fallback stores the opening fence
            # ("```json") as the session title (#91927).
            reasoning_config={"enabled": False},
        )
        message = response.choices[0].message
        title = _clean_title(_extract_title_text(message.content or "") or _title_from_reasoning(message))
        # Answer-shaped output guard: titling is a 3-7 word task, so a title with many words is a model that
        # ignored the task and answered the user's message instead ("I don't have context on X — that's not
        # something I recognize..."). Truncating would store half an assistant blob as the session title,
        # which is still an assistant blob — reject instead so the caller retries on the next exchange
        # (maybe_auto_title retries a placeholder title through the third exchange). Port of can1357/oh-my-pi#7306.
        if title is not None and len(title.split()) > _MAX_TITLE_WORDS:
            # Answer-shaped output: reject (not truncate) so the caller retries next exchange.
            logger.debug("Rejecting answer-shaped title output (%d words > %d)", len(title.split()), _MAX_TITLE_WORDS)
            return None
        # Example-echo guard: a title that parrots one of the prompt's own
        # examples back verbatim says nothing about the session — reject it so
        # the instant derived title (a slice of the user's actual words)
        # survives instead. Exact match after wrapper-stripping, deliberately
        # not fuzzy, so a genuinely topical title that merely resembles an
        # example still passes. Wrappers are stripped for the comparison only
        # ("(Fix login button on mobile)" is the same canned echo as the bare
        # example). Port of QwenLM/qwen-code#9709.
        if title is not None and _is_prompt_example_echo(title):
            logger.debug("Rejecting prompt-example echo title: %r", title)
            return None
        return title
    except Exception as e:
        # WARNING so it shows in agent.log without debug mode; stack at debug.
        logger.warning("Title generation failed: %s", e)
        logger.debug("Title generation traceback", exc_info=True)
        _report_failure(failure_callback, e, "Title generation")
        return None


def _has_upgraded_title(session_db, session_id: str) -> bool:
    """True when the session already carries an ``llm``/``user`` title (or the check fails)."""
    try:
        source_fn = getattr(session_db, "get_session_title_source", None)
        if source_fn is not None:
            return source_fn(session_id) not in (None, "derived")
        return bool(session_db.get_session_title(session_id))
    except Exception:
        return True


def _persist_session_title(session_db, session_id, title, *, source, dedupe=True):
    """Persist at *source* authority via ``set_auto_title`` (precedence check + write in one
    transaction, so a manual ``/title`` is never overwritten); None when a higher authority held the row.
    ``ValueError`` = unique-title index collision → append ``#N`` via ``get_next_title_in_lineage``;
    ``dedupe=False`` re-raises instead (the derived title is on the critical path, collides constantly
    on "hi", and the model replaces it a second later anyway).

    ``ValueError`` means the name is taken by an unrelated session (the unique-title index); rather than
    leave the session untitled (#50537), append a ``#N`` suffix via ``get_next_title_in_lineage``.
    """
    auto_fn = getattr(session_db, "set_auto_title", None)

    def _set(candidate):
        if auto_fn is not None:
            if auto_fn(session_id, candidate, source=source):
                return candidate
            logger.debug("Skipping %s title: a higher-authority title already holds session %s", source, session_id)
            return None
        legacy_fn = getattr(session_db, "set_auto_title_if_empty", None)  # older store without provenance
        if legacy_fn is not None:
            return candidate if legacy_fn(session_id, candidate) else None
        if session_db.set_session_title(session_id, candidate) is False:
            raise RuntimeError(f"session {session_id} not found when storing title")
        return candidate

    try:
        return _set(title)
    except ValueError:
        next_title_fn = getattr(session_db, "get_next_title_in_lineage", None)
        deduped = next_title_fn(title) if dedupe and next_title_fn is not None else None
        if not deduped or deduped == title:
            raise
        return _set(deduped)


def apply_subagent_title(session_db, session_id: str, goal: str) -> Optional[str]:
    """Title a delegate run ``Subagent: <goal's first line>`` at ``derived`` authority. No model call:
    runs fan out in bulk, and the prefix alone is what tells them apart from conversations wherever
    ``sessions.show_subagents`` lists them (#97202). Collisions get ``#N``. Never raises."""
    try:
        derived = derive_title(goal) if is_titleable_user_message(goal) else None
        return _persist_session_title(session_db, session_id, f"Subagent: {derived}", source="derived") if derived else None
    except Exception:
        logger.debug("Subagent title failed for %s", session_id, exc_info=True)
        return None


def apply_instant_title(
    session_db, session_id: str, user_message: str, title_callback: Optional[TitleCallback] = None,
    title_preview: str | None = None,
) -> Optional[str]:
    """Write the derived title inline. Returns it, or None (no usable text, or a ``derived``+ title exists). Never raises.

    ``title_preview`` must reach this stage too: the model upgrade's own ``derive_title`` fallback writes
    ``derived`` provenance, which never replaces the ``derived`` title written here."""
    if not session_db or not session_id:
        return None
    try:
        title = derive_title(user_message, title_preview) if is_titleable_user_message(user_message) else None
        persisted = _persist_session_title(session_db, session_id, title, source="derived", dedupe=False) if title else None
        if persisted:
            _notify_title(title_callback, persisted, "derived", "Instant-title")
        return persisted
    except Exception:
        logger.debug("Instant title failed", exc_info=True)
        return None


def auto_title_session(
    session_db,
    session_id: str,
    user_message: str,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: dict | None = None,
    title_callback: Optional[TitleCallback] = None,
    runtime_validator: Optional[RuntimeValidator] = None,
    title_preview: str | None = None,
) -> None:
    """Generate and store the model title (daemon-thread target); skips sessions already carrying an
    ``llm``/``user`` title (a ``derived`` one is expected — upgrading it is the point). Never lets an
    exception escape (the threading excepthook would spray a traceback into the terminal); the canonical
    trigger is the post-``hermes update`` window where lazy imports read NEW source against OLD modules."""
    try:
        if not session_db or not session_id or _has_upgraded_title(session_db, session_id):
            return
        # This thread starts AFTER the turn's ambient context was reset; republish it so the call carries
        # the same Portal ``conversation=`` tag (root-of-lineage) and bills usage to this session.
        from agent.aux_accounting import set_accounting_context
        from agent.portal_tags import set_conversation_context
        conversation_id = session_id
        with suppress(Exception):
            conversation_id = session_db.get_conversation_root(session_id) or session_id
        set_conversation_context(conversation_id)
        # Same for the accounting context, so the title call's token usage is recorded against this session
        # (task='title_generation', #23270).
        set_accounting_context(session_db, session_id)
        title, source = generate_title(
            user_message, failure_callback=failure_callback, main_runtime=main_runtime,
            runtime_validator=runtime_validator, title_preview=title_preview,
        ), "llm"
        if title and _is_provisional_greeting_title(title):
            source = "derived"
        if not title:  # the inline attempt declined collisions; off the critical path the lineage scan is affordable
            title, source = derive_title(user_message, title_preview), "derived"
        if not title:
            return
        try:
            persisted = _persist_session_title(session_db, session_id, title, source=source)
        except Exception as e:
            logger.debug("Failed to set auto-generated title: %s", e)
            return
        if persisted is not None:
            logger.debug("Auto-generated session title: %s", persisted)
            _notify_title(title_callback, persisted, source, "Auto-title")
    except Exception as e:
        # WARNING so operators see it in agent.log; names the likely cause.
        logger.warning("Auto-title failed (harmless; if this started after an update, restart the running Hermes process): %s", e)
        logger.debug("Auto-title traceback", exc_info=True)
        _report_failure(failure_callback, e, "Auto-title")


def _is_real_user_turn(message: Any) -> bool:
    """A question a person actually asked (Hermes persists machinery under ``role="user"``)."""
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    content = message.get("content")
    return is_titleable_user_message(content if isinstance(content, str) else flatten_message_text(content))


def _session_is_untitled(session_db, session_id: str) -> bool:
    """No title of any provenance; False when it can't tell (no model call per turn for an unreadable title)."""
    getter = getattr(session_db, "get_session_title", None)
    try:
        return callable(getter) and not str(getter(session_id) or "").strip()
    except Exception:
        logger.debug("Untitled check failed for %s", session_id, exc_info=True)
        return False


def _kanban_task_title() -> Optional[str]:
    """Kanban worker: the card's title, or ``Kanban task <id>`` when the board can't be read; None elsewhere
    (including delegate_task children of the worker, which inherit the env var but are not the card)."""
    task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    if not task_id or not is_dispatcher_owned_worker_context():
        return None
    try:
        from hermes_cli import kanban_db, kanban_db_connect
        from hermes_state import SessionDB
        with kanban_db_connect.connect_closing() as conn:
            task = kanban_db.get_task(conn, task_id)
        title = " ".join((task.title or "").split()) if task is not None else ""
        # Cards have no length cap; the title store rejects past MAX_TITLE_LENGTH (and the ``#N``
        # retry suffix needs room), which would leave the worker nameless.
        cap = SessionDB.MAX_TITLE_LENGTH - 4
        if len(title) > cap:
            title = title[: cap - 1].rstrip() + "…"
    except Exception:
        logger.debug("Kanban task %s unreadable; naming the session after its id", task_id, exc_info=True)
        title = ""
    return title or f"Kanban task {task_id}"


def maybe_auto_title(
    session_db,
    session_id: str,
    user_message: str,
    conversation_history: Optional[list] = None,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: dict | None = None,
    title_callback: Optional[TitleCallback] = None,
    runtime_validator: Optional[RuntimeValidator] = None,
    title_preview: str | None = None,
) -> Optional[threading.Thread]:
    """Instant inline title, then a daemon-thread upgrade. Call at the START of a turn, before the model.

    Returns the upgrade thread: already started, or — when ``title_upgrade_must_wait_for_turn`` — left
    UNSTARTED for the caller to hand to ``start_title_upgrade`` once the turn's model request settled."""
    if not session_db or not session_id or not user_message:
        return None
    # History may be pre- or post-message. Past the opening turn, skip once the session holds an
    # ``llm``/``user`` name: count alone left a machinery-opened session nameless, and a ``derived``
    # name is still a placeholder (instant slice, or the model's greeting title for a bare "hi") that
    # the first substantive turn should replace. Untitled sessions always get another shot; a
    # placeholder gets turns 2-3, so a failing title model costs at most three calls, not one per turn.
    user_msg_count = sum(1 for m in (conversation_history or []) if _is_real_user_turn(m))
    if user_msg_count > 1 and (
        _has_upgraded_title(session_db, session_id)
        or (user_msg_count > 3 and not _session_is_untitled(session_db, session_id))
    ):
        return None
    kanban_title = _kanban_task_title()
    if kanban_title:
        # The card already carries a human-written name; an auxiliary model call per spawned worker
        # only competes with the worker for capacity (#111166). Final (``llm``) authority: nothing
        # upgrades it later, and a manual ``/title`` still wins inside ``set_auto_title``.
        with suppress(Exception):
            persisted = _persist_session_title(session_db, session_id, kanban_title, source="llm")
            if persisted:
                _notify_title(title_callback, persisted, "llm", "Kanban task title")
        return None
    if not is_titleable_user_message(user_message):
        return None
    if not _auto_title_enabled():  # config read after the cheap guards so the file isn't touched every turn
        logger.debug("Auto-title skipped: auxiliary.title_generation.enabled=false")
        return None
    apply_instant_title(session_db, session_id, user_message, title_callback, title_preview=title_preview)
    if not _model_title_upgrade_enabled():
        logger.debug("Instant title persisted; model upgrade disabled by auxiliary.title_generation.model_upgrade_enabled=false")
        return None
    # The thread must resolve auxiliary.title_generation (config, provider key, language) for the
    # profile whose turn this is: a bare Thread starts with an empty context and lands on the launch
    # profile under multiplex, titling X's session with the default profile's model and billing its key.
    from hermes_threading import spawn_context_thread
    upgrade_kwargs = dict(failure_callback=failure_callback, main_runtime=main_runtime, title_callback=title_callback,
                          runtime_validator=runtime_validator)
    if isinstance(title_preview, str) and title_preview.strip():
        upgrade_kwargs["title_preview"] = title_preview
    upgrade = spawn_context_thread(
        auto_title_session, name="auto-title",
        args=(session_db, session_id, user_message),
        kwargs=upgrade_kwargs,
    )
    if title_upgrade_must_wait_for_turn(main_runtime):
        logger.debug("Auto-title upgrade deferred past the turn: shares the self-hosted endpoint with the main request")
        return upgrade
    start_title_upgrade(upgrade)
    return upgrade


# ── title drift (ADR-17 P5) ───────────────────────────────────────────────────────────────────
#
# Port of omp ``title-drift-classifier.ts`` + ``buildTitleDriftTurns`` (``session/messages.ts``):
# after a settled turn, once every ``refresh_after_prompts`` operator prompts, ask the judge whether
# the stored title still names the recent conversation and regenerate it when it does not. The check
# is advisory: every failure keeps the title, logs at debug, and never fails the turn.

_TITLE_DRIFT_PROMPT_COUNT_ATTR = "_title_drift_prompt_count"
_TITLE_DRIFT_LAST_PROMPT_ATTR = "_title_drift_last_prompt"
_TITLE_DRIFT_IN_FLIGHT_ATTR = "_title_drift_in_flight"
_title_drift_lock = threading.Lock()


def _clean_title_drift_text(text: str) -> str:
    """Collapse whitespace noise so raw paragraphs and tool-shaped text stay compact."""
    cleaned = re.sub(r"[ \t]+", " ", text or "")
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _title_drift_turn(message: Any) -> Optional[dict]:
    """One judge-visible turn: an operator prompt or an assistant's visible reply text (never
    thinking), or None for machinery rows and aborted/errored assistant rows."""
    if not isinstance(message, dict):
        return None
    role = message.get("role")
    if role == "assistant":
        if str(message.get("finish_reason") or "").strip().lower() in _TITLE_DRIFT_DEAD_FINISH_REASONS:
            return None
        raw = flatten_message_text(message.get("content"))
    elif role == "user":
        if not _is_real_user_turn(message):
            return None
        raw = _summarize_user_message(flatten_message_text(message.get("content")))
    else:
        return None
    text = _clean_title_drift_text(raw)[:TITLE_DRIFT_TURN_CHARS]
    return {"role": role, "text": text} if text else None


def build_title_drift_recent(messages: Any) -> list[dict]:
    """The latest conversation the drift judge compares with the title, oldest first.

    Collected newest-first, one bounded turn at a time, until ``TITLE_DRIFT_CONTEXT_CHARS`` is
    spent. When the window holds no operator prompt (a long tool loop's assistant notes), the
    latest operator prompt is prepended and displaces the oldest turns it would push past the
    window — the notes alone rarely say what was asked.
    """
    messages = list(messages or ())
    turns: list[dict] = []
    chars = 0
    index = len(messages) - 1
    while index >= 0:
        turn = _title_drift_turn(messages[index])
        if turn is None:
            index -= 1
            continue
        if turns and chars + len(turn["text"]) > TITLE_DRIFT_CONTEXT_CHARS:
            # Leave *index* on the turn that did not fit: the anchor scan below re-reads it.
            break
        turns.append(turn)
        chars += len(turn["text"])
        index -= 1
    turns.reverse()
    if any(turn["role"] == "user" for turn in turns):
        return turns
    while index >= 0:
        anchor = _title_drift_turn(messages[index])
        index -= 1
        if anchor is None or anchor["role"] != "user":
            continue
        start = 0
        while start < len(turns) - 1 and chars + len(anchor["text"]) > TITLE_DRIFT_CONTEXT_CHARS:
            chars -= len(turns[start]["text"])
            start += 1
        return [anchor, *turns[start:]]
    return turns


def _format_title_drift_context(recent: list[dict]) -> str:
    """The bounded window as a chat envelope the title model can name the work from."""
    turns = "\n\n".join(f"<{turn['role']}>\n{turn['text']}\n</{turn['role']}>" for turn in recent)
    return f"<chat>\n{turns}\n</chat>" if turns else ""


def _terminal_title_status(final_status: Any) -> bool:
    """True when the turn ended in a state that must never spend a judgment or rewrite a title."""
    status = str(final_status or "").strip().lower()
    return (status in {"interrupted", "failed", "aborted", "error", "cancelled", "canceled"}
            or "interrupt" in status or status.startswith("aborted"))


def _session_title(session_db, session_id: str) -> str:
    getter = getattr(session_db, "get_session_title", None)
    try:
        return str(getter(session_id) or "").strip() if callable(getter) else ""
    except Exception:
        logger.debug("Title-drift title read failed for %s", session_id, exc_info=True)
        return ""


def _session_title_source(session_db, session_id: str) -> Optional[str]:
    getter = getattr(session_db, "get_session_title_source", None)
    try:
        return getter(session_id) if callable(getter) else None
    except Exception:
        logger.debug("Title-drift source read failed for %s", session_id, exc_info=True)
        return None


def _latest_operator_prompt(messages: list) -> tuple[int, int, str]:
    """``(real operator prompt count, latest index, latest text)``.

    The count keys the per-agent interval counter: assistant/tool rows and synthetic user rows
    appended after the same prompt must not advance it, while a new prompt (or a duplicate
    "continue") counts once. ``(-1, "")``-style absence is ``(0, -1, "")``.
    """
    count = 0
    latest_index = -1
    latest_text = ""
    for index, message in enumerate(messages):
        if isinstance(message, dict) and message.get("role") == "user" and _is_real_user_turn(message):
            count += 1
            latest_index = index
            latest_text = flatten_message_text(message.get("content"))
    return count, latest_index, latest_text


def _regenerate_title_from_recent(agent, session_db, session_id: str, recent: list[dict]) -> Optional[str]:
    """One title generation over the bounded conversation context (same model/guards as auto-title)."""
    context = _format_title_drift_context(recent)
    if not context:
        return None
    # The thread starts after the turn's ambient context was reset: republish it so the call bills
    # to this session and resolves the profile whose turn this is (same as ``auto_title_session``).
    # Restore afterwards: a caller running this inline (tests, CLI) must not leak the session into
    # every later auxiliary call in the same context.
    conversation_token = accounting_token = None
    try:
        with suppress(Exception):
            from agent.aux_accounting import set_accounting_context
            from agent.portal_tags import set_conversation_context
            conversation_token = set_conversation_context(session_id)
            accounting_token = set_accounting_context(session_db, session_id)
        return generate_title(
            context,
            failure_callback=(
                getattr(agent, "_title_failure_callback", None)
                or getattr(agent, "_emit_auxiliary_failure", None)
            ),
            max_input_chars=TITLE_DRIFT_CONTEXT_CHARS + 64,
        )
    finally:
        if conversation_token is not None:
            with suppress(Exception):
                from agent.portal_tags import reset_conversation_context
                reset_conversation_context(conversation_token)
        if accounting_token is not None:
            with suppress(Exception):
                from agent.aux_accounting import reset_accounting_context
                reset_accounting_context(accounting_token)


def _refresh_title_if_drifted(agent, session_db, session_id: str, title: str, source: str,
                              recent: list[dict]) -> Optional[str]:
    """Judge ``fits`` and refresh the title when the conversation moved away from it."""
    from agent.judgment import JudgmentError, JudgmentRequest, judge, load_questions

    try:
        question = load_questions("title-drift")["fits"]
        result = judge(
            JudgmentRequest(state={"title": title, "recent": recent}, questions={"fits": question}),
            purpose="title-drift",
            timeout_ms=TITLE_DRIFT_JUDGE_TIMEOUT_MS,
        )
        probability = getattr(result.answers.get("fits"), "noul", None)
        if isinstance(probability, bool) or not isinstance(probability, (int, float)):
            logger.debug("Title-drift judge returned no probability; keeping %r", title)
            return None
        if probability >= TITLE_DRIFT_FITS_THRESHOLD:
            return None
    except JudgmentError as exc:
        logger.debug("title drift: classification failed: %s", exc)
        return None
    except Exception as exc:  # health: allow BLE001 -- a judged check must never fail the turn
        logger.debug("title drift: classification failed: %s", exc)
        return None

    refreshed = _regenerate_title_from_recent(agent, session_db, session_id, recent)
    if not refreshed or refreshed == title or _is_provisional_greeting_title(refreshed):
        return None
    refresh = getattr(session_db, "refresh_auto_title", None)
    if not callable(refresh):
        logger.debug("title drift: %r drifted, but this session store cannot refresh in place; keeping it", title)
        return None
    try:
        persisted = bool(refresh(session_id, refreshed, source=source, expected_title=title))
    except ValueError as exc:  # unique-title index: keep the current title instead of a "#N" refresh
        logger.debug("title drift: refreshed title collided: %s", exc)
        return None
    except Exception:
        logger.debug("title drift: title refresh failed", exc_info=True)
        return None
    if not persisted:
        current = _session_title(session_db, session_id)
        current_source = _session_title_source(session_db, session_id)
        logger.debug(
            "title drift: refresh refused (current=%r source=%r, expected=%r source=%r); keeping the current title",
            current, current_source, title, source,
        )
        return None
    logger.debug("title drift: refreshed session title %r -> %r", title, refreshed)
    _notify_title(getattr(agent, "_on_session_title", None), refreshed, source, "Title-drift")
    return refreshed


def maybe_refresh_title_drift(agent, session_db, session_id, messages, *, final_status) -> Optional[str]:
    """ADR-17 P5: check a generated title against the settled turn's conversation, once every
    ``refresh_after_prompts`` operator prompts, and rewrite it only when the judge says it drifted.

    Returns the newly persisted title, else None. Every skip is silent (debug); neither the judge
    nor the regeneration may raise into the turn.
    """
    try:
        interval = _title_refresh_after_prompts()
        if interval <= 0:
            return None
        if _terminal_title_status(final_status):
            return None
        if agent is None or session_db is None or not session_id:
            return None
        messages = list(messages or ())
        if not messages:
            return None
        # A user's name, an untitled row, NULL provenance (a pre-provenance manual rename), and
        # the provisional greeting are never judged: there is no automatic title to keep fitting.
        title = _session_title(session_db, session_id)
        source = _session_title_source(session_db, session_id)
        if not title or source not in ("derived", "llm") or _is_provisional_greeting_title(title):
            return None
        prompt_count, prompt_index, prompt = _latest_operator_prompt(messages)
        if prompt_index < 0 or not prompt.strip():
            return None
        with _title_drift_lock:
            key = (prompt_count, prompt)
            count = int(getattr(agent, _TITLE_DRIFT_PROMPT_COUNT_ATTR, 0) or 0)
            if key != getattr(agent, _TITLE_DRIFT_LAST_PROMPT_ATTR, None):
                count += 1
                setattr(agent, _TITLE_DRIFT_LAST_PROMPT_ATTR, key)
                setattr(agent, _TITLE_DRIFT_PROMPT_COUNT_ATTR, count)
            # A prompt sent while the previous check runs counts toward the next interval.
            if getattr(agent, _TITLE_DRIFT_IN_FLIGHT_ATTR, False):
                return None
            if count < interval:
                return None
            recent = build_title_drift_recent(messages)
            if not recent:
                return None
            # A check consumes the prompts that triggered it whatever its verdict.
            setattr(agent, _TITLE_DRIFT_PROMPT_COUNT_ATTR, 0)
            setattr(agent, _TITLE_DRIFT_IN_FLIGHT_ATTR, True)
        try:
            return _refresh_title_if_drifted(agent, session_db, session_id, title, source, recent)
        finally:
            setattr(agent, _TITLE_DRIFT_IN_FLIGHT_ATTR, False)
    except Exception:
        logger.debug("title drift: check failed", exc_info=True)
        return None


def start_title_drift_check(agent, messages, *, final_status) -> Optional[threading.Thread]:
    """Spawn the post-turn drift check off the critical path; None when it cannot or should not run.

    Uses ``spawn_context_thread`` so the judgment/title request resolves the profile whose turn
    this was, and registers the thread with the joinable title threads so a usage-ledger read at
    process exit waits for its billing.
    """
    try:
        if _title_refresh_after_prompts() <= 0 or _terminal_title_status(final_status):
            return None
        session_db = getattr(agent, "_session_db", None)
        session_id = getattr(agent, "session_id", None)
        if not session_db or not session_id:
            return None
        from hermes_threading import spawn_context_thread
        thread = spawn_context_thread(
            maybe_refresh_title_drift,
            name="title-drift",
            args=(agent, session_db, session_id, list(messages or ())),
            kwargs={"final_status": final_status},
        )
        _start_title_thread(thread)
        return thread
    except Exception:
        logger.debug("title drift: check could not start", exc_info=True)
        return None
