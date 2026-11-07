"""Small top-level config sections folded out of ``DEFAULT_CONFIG``.

Pure data only. Keeping them here offsets the consumer defaults added by ADR-17
P5 under the code-health file cap, without changing any value.
"""

from __future__ import annotations


def misc_defaults() -> dict:
    """Native hosted rooms, outbound network, and live doctor probes."""
    return {
        # Native hosted-room Discussion ceilings, shared by all members per discussion.
        # Positive integers only (no unlimited/0). Read from the root owner's config at service
        # startup for all hosted rooms; restart the owning service after changing them.
        "hosted_rooms": {
            "discussion": {
                "max_rounds": 3,
                "max_messages": 10,
            },
        },
        "network": {
            # Force IPv4. With broken/unreachable IPv6, Python tries AAAA first and hangs for the full
            # TCP timeout before falling back. True skips IPv6 entirely.
            "force_ipv4": False,
        },
        "doctor": {
            # Per-probe timeout (seconds) for `hermes doctor --live` real-call probes.
            "live_probe_timeout": 10,
        },
    }
