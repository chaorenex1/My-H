"""Notification payloads: every ``event`` frame the gateway emits (``params.payload``).

One ``Payload`` model per event name, registered with ``event(...)``; ``request.cancel`` lives in
``server_requests.py`` next to the requests it withdraws. Each model's docstring names the Python
emitter it was typed from — that emitter is the source of truth, the hand-written TS in
``apps/shared/src/gateway-events.ts`` was only the map. Fields the emitter always sets are required;
anything conditional is ``X | None = None``.

A handful of payloads stay ``extra="allow"`` on purpose: their closed shape is owned by another
module (the skin engine, the pet store, the goal/loop/heartbeat state files, the free-tier bootstrap
record) or they are watcher signals whose payload is ``{}`` today and may grow. Everything else is
closed, so a drifted emitter fails the suite (``registry.check_payload`` raises under
``HERMES_TEST_ISOLATION``).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .base import JsonValue, Payload, WireEnum
from .common import (MessageReaction, PlanGauge, SessionLiveInfo, SubagentStatus, ToolLabel, ToolLabelKind,
                     TranscriptMessage, Usage)
from .config_free_tier_control import FreeTierChallengePayload, SessionControlSnapshot
from .protocol import (
    MAX_FRAME_BYTES,
    MAX_REASSEMBLED_FRAME_BYTES,
    PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
)
from .registry import event


class OpenPayload(Payload):
    """A payload whose known keys are typed but whose closed set is owned elsewhere."""

    model_config = Payload.model_config | {"extra": "allow"}


# ── gateway lifecycle ─────────────────────────────────────────────────────────────────────────


class SkinPayload(OpenPayload):
    """``tui_gateway/change_watcher.py::resolve_skin`` — the resolved active skin (``HermesSkin``).
    ``{}`` when the skin engine failed to load. Colour maps are token → colour string."""

    name: str = ""
    description: str = ""
    colors: dict[str, str] = Field(default_factory=dict)
    light_colors: dict[str, str] = Field(default_factory=dict)
    dark_colors: dict[str, str] = Field(default_factory=dict)
    branding: dict[str, str] = Field(default_factory=dict)
    banner_logo: str = ""
    banner_hero: str = ""
    tool_prefix: str = ""
    help_header: str = ""


class GatewayReadyPayload(Payload):
    """``tui_gateway/entry.py`` (stdio) / ``tui_gateway/ws.py`` (WebSocket) first frame."""

    skin: SkinPayload
    change_events: bool
    replay_epoch: str
    heartbeat: bool | None = None  # WebSocket transport only
    # WebSocket transport bound to a session authority: session verbs speak the canonical wire
    # (``runtime.describe`` / ``session.create source=…``), so an explicit attach must negotiate it.
    session_authority: bool | None = None
    # Transport negotiation for a peer whose wire is not JSON-RPC (omp-ui): the version this backend
    # emits on, the peer versions it can answer, and the frame limits it enforces. ``0`` means "not
    # enforced"; this gateway never emits ``rpc_chunk`` frames (see ``contracts/protocol.py``).
    protocol_version: int = PROTOCOL_VERSION
    supported_protocol_versions: list[int] = Field(default_factory=lambda: list(SUPPORTED_PROTOCOL_VERSIONS))
    max_frame_bytes: int = MAX_FRAME_BYTES
    max_reassembled_frame_bytes: int = MAX_REASSEMBLED_FRAME_BYTES


event("gateway.ready", GatewayReadyPayload,
      doc="First frame of a connection: the resolved skin, the change-event capability and the replay epoch.")
event("skin.changed", SkinPayload,
      doc="The active skin moved (name switch or live colour edit); repaint from this palette.")


class SetupReadyPayload(OpenPayload):
    """``hermes_cli/free_tier_bootstrap.py::SetupRecord.as_payload``."""

    provider_configured: bool
    inference_provider: str
    free_tier_account: bool
    free_tier_route: bool
    has_identity: bool
    other_providers: bool
    error: str = ""
    # Present only when the free-tier mint did not happen (``anon_auth.MintFailure.as_payload``).
    error_code: str | None = None
    retryable: bool | None = None
    retry_after: int | None = None
    finished_at: float


class OnboardingOutcome(WireEnum):
    done = "done"
    skipped = "skipped"
    replaced = "replaced"
    deleted = "deleted"


class OnboardingEndedPayload(Payload):
    """``agent/onboarding_record.py::_transition``: the run ``run_id`` stopped being in progress.
    ``stored_session_id`` is its chat at the compression tip. ``done``: the first-task card was answered;
    ``skipped``: Skip setup; ``replaced``: a rerun opened a new run; ``deleted``: its chat was deleted."""

    run_id: str
    stored_session_id: str
    outcome: OnboardingOutcome


event("onboarding.ended", OnboardingEndedPayload,
      doc="A first-run onboarding run ended; sent to every connected window.")
event("setup.ready", SetupReadyPayload,
      doc="The free-tier bootstrap finished (broadcast); the desktop's setup gate reads the record.")


event("free_tier.challenge", FreeTierChallengePayload,
      doc="The account service wants a browser challenge cleared before the free-tier token exchange "
          "(broadcast); the desktop loads ``url`` in a hidden window.")


class ErrorPayload(Payload):
    """Every ``_emit("error", …)`` site sets exactly ``message``."""

    message: str


event("error", ErrorPayload, doc="A session-level failure outside a turn (agent init, model switch, compression, resume).")


class NoticePayload(Payload):
    """``tui_gateway/model_switch.py`` capability-refresh notice."""

    message: str


event("notice", NoticePayload, doc="Informational one-liner for the session (capabilities refreshed).")


# ── turn stream ───────────────────────────────────────────────────────────────────────────────


class MessageStartPayload(Payload):
    """``message.start``. Empty from the TUI gateway's emitters and for an ordinary admission;
    ``gateway/session_authority._stamp_claim`` adds an automation admission's async-result card
    (``session_automation.admission_display``: a background-process completion) and its model
    text, so a viewer that did not submit the turn paints the same header the submitter would."""

    display_kind: str | None = None
    display_text: str | None = None
    notification_category: str | None = None
    text: str | None = None
    #: ``stream_segments.TurnSegments`` identity of the prompt turn; a client that sees the same id again is
    #: looking at a re-sent frame of the turn it already has, not a new turn.
    turn_id: str | None = None
    #: Identity of the assistant message this turn produces (one per turn today); shared by its start,
    #: deltas and completion so a client can upsert the message instead of trusting frame order.
    message_id: str | None = None


event("message.start", MessageStartPayload,
      doc="A turn began streaming; an automation turn carries its async-result card.")


class StreamDeltaPayload(Payload):
    """``prompt_turn._invoke_agent._stream`` (message.delta: ``text`` + optional ``rendered``),
    ``agent_callbacks._agent_cbs`` (reasoning.delta / thinking.delta), ``tool_progress._progress_reasoning``
    (reasoning.available). ``verbose`` rides only when the session's verbose reasoning mode is on."""

    text: str
    rendered: str | None = None
    verbose: bool | None = None


