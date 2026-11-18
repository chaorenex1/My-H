"""Internal ``$HERMES_HOME`` locations listed under ``§ Runtime``.

The renderer builds this list itself: callers cannot inject extra directories into the
prompt, and every row uses the ``$HERMES_HOME`` token rather than a host path so a
profile's real home never leaks into the prompt (or, from there, into output).
"""

from __future__ import annotations

# Canonical layout, in the order the model should read it. Directories may be created on
# first use, so existence is not a condition for listing them.
_INTERNAL_LOCATIONS: tuple[tuple[str, str], ...] = (
    # The home itself is named by the section intro, not repeated as a row.
    ("skills", "$HERMES_HOME/skills"),
    ("agents", "$HERMES_HOME/agents (also $HERMES_HOME/.agents and $HERMES_HOME/skills/<skill>/agents)"),
    ("rules", "$HERMES_HOME/rules"),
    ("memories", "$HERMES_HOME/memories"),
    ("logs", "$HERMES_HOME/logs"),
    ("sessions", "$HERMES_HOME/sessions"),
    ("cache", "$HERMES_HOME/cache (scratch: $HERMES_HOME/cache/scratch)"),
)


def internal_url_rows() -> list[str]:
    """``"<label>: <path>"`` rows for the important ``$HERMES_HOME`` directories."""
    return [f"{label}: {path}" for label, path in _INTERNAL_LOCATIONS]


__all__ = ["internal_url_rows"]
