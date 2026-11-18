"""Tests for live session context breakdown."""

from unittest.mock import MagicMock, patch

from agent.context_breakdown import compute_session_context_breakdown

def _make_agent(
    *,
    stable: str = "identity and guidance",
    context: str = "",
    volatile: str = "timestamp line",
    tools: list | None = None,
    context_length: int = 200_000,
    last_prompt_tokens: int = 0,
    threshold_tokens: int = 170_000,
):
    agent = MagicMock()
    agent.model = "openai/gpt-5.4"
    agent.tools = tools or [
        {"type": "function", "function": {"name": "terminal", "description": "run"}},
        {"type": "function", "function": {"name": "mcp_demo_tool", "description": "mcp"}},
        {"type": "function", "function": {"name": "delegate_task", "description": "spawn"}},
    ]
    agent._memory_store = None
    agent._memory_enabled = True
    agent._user_profile_enabled = True
    agent.context_compressor = MagicMock(
        context_length=context_length,
        last_prompt_tokens=last_prompt_tokens,
        threshold_tokens=threshold_tokens,
    )
    return agent, {"stable": stable, "context": context, "volatile": volatile}

def test_breakdown_includes_major_categories():
    stable = (
        "base guidance\n"
        "<available_skills>\n  demo:\n    - hello: hi\n</available_skills>"
    )
    context = "# Project Context\nFollow AGENTS.md"
    volatile = "Current time: now"
    history = [{"role": "user", "content": "hello there"}]
    agent, parts = _make_agent(stable=stable, context=context, volatile=volatile)

    with patch("agent.system_prompt.build_system_prompt_parts", return_value=parts):
        data = compute_session_context_breakdown(agent, history)

    ids = {item["id"] for item in data["categories"]}
    assert {"system_prompt", "tool_definitions", "rules", "skills", "mcp", "subagent_definitions", "conversation"} <= ids
    assert data["context_max"] == 200_000
    assert data["estimated_total"] > 0

def test_context_used_never_exceeds_model_window():
    """Regression for #109760: anchor + appended-delta estimate can overshoot the window
    (355.8k / 262.1k); one prompt can never be larger than the model's context."""
    from agent.context_breakdown import context_usage_fields
    from agent.usage_anchor import capture_usage_anchor

    history = [{"role": "user", "content": "start"}, {"role": "assistant", "content": "ok"}]
    agent, parts = _make_agent(context_length=262_144)
    agent._turn_base_usage_anchor = capture_usage_anchor(250_000, 1_000, history)
    history = history + [{"role": "user", "content": "x" * 800_000}]

    with patch("agent.system_prompt.build_system_prompt_parts", return_value=parts):
        data = compute_session_context_breakdown(agent, history)

    assert data["context_source"] == "provider_usage_plus_estimate"
    assert data["context_used"] <= data["context_max"]
    assert data["context_percent"] == 100

    seeded = context_usage_fields(MagicMock(context_length=262_144, last_prompt_tokens=400_000,
                                            last_real_prompt_tokens=150_000))
    assert seeded["context_used"] <= seeded["context_max"]

def test_empty_transcript_still_reports_the_conversation_category():
    """Zero conversation tokens is a measurement, not an absence (#87903).

    A fresh session (or one whose first turn never completed) estimates the
    conversation at zero. Dropping the row there made "the transcript is empty"
    render identically to "the breakdown never measured it" — the Desktop
    Context usage panel hid Conversation until a turn completed. Structurally
    absent categories (mcp/memory/skills with nothing configured) stay dropped.
    """
    agent, parts = _make_agent(
        tools=[{"type": "function", "function": {"name": "terminal", "description": "run"}}]
    )
    history = []  # nothing said yet

    with patch("agent.system_prompt.build_system_prompt_parts", return_value=parts):
        data = compute_session_context_breakdown(agent, history)

    categories = {item["id"]: item["tokens"] for item in data["categories"]}
    assert categories["conversation"] == 0
    # Optional surfaces with nothing configured are still omitted at zero —
    # "not configured" is exactly what their absence communicates.
    assert "mcp" not in categories
    assert "skills" not in categories

