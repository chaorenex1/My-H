"""Pure-data factory for auxiliary model configuration defaults (DEFAULT_CONFIG["auxiliary"])."""


def _aux(timeout, *, reasoning_effort=True, **extra):
    """Standard auxiliary-task model block (see :func:`auxiliary_defaults`).

    reasoning_effort=False omits that key (MoA blocks configure depth per slot);
    ``extra`` keys are appended after the standard ones. ``fallback_chain`` is a fresh list per
    block so a mutated default can never leak into a sibling slot.
    """
    d = {"provider": "auto", "model": "", "base_url": "", "api_key": "", "timeout": timeout,
         "fallback_chain": [], "extra_body": {}}
    if reasoning_effort:
        d["reasoning_effort"] = ""
    d.update(extra)
    return d


def auxiliary_defaults() -> dict:
    """The ``auxiliary`` section: provider/model per side task.

    provider "auto" = auto-detect; empty model = provider's default aux model; all tasks fall
    back to openrouter:google/gemini-3-flash-preview when the configured provider is unavailable.
    extra_body is forwarded verbatim as request body fields for that task, e.g. OpenRouter
    routing prefs / Pareto Code floor:

        auxiliary:
          compression:
            extra_body:
              provider: {order: [anthropic, google], sort: throughput}  # or price | latency
              plugins: [{id: pareto-router, min_coding_score: 0.5}]

    Each task is independent — main-agent provider_routing and openrouter.min_coding_score
    do NOT propagate to aux calls by design. The per-task key order here is the canonical
    auxiliary-slot order (``hermes_cli.aux_slots.AUX_TASK_KEYS``).
    """
    return {
        # Same-provider retries for a transient blip (reset/timeout/5xx/408) on ANY aux call before
        # falling back; clamped [0,6]. Matters for pinned calls (MoA advisors) where provider
        # fallback is not meaningful recovery.
        "transient_retries": 2,
        # When true, the auto-chain's OpenRouter step is skipped unless the fallback model ends in
        # ":free" — a PAID lane is never used for background aux traffic even with
        # OPENROUTER_API_KEY set.
        "free_only": False,
        # Override the auto-chain's OpenRouter fallback model (default google/gemini-3.6-flash,
        # PAID). Pair e.g. "nvidia/nemotron-3-ultra-550b-a55b:free" with free_only: true. A one-time
        # WARNING is logged whenever a non-":free" model is engaged.
        "openrouter_model": "",
        # Model-keyed rescue chains, the equivalent of omp's retry.fallbackChains:
        # {provider/model | provider/*: [{provider, model, base_url?}, ...]}. Tried before the
        # per-task fallback_chain — exact key (split on the first "/", so model ids keep their own
        # slashes) first, then the provider wildcard. Routes use the same grammar (provider+model
        # required, base_url optional, max 8). {} = off.
        "fallback_chains": {},
        # Endpoints that reject NON-streaming chat (HTTP 400): aux calls are sent with stream=True
        # and aggregated. Case-insensitive URL substrings; some vendor endpoints are always
        # stream-only.
        "stream_only_base_urls": [],
        # Per-task blocks share one shape (_aux): provider "auto" = inherit the main model; base_url
        # overrides provider; api_key falls back to OPENAI_API_KEY; reasoning_effort:
        # none|minimal|low|medium|high|xhigh|max|ultra ("" = provider default); extra_body =
        # OpenAI-compatible request fields. fallback_chain = ordered routes
        # [{provider, model, base_url?}] tried after the model-keyed auxiliary.fallback_chains and
        # before the built-in chain (max 8, managed from the dashboard's Models page).
        # Vision: download_timeout = image HTTP download (s).
        "vision": _aux(120, download_timeout=30),
        # omp role vocabulary, addressable but not yet consumed by a built-in workload (see
        # hermes_cli/aux_slots.py): a plugin or future feature can pin them, the Models hub can
        # assign them, and ``auto`` keeps them on the main model so nothing changes by default.
        # ``tiny`` is the judgment chain's cheap lane (ADR-17 default tail); ``default`` is the
        # main-model fallback at the end of that chain.
        "tiny": {**_aux(30), "prefer_fast_model": True},  # judgment's cheap lane
        "smol": _aux(60),    # omp "fast lane"
        "slow": _aux(180),   # omp "strong/thinking lane"
        "plan": _aux(600),   # omp planning role (/plan itself still runs on the main agent)
        # web_extract and session_search no longer use an aux LLM; leftover blocks in user config
        # are ignored. Compression: raise timeout for local models. no_progress_timeout:
        # seconds a streamed call goes without a substantive chunk before it fails fast into
        # retry/fallback; None = built-in 60s default. Independent of "timeout" (the overall request
        # budget) — raising "timeout" alone does not widen this window. See #108104.
        "compression": _aux(120, no_progress_timeout=None),
        "approval": _aux(30),   # classifier — a fast/cheap model is recommended
        # /review reviewer: a full subagent on the async delegation rail, credentials resolved like
        # delegation.provider pins. "auto" + "" = main agent's model. api_mode forces transport:
        # chat_completions | anthropic_messages | codex_responses.
        "review": {"provider": "auto", "model": "", "base_url": "", "api_key": "", "api_mode": ""},
        # Quick 'btw' side questions: a rendered transcript through agent/oneshot.run_oneshot.
        "side_question": _aux(180),
        "mcp": _aux(30),
        # prefer_fast_model opts in to the provider fast tier; auto otherwise = main model.
        "title_generation": {
            "enabled": True,
            "model_upgrade_enabled": True,  # False = keep the instant derived title, never call a model
            # session_search no longer uses an aux LLM (#27590); leftover auxiliary.session_search is ignored.
            "provider": "auto",
            "model": "",
            "prefer_fast_model": False,
            "base_url": "",
            "api_key": "",
            "timeout": 30,
            "extra_body": {},
            "reasoning_effort": "",
            "language": "",
            # Judge whether the title still fits every N operator prompts (ADR-17 P5).
            # 0 = off: drift checks cost a paid judgment per interval.
            "refresh_after_prompts": 0,
        },
        # One-shot git commit message drafts (agent/oneshot.py PROMPT_TEMPLATES["commit_message"]).
        "commit_message": _aux(60),
        "memory_query_rewrite": _aux(8, reasoning_effort=False),
        "tts_audio_tags": _aux(30),
        "voice_chat": {**_aux(120), "reasoning_effort": "none"},  # agent/voice_turn_route.py; off = lowest valid
        # Kanban: triage_specifier expands a Triage one-liner into a spec (cheap model OK);
        # kanban_decomposer emits a JSON graph of child tasks (more tokens).
        "triage_specifier": _aux(120),
        "kanban_decomposer": _aux(180),
        # Effort/points estimates on a kanban card; a short JSON call.
        "kanban_estimator": _aux(60, reasoning_effort=False),
        "profile_describer": _aux(60),   # 1-2 sentence profile blurb; short, cheap
        "goal_judge": _aux(60),          # /goal satisfaction + contract drafting; JSON calls
        # Typed side-channel judgments (ADR-11). Text/one-hot fallback only; native routes are
        # configured separately under the top-level `judgment:` section.
        # The text budget and reasoning override live under ``judgment.text`` (ADR-17 D3/D8).
        "judge": {**_aux(30), "images": False},
        "group_router": _aux(20),        # Group Chat routing "auto": who answers; fast/cheap model OK
        # Main-model fallback at the end of the judgment chain (ADR-17 D4): provider "auto"
        # and an empty model mean the session's main model.
        "default": _aux(60),
        # Curator skill-usage review can take minutes on reasoning models (umbrellas over hundreds
        # of skills); route cheaper via `hermes model` → auxiliary → Curator.
        "curator": _aux(600),
        "monitor": _aux(60),   # important-mail 0-10 scorer; high-volume, small model fine
        # Post-turn self-improvement fork (save memory / patch skill). "auto" = main model replaying
        # the full conversation (warm cache); other models replay a compact digest (~3-5x cheaper).
        # enabled=false skips auto spawns (/refine still works). An explicit max_input_tokens caps
        # the SUM of replayed input tokens over the review loop (iterations capped at 16); the loop
        # stops before crossing it. When unset, the runtime derives a budget from the active model
        # context window. <= 0 = unlimited.
        # reasoning_effort is IGNORED while the review stays on the main model: the fork inherits the
        # conversation's reasoning config verbatim so its request bytes keep the parent's warm
        # prompt-cache prefix (#30532). Set provider/model below to route the review to another model
        # if you want a different effort level; a one-time warning says so when the key is set.
        "widget": _aux(120),  # omp job-widget role
        "background_review": {"enabled": True, **_aux(120)},
        # No reasoning_effort on MoA blocks by design — configured PER SLOT in the preset
        # (moa.presets.<name>.reference_models[].reasoning_effort / aggregator.reasoning_effort).
        "moa_reference": _aux(900, reasoning_effort=False),
        "moa_aggregator": _aux(900, reasoning_effort=False),
    }
