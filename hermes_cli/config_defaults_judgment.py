"""Pure-data factory for the top-level ``judgment`` section (ADR-17 D4/D8)."""

from hermes_cli.judgment_catalog import DEFAULT_CHAIN, DEFAULT_MODELS


def judgment_defaults() -> dict:
    """Typed side-channel judgment routing, cache/audit, and local backend knobs.

    The chain references ``models`` (a judgment catalog, not the chat model picker).
    Native routes never send anything without their own credential; ``mode: auto``
    falls through to the prompted ``tiny → smol → default`` roles only when no native
    candidate resolves. ``judgment.native`` (ADR-11) is still accepted and folded into
    the catalog and the front of the chain.
    """
    return {
        # auto = chain order (native-only tail); native = native candidates only;
        # text = tiny → smol → default (plus an explicitly pinned auxiliary.judge);
        # off = no candidate.
        "mode": "auto",
        "text_after_native": False,
        "chain": list(DEFAULT_CHAIN),
        # [{id, kind: native|local|online, api, provider, model, base_url?, api_key_env?,
        # request_model_id?, max_questions?, image_input?, boxes?, timeout_ms?}].
        # TypeSafe/Cloudflare are judgment transports, not chat providers, so they never
        # appear in the model picker.
        "models": [dict(model) for model in DEFAULT_MODELS],
        # 0 disables a cap. Enforced per judgment call and per ambient session.
        "cost_gate": {"max_usd_per_call": 0.05, "max_usd_per_session": 0.50},
        # Native answers only; `usage` is the native billed-attempt ledger (ADR-17 D5).
        "cache": {"enabled": True, "max_states": 50_000, "usage": True},
        "audit": {"enabled": True, "max_runs": 20_000},
        "eval_debug": False,
        "eval_max_bytes": 52_428_800,
        "timeout_ms": 10_000,
        "redact_state": True,
        "images": {
            "enabled": True,  # false rejects image/box requests before any candidate
            # Path assets are confined to cwd / $HERMES_HOME; remote URLs are never fetched.
            "assets": {"allow_local": True, "allow_remote": False, "max_bytes": 16_777_216},
        },
        "tools": {"enabled": "auto"},
        "text": {
            "parse_retries": 2,
            "max_tokens": 4096,
            "disable_reasoning": True,
        },
        "local": {
            "autostart": False,
            "merge_prompt": True,
            "parse_retries": 0,
            "answer_max_tokens": 16,
            "reasoning_max_tokens": 1024,
        },
        # Legacy explicit native routes; folded into ``models``/``chain`` when set.
        "native": [],
    }
