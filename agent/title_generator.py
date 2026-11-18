"""Auto-generate short session titles from the user's opening message.

Two stages, both off the critical path: an **instant** deterministic title (written before the model
is called, cannot fail), then an **upgrade** from one small-model call under the ``<title>`` marker
protocol (ADR-21 D4, parsed by ``agent.title_parser``). The upgrade defaults to a **fork** side turn
over the main reply's warm prefix (``title-fork.md``); a fork that produces nothing falls back to the
**plan** of ordered title-model candidates (``title-system.md``). Only a plan result gets one
``title-card`` top-up request when cards are on. A card is a string sub-form of the stored title
(``🅿️ PARK: Fix flaky park tests``, ``agent.title_card``), never a column. Storage enforces
provenance ``derived < llm < user``: stage 2 only replaces stage 1 and neither replaces a name the
user typed."""

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
from agent.title_card import keep_title_card, parse_card_reply, split_card_title

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
# Output budget for the title call: room for a reasoning model that keeps thinking visibly despite the
# thinking-disabled request below, without letting a runaway reply burn minutes. A model that honours the
# marker contract stops after ~15 tokens regardless, so the ceiling only costs on replies that would have
# been garbage anyway.
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

# "Friendly greeting" is a legacy of the retired JSON prompt, which prescribed it for bare
# greetings, so sessions opened before the `<title>` marker protocol still carry it. It is the
# one model title that must NOT settle the session: it is persisted at ``derived`` authority
# (a placeholder, like the instant title) and the next substantive turn upgrades it. The new
# prompt answers greetings with `<title/>`, but the placeholder stays detectable either way —
# a predictable placeholder is upgradeable, an improvised one ("Casual check-in chat") would
# lock the title as ``llm``.
_PROVISIONAL_GREETING_TITLE = "friendly greeting"

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


def _title_generator() -> str:
    """``auxiliary.title_generation.generator``, normalized onto the two implemented paths.

    ADR-21 D6 names two generators: ``fork`` (the default) runs the titling as a side turn sharing
    the main reply's prefix, and ``plan`` spends the ordered candidate plan. ``fork`` also covers an
    unset key and an unknown value: the default must never leave a session unnamed over a typo, and
    a failed fork falls back to the plan anyway.
    """
    try:
        requested = str(_title_config().get("generator") or "").strip().lower()
    except Exception:
        logger.debug("Failed to read title_generation.generator", exc_info=True)
        requested = ""
    return "plan" if requested == "plan" else "fork"