class MessageDeltaPayload(StreamDeltaPayload):
    """``message.delta``. A session-authority local-viewer turn also publishes ``text: null``: the
    reply segment boundary (the agent's ``stream_delta_callback(None)``, before tool lines) that
    closes the classic CLI's reply box. Every other viewer ignores an empty chunk.

    A prompt turn stamps ``turn_id`` + ``message_id`` + ``segment`` (interim seals before it) + ``offset``
    (UTF-16 code units of that segment already sent): a client drops a re-sent chunk it already applied.
    ``content_index`` names the assistant message block the chunk belongs to (the text block a client is
    appending to; it advances with each interim seal)."""

    text: str | None
    turn_id: str | None = None
    message_id: str | None = None
    content_index: int | None = None
    segment: int | None = None
    offset: int | None = None


event("message.delta", MessageDeltaPayload, doc="One streamed chunk of the assistant reply (null text: segment boundary).")
event("reasoning.delta", StreamDeltaPayload, doc="One streamed chunk of the model's reasoning.")
event("reasoning.available", StreamDeltaPayload, doc="A completed reasoning block (non-streaming providers).")
event("thinking.delta", StreamDeltaPayload, doc="Legacy thinking-text chunk (thinking_callback).")


class MessageInterimPayload(Payload):
    """``prompt_turn._interim_assistant_cb`` / ``agent_callbacks`` interim_assistant_callback."""

    text: str
    already_streamed: bool
    #: The turn and segment this seal closes; a re-sent seal of a closed segment is not a new bubble.
    turn_id: str | None = None
    message_id: str | None = None
    segment: int | None = None


event("message.interim", MessageInterimPayload,
      doc="Interim assistant commentary (text beside tool calls) sealed as its own segment.")


class MessageBlockStartPayload(Payload):
    """``prompt_turn._emit_delta`` opens the text block with the first non-empty chunk of a segment.
    ``content_index`` is the segment counter (every interim seal starts a new block); ``kind`` is the
    block's content type (``text`` today)."""

    kind: str
    content_index: int = 0
    message_id: str | None = None


class MessageBlockEndPayload(Payload):
    """``prompt_turn`` closes the text block at the interim seal or turn completion, carrying the
    block's whole content so a client that missed deltas can still render it."""

    kind: str
    content_index: int = 0
    content: JsonValue = None
    message_id: str | None = None


event("message.block_start", MessageBlockStartPayload,
      doc="An assistant message block (text) began; its deltas carry the same content_index.")
event("message.block_end", MessageBlockEndPayload,
      doc="An assistant message block closed, with its full content.")


class MessageAdviserPayload(Payload):
    """``agent_callbacks.plugin_message_callback``: one injected adviser row. ``row_id`` is the
    durable row the live event refers to (a snapshot that already has it merges, never appends);
    ``details`` is ADR-10 D1's advisor shape, so a client never parses the note text."""

    id: str
    text: str
    severity: str
    label: str
    row_id: int | None = None
    details: JsonValue | None = None


event("message.adviser", MessageAdviserPayload,
      doc="Plugin adviser advice injected into the live transcript; render as an adviser card.")


class TranscriptNoticePayload(Payload):
    """ADR-10 D7: one already-happened lifecycle line, deduped by the gateway across
    ``agent.print`` / ``status.update``. ``row_id`` is set when a durable custom row exists."""

    id: str
    level: Literal["info", "warning", "error"]
    text: str
    source: str
    row_id: int | None = None


