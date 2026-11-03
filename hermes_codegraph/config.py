"""Configuration for the code-graph subsystem.

Defaults live in ``hermes_cli/config_defaults.py`` so ``config.yaml`` is the
only user-facing surface; there are no ``HERMES_CODEGRAPH_*`` environment
variables. Limits are immutable and passed down explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_MAX_FILE_BYTES = 1024 * 1024
DEFAULT_MAX_FILES = 20_000
DEFAULT_MAX_TOTAL_BYTES = 128 * 1024 * 1024
DEFAULT_MAX_SYMBOLS = 1_000_000
DEFAULT_PARSE_TIMEOUT_S = 10.0
DEFAULT_BATCH_SIZE = 100
SEMGREP_MODES = ("auto", "custom_dir")


@dataclass(frozen=True)
class CodeGraphLimits:
    enabled: bool = True
    update: bool = True
    auto_update: bool = True
    auto_install: bool = True
    semantic: bool = False
    semgrep_enabled: bool = False
    semgrep_mode: str = "auto"
    languages: tuple[str, ...] = ()
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_files: int = DEFAULT_MAX_FILES
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES
    max_symbols: int = DEFAULT_MAX_SYMBOLS
    exclude: tuple[str, ...] = ()
    parse_timeout_s: float = DEFAULT_PARSE_TIMEOUT_S
    batch_size: int = DEFAULT_BATCH_SIZE

    def __post_init__(self) -> None:
        for name in (
            "max_file_bytes",
            "max_files",
            "max_total_bytes",
            "max_symbols",
            "batch_size",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"CodeGraphLimits.{name} must be a positive integer")
        if not isinstance(self.parse_timeout_s, (int, float)) or self.parse_timeout_s <= 0:
            raise ValueError("CodeGraphLimits.parse_timeout_s must be positive")
        if self.semgrep_mode not in SEMGREP_MODES:
            raise ValueError(
                f"code_intel.semgrep.mode must be one of {SEMGREP_MODES}, "
                f"got {self.semgrep_mode!r}"
            )

    @classmethod
    def from_config(cls, config: dict[str, Any] | None = None) -> CodeGraphLimits:
        if config is None:
            try:
                from hermes_cli.config import load_config

                config = load_config()
            except Exception:  # health: allow BLE001 -- config is optional for embedded callers
                config = {}
        section = config.get("code_intel") if isinstance(config, dict) else None
        if not isinstance(section, dict):
            section = {}
        semgrep = section.get("semgrep")
        if not isinstance(semgrep, dict):
            semgrep = {}
        semgrep_mode = semgrep.get("mode", "auto")
        if not isinstance(semgrep_mode, str):
            raise ValueError(
                f"code_intel.semgrep.mode must be a string, got {semgrep_mode!r}"
            )
        if semgrep_mode not in SEMGREP_MODES:
            raise ValueError(
                f"code_intel.semgrep.mode must be one of {SEMGREP_MODES}, "
                f"got {semgrep_mode!r}"
            )

        def _int(name: str, default: int) -> int:
            value = section.get(name, default)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                return default
            return value

        languages = section.get("languages", [])
        if not isinstance(languages, (list, tuple)):
            languages = []
        exclude = section.get("exclude", [])
        if not isinstance(exclude, (list, tuple)):
            exclude = []
        return cls(
            enabled=bool(section.get("enabled", True)),
            update=bool(section.get("update", True)),
            auto_update=bool(section.get("auto_update", True)),
            auto_install=bool(section.get("auto_install", True)),
            semantic=bool(section.get("semantic", False)),
            semgrep_enabled=bool(semgrep.get("enabled", False)),
            semgrep_mode=semgrep_mode,
            languages=tuple(str(item) for item in languages),
            max_file_bytes=_int("max_file_bytes", DEFAULT_MAX_FILE_BYTES),
            max_files=_int("max_files", DEFAULT_MAX_FILES),
            max_total_bytes=_int("max_total_bytes", DEFAULT_MAX_TOTAL_BYTES),
            max_symbols=_int("max_symbols", DEFAULT_MAX_SYMBOLS),
            exclude=tuple(str(item) for item in exclude),
            batch_size=DEFAULT_BATCH_SIZE,
        )
