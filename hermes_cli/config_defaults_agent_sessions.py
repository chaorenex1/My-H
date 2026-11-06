"""Pure-data factory for resident named-agent session defaults (``agent_sessions:``).

A background ``agent`` run whose definition does not force ``blocking`` and that does not use a
worktree stays resident after it settles: the session keeps its live slot until the idle TTL
parks it, or a message wakes/revives it. Resident and wake runs occupy this pool's slots, never
``agent_loop``'s. See ADR-15 D7.
"""


def _agent_sessions(keep_alive: bool = True, idle_ttl_seconds: int = 1800,
                    max_runtime_seconds: int = 0, max_live: int = 8, max_parked: int = 64,
                    wake_queue: int = 32, max_wake_turns: int = 1) -> dict:
    """One ``agent_sessions`` config block (see ``DEFAULT_CONFIG['agent_sessions']``).

    ``idle_ttl_seconds`` 0 disables parking; ``max_runtime_seconds`` 0 disables the hard wall
    clock. ``keep_alive: false`` restores the one-shot behaviour (the agent closes when the run
    settles).
    """
    return {
        "keep_alive": keep_alive,
        "idle_ttl_seconds": idle_ttl_seconds,
        "max_runtime_seconds": max_runtime_seconds,
        "max_live": max_live,
        "max_parked": max_parked,
        "wake_queue": wake_queue,
        "max_wake_turns": max_wake_turns,
    }