event("transcript.notice", TranscriptNoticePayload,
      doc="A deduped one-shot transcript notice; omp-ui ignores agent.print/status.update for these lines.")


class TranscriptCustomPayload(Payload):
    """ADR-9 D4: one already-durable ``role:"custom"`` row, projected exactly as
    ``session.resume``/``session.history`` would project it (``row_id`` is required for merging)."""

    message: TranscriptMessage


event("transcript.custom", TranscriptCustomPayload,
      doc="A durable custom transcript row injected live; omp-ui merges it by row_id and renders it "
          "with the same component a resume snapshot uses.")


class TurnStatus(WireEnum):
    """``prompt_turn._result_status``."""

    complete = "complete"
    error = "error"
    interrupted = "interrupted"


class UsageLimitSuggestion(Payload):
    """``agent/plan_limits.py::ModelSuggestion`` — a recommended model that still works past the limit."""

    model: str
    label: str
    note: str                    # "free" | "included"
    provider: str = "nous"


class UsageLimitCredits(Payload):
    """``agent/plan_limits.py::CreditsOption`` — ``use_credits`` (turn on Keep going), ``add_credits``, or
    ``add_credits_and_keep_going`` (both: Keep going is off and there is no balance)."""

    action: str


class UsageLimit(Payload):
    """``agent/plan_limits.py::LimitHit`` — a usage-window plan's limit: what ran out, when it resets, and
    the one-click ways on (switch to a recommended model, use / add credits)."""

    gauge_id: str
    scope: str
    limit_name: str
    resets_at_ms: int | None = None
    message: str
    suggestions: list[UsageLimitSuggestion] = Field(default_factory=list)
    credits: UsageLimitCredits | None = None
    current_model: str = ""


class ErrorSurface(Payload):
    """``agent/error_surface.py::_surface`` — advisory {layer, code, retryable} (+ identity, + auth hint,
    + ``resets_at`` epoch seconds when the provider named when its limit lifts, + ``usage_limit`` on a
    usage-window plan's limit)."""

    layer: str
    code: str
    retryable: bool
    provider: str | None = None
    model: str | None = None
    resets_at: float | None = None
    usage_limit: UsageLimit | None = None
    model_config = Payload.model_config | {"extra": "allow"}


class BillingBlock(Payload):
    """``agent/billing_links.py::BillingBlock.to_dict`` (+ ``unverified`` from conversation_loop)."""

    provider: str
    provider_label: str
    model: str
    billing_url: str | None
    is_nous: bool
    message: str
    unverified: bool | None = None


class PersistedTurn(Payload):
    """Committed SQLite row addresses for the agent's current-turn suffix. Missing ids are
    unproven, never negative acknowledgements. ``complete`` permits retiring the whole local
    turn only when the original turn boundary, every row and final body are still accounted
    for; compaction, redirects and partial writes conservatively leave it false. Row ids are
    scoped to the owning profile's store, as in ``SessionMessage.row_id``."""

    row_ids: list[int]
    complete: bool
    user_row_id: int | None = None
    final_assistant_row_id: int | None = None
    #: Every rendered user row of the turn in order (prompt, then steer/redirect rows); absent
    #: unless all of them committed. Binds optimistic bubbles a submit receipt could not name.
    user_row_ids: list[int] | None = None
    #: The client submission the turn ran (session authority only; its ack precedes the user row).
    submission_id: str | None = None


class MessageCompletePayload(Payload):
    """``prompt_turn._complete_turn_payload`` / ``session_auto_continue._emit_terminal_turn_error`` /
    ``agent_callbacks._mirror_subagent_to_child`` (child watch mirror: ``text`` only) /
    ``compute_host_bridge`` (``text`` + ``status``)."""

    text: str | JsonValue = ""
    usage: Usage | None = None
    #: ADR-2 D1: the turn's LAST provider response, in ``Usage`` shape. ``usage`` stays the
    #: session-cumulative view; a response without usage leaves this null (never a copy of it).
    step_usage: Usage | None = None
    #: ADR-2 D2: the model id the request asked for and the one that actually served it
    #: (proxy-reported deployment / Hermes' fallback route). Absent is null.
    served_model: str | None = None
    requested_model: str | None = None
    status: TurnStatus | None = None
    #: The assistant message this completion closes (the turn's ``message_id`` from message.start).
    message_id: str | None = None
    #: The model's stop reason in omp's vocabulary: ``stop`` | ``length`` | ``toolUse`` | ``error`` |
    #: ``aborted``. Hermes carries the same outcome as ``status``; this is the one-way projection.
    stop_reason: str | None = None
    reasoning: str | None = None
    warning: str | None = None
    response_previewed: bool | None = None
    #: ``text`` is a response this turn already delivered (streamed and/or sealed) — it adds no text.
    response_reused: bool | None = None
    response_transformed: bool | None = None
    billing: BillingBlock | None = None
    failure_reason: str | None = None
    rendered: str | None = None
    error: str | None = None
    recoverable: bool | None = None
    error_surface: ErrorSurface | None = None
    partial: bool | None = None
    persisted_turn: PersistedTurn | None = None


event("message.complete", MessageCompletePayload, doc="The turn ended: final text, usage and outcome.")


