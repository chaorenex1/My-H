"""Sprint-5 acceptance: the naming documentation matches its post-ADR-21 roles (AS-5).

Documentation is asserted as prose (``read_text`` + text-anchored slices), never as rendered HTML:
the title chapter is anchored on its own opening sentence and runs to ``### Stream-only endpoints``,
because the nearest heading would pull in the unrelated picker copy above it. The shipped sample
config is sliced to its ``auxiliary:`` → ``title_generation:`` block only (a second legacy
``max_concurrency`` title block exists further down). The picker copy and the prompt registry are
imported, never scraped, so behaviour is what gets checked.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_CONFIGURATION = _ROOT / "website/docs/user-guide/configuration.md"
_PROMPT_SYSTEM = _ROOT / "docs/prompt-system.md"
_SAMPLE_CONFIG = _ROOT / "cli-config.yaml.example"

_TITLE_CHAPTER_START = "If you do not want Hermes to auto-generate titles"
_STREAM_ONLY = "### Stream-only endpoints"
_REQUEST_KEYS = ("title_system", "task_label", "title_card", "title_fork")


def _title_chapter() -> str:
    text = _CONFIGURATION.read_text(encoding="utf-8-sig")
    start = text.index(_TITLE_CHAPTER_START)
    return text[start:text.index(_STREAM_ONLY, start)]


def _normalized_title_chapter() -> str:
    return " ".join(_title_chapter().split()).lower()


def _sample_title_block() -> str:
    text = _SAMPLE_CONFIG.read_text(encoding="utf-8-sig")
    start = text.index("#   title_generation:")
    return text[start:text.index("#   session_search:", start)]


def _bullet(prose: str, marker: str) -> str:
    start = prose.index(marker)
    end = prose.find("\n- ", start + len(marker))
    return prose[start:] if end == -1 else prose[start:end]


def test_as5_title_chapter_names_the_workload_switch_and_the_route_override():
    """The slot is a workload switch + explicit route override, no longer "the" title model."""
    chapter = _normalized_title_chapter()
    assert "workload switch" in chapter
    assert "route override" in chapter
    assert "not the only source" in chapter


def test_as5_title_chapter_names_fork_and_the_fallback_plan_order():
    """Default `fork`; the plan is `tiny` → `commit_message` → `smol` → the session model, and a
    pinned route heads it only when pinned (an unpinned slot has no fixed position)."""
    chapter = _normalized_title_chapter()
    assert "`fork` (the default" in chapter
    assert "`tiny` → `commit_message` → `smol` → the current session model" in chapter
    assert "heads the candidate plan" in chapter
    assert "no fixed position" in chapter


def test_as5_title_chapter_states_the_marker_protocol_as_the_request_format():
    """The scored request-format statement is positive: `<title>…</title>` is the current format."""
    chapter = _normalized_title_chapter()
    assert "<title>…</title>" in chapter and "marker format" in chapter
    assert "<title/>" in chapter and "<title>none</title>" in chapter
    # Non-scoring negative guard: the retired inline JSON-schema format never comes back.
    assert "json_schema" not in chapter


def test_as5_sample_config_title_block_is_commented_and_documents_the_override():
    """``cli-config.yaml.example`` is copied verbatim on install, so the wording stays comments."""
    block = _sample_title_block()
    lowered = block.lower()
    assert "workload" in lowered and "override" in lowered
    lines = [line for line in block.splitlines() if line.strip()]
    assert lines[0].strip() == "#   title_generation:"
    assert all(line.strip().startswith("#") for line in lines)


def test_as5_picker_hint_names_the_switch_and_the_override():
    """The imported picker/dashboard copy is the asserted surface (no source-text read)."""
    from hermes_cli.aux_slots import AUX_TASK_META

    label, hint = AUX_TASK_META["title_generation"]
    lowered = hint.lower()
    assert label.strip()
    assert "workload" in lowered and "override" in lowered
    assert "backup" not in lowered


def test_as5_prompt_registry_and_lists_keep_the_named_prompts_request_scoped():
    """The four named prompts are request-scoped; the archived marker prompt stays unwired."""
    from agent.prompts.prompt_sections import load_sections

    sections = load_sections()
    for key in _REQUEST_KEYS:
        assert sections[key].scope == "request", key

    prose = _PROMPT_SYSTEM.read_text(encoding="utf-8-sig")
    request_list = _bullet(prose, "请求级模板")
    unwired_list = _bullet(prose, "未接线（只建文件、不登记 manifest）")
    for key in _REQUEST_KEYS:
        assert key in request_list, key
    assert "title-marker-instruction.md" in unwired_list
