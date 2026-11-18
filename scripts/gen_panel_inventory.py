#!/usr/bin/env -S bash -c 'exec "$BASH" "$(dirname "$0")/run-in-hermes-env" python3 "$0" "$@"'
"""Render ``gateway/panel_inventory.py`` into ``docs/omp-ui-panels.json``.

The module is canonical because the runtime reads it (``runtime.describe.panels``); this file is
the reviewable on-disk matrix the UI port and ``tests/tui_gateway/panels/test_panel_coverage.py``
read. ``--check`` fails when the committed file is stale, mirroring
``scripts/gen_gateway_contracts.py``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway.panel_inventory import PANELS, SUPPORTED_STATUSES

OUT = ROOT / "docs" / "omp-ui-panels.json"

STATUS_LEGEND = {
    "A": "authority-served: a gateway/* handler serves it and the contract is declared",
    "L": "legacy sidecar only: the contract exists, but the authority does not serve it yet",
    "M": "missing: no contract and no handler",
}

PANEL_LEGEND = {
    "ready": "every method/event is authority-served and nothing is pending",
    "client": "no gateway surface; the client owns the whole panel",
    "pending": "work remains (promotion/new method/contract extension); see note",
    "partial": "some capability exists, but a design gap remains (P6); see note",
    "unsupported": "no backend or no design; the client must not render a live entry",
}


def render() -> str:
    """The exact committed JSON text."""
    document = {
        "schema_version": 1,
        "generated_by": "scripts/gen_panel_inventory.py from gateway/panel_inventory.py — DO NOT EDIT",
        "status_legend": STATUS_LEGEND,
        "panel_legend": PANEL_LEGEND,
        "panels": [
            {
                "id": panel.id,
                "title": panel.title,
                "status": panel.status,
                "supported": panel.status in SUPPORTED_STATUSES,
                "note": panel.note,
                "methods": [
                    {
                        "name": method.name,
                        "access": method.access,
                        "status": method.status,
                        "change": list(method.change),
                    }
                    for method in panel.methods
                ],
                "events": [{"name": event.name, "status": event.status} for event in panel.events],
            }
            for panel in PANELS
        ],
    }
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail when the committed file is stale")
    args = parser.parse_args()
    text = render()
    if args.check:
        current = OUT.read_text(encoding="utf-8-sig") if OUT.exists() else None
        if current != text:
            print(f"stale {OUT.relative_to(ROOT)}: run scripts/gen_panel_inventory.py")
            return 1
        print(f"up to date: {OUT.relative_to(ROOT)}")
        return 0
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