class TurnStartPayload(Payload):
    """``prompt_turn._run_prompt_submit`` after the prompt is admitted (one per accepted prompt turn).
    ``resumed_tool_call_ids`` is absent: Hermes never resumes a tool call mid-flight."""

    resumed_tool_call_ids: list[str] | None = None
    message_id: str | None = None


class TurnEndPayload(Payload):
    """``prompt_turn.run_body``'s ``finally`` — the closing half of ``turn.start``, emitted on every
    exit path (completion, error, interrupt). ``message``/``tool_results`` stay absent until the
    turn result exposes the omp ``AgentMessage`` shape; ``status`` is Hermes-native
    (``complete`` / ``error`` / ``interrupted``)."""

    status: str | None = None
    message: JsonValue | None = None
    tool_results: list[JsonValue] = Field(default_factory=list)
    message_id: str | None = None


event("turn.start", TurnStartPayload, doc="An accepted prompt turn began (paired with turn.end).")
event("turn.end", TurnEndPayload, doc="The accepted prompt turn finished; emitted exactly once, on every exit path.")


class PromptResultError(Payload):
    """The terminal error of a prompt, in omp's ``prompt_result.error`` shape."""

    message: str
    provider: str | None = None
    model: str | None = None
    http_status: int | None = None
    retryable: bool = False
    code: str | None = None


class PromptResultPayload(Payload):
    """``prompt_turn.run_body``'s ``finally``: the terminal outcome of one accepted prompt, correlated
    by the submit request id (``admission_id``). ``session_settled`` stays false; the separate
    ``session.settled`` frame closes the queue."""

    admission_id: str
    agent_invoked: bool
    status: str  # completed | aborted | error
    error: PromptResultError | None = None
    session_settled: bool = False


class SessionSettledPayload(Payload):
    """``prompt_turn.run`` once post-turn follow-ups left the session idle (no queued prompt, no goal
    continuation, no running turn)."""

    idle_since: float | None = None


event("prompt.result", PromptResultPayload,
      doc="A prompt reached its terminal outcome (completed / aborted / error), keyed by the submit id.")
event("session.settled", SessionSettledPayload,
      doc="The session is idle: no turn running and no queued prompt left to drain.")


class StatusUpdatePayload(Payload):
    """``server._status_update`` and the direct emitters (goal / loop / heartbeat / process)."""

    kind: str
    text: str


event("status.update", StatusUpdatePayload, doc="Transient status line (kind: status, lifecycle, compacting, goal, loop, heartbeat, process, …).")


class SessionUsagePayload(Payload):
    """``usage``: the status-bar view (``server._get_usage``), sent by the embedded backend's ticker
    (``server._start_usage_ticker``) and by a canonical local viewer's gateway after every API call
    and at turn end (``gateway/session_usage.py::usage_frame``). Read this.

    Deprecated: the top-level session-cumulative counters (``usage_stats``, the ``session.stats``
    dict) that the gateway frame also carries. They are kept for existing readers; new readers use
    ``usage``, or ``session.stats`` for the raw counters. The embedded ticker never sends them."""

    usage: Usage | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    reasoning_tokens: int | None = None
    api_calls: int | None = None
    last_prompt_tokens: int | None = None
    context_tokens: int | None = None
    context_length: int | None = None
    config_context_length: int | None = None
    context_source: str | None = None
    compression_count: int | None = None
    avg_latency_s: float | None = None
    tokens_per_s: float | None = None
    last_turn_tokens: int | None = None
    rate_limit: dict[str, JsonValue] | None = None
    active_subagents: int | None = None
    model: str | None = None
    provider: str | None = None
    fallback_model: str | None = None
    plan_gauge: PlanGauge | None = None


event("session.usage", SessionUsagePayload, doc="Mid-turn usage tick; message.complete carries the authoritative final usage.")


class SessionTitlePayload(Payload):
    """``prompt_turn._invoke_agent`` ``_on_session_title`` hook."""

    session_id: str
    title: str


event("session.title", SessionTitlePayload, doc="Auto-titling renamed the session (``session_id`` is the stored key).")


class ReactionPayload(Payload):
    """``agent_callbacks`` reaction_callback."""

    kind: str


event("reaction", ReactionPayload, doc="Affection reaction detected in the user's message (hearts etc.).")


class ReviewSummaryPayload(Payload):
    """``server`` background_review_callback."""

    text: str


event("review.summary", ReviewSummaryPayload, doc="Background review of the last turn finished.")


class CompactionSummaryPayload(Payload):
    """ADR-5 D4: a committed compaction boundary, from the same carrier row ``session.resume``
    projects as ``role:"compaction_summary"`` (``agent/compaction_display.py``). Counters are the
    message-level ``estimate_messages_tokens_rough`` values, never the committed-shape re-estimate.
    ``trigger`` is why this attempt ran (``idle``/``pre_api``/``overflow``/``manual``/...);
    ``boundary_row_id`` is the durable carrier row a snapshot uses to merge with this event."""

    text: str
    tokens_before: int
    tokens_after: int | None = None
    token_method: str
    method: str
    trigger: str
    timestamp: float
    boundary_row_id: int | None = None


event("compaction.summary", CompactionSummaryPayload,
      doc="A real compaction commit: the summary body plus the attempt's token estimate and method.")


