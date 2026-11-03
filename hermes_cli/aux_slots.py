"""Canonical registry of built-in auxiliary task slots and their picker metadata.

A dependency-free leaf: ``agent/oneshot``, the plugin loaders, the CLI picker and the dashboard
all read the same tuples instead of keeping their own copies in sync. ``AUX_TASK_KEYS`` is
pinned to ``DEFAULT_CONFIG["auxiliary"]``'s task order by ``tests/hermes_cli/test_aux_slots.py``.
"""

# (task_key, display label, one-line hint). Picker rows, the dashboard's ``label``/``hint``, and
# the plugin reservation list all come from AUX_TASK_META.
AUX_TASK_META: dict[str, tuple[str, str]] = {
    "vision": ("Vision", "image/screenshot analysis"),
    # omp's fast/strong/planning/job-widget role vocabulary. These four ship as addressable
    # ``auxiliary.<task>`` routes so the Models hub and plugins can pin them (``call_llm(task=...)``
    # reads any task key), but no built-in workload consumes them yet: Hermes' ``/plan`` runs on the
    # main agent, and nothing writes background-job widget specs. Remove a key here once its
    # intended consumer either lands or is dropped.
    "smol": ("Smol (fast role)", "omp fast-lane role · no built-in workload yet"),
    "slow": ("Slow (thinking role)", "omp strong-lane role · no built-in workload yet"),
    "plan": ("Plan role", "omp planning role · /plan still runs on the main model"),
    "compression": ("Compression", "context summarization"),
    "approval": ("Approval", "smart command approval"),
    "review": ("Review", "/review reviewer subagent"),
    "side_question": ("Side question", "quick 'btw' side questions"),
    "mcp": ("MCP", "MCP tool reasoning"),
    "title_generation": ("Title generation", "session titles"),
    "commit_message": ("Commit message", "git commit message drafts"),
    "memory_query_rewrite": ("Memory query rewrite", "memory retrieval queries"),
    "tts_audio_tags": ("TTS audio tags", "Gemini TTS tag insertion"),
    "voice_chat": ("Voice chat", "spoken voice-mode replies"),
    "triage_specifier": ("Triage specifier", "kanban spec fleshing"),
    "kanban_decomposer": ("Kanban decomposer", "task decomposition"),
    "kanban_estimator": ("Kanban estimator", "task effort estimates"),
    "profile_describer": ("Profile describer", "auto profile descriptions"),
    "goal_judge": ("Goal judge", "/goal satisfaction checks"),
    "judge": ("Judge", "typed side-channel judgments"),
    "group_router": ("Group router", "who answers in auto-routed Group Chats"),
    "curator": ("Curator", "skill-usage review pass"),
    "monitor": ("Monitor", "important-mail scoring"),
    "widget": ("Widget role", "omp job-widget role · no built-in workload yet"),
    "background_review": ("Background review", "post-turn self-improvement"),
    "moa_reference": ("MoA reference", "mixture-of-agents advisors"),
    "moa_aggregator": ("MoA aggregator", "mixture-of-agents aggregator"),
}

#: Every ``auxiliary.<task>`` key, in ``DEFAULT_CONFIG["auxiliary"]`` task order.
AUX_TASK_KEYS: tuple[str, ...] = tuple(AUX_TASK_META)

#: Slots the model pickers must not offer: MoA owns its own preset UI and reset path, and its
#: depth is configured per slot inside ``moa.presets.<name>``.
AUX_PICKER_EXCLUDED: frozenset[str] = frozenset({"moa_reference", "moa_aggregator"})

#: omp roles Hermes backs outside ``auxiliary.<task>``: ``(label, hint, target)``. ``task`` is the
#: delegated-child route (``delegation:``), ``advisor`` the review model (``adviser:``); their
#: readers/writers live in ``hermes_cli/imported_roles.py``.
IMPORTED_ROLE_META: dict[str, tuple[str, str, str]] = {
    "task": ("Task", "delegated subagent route", "delegation"),
    "advisor": ("Advisor", "adviser review model", "adviser"),
}

#: Every imported role id, in picker order. omp ids, kept distinct from ``auxiliary`` task keys.
IMPORTED_ROLE_IDS: tuple[str, ...] = tuple(IMPORTED_ROLE_META)

#: The omp tag each row wears in the Models hub. Presentation only: one map, served by the gateway
#: so the shell never keeps its own copy. An unlisted role falls back to its uppercased label.
OMP_ROLE_TAGS: dict[str, str] = {
    "vision": "VISION",
    "smol": "SMOL",
    "slow": "SLOW",
    "plan": "PLAN",
    "widget": "WIDGET",
    "title_generation": "TINY",
    "background_review": "MEMORY",
    "goal_judge": "JUDGE",
    "judge": "JUDGE",
    "commit_message": "COMMIT",
    "task": "TASK",
    "advisor": "ADVISOR",
}


def omp_role_tag(role_id: str, label: str = "") -> str:
    """The row tag: a mapped omp tag, else the role's uppercased label (id when no label)."""
    tag = OMP_ROLE_TAGS.get(str(role_id))
    if tag:
        return tag
    return (str(label or "").strip() or str(role_id)).upper()


def aux_picker_keys() -> tuple[str, ...]:
    """Every auxiliary slot a model picker may show or assign (AUX_TASK_KEYS minus MoA)."""
    return tuple(key for key in AUX_TASK_KEYS if key not in AUX_PICKER_EXCLUDED)


def imported_role_target(role_id: str) -> str:
    """``delegation``/``adviser`` for an imported omp role, else ``""``."""
    meta = IMPORTED_ROLE_META.get(str(role_id))
    return meta[2] if meta else ""
