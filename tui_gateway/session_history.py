"""Session history/message shaping: image-ref messages, content coercion, history->wire messages, in-flight
turn tracking and turn-failure detail. Bodies are rebound onto server.py's globals (method_ctx.bind_module)."""

from __future__ import annotations

import re

from .method_ctx import bind_module
from agent.custom_messages import DISPLAY_KIND_TO_CUSTOM_TYPE
from agent.prompt_builder import STEER_DISPLAY_KIND
from tui_gateway.contracts.transcript_roles import TRANSCRIPT_ROLE_NAMES

# Discord routing note (gateway/run_inbound.py::discord_triggering_note) persisted as user
# ``content`` by gateways before the authored-text fix; presentation-only heal for those rows.
_DISCORD_TRIGGERING_NOTE_RE = re.compile(
    r"(^|\n)\[Triggering message id: `[^`\n]*` — use as `message_id` for reply/react/pin via the discord tools\.\]\n*"
)


def _bridged_tool_labels(name: str, args: dict) -> list[dict]:
    from agent.display import tool_labels_for_call

    return [label.as_payload() for label in tool_labels_for_call(name, args)]


def _active_image_routing_identity(agent: Any) -> tuple[str, str]:
    """Return the live provider/model, falling back before agent startup."""
    from agent.auxiliary_client import _read_main_model, _read_main_provider
    return (getattr(agent, "provider", "") or _read_main_provider(), getattr(agent, "model", "") or _read_main_model())


def _build_image_ref_message(user_text: str, image_paths: list[str]) -> str:
    """Reference attached images by path so the agent analyzes them in-loop with ``vision_analyze``: pre-
    analyzing with the auxiliary vision model blocked submit 60-90s/photo and poisoned auto-titles.

    This used to pre-analyze every image with the auxiliary vision model *before* the turn was dispatched
    (``_enrich_with_attached_images``): serial blocking calls on the submit path — 60-90s per large photo —
    with failures silently swallowed and an interrupt during the window killing the turn with zero API calls
    (#83291). It also prepended the vision description to the first user message, poisoning session
    auto-titles (#82339). The CLI never gates turn dispatch on vision like this, which is why the same
    message was seconds there and minutes on desktop.
    """
    prefix = "\n\n".join(
        f"[The user attached an image: {p.name}]\n[Examine it with the vision_analyze tool using image_url: {p}]"
        for p in map(Path, image_paths) if p.exists()
    )
    text = user_text or ""
    if prefix:
        return f"{prefix}\n\n{text}" if text else prefix
    return text or "What do you see in this image?"


def _build_persist_message_with_image_refs(user_text: str, image_paths: list[str]) -> str:
    """Persisted form of the user's message: ``@image:<path>`` directives (the desktop renders them as
    images); ``_build_image_ref_message``'s ``image_url:`` hint is model-only, never persisted. Caption
    first, directives last: session previews are the first 60 chars of the first user message."""
    from agent.context_references import format_reference_value
    text = user_text or ""
    refs = "\n".join(f"@image:{format_reference_value(p)}" for p in image_paths if Path(p).exists())
    if not refs:
        return text
    return f"{text}\n{refs}" if text else refs


def _build_persist_user_message(user_text: str, image_paths: list[str], run_message: Any) -> Any:
    """Shape the persisted user turn like the model payload: ``_flush_messages_to_session_db`` ignores a
    plain-string override for a list (native-vision) payload, so swap only the text part for the
    ``@image:`` form, keep image parts, drop API-only text parts (barge-in note)."""
    persist_text = _build_persist_message_with_image_refs(user_text, image_paths)
    if not isinstance(run_message, list):
        return persist_text
    image_parts = [p for p in run_message if not (isinstance(p, dict) and p.get("type") == "text")]
    return [{"type": "text", "text": persist_text}, *image_parts]


_HISTORY_TEXT_KINDS = frozenset({"text", "input_text", "output_text"})
_HISTORY_IMAGE_KINDS = frozenset({"image_url", "input_image", "image"})
_HISTORY_AUDIO_KINDS = frozenset({"input_audio", "audio"})


def _history_part_image_url(part: dict) -> str:
    """The URL carried by an image part (``image_url`` dict or str), else ""."""
    image_url = part.get("image_url")
    if isinstance(image_url, dict):
        image_url = image_url.get("url")
    return image_url if isinstance(image_url, str) else ""