class AgentPrintPayload(Payload):
    """A session-authority local-viewer turn: one line the agent printed through ``_print_fn``
    (subagent completion, compaction, errors, budget notices), which main's classic CLI rendered
    through ``_agent_status_print``."""

    text: str


event("agent.print", AgentPrintPayload, doc="A status line the agent printed during a local viewer's turn.")


# ── TTSR (ADR-6 P1) ───────────────────────────────────────────────────────────────────────────


class TtsrRulePayload(Payload):
    """One matched rule, exactly the fields ``TtsrNotificationComponent`` renders.

    ``description``/``content`` may be empty strings; the component falls back from one to the
    other. Local paths and matching metadata never cross the wire (ADR-6 D3).
    """

    name: str
    description: str | None = None
    content: str | None = None


class TtsrTriggeredPayload(Payload):
    """``gateway.local_viewer_events.publish_ttsr_triggered`` (ADR-6 D3)."""

    rules: list[TtsrRulePayload]


event("ttsr.triggered", TtsrTriggeredPayload,
      doc="A TTSR rule matched this local viewer's turn; render the notification component.")


class MessageRewindPayload(Payload):
    """``gateway.local_viewer_events.publish_message_rewind`` (ADR-6 D13).

    Live-only: the discarded assistant draft is not a transcript row, so a client removes its
    streaming block and reopens on the next delta. ``segment`` is the retry's fresh counter.
    """

    reason: str
    message_id: str | None = None
    segment: int | None = None


event("message.rewind", MessageRewindPayload,
      doc="The live assistant draft was discarded (TTSR stream rewind); drop it and reopen.")


# ── tools ─────────────────────────────────────────────────────────────────────────────────────


class ToolStartPayload(Payload):
    """``tool_progress._on_tool_start`` (+ ``agent_callbacks._mirror_subagent_to_child`` rows with
    ``preview`` and empty ``args``). ``todos``/``revision`` are NOT set by the emitter; kept optional
    because tool.start rows may pass through connector redaction unchanged."""

    tool_id: str
    name: str
    context: str | None = None
    args: dict[str, JsonValue] | None = None
    args_text: str | None = None
    preview: str | None = None
    labels: list[ToolLabel] | None = None
    #: ADR-2 D1: the provider response that requested this call, in ``Usage`` shape. JSON-only
    #: (no ``model``/cumulative context fields) and absent for responses that reported no usage.
    step_usage: Usage | None = None


event("tool.start", ToolStartPayload, doc="A tool call began (stable id + full args).")


class ToolCompletePayload(Payload):
    """``tool_progress._on_tool_complete``; ``todos``/``revision`` merged in for the todo tools."""

    tool_id: str
    name: str
    args: dict[str, JsonValue] | None = None  # mirrored child rows / room relays omit it
    duration_s: float | None = None
    result: JsonValue = None
    summary: str | None = None
    #: The executor's failure verdict for this call (``_detect_tool_failure``); absent from older
    #: emitters that never learned the outcome.
    is_error: bool | None = None
    result_text: str | None = None
    inline_diff: str | None = None
    todos: list[JsonValue] | None = None
    revision: int | None = None
    labels: list[ToolLabel] | None = None
    image_path: str | None = None  # local image the call produced or looked at (agent.terminal_image.tool_image_path)


event("tool.complete", ToolCompletePayload, doc="A tool call finished: parsed result, summary, optional diff / todo snapshot.")


class ToolGeneratingPayload(Payload):
    """``agent_callbacks`` tool_gen_callback."""

    name: str


event("tool.generating", ToolGeneratingPayload, doc="The model is emitting a tool call's arguments.")


class ToolOutputRiskPayload(Payload):
    """``tool_progress._progress_output_risk``."""

    tool_id: str
    name: str
    risk: str
    findings: list[str]
    redacted: bool


event("tool.output_risk", ToolOutputRiskPayload, doc="Tool output was classified as risky (prompt-injection / secret findings).")


class ToolProgressPayload(Payload):
    """``agent.tool_progress_relay.tool_progress_payload`` — an MCP server's
    ``notifications/progress`` for a running call, throttled to one per 500 ms per call."""

    tool_id: str
    name: str
    progress: float
    total: float | None = None
    percent: int | None = None
    message: str | None = None


event("tool.progress", ToolProgressPayload, doc="Live progress of a running tool call (MCP notifications/progress).")


class TodoUpdatedPayload(Payload):
    """``tool_progress._normalize_todo_state`` — full task snapshot."""

    todos: list[JsonValue]
    revision: int


event("todo.updated", TodoUpdatedPayload, doc="Full todo snapshot after a todo tool ran.")


class TodoReminderPayload(Payload):
    """ADR-10 D3: one stop-gate reminder row already appended to the transcript.
    ``row_id`` is the durable row so a snapshot that already has it merges instead of appending
    a second card; ``todos`` uses omp's ``TodoItem`` status vocabulary."""

    todos: list[JsonValue]
    attempt: int
    max_attempts: int
    row_id: int


event("todo.reminder", TodoReminderPayload,
      doc="The agent stopped with unfinished todos; render the durable reminder row as a card.")


