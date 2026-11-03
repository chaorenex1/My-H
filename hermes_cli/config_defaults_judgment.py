"""Pure-data factory for the top-level ``judgment`` section (ADR-11 D10)."""


def judgment_defaults() -> dict:
    """Typed side-channel judgment routing, cache/audit, and local backend knobs.

    Native routes are empty by default: enabling the wire does not send anything until a
    user or consumer adds a route. ``mode: auto`` therefore falls back to the
    ``auxiliary.judge`` text route only when a caller explicitly invokes a judgment.
    """
    return {
        # auto = native routes first, text only when no native route is available
        # (or text_after_native is on); native = native only; text = auxiliary.judge only.
        "mode": "auto",
        "text_after_native": False,
        "cache": True,
        "cache_max_states": 50_000,
        "audit": True,
        "audit_max_runs": 20_000,
        "eval_debug": False,
        "eval_max_bytes": 52_428_800,
        "timeout_ms": 10_000,
        "parse_retries": 2,
        "redact_state": True,
        "images": True,  # P4: image/box requests; false rejects them before any candidate
        "tools": {"enabled": "auto"},
        "local": {
            "autostart": False,
            "merge_prompt": True,
            "answer_max_tokens": 16,
            "reasoning_max_tokens": 1024,
        },
        # Explicit native routes: [{api, provider, model, base_url?, api_key_env?,
        # max_questions?, timeout_ms?}]. TypeSafe/Cloudflare are judgment transports,
        # not chat providers, so they never appear in the model picker.
        "native": [],
    }
