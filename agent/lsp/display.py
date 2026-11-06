"""``lsp.display``: which diagnostics surface shows, and where (ADR-9 D12).

Pure presentation policy. It never opens a scanner: ``lsp.enabled`` / ``code_intel.semgrep.enabled``
stay the on/off switches for the subsystem, and Semgrep is live-only (ADR-15), so ``hermes code
index`` never runs it and this block only gates the write path and explicit queries. Every consumer reads the policy BEFORE it branches on custom vs legacy sessions, so a CLI,
Desktop or messaging turn gets the same ``inline``/``severities`` filtering as omp-ui.

The defaults reproduce the pre-ADR-9 behaviour byte for byte: LSP ``both`` + ``error`` (the inline
verdict and the late verdict), Semgrep ``both`` + ``error``/``warning`` (its own scanner defaults).
A malformed block falls back per field and never fails a write.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("agent.lsp.display")

MODES = ("off", "inline", "late", "both")
DEFAULT_MODE = "both"

# Severity *names*. ``info``/``hint`` are deliberately unsupported: pi-tui's late-diagnostics
# component has no tone for them (ADR-9 遗留), so accepting them here would promise a rendering
# that does not exist.
SEVERITY_NAMES = ("error", "warning")
LSP_SEVERITY_IDS = {"error": 1, "warning": 2}

DEFAULT_LSP_SEVERITIES = ("error",)
DEFAULT_SEMGREP_SEVERITIES = ("error", "warning")

_WARNED: set[tuple[str, str]] = set()
_WARN_LOCK = threading.Lock()


@dataclass(frozen=True)
class SourceDisplay:
    """One diagnostic source's placement (``mode``) and severity filter."""

    mode: str = DEFAULT_MODE
    severities: tuple[str, ...] = DEFAULT_LSP_SEVERITIES

    @property
    def inline(self) -> bool:
        """A verdict inside the write budget goes into the tool result."""
        return self.mode in ("inline", "both")

    @property
    def late(self) -> bool:
        """A verdict that outlived the budget becomes a late diagnostics row."""
        return self.mode in ("late", "both")

    @property
    def off(self) -> bool:
        return self.mode == "off"

    @property
    def lsp_severity_ids(self) -> frozenset[int]:
        """LSP numeric severities (1=error, 2=warning) this source displays."""
        return frozenset(LSP_SEVERITY_IDS[name] for name in self.severities)


@dataclass(frozen=True)
class DisplayPolicy:
    """The resolved ``lsp.display`` block; the defaults are today's behaviour."""

    lsp: SourceDisplay = SourceDisplay()
    semgrep: SourceDisplay = SourceDisplay(severities=DEFAULT_SEMGREP_SEVERITIES)

    @classmethod
    def from_config(cls) -> DisplayPolicy:
        """Read ``lsp.display`` from the active profile; never raises."""
        raw = None
        try:
            from hermes_cli.config import load_config_readonly

            lsp_cfg = load_config_readonly().get("lsp")
            display = lsp_cfg.get("display") if isinstance(lsp_cfg, dict) else None
            raw = display if isinstance(display, dict) else None
        except Exception:  # a broken config must not fail a write
            logger.debug("lsp.display config read failed; using defaults", exc_info=True)
        lsp = _source(
            raw.get("lsp") if isinstance(raw, dict) else None, "lsp", DEFAULT_LSP_SEVERITIES)
        semgrep = _source(
            raw.get("semgrep") if isinstance(raw, dict) else None, "semgrep", DEFAULT_SEMGREP_SEVERITIES)
        return cls(lsp=lsp, semgrep=semgrep)


def display_policy() -> DisplayPolicy:
    """The policy for this write. Config reads are cached upstream by ``load_config_readonly``."""
    return DisplayPolicy.from_config()


def _source(raw: Any, name: str, defaults: tuple[str, ...]) -> SourceDisplay:
    """One source's resolved policy: unknown mode → warning + ``both``; bad severity → defaults.

    A present-but-invalid entry is a user error worth one warning; the write still goes through.
    """
    if not isinstance(raw, dict):
        return SourceDisplay(severities=defaults)
    mode = raw.get("mode")
    if mode is None:
        mode = DEFAULT_MODE
    elif not isinstance(mode, str) or mode.strip().lower() not in MODES:
        _warn_once(f"lsp.display.{name}.mode", str(mode))
        mode = DEFAULT_MODE
    else:
        mode = mode.strip().lower()
    severities = _severities(raw.get("severities"), name, defaults)
    return SourceDisplay(mode=mode, severities=severities)


def _severities(raw: Any, name: str, defaults: tuple[str, ...]) -> tuple[str, ...]:
    """Keep the valid ``error``/``warning`` entries, in canonical order; empty/invalid → defaults."""
    if raw is None:
        return defaults
    if not isinstance(raw, (list, tuple)):
        _warn_once(f"lsp.display.{name}.severities", str(raw))
        return defaults
    kept = set()
    for entry in raw:
        value = entry.strip().lower() if isinstance(entry, str) else None
        if value in SEVERITY_NAMES:
            kept.add(value)
        else:
            _warn_once(f"lsp.display.{name}.severities", str(entry))
    if not kept:
        return defaults
    return tuple(entry for entry in SEVERITY_NAMES if entry in kept)


def _warn_once(key: str, value: str) -> None:
    """Warn about one malformed setting once per process; a per-write log would spam."""
    with _WARN_LOCK:
        if (key, value) in _WARNED:
            return
        _WARNED.add((key, value))
    logger.warning("%s=%r is invalid; using the default instead", key, value)


__all__ = [
    "DEFAULT_LSP_SEVERITIES",
    "DEFAULT_SEMGREP_SEVERITIES",
    "LSP_SEVERITY_IDS",
    "MODES",
    "SEVERITY_NAMES",
    "DisplayPolicy",
    "SourceDisplay",
    "display_policy",
]