class QueueUpdatedPayload(Payload):
    """The displayable prompt queue, in omp's ``queue_update`` shape.

    Emitted by ``session_auto_continue._emit_queue_update`` (the TUI's next-turn queue) and by
    ``gateway/session_authority._publish_pending`` (canonical admissions: ``steering`` rows are the
    accepted steers, ``follow_up`` the still-queued user texts). ``live_steered`` counts steering
    entries already handed to the streaming response."""

    steering: list[str] = Field(default_factory=list)
    follow_up: list[str] = Field(default_factory=list)
    live_steered: int = 0


class CommandOutputPayload(Payload):
    """``methods_tools.command.dispatch`` / ``slash.exec``: the text a slash command printed
    out of band (the synchronous result already carries the same text)."""

    text: str
    name: str | None = None


event("queue.updated", QueueUpdatedPayload,
      doc="The pending prompt queue changed; replace the client's queue chips with this snapshot.")
event("command.output", CommandOutputPayload,
      doc="A slash command produced renderer-visible output text.")


# ── notifications ─────────────────────────────────────────────────────────────────────────────


class NotificationShowPayload(Payload):
    """``agent/credits_tracker.py::AgentNotice`` via notice_callback, and ``server._await_agent_ready``'s
    slow-build notice. ``level``: info | warn | error | success; ``kind``: sticky | ttl | agent.
    ``detail``: the full text behind a short ``text`` (the low-disk auto-prune question): Desktop shows
    it in the toast's expandable body, the Ink TUI prints it into the transcript."""

    text: str
    level: str
    kind: str
    ttl_ms: int | None = None
    key: str | None = None
    id: str | None = None
    detail: str | None = None


class NotificationClearPayload(Payload):
    key: str


event("notification.show", NotificationShowPayload, doc="Show / replace a keyed out-of-band notice (toast or status bar).")
event("notification.clear", NotificationClearPayload, doc="Withdraw the notice with this key.")


class TipShowPayload(Payload):
    """``tools/tip_tool.py``."""

    selector: str
    text: str
    title: str | None = None
    side: str | None = None


event("tip.show", TipShowPayload, doc="Point at a desktop element with a one-line tip bubble.")


# ── session lifecycle ─────────────────────────────────────────────────────────────────────────


# SessionLiveInfo is a Result (it is also the ``info`` of create/resume/activate); the registry only
# needs ``model_validate`` and the generator renders one TS type either way.
event("session.info", SessionLiveInfo,  # type: ignore[arg-type]
      doc="Live session settings snapshot (``server._session_info``); also the ``info`` of create/resume/activate.")


class ResumePhaseStatus(WireEnum):
    loading = "loading"
    complete = "complete"
    failed = "failed"


class SessionResumeProgressPayload(Payload):
    """``server._hydrate_resume_history``."""

    phase: str
    status: ResumePhaseStatus
    message_count: int | None = None
    message: str | None = None


event("session.resume_progress", SessionResumeProgressPayload, doc="Deferred resume hydration progress.")


class SessionReclaimedPayload(Payload):
    """``session_lifecycle._announce_session_reclaimed`` (broadcast)."""

    session_id: str
    stored_session_id: str
    reason: str  # idle_timeout | lru_evict | ws_orphan_reap


event("session.reclaimed", SessionReclaimedPayload, doc="The backend reclaimed a live session out from under its clients.")


class ApprovalCancelledPayload(Payload):
    """``session_lifecycle._announce_cancelled_gateway_approvals`` (broadcast).

    One frame for every pending approval dropped by an interrupt / reap / teardown (#106678) — the
    deny-resolve is silent without it, so a reconnecting client's prompt looks lost rather than cancelled.
    ``cancelled_count`` is the number of dropped entries; ``request_ids`` omits empty/missing ids, so the
    two can disagree when an entry has no request_id.
    """

    session_id: str
    stored_session_id: str
    reason: str  # interrupt | ws_orphan_reap | idle_timeout | lru_evict | tui_close | ...
    cancelled_count: int
    request_ids: list[str]


event("approval.cancelled", ApprovalCancelledPayload,
      doc="Pending gateway approvals were dropped by interrupt/reap/teardown; the wait resolved as deny (not a user refusal).")


class SessionControlUpdatePayload(Payload):
    control: SessionControlSnapshot


event("session.control.update", SessionControlUpdatePayload, doc="Persisted goal / loop / heartbeat state changed.")


class BillingStepUpVerificationPayload(Payload):
    """``methods_session`` billing.step_up on_verification."""

    verification_url: str
    user_code: str


event("billing.step_up.verification", BillingStepUpVerificationPayload,
      doc="Device-flow URL + code for the billing scope step-up; the client opens the browser.")


# ── side agents (methods_prompt._spawn_side_agent) ────────────────────────────────────────────


class SideAgentCompletePayload(Payload):
    """``methods_prompt._spawn_side_agent``: ``{task_id, **extra, text}``; btw adds ``question``."""

    task_id: str
    text: str
    question: str | None = None


event("background.complete", SideAgentCompletePayload, doc="A /background side agent finished.")
event("btw.complete", SideAgentCompletePayload, doc="A /btw side question was answered.")
event("preview.restart.complete", SideAgentCompletePayload, doc="The hidden preview-restart agent finished.")