def _history_dict_text(content: dict, *, image_urls: bool) -> str:
    """Placeholder/text rendering of one structured content dict."""
    kind = content.get("type")
    if kind in _HISTORY_TEXT_KINDS:
        return str(content.get("text") or content.get("content") or "")
    if kind in _HISTORY_IMAGE_KINDS:
        return (_history_part_image_url(content) if image_urls else "") or "[image]"
    if kind in _HISTORY_AUDIO_KINDS:
        return "[audio]"
    if kind:
        return f"[{kind}]"
    if "text" in content:
        return str(content.get("text") or "")
    return "[structured content]"


def _content_display_text(content: Any) -> str:
    if isinstance(content, list):
        return "\n".join(t for t in (_content_display_text(part).strip() for part in content) if t)
    if isinstance(content, dict):
        return _history_dict_text(content, image_urls=False)
    return "" if content is None else str(content)


def _coerce_message_text(content: Any, *, image_urls: bool = True) -> str:
    """Render ``message['content']`` (str, parts list, or one structured dict) as a plain string. Image parts
    keep their URL inline so the desktop's ``extractEmbeddedImages`` and the resume payload agree with the
    cached message (else the inline image flashed, then vanished); other shapes become a placeholder.
    ``image_urls=False`` renders ``[image]`` instead — the ``inline_images=false`` read (#116511): a remote
    client reads a transcript in kilobytes instead of re-transmitting every stored attachment."""
    if isinstance(content, list):
        chunks: list[str] = []
        for part in content:
            if isinstance(part, str) or (isinstance(part, dict) and isinstance(part.get("text"), str)):
                chunks.append(part if isinstance(part, str) else part["text"])
            elif isinstance(part, dict) and part.get("type"):
                rendered = _history_dict_text(part, image_urls=image_urls)
                chunks.append(rendered if part["type"] in _HISTORY_TEXT_KINDS else f"\n{rendered}")
        return "".join(chunks)
    if isinstance(content, dict):
        return _history_dict_text(content, image_urls=image_urls)
    return "" if content is None else str(content)


_IMAGE_HINT_RE = re.compile(r"\[Image attached at: ([^\n\]]+)\]")
_IMAGE_DATA_RE = re.compile(r"data:image/[^;,]+;base64,([A-Za-z0-9+/=]+)\Z")
_IMAGE_REF_RE = re.compile(r"@image:(`[^`\n]+`|\"[^\"\n]+\"|'[^'\n]+'|\S+)\Z")
# The session store's text-only projection replaces each image part with this
# stand-in, so a flattened row carries one placeholder per attached image. It
# describes the same attachment the refs above describe, so the projection
# drops it (mirroring the desktop's extractImageRefs) instead of refusing.
_SCREENSHOT_PLACEHOLDER = "[screenshot]"


def _strip_trailing_screenshot_placeholders(text: str) -> tuple[str, int]:
    lines = text.split("\n")
    count = 0
    while lines and lines[-1] == _SCREENSHOT_PLACEHOLDER:
        lines.pop()
        count += 1
    return "\n".join(lines), count


