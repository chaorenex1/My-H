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
    }