class PreviewRestartProgressPayload(Payload):
    """``methods_prompt`` restart body + ``agent_callbacks._preview_restart_callbacks``."""

    task_id: str
    text: str
    level: str | None = None


event("preview.restart.progress", PreviewRestartProgressPayload, doc="Progress line from the preview-restart agent.")


# ── subagents (tool_progress._progress_subagent) ──────────────────────────────────────────────


class SubagentOutputTailEntry(Payload):
    """``tools/delegate_tool_results.py::_extract_output_tail`` row."""

    tool: str
    preview: str
    is_error: bool


class SubagentEventPayload(Payload):
    """``tool_progress._progress_subagent`` — every ``subagent.*`` frame; identity fields are optional
    because older emitters omit them and the TUI spawn tree falls back to flat rendering."""

    goal: str
    task_count: int
    task_index: int
    subagent_id: str | None = None
    parent_id: str | None = None
    child_session_id: str | None = None
    delegation_id: str | None = None
    depth: int | None = None
    model: str | None = None
    tool_count: int | None = None
    toolsets: list[str] | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    api_calls: int | None = None
    files_read: list[str] | None = None
    files_written: list[str] | None = None
    output_tail: list[SubagentOutputTailEntry] | None = None
    tool_name: str | None = None
    text: str | None = None
    status: SubagentStatus | None = None
    summary: str | None = None
    duration_seconds: float | None = None
    tool_preview: str | None = None


for _name, _doc in (
    ("subagent.spawn_requested", "delegate_task accepted a child goal (before the child starts)."),
    ("subagent.start", "A delegated child started running."),
    ("subagent.progress", "Batched tool-name progress from a child."),
    ("subagent.thinking", "A child's reasoning chunk."),
    ("subagent.tool", "A child called a tool."),
    ("subagent.complete", "A child finished (status + observability rollup)."),
):
    event(_name, SubagentEventPayload, doc=_doc)


# ── MoA ───────────────────────────────────────────────────────────────────────────────────────


class MoaReferencePayload(Payload):
    """``tool_progress._progress_moa_reference``."""

    label: str
    text: str
    index: int | None = None
    count: int | None = None


class MoaAggregatingPayload(Payload):
    aggregator: str


class MoaProgressPayload(Payload):
    label: str
    refs_done: int
    refs_total: int


class MoaPhasePayload(Payload):
    phase: str
    refs_done: int | None = None
    refs_total: int | None = None
    aggregator: str | None = None


event("moa.reference", MoaReferencePayload, doc="One MoA reference model's output.")
event("moa.aggregating", MoaAggregatingPayload, doc="The MoA aggregator started.")
event("moa.progress", MoaProgressPayload, doc="MoA reference fan-out progress (n/total).")
event("moa.phase", MoaPhasePayload, doc="MoA phase transition (currently only ``aggregator``).")


# ── desktop GUI (tools/desktop_ui emitters) ──────────────────────────────────────────────────


class PreviewOpenPayload(Payload):
    """``tools/open_preview_tool.py``."""

    url: str
    label: str = ""


class PreviewClosePayload(Payload):
    """``tools/preview_tool.py`` / ``tools/close_preview_tool.py``; ``url`` '' closes every tab."""

    url: str = ""


event("preview.open", PreviewOpenPayload, doc="Open a URL / file in the desktop preview pane.")
event("preview.close", PreviewClosePayload, doc="Close the preview pane or one tab.")


class PaneRevealPayload(OpenPayload):
    """``tools/focus_pane_tool.py``."""

    pane: str


event("pane.reveal", PaneRevealPayload, doc="Focus / reveal a named desktop pane.")


class MessageReactionPayload(Payload):
    """``tools/react_to_message_tool.py``."""

    row_id: int
    reactions: list[MessageReaction]
    role: str


event("message.reaction", MessageReactionPayload, doc="The agent reacted to a message; paint it live.")


class TerminalOutputPayload(Payload):
    """``session_notifications`` process_registry.on_output."""

    process_id: str
    chunk: str


class TerminalClosePayload(Payload):
    process_id: str


event("agent.terminal.output", TerminalOutputPayload, doc="Output chunk from an agent-owned background process.")
event("terminal.close", TerminalClosePayload, doc="An agent-owned background process closed.")


class BrowserProgressPayload(Payload):
    """``methods_browser`` announce(); ``level``: info | warn | error."""

    message: str
    level: str


event("browser.progress", BrowserProgressPayload, doc="Browser (CDP) connect / install progress line.")


# ── voice ─────────────────────────────────────────────────────────────────────────────────────


event("voice.interrupted", None, doc="Barge-in: the spoken interjection interrupted the turn; no payload.")


class VoiceStatusPayload(Payload):
    """``methods_voice._vr_on_status``; states come from the recorder (idle / listening / transcribing …)."""

    state: str


class VoicePartialPayload(Payload):
    """``methods_voice`` voice.record ``on_partial`` — live STT text so far (``stt.streaming``)."""

    text: str


class VoiceTranscriptPayload(Payload):
    """``methods_voice._vr_transcript`` / ``_deliver_fd_transcript`` / typed stop phrase in methods_prompt."""

    text: str | None = None
    stop_phrase: bool | None = None
    typed: bool | None = None
    no_speech_limit: bool | None = None