def _user_image_display_text(content: Any) -> str | None:
    """Project verified native-vision images as refs without changing stored/model content."""
    if not isinstance(content, list) or len(content) < 2 or not isinstance(content[0], dict):
        return None
    text_part = content[0]
    if text_part.get("type") != "text" or not isinstance(text_part.get("text"), str):
        return None
    text = text_part["text"]
    from agent.context_references import format_reference_value
    if "\n\n[Image attached at: " in text:
        caption, hints = text.split("\n\n[Image attached at: ", 1)
        if not caption or caption == "What do you see in this image?":
            return None
        hints, placeholders = _strip_trailing_screenshot_placeholders(hints)
        matches = [_IMAGE_HINT_RE.fullmatch(line) for line in ("[Image attached at: " + hints).split("\n")]
        if not all(matches):
            return None
        paths = [match[1] for match in matches]
        if placeholders not in (0, len(paths)):
            return None
        projected = caption + "\n\n" + "\n".join(f"@image:{format_reference_value(p)}" for p in paths)
    else:
        body, placeholders = _strip_trailing_screenshot_placeholders(text)
        lines = body.splitlines()
        ref_lines = [line for line in lines if line.startswith("@image:")]
        matches = [_IMAGE_REF_RE.fullmatch(line) for line in ref_lines]
        if not matches or not all(matches) or lines[-len(matches):] != ref_lines:
            return None
        paths = [match[1][1:-1] if match[1][0] in ('`', '"', "'") else match[1] for match in matches]
        if any(format_reference_value(p) != match[1] for p, match in zip(paths, matches)):
            return None
        if placeholders not in (0, len(paths)):
            return None
        projected = body

    import base64
    import binascii
    from pathlib import Path
    from fastapi import HTTPException
    from hermes_cli.web_routers.files import _fs_regular_file
    from hermes_cli.web_server import _FS_DATA_URL_MAX_BYTES
    image_parts = content[1:1 + len(paths)]
    if len(image_parts) != len(paths) or len(content) not in (1 + len(paths), 2 + len(paths)):
        return None
    if len(content) > 1 + len(paths):
        from agent.memory_context import sanitize_context
        memory = content[-1]
        if not isinstance(memory, dict) or memory.get("type") != "text" or not isinstance(memory.get("text"), str) or sanitize_context(memory["text"]).strip():
            return None
    for path, part in zip(paths, image_parts):
        if not Path(path).is_absolute() or not isinstance(part, dict) or part.get("type") != "image_url":
            return None
        match = _IMAGE_DATA_RE.fullmatch(_history_part_image_url(part))
        if not match or len(match[1]) > ((_FS_DATA_URL_MAX_BYTES + 2) // 3) * 4:
            return None
        try:
            target, st = _fs_regular_file(Path(path))
            if st.st_size > _FS_DATA_URL_MAX_BYTES or target.read_bytes() != base64.b64decode(match[1], validate=True):
                return None
        except (OSError, ValueError, binascii.Error, HTTPException):
            return None
    return projected


def _history_text_only_part(part: dict) -> bool:
    kind = part.get("type")
    return kind in _HISTORY_TEXT_KINDS or (kind is None and isinstance(part.get("text"), str))


def _is_text_only_busy_payload(content: Any) -> bool:
    """True when a busy submit carries only plain text, not attachments/media."""
    if isinstance(content, list):
        return bool(content) and all(
            isinstance(part, str) or (isinstance(part, dict) and _history_text_only_part(part)) for part in content
        )
    return isinstance(content, (str, int, float)) or (isinstance(content, dict) and _history_text_only_part(content))


def _is_display_hidden_marker(role: str | None, text: str) -> bool:
    """Gateway notices (model-switch, personality) persist as role=user ``[System: …]`` rows so strict providers
    accept them mid-history; they must never render as a user bubble. Filtering in this one projection hides
    them everywhere (raw marker stays in ``session["history"]``) and keeps the desktop's user ordinals stable.

    It also removes the stored marker from the payload the desktop reconciles against, so it can no longer
    shift user-message ordinals and duplicate the optimistic prompt (#67603).
    """
    return role == "user" and text.lstrip().startswith("[System:")


def _skill_scaffold_projection(content_text: str) -> str:
    """The invocation a slash-skill-expanded turn came from, else "" — UIs render ``/work fix the leak``."""
    return describe_skill_invocation(content_text, separator=" ") or ""


def _expand_skill_invocation_for_replay(text: str, task_id: str) -> str:
    """Inverse of :func:`_skill_scaffold_projection`: rewind/regenerate hands back the projected invocation,
    and re-running it verbatim would drop the skill. Unchanged when not resolvable."""
    head, _, arg = (text or "").strip().partition(" ")
    if not head.startswith("/"):
        return text
    try:
        from agent.skill_commands import build_skill_invocation_message, resolve_skill_command_key
        cmd_key = resolve_skill_command_key(head.lstrip("/"), interactive=True)
        return text if cmd_key is None else (build_skill_invocation_message(cmd_key, arg.strip(), task_id=task_id) or text)
    except Exception:  # a skill that no longer resolves must not break the rewind
        logger.debug("skill re-expansion failed for replay", exc_info=True)
        return text


# Opening of the crash-recovery note synthesized by session_auto_continue._auto_continue_note.
# ADR-10 P5 removed the read-time prefix sniff; the builder still shares this fixed opening.
_AUTO_CONTINUE_NOTE_PREFIX = "[System note: Your previous turn was interrupted mid-run"


_HISTORY_ASSISTANT_DETAIL_KEYS = (
    "reasoning",
    "reasoning_content",
    "reasoning_details",
    "codex_reasoning_items",
    "codex_message_items",
)
# ADR-10 D10: the wire role set is owned by the seat table; ``system`` stays as read compatibility.
_HISTORY_ROLES = frozenset(TRANSCRIPT_ROLE_NAMES) | {"system"}


def _marker_custom_type(m: dict) -> str | None:
    """ADR-10 P5: the custom block a stored internal marker projects to, else ``None``.

    Producers write ``role:"custom"`` rows; this map keeps older rows and the live marker rows
    (adviser steer, delegation notices) renderable. The marker itself never reaches the wire.
    """
    kind = m.get("display_kind")
    return DISPLAY_KIND_TO_CUSTOM_TYPE.get(kind) if isinstance(kind, str) else None


# Internal markers that are NOT custom blocks: a synthetic user/assistant row a viewer must
# recognise as "not a human turn" (the one remaining wire marker, ``synthetic_kind``).
_SYNTHETIC_MARKERS = frozenset({"auto_continue", "failed_turn", STEER_DISPLAY_KIND})


def _marker_details(m: dict) -> dict:
    """D1's notification shape plus the renderer extras, read from the internal row once."""
    kind = m.get("display_kind")
    meta = m.get("display_metadata")
    meta = meta if isinstance(meta, dict) else {}
    level = meta.get("level")
    details = {"level": level if level in {"warning", "error"} else "info",
               "source": kind if isinstance(kind, str) else ""}
    for key in ("display_text", "task_count", "notification_category"):
        if meta.get(key) is not None:
            details[key] = meta[key]
    return details


def _custom_message(m: dict, *, text: str) -> dict:
    """ADR-10 P5: one custom row as the wire shape. Always ``role:"custom"``.

    Legacy ``display_kind`` rows are normalised here (gateway-side, once) so clients never parse
    text and never read a marker: adviser steer rows, older adviser/todo rows, and the
    delegation/notice markers all arrive as ``custom_type`` + ``details``.
    """
    from agent.custom_messages import DISPLAY_ONLY_CUSTOM_TYPES, custom_fields, legacy_details

    fields = custom_fields(m)
    marker = m.get("display_kind") if isinstance(m.get("display_kind"), str) else None
    custom_type = fields.get("custom_type") or DISPLAY_KIND_TO_CUSTOM_TYPE.get(marker)
    legacy_role = fields.get("legacy_role") or m.get("role") or "user"
    details = fields.get("details")
    if details is None and marker:
        # The legacy skill row's stored content is the expanded body; the wire text is the
        # invocation (`/skill:name args`), so details must read the projected text.
        details = legacy_details({**m, "content": text}, display_kind=marker)
        if details is None:
            details = _marker_details(m)
    msg = {"text": text}
    if isinstance(m.get("timestamp"), (int, float)) and m["timestamp"] > 0:
        msg["timestamp"] = float(m["timestamp"])
    if m.get("_row_id") is not None:
        msg["row_id"] = m["_row_id"]
    msg.update(role="custom", legacy_role=legacy_role)
    if custom_type:
        msg["custom_type"] = custom_type
    msg["display"] = bool(fields.get("display", True))
    msg["display_only"] = bool(fields.get("display_only", custom_type in DISPLAY_ONLY_CUSTOM_TYPES))
    msg["attribution"] = fields.get("attribution") or ("user" if custom_type == "skill-prompt" else "agent")
    if details is not None:
        msg["details"] = details
    return msg


def _projects_custom(role: str, m: dict) -> bool:
    """True for a custom row, or a stored internal marker that projects to a custom block."""
    return role == "custom" or _marker_custom_type(m) is not None


def _custom_row_text(role: str, text: str) -> str:
    return (_skill_scaffold_projection(text) if role == "user" else "") or text


def _tool_result_is_error(metadata: Any) -> bool | None:
    """ADR-10 D5: the executor's verdict, read from the one durable copy (display metadata)."""
    from agent.message_metadata import TOOL_RESULT_METADATA

    if not isinstance(metadata, dict):
        return None
    tool_metadata = metadata.get(TOOL_RESULT_METADATA)
    if not isinstance(tool_metadata, dict) or "is_error" not in tool_metadata:
        return None
    return bool(tool_metadata["is_error"])


def _remember_tool_call_args(tool_calls: list, tool_call_args: dict) -> None:
    """Index an assistant row's calls by id so their tool rows render with name and args."""
    for tc in tool_calls:
        fn, tc_id = tc.get("function", {}), tc.get("id", "")
        if tc_id and fn.get("name"):
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                args = {}
            tool_call_args[tc_id] = (fn["name"], args)


def _compaction_summary_projection(m: dict, *, image_urls: bool) -> dict | None:
    """ADR-10 D10: a STANDALONE compaction handoff as a ``compaction_summary`` row, else None.

    A merged handoff keeps its recovered live content (today's projection); only a pure handoff
    has no other display text, so only it becomes the summary block. This is the ADR-10 P2
    ``partial`` shape: ADR-5 D1/D2 owns the durable ``display_metadata.compaction`` stamp
    (tokens/method/boundary row) and the merged carrier's second entry. Until that lands the row
    carries no counters and the client defaults ``tokensBefore`` to 0 (no badge).
    """
    from agent.context_compressor import ContextCompressor, is_compaction_summary_message

    if not is_compaction_summary_message(m):
        return None
    if ContextCompressor.classify_summary_content(m.get("content")) != "standalone":
        return None
    text = ContextCompressor._strip_summary_prefix(
        _coerce_message_text(m.get("content"), image_urls=image_urls))
    if not text.strip():
        return None
    msg = {"role": "compaction_summary", "text": text}
    metadata = m.get("display_metadata")
    stamp = metadata.get("compaction") if isinstance(metadata, dict) else None
    if isinstance(stamp, dict):
        # ADR-5 D2: the summary row points back at the row it replaced; ADR-10 P5 moved the
        # counters off the deleted ``display_metadata`` wire field onto ``details``.
        msg["details"] = {"compaction": {**stamp, "boundary_row_id": m.get("_row_id")}}
    ts = m.get("timestamp")
    if isinstance(ts, (int, float)) and ts > 0:
        msg["timestamp"] = float(ts)
    if m.get("_row_id") is not None:
        msg["row_id"] = m["_row_id"]
    return msg


def _display_row(m: dict, messages: list[dict], *, image_urls: bool, include_compaction: bool) -> dict | None:
    """One stored row after the display projection.

    ADR-10 D10's compaction gate runs first: a standalone handoff is appended as a
    ``compaction_summary`` row and ``None`` means the row was already emitted.
    """
    if include_compaction:
        summary = _compaction_summary_projection(m, image_urls=image_urls)
        if summary is not None:
            messages.append(summary)
            return None
    return project_compaction_message_for_display(m)


def _tool_row_message(m: dict, tool_call_args: dict, include_tool_content: str) -> dict:
    """ADR-10 D4/D5: one tool row with its call's name/args/labels and the requested content depth."""
    tc_name, tc_args = tool_call_args.get(m.get("tool_call_id") or "", (None, None))
    name = tc_name or m.get("tool_name") or "tool"
    args = tc_args or {}
    # `context` is an 80-char preview; ship args so a full-call renderer isn't truncated.
    labels = _bridged_tool_labels(name, args)
    if include_tool_content == "none":
        # Today's compact projection: only edit cards carry the original result.
        display_content = m.get("content") if name in {"write_file", "patch", "skill_manage"} else None
    else:
        from agent.tool_dispatch_helpers import display_tool_result_content
        display_content = display_tool_result_content(
            name, m.get("content"), images=include_tool_content == "full")
    entry = {"role": "tool", "name": name, "context": _tool_ctx(name, args)}
    if display_content is not None:
        entry["content"] = display_content
    for key in ("tool_call_id", "timestamp"):
        if m.get(key) is not None:
            entry[key] = m[key]
    if m.get("_row_id") is not None:
        entry["row_id"] = m["_row_id"]
    if args:
        entry["args"] = args
    if labels:
        entry["labels"] = labels
    is_error = _tool_result_is_error(m.get("display_metadata"))
    if is_error is not None:
        entry["is_error"] = is_error
    return entry


def _stamp_row_identity(msg: dict, m: dict) -> dict:
    """Copy the display timestamp and durable row id a stored row carries."""
    ts = m.get("timestamp")
    if isinstance(ts, (int, float)) and ts > 0:
        msg["timestamp"] = float(ts)
    if m.get("_row_id") is not None:
        msg["row_id"] = m["_row_id"]
    return msg


def _skill_block(role: str, content_text: str, m: dict) -> dict | None:
    """A slash-skill turn as its invocation block, else ``None`` (ADR-10 D1/P5).

    The stored user row is the expanded body; the wire text is the invocation and the body rides
    ``details.prompt`` so clients never parse text.
    """
    invocation = _skill_scaffold_projection(content_text) if role == "user" else ""
    if not invocation:
        return None
    from agent.custom_messages import skill_details
    return _stamp_row_identity(
        {"role": "custom", "custom_type": "skill-prompt", "text": invocation,
         "display": True, "display_only": False, "attribution": "user", "legacy_role": "user",
         "details": skill_details(invocation, prompt=content_text)}, m)


def _history_to_messages(history: list[dict], *, profile_home=None, image_urls: bool = True,
                         include_tool_content: str = "none",
                         include_compaction: bool = False) -> list[dict]:
    """``image_urls=False`` is the ``inline_images=false`` projection (#116511): image parts render ``[image]``
    so a remote client's history read stays kilobytes instead of re-transmitting every stored attachment.

    ADR-10 P5: the wire is always the custom shape — custom blocks carry ``custom_type``/``details``,
    synthetic user/assistant rows carry ``synthetic_kind``, and ``display_kind``/``display_metadata``
    never leave the gateway. ``include_tool_content`` (ADR-10 D4) is ``none`` (today's whitelist),
    ``text`` or ``full``; ``include_compaction`` (ADR-10 D10) projects standalone compaction handoffs
    as ``role:"compaction_summary"`` instead of hiding them.
    """
    from agent.history_commentary import project_history_commentary
    from agent.message_metadata import withheld_prompt_text

    messages = []
    tool_call_args = {}
    for m in history:
        if not isinstance(m, dict):
            continue
        m = _display_row(m, messages, image_urls=image_urls, include_compaction=include_compaction)
        if m is None:
            continue
        role = m.get("role")
        if role not in _HISTORY_ROLES:
            continue
        if role == "assistant" and m.get("tool_calls"):
            _remember_tool_call_args(m["tool_calls"], tool_call_args)
        # display_kind="hidden": model-facing scaffolding the "[System:" sniff does not catch (a held
        # turn's commentary too: its tool rows still render, labelled from the calls read above).
        marker = m.get("display_kind") if isinstance(m.get("display_kind"), str) else None
        if marker == "hidden":
            continue
        content_text = _user_image_display_text(m.get("content")) if role == "user" else None
        if content_text is None:
            content_text = _coerce_message_text(m.get("content"), image_urls=image_urls)
        if _is_display_hidden_marker(role, content_text):
            continue
        if role == "user":
            content_text = _DISCORD_TRIGGERING_NOTE_RE.sub(r"\1", content_text)
            # A pre_llm_call veto stores a placeholder; the transcript shows what the user typed.
            content_text = withheld_prompt_text(m) or content_text
        if role == "user" and marker == STEER_DISPLAY_KIND:
            # Mid-turn /steer: show the user's own words, not the model-facing marker wrapper.
            from agent.conversation_compression import _extract_steer_text_from_message
            content_text = _extract_steer_text_from_message(m) or content_text
        # ADR-10 P5: a custom row or a stored internal marker projects through the custom shape.
        # auto_continue/failed_turn/steer keep their real role and only carry ``synthetic_kind``.
        if _projects_custom(role, m):
            messages.append(_custom_message(m, text=_custom_row_text(role, content_text)))
            continue
        if role == "tool":
            messages.append(_tool_row_message(m, tool_call_args, include_tool_content))
            continue
        # A user turn shows its skill invocation, never the expanded body (rewind re-sends by ordinal);
        # the expanded prompt rides ``details.prompt`` so clients never parse text.
        if (block := _skill_block(role, content_text, m)) is not None:
            messages.append(block)
            continue
        # Assistant detail sidecars can carry the only visible reply or reasoning after resume/reload.
        has_assistant_detail = role == "assistant" and any(m.get(key) for key in _HISTORY_ASSISTANT_DETAIL_KEYS)
        if not content_text.strip() and not has_assistant_detail:
            continue
        msg = {"role": role, "text": content_text}
        # Authoring time + durable identity (display-only; never fed back into model context).
        _stamp_row_identity(msg, m)
        if role == "assistant":
            msg.update((key, m[key]) for key in _HISTORY_ASSISTANT_DETAIL_KEYS if m.get(key) is not None)
        # Read by project_history_commentary's withheld gate; stripped from the wire below.
        if role == "assistant" and m.get("display_metadata"):
            msg["display_metadata"] = m["display_metadata"]
        if marker in _SYNTHETIC_MARKERS:
            msg["synthetic_kind"] = marker
        if marker == STEER_DISPLAY_KIND:
            msg["live_steered"] = True
        messages.append(msg)
    projected = project_history_commentary(messages, home=profile_home)
    # ADR-10 P5: internal display bookkeeping must never reach a client.
    for message in projected:
        message.pop("display_kind", None)
        message.pop("display_metadata", None)
    return projected


def _coerce_seed_history(value: Any) -> list[dict]:
    history = []
    for item in value if isinstance(value, list) else ():
        if not isinstance(item, dict) or item.get("role") not in ("user", "assistant", "system"):
            continue
        content = item.get("text") if item.get("content") is None else item.get("content")
        if isinstance(content, str) and content.strip():
            row = {"role": item["role"], "content": content}
            # "hidden" is the one display_kind a seeding client may author: model-facing scaffolding the
            # renderer must not paint (a guided-chat runbook). Every other kind is stamped by the gateway
            # at turn time, so it is not accepted from the wire.
            if item.get("display_kind") == "hidden":
                row["display_kind"] = "hidden"
            history.append(row)
    return history


def _inflight_text(value: Any) -> str:
    return _content_display_text(value).strip()


def _start_inflight_turn(
    session: dict, text: Any, *, display_kind: str | None = None,
    display_metadata: dict | None = None,
) -> None:
    now = time.time()
    turn = {
        "assistant": "", "started_at": now, "streaming": True, "updated_at": now,
        "user": _inflight_text(text),
    }
    if display_kind:
        turn["display_kind"] = display_kind
    if isinstance(display_metadata, dict):
        turn["display_metadata"] = dict(display_metadata)
    session["inflight_turn"] = turn


def _append_inflight_delta(session: dict, delta: Any) -> None:
    text = "" if delta is None else str(delta)
    if not text:
        return
    turn = session.get("inflight_turn")
    if not isinstance(turn, dict):
        turn = {"assistant": "", "streaming": True, "user": ""}
    turn.update(assistant=f"{turn.get('assistant') or ''}{text}", streaming=True, updated_at=time.time())
    session["inflight_turn"] = turn


def _record_inflight_correction(session: dict, text: Any) -> None:
    """Record an accepted mid-turn correction on the live turn — appended, never written over ``user``,
    so a resuming client can rebuild BOTH bubbles."""
    correction = _inflight_text(text)
    turn = session.get("inflight_turn")
    if not correction or not isinstance(turn, dict):
        return
    # correction_offsets: arrival-order boundary (assistant chars already streamed) so resuming clients
    # place the bubble between the output seen and the output redirected.
    turn = dict(turn)
    turn["corrections"] = [*(turn.get("corrections") or []), correction]
    turn["correction_offsets"] = [*(turn.get("correction_offsets") or []), len(str(turn.get("assistant") or ""))]
    turn["updated_at"] = time.time()
    session["inflight_turn"] = turn


def _clear_inflight_turn(session: dict) -> None:
    session["inflight_turn"] = None
    # A turn that never reached the agent (cancelled/refused before ready) leaves its submit-time row
    # as the durable record of the send; a later turn must not adopt it as its own input.
    session.pop("_submit_user_row", None)


def _fail_inflight_turn(session: dict, error: Any, error_surface: Optional[dict] = None) -> None:
    """Mark the in-flight turn terminal-error but keep it replayable: a failure's terminal frame can be lost on
    WS disconnect and the turn may never have been committed, so the snapshot lets ``session.resume`` replay
    prompt, partial text and error. Lives until the next turn starts or the session closes. Caller holds history_lock."""
    message = str(error) if not isinstance(error, BaseException) else (str(error) or type(error).__name__)
    now = time.time()
    turn = session.get("inflight_turn")
    if not isinstance(turn, dict):
        turn = {"assistant": "", "user": "", "started_at": now}
    turn.update(
        assistant=str(turn.get("assistant") or ""), user=str(turn.get("user") or ""),
        error=message or "turn failed", status="error", recoverable=True,
    )
    if error_surface:  # {layer, code, retryable} so a reconnect renders the same layered error card
        turn["error_surface"] = dict(error_surface)
    else:
        turn.pop("error_surface", None)
    turn.update(streaming=False, updated_at=now)
    session["inflight_turn"] = turn
    # The turn is over (build failed, agent missing, prologue raised): the submit-time row stays as the
    # durable record of the send, but a later turn must not adopt it as its own input.
    session.pop("_submit_user_row", None)


_TURN_FAILURE_DETAIL_LIMIT = 240
# Shortest prompt run counting as a quote-back: above shared boilerplate, below a quoted sentence.
_TURN_PROMPT_ECHO_WINDOW = 24
# Ceiling on the prompt we shingle (an @-expanded prompt can carry a whole file).
_TURN_PROMPT_ECHO_MAX_PROMPT = 65536


def _strip_prompt_echo(message: str, prompt: Any) -> str:
    """Blank runs of the submitted prompt that ``message`` quotes back: secret redaction is pattern-based and a
    provider 4xx echoing the request carries private prose matching no pattern. Any ``_TURN_PROMPT_ECHO_WINDOW``+
    char run shared with the prompt (or its JSON-escaped form) becomes ``<prompt>``; shingles keep it linear."""
    if not message or not prompt:
        return message
    needle = " ".join(str(prompt).split())[:_TURN_PROMPT_ECHO_MAX_PROMPT]
    window = _TURN_PROMPT_ECHO_WINDOW
    if len(needle) < window or len(message) < window:
        return message
    shingles = {needle[i:i + window] for i in range(len(needle) - window + 1)}
    escaped = json.dumps(needle)[1:-1]
    if escaped != needle:
        shingles.update(escaped[i:i + window] for i in range(len(escaped) - window + 1))
    out: list[str] = []
    i, n = 0, len(message)
    while i <= n - window:
        if message[i:i + window] in shingles:
            j = i + window
            while j < n and message[j - window + 1:j + 1] in shingles:
                j += 1
            out.append("<prompt>")
            i = j
        else:
            out.append(message[i])
            i += 1
    out.append(message[i:])
    return "".join(out)


def _turn_failure_detail(error: Any, reason: Any = None, prompt: Any = None) -> str:
    """Why a turn failed, for the ``tui turn finished`` bookend: ``""`` when nothing to say, else a fragment with
    its own leading space. ``redact_sensitive_text`` removes credentials; ``_strip_prompt_echo`` removes a 4xx
    body quoting ``prompt`` back. This record may gain failure detail, never the user's own content.

    86865 added the bookend to trace compression rotations, so it logs identities and a coarse ``status``
    and deliberately logs no content. 89117 is what the missing cause costs: a report consisting of two
    lines reading ``status=error error_retained=True duration=0.9s`` with no way to tell a provider 4xx from
    a budget wall from a crashed finalizer. The returned-error path -- the one a 0.9 s failure almost always
    takes -- emits no other log line at all; only the exception path prints to stderr, which is why the
    quiet failures are the ones that get filed. See #86865, #89117.
    Content discipline follows #86865's, and it takes two separate steps because it is two separate
    contracts. It does nothing about a 4xx body that quotes the request back, because ordinary private prose
    is not pattern-shaped -- so ``_strip_prompt_echo`` removes that separately, using the submitted
    ``prompt`` itself as the thing to look for.
    """
    reason_text = str(reason or "").strip()
    message = str(error or "").strip()
    if isinstance(error, BaseException):
        message = message or type(error).__name__
    if not message and not reason_text:
        return ""
    try:
        from agent.redact import redact_sensitive_text
        message = redact_sensitive_text(message, force=True)
    except Exception:
        message = "<unredactable>"  # never fail open
    message = " ".join(message.split())
    # After the collapse (same shape both sides), before truncation (a quote must not survive the cut).
    message = _strip_prompt_echo(message, prompt)
    if len(message) > _TURN_FAILURE_DETAIL_LIMIT:
        message = message[:_TURN_FAILURE_DETAIL_LIMIT] + "\u2026"
    out = " failure_reason={}".format(" ".join(reason_text.split())) if reason_text else ""
    return out + (f" cause={message!r}" if message else "")


def register(server) -> None:
    """Publish this module's helpers + handlers onto ``server``, rebound to its globals."""
    bind_module(globals(), server, skip=("_",))
