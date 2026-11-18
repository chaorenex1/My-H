#!/usr/bin/env python3
"""Regenerate ``hermes_cli/data/omp_appearance_defaults.json`` from omp-ui's setting registry.

omp owns the Appearance page's defaults; Hermes owns the profile's overrides under
``omp.appearance``. This script copies the current defaults across the boundary so
``DEFAULT_CONFIG``, ``config.schema`` and ``omp.appearance.get`` can render every setting before a
profile has saved one. Run it after any change to ``omp-ui/src/modes/settings.ts`` and the
domains it imports:

    python scripts/gen_omp_appearance_defaults.py

The committed copy is gated by ``omp-ui/src/modes/rpc/gateway-appearance.test.ts``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DUMP = ROOT / "omp-ui" / "scripts" / "dump-appearance-defaults.ts"
OUT = ROOT / "hermes_cli" / "data" / "omp_appearance_defaults.json"


def bun_bin() -> str:
    override = shutil.which("bun")
    if override:
        return override
    home = Path.home()
    for candidate in (home / ".bun" / "bin" / "bun.exe", home / ".bun" / "bin" / "bun"):
        if candidate.is_file():
            return str(candidate)
    raise SystemExit("bun not found: install https://bun.sh or run this with bun on PATH")


def main() -> int:
    completed = subprocess.run(
        [bun_bin(), str(DUMP)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        sys.stderr.write(completed.stderr or "dump-appearance-defaults failed")
        return completed.returncode
    values = json.loads(completed.stdout)
    if not isinstance(values, dict) or not values:
        raise SystemExit("dump-appearance-defaults returned no settings")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(values, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(values)} settings)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