class WakeDetectedPayload(Payload):
    """``methods_voice`` wake detector ``_on_detect``."""

    phrase: str
    profile: str | None = None
    start_new_session: bool


event("voice.status", VoiceStatusPayload, doc="Voice recorder state changed.")
event("voice.partial", VoicePartialPayload, doc="Live STT text so far while the user is still speaking.")
event("voice.transcript", VoiceTranscriptPayload, doc="A voice capture produced text (or a stop phrase / silence limit).")
event("wake.detected", WakeDetectedPayload, doc="A wake phrase fired.")


# ── pets ──────────────────────────────────────────────────────────────────────────────────────


class PetChangedPayload(OpenPayload):
    """``change_watcher._pet_changed_payload`` — ``pet.info.meta``-shaped; ``{enabled: false}`` when off."""

    enabled: bool
    slug: str | None = None
    displayName: str | None = None
    scale: float | None = None
    spritesheetRevision: str | None = None


class PetGenerateProgressPayload(OpenPayload):
    """``methods_session`` pet.generate: token-only init frame, then one per draft."""

    token: str
    count: int
    index: int | None = None
    dataUri: str | None = None


class PetHatchProgressPayload(OpenPayload):
    """``methods_session`` pet.hatch ``_on_progress``: ``{event, detail}`` or the parsed row form."""

    event: str
    detail: str | None = None
    state: str | None = None
    done: str | None = None
    total: str | None = None


event("pet.changed", PetChangedPayload, doc="The active pet / its spritesheet changed (watcher).")
event("pet.generate.progress", PetGenerateProgressPayload, doc="Pet base-draft generation progress.")
event("pet.hatch.progress", PetHatchProgressPayload, doc="Pet hatch (row drawing) progress.")


# ── change watcher signals (payload is {} today) ─────────────────────────────────────────────


class ChangeSignalPayload(OpenPayload):
    """``change_watcher._CHANGE_WATCHES`` payload fn — ``{}`` for every watch except pet.changed."""


event("cron.changed", ChangeSignalPayload, doc="cron/jobs.json moved; refetch the cron list.")
event("sessions.changed", ChangeSignalPayload, doc="state.db moved; refetch the session list.")
event("platforms.changed", ChangeSignalPayload, doc="gateway_state.json moved; refetch platform status.")
event("projects.changed", ChangeSignalPayload, doc="projects.db moved; refetch the project list + tree.")
event("pairing.changed", ChangeSignalPayload, doc="Pairing state moved; refetch pairing.")
event("skills.changed", ChangeSignalPayload,
      doc="A skill root moved (installed/removed elsewhere); refetch commands.catalog from the gateway.")


class CommandsChangedPayload(Payload):
    """``change_watcher`` after the skill-root signature moved: the slash catalog may have changed.
    ``commands`` stays absent; the client refetches ``commands.catalog`` (the catalog is
    profile-scoped and too large for a watcher frame)."""

    commands: list[JsonValue] | None = None


event("commands.changed", CommandsChangedPayload,
      doc="The available slash-command catalog may have changed; refetch it.")
event("bot_relay.outbox.pending", ChangeSignalPayload, doc="A bot-relay outbox envelope is queued; drain it.")


__all__ = [
    "BillingBlock", "BillingStepUpVerificationPayload", "BrowserProgressPayload", "ChangeSignalPayload", "CommandOutputPayload",
    "CommandsChangedPayload", "ErrorPayload",
    "ErrorSurface", "FreeTierChallengePayload", "GatewayReadyPayload",
    "MessageBlockEndPayload", "MessageBlockStartPayload",
    "MessageCompletePayload", "MessageInterimPayload", "MessageReaction", "MessageReactionPayload", "MoaAggregatingPayload",
    "MoaPhasePayload", "MoaProgressPayload", "MoaReferencePayload", "NoticePayload",
    "NotificationClearPayload", "NotificationShowPayload", "OpenPayload", "PaneRevealPayload",
    "PetChangedPayload", "PetGenerateProgressPayload", "PetHatchProgressPayload", "PreviewClosePayload",
    "PreviewOpenPayload", "PreviewRestartProgressPayload", "PromptResultError", "PromptResultPayload",
    "QueueUpdatedPayload", "ReactionPayload", "ResumePhaseStatus",
    "ReviewSummaryPayload", "SessionControlSnapshot", "SessionControlUpdatePayload",
    "SessionReclaimedPayload", "SessionResumeProgressPayload", "SessionSettledPayload", "SessionTitlePayload",
    "SessionUsagePayload",
    "SetupReadyPayload", "SideAgentCompletePayload", "SkinPayload", "StatusUpdatePayload",
    "StreamDeltaPayload", "SubagentEventPayload", "SubagentOutputTailEntry", "TerminalClosePayload",
    "TerminalOutputPayload", "TipShowPayload", "TodoUpdatedPayload", "ToolCompletePayload",
    "ToolGeneratingPayload", "ToolLabel", "ToolLabelKind", "ToolOutputRiskPayload", "ToolProgressPayload",
    "ToolStartPayload",
    "TurnEndPayload", "TurnStartPayload", "TurnStatus", "VoicePartialPayload", "VoiceStatusPayload",
    "VoiceTranscriptPayload", "WakeDetectedPayload",
]