def _card_icons() -> str:
    """``auxiliary.title_generation.card_icons``; ``boring`` turns every card off (ADR-21 D11).

    Only ``boring`` disables the card: it is the naming pipeline's gate, not a parser mode
    (``split_card_title`` always parses, so de-carded comparison and search keep working).
    """
    try:
        return str(_title_config().get("card_icons") or "emoji").strip().lower()
    except Exception:
        logger.debug("Failed to read title_generation.card_icons", exc_info=True)
        return "emoji"


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
    shares one local server between the streaming main request and the concurrent title request.
    Single-slot servers then serve the title completion into the main turn: the user's reply arrives
    as ``<title>…</title>``, is persisted as a genuine assistant row and replayed, and the model
    adopts the marker as its answer format (#117296).
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


def _first_line(text: str) -> str:
    return next((ln.strip() for ln in text.splitlines() if ln.strip()), "")


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
    """The greeting placeholder (also "Friendly greeting in chat" and quoted/bracketed variants)."""
    normalized = re.sub(r"^[\W_]+|[\W_]+$", "", title.strip(), flags=re.UNICODE).lower()
    return normalized in (_PROVISIONAL_GREETING_TITLE, _PROVISIONAL_GREETING_TITLE + " in chat")


def generate_title(
    user_message: str,
    timeout: Optional[float] = None,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: dict | None = None,
    runtime_validator: Optional[RuntimeValidator] = None,
    title_preview: str | None = None,
    max_input_chars: int = MAX_TITLE_INPUT_CHARS,
    on_terminal: Optional[Callable[[], None]] = None,
) -> Optional[str]:
    """Title from the opening message alone (waiting for the assistant made this slow and bought
    nothing). ``runtime_validator`` runs right before the request; False skips silently.

    If it returns False (e.g. the user's model was switched since the background thread captured its runtime
    snapshot), the call is skipped silently — no request is sent, so a stale title request can't reload a
    model the runtime already unloaded (#19027). ``max_input_chars`` lets a caller pass an already bounded
    conversation context (title drift) without the opening-message budget cutting its latest turns.
    ``on_terminal`` fires on the model's prescribed ``<title/>`` decline: ``None`` is then a decision
    ("nothing to name"), not a failure, and the caller must not synthesise a title.
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
    try:
        from agent.prompts.renderer import render_request
        from agent.title_router import run_title_plan_attempt, title_model_plan

        # ADR-21 D6/D3: the titler is a consumer of the sprint-2 plan — it resolves the route
        # ladder once (cheap lane first, session model last) and spends at most one call per
        # candidate. An empty plan (both gates off, or nothing configured) means zero calls.
        plan = title_model_plan(main_runtime, require_model_upgrade=False)
        if not plan:
            logger.debug("Title generation skipped: no title-model candidate is configured")
            return None
        # The prompt is the registered `title_system` request template (ADR-21 D5); the language
        # rule rides the template's `{% if title_language %}` line, not string surgery.
        prompt = render_request("title_system", {"title_language": _title_language()})
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_snippet},
        ]

        def _call(**kwargs):
            try:
                return call_llm(**kwargs)
            except Exception as exc:  # the plan advances; the caller still hears about it
                _report_failure(failure_callback, exc, "Title generation")
                raise

        # Use the provider's default temperature instead of forcing 0.3.
        # Some models (e.g. GPT-5.6) only accept their server-side default
        # and reject explicit temperature values, causing the daemon title
        # thread to fail with "Unsupported value: 'temperature'".
        # See: #72351, #51083, #51157
        # A title is a handful of tokens, but a low ceiling was cut off by reasoning models whose
        # thinking survives the disable below (#83903, #82291). A model that honours the marker
        # contract stops after ~15 tokens regardless, so the ceiling only costs on replies that
        # would have been garbage anyway. temperature=None: omitted from the wire so default-only
        # reasoning models accept the first request (#72351).
        # The module contract above promises thinking-disabled operation, but nothing enforced it:
        # with the aux default reasoning_effort "" (provider default), Gemini enables internal
        # thinking and bills thought tokens against a small max_tokens — the marker never lands,
        # and the prose fallback stores stray thinking text as the session title (#91927).
        options = {
            "max_tokens": TITLE_MAX_TOKENS,
            "temperature": None,
            "reasoning_config": {"enabled": False},
        }
        if timeout is not None:
            options["timeout"] = timeout
        # The marker parser (inside the executor) owns the shared guard (quotes / `Title:` /
        # punctuation stripped; an answer-shaped output is rejected, not truncated) and the
        # `<title/>` / `<title>none</title>` decline, which ends this naming attempt.
        result = run_title_plan_attempt(plan, messages, call=_call, request_options=options)
        if result.declined:
            _safe_callback(on_terminal, (), "%s terminal callback failed", "Title generation")
        return result.title
    except Exception as e:
        # WARNING so it shows in agent.log without debug mode; stack at debug.
        logger.warning("Title generation failed: %s", e)
        logger.debug("Title generation traceback", exc_info=True)
        _report_failure(failure_callback, e, "Title generation")
        return None


def run_title_fork(parent: Any, prompt: str, *, history: Optional[list[dict[str, Any]]] = None) -> str:
    """One naming side turn over *parent*'s warm prefix; ``""`` when it produced nothing.

    Reuses the shared ephemeral-fork lifecycle (``agent.ephemeral_prompt``): same runtime, cached
    system prompt and ``tools[]`` as the parent, an empty tool whitelist, ``max_iterations=1``, no
    ``task_cfg`` (so the fork is never routed away from the parent model and the history is
    replayed verbatim rather than digested), and usage billed to ``title_generation``. Never
    raises: a timeout, a provider refusal or an empty reply is a fallback signal, not an error.
    """
    from agent.ephemeral_prompt import run_ephemeral_prompt

    try:
        answer = run_ephemeral_prompt(
            parent,
            prompt,
            task="title_generation",
            max_iterations=1,
            max_output_tokens=TITLE_MAX_TOKENS,
            history=history,
        )
    except Exception as exc:
        logger.warning("title-fork: outcome=error (%s)", exc)
        logger.debug("title-fork traceback", exc_info=True)
        return ""
    if not answer:
        logger.debug("title-fork: outcome=fallback (empty reply)")
        return ""
    logger.debug("title-fork: outcome=ok")
    return answer


def _first_nonempty(text: str) -> str:
    return next((line.strip() for line in (text or "").splitlines() if line.strip()), "")


def parse_fork_reply(raw: str) -> tuple[Optional[str], bool]:
    """``(title, terminal)`` from one naming side turn's reply.

    The card forms are the fork prompt's own output shape, so the card split runs before the shared
    guard: ``<title emoji=… code=…>Fix …</title>`` and ``🅿️ PARK: Fix …`` both yield the carded
    title, while prose keeps the ``<title>`` marker parser's first-line/guard behaviour. A terminal
    ``<title/>`` is reported so the caller stops instead of falling back to the plan.
    """
    from agent.title_parser import clean_title, parse_title_output

    probe = parse_title_output(raw)
    if probe.terminal:
        return None, True
    card, plain = split_card_title(raw)
    if card is not None or plain != _first_nonempty(raw):
        guarded = clean_title(plain)
        if not guarded:
            return None, False
        return (f"{card}: {guarded}" if card else guarded), False
    return probe.title, False


def _title_fork_message(opener: str, *, cards: bool) -> str:
    """The fork's one user turn: the registered ``title_fork`` request plus the turn's opener."""
    from agent.prompts.renderer import render_request

    request = render_request(
        "title_fork",
        {"card": cards, "title_language": _title_language(), "nerd_fonts": False},
    )
    return f"{request}\n\n{opener}" if opener else request


def _fork_title(agent: Any, opener: str, *, cards: bool) -> tuple[Optional[str], bool, bool]:
    """``(title, terminal, produced)`` from one fork attempt over the parent's snapshot.

    The fork is an integration boundary (its own process state, provider, timeout), so a raise is a
    fallback signal as much as an empty reply is: naming must never fail the caller's turn.
    """
    history = list(getattr(agent, "_session_messages", None) or [])
    try:
        reply = run_title_fork(agent, _title_fork_message(opener, cards=cards), history=history)
    except Exception as exc:  # health: allow BLE001 -- a fork failure falls back to the plan
        logger.warning("title-fork: outcome=error (%s)", exc)
        logger.debug("title-fork traceback", exc_info=True)
        return None, False, False
    if not reply:
        return None, False, False
    title, terminal = parse_fork_reply(reply)
    if terminal:
        return None, True, False
    return title, False, title is not None


def request_title_card(plain_title: str, *, main_runtime: dict | None = None) -> Optional[str]:
    """One ``title-card`` request for *plain_title*; the parsed card, or ``None``.

    A single call on the ``title_generation`` route (the one place the auxiliary pin applies besides
    the plan), with no retry: a failed, empty or unusable reply leaves the plain title and never
    spends a second request. Only the outcome is logged, never the title.
    """
    from agent.prompts.renderer import render_request

    try:
        response = call_llm(
            task="title_generation",
            messages=[
                {
                    "role": "system",
                    "content": render_request("title_card", {"nerd_fonts": False}),
                },
                {"role": "user", "content": plain_title},
            ],
            main_runtime=main_runtime,
            max_tokens=TITLE_MAX_TOKENS,
            temperature=None,
            reasoning_config={"enabled": False},
            fallback=False,
        )
        card = parse_card_reply(response.choices[0].message.content or "")
    except Exception as exc:
        logger.warning("title-card: outcome=error (%s)", exc)
        logger.debug("title-card traceback", exc_info=True)
        return None
    logger.debug("title-card: outcome=%s", "ok" if card else "cancel")
    return card


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


def _naming_attempt(
    user_message: str,
    *,
    agent: Any,
    main_runtime: dict | None,
    failure_callback: Optional[FailureCallback],
    runtime_validator: Optional[RuntimeValidator],
    title_preview: str | None,
) -> tuple[Optional[str], bool]:
    """``(title, terminal)`` for one stage-1 attempt: fork first, plan on a fork fallback.

    The fork is the default generator and the only path that needs the live parent. A fork that
    produces nothing falls back to ``generate_title``'s ordered plan; only a *plan* result of a
    fork-requested naming earns the single ``title-card`` top-up (ADR-21 D11). ``boring`` strips
    cards from whichever generator answered and sends no card request at all.
    """
    if not _auto_title_enabled():
        logger.debug("Auto-title skipped: auxiliary.title_generation.enabled=false")
        return None, False
    if runtime_validator is not None:
        try:
            if not runtime_validator():
                logger.debug("Title generation skipped: runtime validator returned False")
                return None, False
        except Exception:  # fail open: a broken validator must not disable titling
            logger.debug("Title runtime validator raised; proceeding", exc_info=True)
    cards = _card_icons() != "boring"
    generator = _title_generator()
    opener = build_title_input(user_message, title_preview)
    title: Optional[str] = None
    produced_by_fork = False
    if generator == "fork" and agent is not None and opener.strip():
        title, terminal, produced_by_fork = _fork_title(agent, opener, cards=cards)
        if terminal:
            return None, True
    if not produced_by_fork:
        declined = False

        def _note_terminal() -> None:
            nonlocal declined
            declined = True

        title = generate_title(
            user_message,
            failure_callback=failure_callback,
            main_runtime=main_runtime,
            runtime_validator=runtime_validator,
            title_preview=title_preview,
            on_terminal=_note_terminal,
        )
        if declined:
            return None, True
    if not title:
        return None, False
    card, plain = split_card_title(title)
    if card is not None:
        title = f"{card}: {plain}" if cards else plain
    elif cards and generator == "fork" and not produced_by_fork:
        top_up = request_title_card(plain, main_runtime=main_runtime)
        if top_up:
            title = f"{top_up}: {plain}"
    return title, False


def auto_title_session(
    session_db,
    session_id: str,
    user_message: str,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: dict | None = None,
    title_callback: Optional[TitleCallback] = None,
    runtime_validator: Optional[RuntimeValidator] = None,
    title_preview: str | None = None,
    agent: Any = None,
) -> None:
    """Generate and store the model title (daemon-thread target); skips sessions already carrying an
    ``llm``/``user`` title (a ``derived`` one is expected — upgrading it is the point). Never lets an
    exception escape (the threading excepthook would spray a traceback into the terminal); the canonical
    trigger is the post-``hermes update`` window where lazy imports read NEW source against OLD modules.

    ``agent`` is the live parent: the default ``fork`` generator names the session from a side turn
    over its cache-warm prefix, and its absence (an unavailable session model) falls through to
    the plan."""
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
        title, terminal = _naming_attempt(
            user_message,
            agent=agent,
            main_runtime=main_runtime,
            failure_callback=failure_callback,
            runtime_validator=runtime_validator,
            title_preview=title_preview,
        )
        if terminal:
            # The model's prescribed `<title/>`: nothing to name, and no candidate may be retried.
            logger.debug("Auto-title ended on the model's <title/> decline")
            return
        source = "llm"
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
    agent: Any = None,
) -> Optional[threading.Thread]:
    """Instant inline title, then a daemon-thread upgrade. Call at the START of a turn, before the model.

    ``agent`` rides into ``auto_title_session`` for the default ``fork`` generator: the side turn
    replays that parent's system prompt, tools and history, so the naming request hits its prompt
    cache. Returns the upgrade thread: already started, or — when ``title_upgrade_must_wait_for_turn``
    — left UNSTARTED for the caller to hand to ``start_title_upgrade`` once the turn's model request
    settled."""
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
                          runtime_validator=runtime_validator, agent=agent)
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
    """Judge ``fits`` and refresh the title when the conversation moved away from it.

    The judge and the write comparison both work on the de-carded text: a card that differs is not
    a rewrite. A regenerated plain title keeps the stored card under ``emoji`` and is stored plain
    under ``boring`` (``keep_title_card`` is not called there — no card is ever added back).
    """
    from agent.judgment import JudgmentError, JudgmentRequest, judge, load_questions

    plain_title = split_card_title(title)[1]
    try:
        question = load_questions("title-drift")["fits"]
        result = judge(
            JudgmentRequest(state={"title": plain_title, "recent": recent}, questions={"fits": question}),
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
    if not refreshed:
        return None
    if _card_icons() == "boring":
        refreshed = split_card_title(refreshed)[1]
    else:
        refreshed = keep_title_card(refreshed, title)
    if (not refreshed or refreshed == title
            or split_card_title(refreshed)[1] == plain_title
            or _is_provisional_greeting_title(refreshed)):
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
        # ADR-21 D3: ``model_upgrade_enabled=false`` closes every stage-1 path, drift included —
        # otherwise the judge and a regeneration would spend the two calls the switch just saved.
        if not _model_title_upgrade_enabled():
            logger.debug("title drift: model_upgrade_enabled=false; keeping the derived title")
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
        if not _model_title_upgrade_enabled():  # ADR-21 D3: no stage-1 calls while the switch is off
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