def test_message_parts_split_the_conversation_without_changing_its_total():
    """Per-role rows are a projection of the conversation estimate, never a second estimator:
    the five parts sum to exactly the ``conversation`` category the old payload carried."""
    from agent.model_metadata import estimate_messages_tokens_rough

    history = [
        {"role": "user", "content": "根据分词表算 token 有没有方法？"},
        {"role": "assistant", "content": "", "reasoning_content": "用户问的是本地词表计数。",
         "tool_calls": [{"id": "c1", "function": {"name": "bash", "arguments": '{"cmd": "ls"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": '{"stdout": "agent tools tests"}'.ljust(4000, "x")},
        {"role": "assistant", "content": "有，用 tiktoken 或 HF tokenizers。"},
    ]
    agent, parts = _make_agent()

    with patch("agent.system_prompt.build_system_prompt_parts", return_value=parts):
        data = compute_session_context_breakdown(agent, history)

    categories = {item["id"]: item["tokens"] for item in data["categories"]}
    by_part = {part["id"]: part["tokens"] for part in data["message_parts"]}
    assert set(by_part) == {"user_messages", "assistant_messages", "assistant_thinking", "tool_calls", "tool_output"}
    assert sum(by_part.values()) == categories["conversation"] == estimate_messages_tokens_rough(history)
    assert all(tokens >= 0 for tokens in by_part.values())
    assert by_part["user_messages"] > 0
    assert by_part["assistant_thinking"] > 0
    assert by_part["tool_calls"] > 0
    assert by_part["tool_output"] > 0
    assert by_part["assistant_messages"] > 0
    assert data["tool_outputs"] == [{"name": "bash", "tokens": by_part["tool_output"]}]


def test_thinking_never_exceeds_the_message_it_belongs_to():
    """A huge ``reasoning`` blob on a message whose wire shadow drops it (because
    ``reasoning_content`` wins) must not leak into the thinking row."""
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "ok", "reasoning": "x" * 100_000, "reasoning_content": "brief"},
    ]
    agent, parts = _make_agent()

    with patch("agent.system_prompt.build_system_prompt_parts", return_value=parts):
        data = compute_session_context_breakdown(agent, history)

    by_part = {part["id"]: part["tokens"] for part in data["message_parts"]}
    categories = {item["id"]: item["tokens"] for item in data["categories"]}
    # The 100k ``reasoning`` blob is shadow-dropped, so it must not reach any part:
    # the whole conversation stays small and the parts still tile it exactly.
    assert sum(by_part.values()) == categories["conversation"] < 5_000
    assert all(tokens >= 0 for tokens in by_part.values())
    assert by_part["assistant_thinking"] < 100  # only the wire-relevant ``reasoning_content``


def test_window_budget_rows_follow_the_compaction_threshold():
    agent, parts = _make_agent(context_length=200_000, threshold_tokens=170_000)
    history = [{"role": "user", "content": "hello"}]

    with patch("agent.system_prompt.build_system_prompt_parts", return_value=parts):
        data = compute_session_context_breakdown(agent, history)

    assert data["context_max"] == 200_000
    assert data["context_threshold"] == 170_000
    assert data["auto_compact_buffer"] == 30_000
    assert data["context_used"] + data["auto_compact_buffer"] + data["free_tokens"] == data["context_max"]

    disabled, disabled_parts = _make_agent(context_length=200_000, threshold_tokens=0)
    with patch("agent.system_prompt.build_system_prompt_parts", return_value=disabled_parts):
        off = compute_session_context_breakdown(disabled, history)
    assert off["context_threshold"] == 0
    assert off["auto_compact_buffer"] == 0


# ── /context renderers (pure functions over the payload) ────────────────────

from agent.context_breakdown import (
    render_context_breakdown_lines,
    render_context_category_lines,
    render_context_grid,
)

def _payload(**overrides):
    base = {
        "categories": [
            {"id": "system_prompt", "label": "System prompt", "tokens": 10_000},
            {"id": "tool_definitions", "label": "Tool definitions", "tokens": 20_000},
            {"id": "skills", "label": "Skills", "tokens": 5_000},
            {"id": "conversation", "label": "Conversation", "tokens": 15_000},
        ],
        "context_max": 200_000,
        "context_percent": 25,
        "context_used": 50_000,
        "estimated_total": 50_000,
        "model": "openai/gpt-test",
    }
    base.update(overrides)
    return base

def test_grid_is_5x20_and_mostly_free():
    rows = render_context_grid(_payload())
    assert len(rows) == 5
    cells = " ".join(rows).split(" ")
    assert len(cells) == 100
    # 50k / 200k → 25 used cells, 75 free
    assert cells.count("·") == 75
    # Category glyphs proportional: 10k→5, 20k→10, 5k→2-3, 15k→7-8 cells
    assert cells.count("■") == 5
    assert cells.count("▣") == 10

def test_category_lines_prefer_the_wire_free_space_field():
    """`/context` text and clients must print the same free-space number: when the payload
    carries `free_tokens` (anchored used + compaction buffer), the renderer uses it instead
    of the local-estimate remainder."""
    with_field = "\n".join(render_context_category_lines(_payload(free_tokens=123_456)))
    assert "123,456" in with_field
    # Payloads predating the field keep the old estimate-remainder fallback.
    without_field = "\n".join(render_context_category_lines(_payload()))
    assert "150,000" in without_field  # 200k window - 50k estimated


def test_breakdown_lines_grid_toggle():
    with_grid = render_context_breakdown_lines(_payload(), grid=True)
    without = render_context_breakdown_lines(_payload(), grid=False)
    assert any("·" in line for line in with_grid[:5])
    assert not any("·" in line for line in without[:2])
