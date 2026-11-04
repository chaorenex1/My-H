"""Thin TTSR hooks for provider stream emitters (ADR-6 D10).

Kept out of ``chat_completion_helpers`` so that file does not grow past its ratchet; importing
this module pulls in nothing heavier than ``getattr`` until a coordinator is actually installed.
"""

from __future__ import annotations

from typing import Any, Callable


def stream_delta(agent: Any, source: str, text: str) -> None:
    """Feed one raw provider delta to the turn's coordinator, when one is installed."""
    coordinator = getattr(agent, "ttsr_coordinator", None)
    if coordinator is not None:
        coordinator.check_stream_delta(agent, source, text)


def wrap_stream_delta(
    agent: Any, source: str, fire: Callable[[str], Any],
    first: Callable[[], Any] | None = None,
) -> Callable[[str], None]:
    """``fire`` with TTSR matching first; ``first`` runs before either, once per stream."""
    def _on(value: str) -> None:
        if first is not None:
            first()
        stream_delta(agent, source, value)
        fire(value)

    return _on
