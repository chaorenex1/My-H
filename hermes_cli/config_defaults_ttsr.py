"""Pure-data factory for the top-level ``ttsr`` section (ADR-6 D7, ADR-11 D9)."""


def ttsr_defaults() -> dict:
    """Passive Time-Traveling Stream Rules for local viewers (omp-ui).

    ``question`` rules need a judgment backend: ``auto`` uses them only when a native
    judge route is configured, ``on`` also allows the text route, ``off`` never judges.
    """
    return {
        "enabled": True,
        "builtin_rules": True,
        "disabled_rules": [],
        "judge": "auto",  # auto | on | off
        # P3 stream rewind: rule-level interruptMode wins; this is the default for rules that
        # omit it. Shipped rules all set `never`, so the default does not change them.
        "interrupt_mode": "always",  # always | prose-only | tool-only | never
        "repeat_mode": "once",  # once | after-gap
        "repeat_gap": 10,  # completed turns before an after-gap rule may fire again
    }
