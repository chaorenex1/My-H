"""Panel coverage gate: ``docs/omp-ui-panels.json`` ↔ contract tables ↔ authority handlers.

P0 of ``docs/omp-ui-panels-gateway-plan.md``: the matrix exists, every declared method status is
true of the wire, and ``runtime.describe.panels`` advertises the same matrix. ``A`` must be served
by ``AuthorityConnection`` (never only by the legacy sidecar), ``L`` must be a declared method the
authority does not serve, ``M`` must not exist at all. Panels may be pending while P1–P4 land;
a panel that claims ``ready`` may not.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import panel_inventory
from gateway.panel_inventory import AUTHORITY, CLIENT, MISSING, PARTIAL, PENDING, READY, SIDECAR, UNSUPPORTED
from gateway.session_authority import LiveSession, SessionAuthority
from gateway.session_controls import AuthorityConnection
from gateway.session_group_controls import GROUP_METHODS
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch
from tui_gateway.contracts.registry import CANONICAL_METHODS, EVENTS, METHODS

REPO = Path(__file__).resolve().parents[3]
DOC = REPO / "docs" / "omp-ui-panels.json"
GEN = REPO / "scripts" / "gen_panel_inventory.py"
CHANGES = frozenset({"P", "N", "E"})


def _served(tmp_path) -> set[str]:
    """Every method the session authority serves (the same oracle the wire-contract test uses)."""
    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("s", source="test")
        epoch = begin_runtime_epoch(db, instance_id="current")
        authority = SessionAuthority(SimpleNamespace(_draining=False), profile_id="owned",
                                     instance_id="current", db=db, epoch=epoch)
        authority.sessions["s"] = LiveSession(None, "route")
        connection = AuthorityConnection(authority, SimpleNamespace(write=lambda frame: None),
                                         {"user_id": "owner"})
        return set(connection.handlers()) | set(GROUP_METHODS) | {"profiles.list"}


def _wire_status(name: str, served: set[str]) -> str:
    if name in served:
        return AUTHORITY
    if name in METHODS:
        return SIDECAR
    assert name not in CANONICAL_METHODS, f"{name!r} is canonical but no authority handler serves it"
    return MISSING


def _open_work(panel) -> bool:
    # ``A`` and ``L`` are both served (authority or the in-process sidecar adapter); only ``M`` or
    # an explicitly planned extension keeps a panel pending.
    return (any(method.status not in {AUTHORITY, SIDECAR} for method in panel.methods)
            or any(method.change for method in panel.methods)
            or any(event.status != AUTHORITY for event in panel.events))


@pytest.fixture(scope="module")
def renderer():
    spec = importlib.util.spec_from_file_location("gen_panel_inventory", GEN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_committed_matrix_is_current(renderer):
    """The on-disk matrix equals the module the runtime serves (edit the module, regenerate)."""
    assert DOC.exists(), "docs/omp-ui-panels.json is missing: run scripts/gen_panel_inventory.py"
    assert DOC.read_text(encoding="utf-8") == renderer.render(), (
        "stale docs/omp-ui-panels.json: run scripts/gen_panel_inventory.py")


def test_matrix_shape():
    """Ids are unique, statuses/access/change tags are known, and the JSON matches the module."""
    document = json.loads(DOC.read_text(encoding="utf-8"))
    ids = [panel["id"] for panel in document["panels"]]
    assert ids == list(panel_inventory.PANEL_IDS)
    assert len(ids) == len(set(ids)), "duplicate panel id"
    for panel in document["panels"]:
        assert panel["status"] in {READY, CLIENT, PENDING, PARTIAL, UNSUPPORTED}, panel["id"]
        assert panel["supported"] == (panel["status"] in panel_inventory.SUPPORTED_STATUSES)
        for method in panel["methods"]:
            assert method["status"] in {AUTHORITY, SIDECAR, MISSING}, (panel["id"], method)
            assert method["access"] in {"read", "write"}, (panel["id"], method)
            assert set(method["change"]) <= CHANGES, (panel["id"], method)
        for event in panel["events"]:
            assert event["status"] in {AUTHORITY, MISSING}, (panel["id"], event)
        if panel["status"] in {PENDING, PARTIAL, UNSUPPORTED}:
            assert panel["note"], f"{panel['id']} states no reason"
        else:
            assert not panel["note"], f"{panel['id']} is {panel['status']} but carries a note"


def test_every_method_status_is_true_of_the_wire(tmp_path):
    """A = authority handler, L = declared sidecar-only, M = absent; every declared name exists."""
    served = _served(tmp_path)
    problems = []
    for panel in panel_inventory.PANELS:
        for method in panel.methods:
            actual = _wire_status(method.name, served)
            if method.status != actual:
                problems.append(f"{panel.id}: {method.name} says {method.status}, wire says {actual}")
    assert not problems, "\n".join(problems)


def test_sidecar_methods_are_executable_through_the_adapter():
    """L is not merely "declared": the in-process adapter must actually run it for this grant.

    ``legacy_fallback_allowed`` is the real gate — process-global verbs and the session/prompt/
    message namespaces the authority owns answer ``-32601`` even though ``METHODS`` declares them,
    so advertising one as adapter-served would be a lie the client only discovers at call time.
    """
    from gateway.runtime_bootstrap import _PURPOSE_CAPABILITIES
    from tui_gateway.ws_legacy_fallback import legacy_fallback_allowed

    actor = SimpleNamespace(capabilities=_PURPOSE_CAPABILITIES["interactive"])
    problems = [
        f"{panel.id}: {method.name} is L but the adapter refuses it"
        for panel in panel_inventory.PANELS
        for method in panel.methods
        if method.status == SIDECAR and not legacy_fallback_allowed(actor, method.name)
    ]
    assert not problems, "\n".join(problems)


def test_every_event_status_is_declared():
    problems = [
        f"{panel.id}: {event.name} says {event.status}, wire says "
        f"{AUTHORITY if event.name in EVENTS else MISSING}"
        for panel in panel_inventory.PANELS
        for event in panel.events
        if event.status != (AUTHORITY if event.name in EVENTS else MISSING)
    ]
    assert not problems, "\n".join(problems)


def test_panel_status_agrees_with_its_surface():
    """ready implies fully served; client has no gateway surface; pending implies open work."""
    problems = []
    for panel in panel_inventory.PANELS:
        if panel.status == READY:
            if not (panel.methods or panel.events):
                problems.append(f"{panel.id}: ready panel has no surface")
            if _open_work(panel):
                problems.append(f"{panel.id}: ready panel has unserved methods, pending changes or events")
        elif panel.status == CLIENT:
            if panel.methods or panel.events:
                problems.append(f"{panel.id}: client panel must not name gateway methods/events")
        elif panel.status == PENDING:
            if not _open_work(panel):
                problems.append(f"{panel.id}: pending panel has no unserved method or pending change")
        elif panel.status == PARTIAL:
            if not (panel.methods or panel.events):
                problems.append(f"{panel.id}: partial panel has no surface at all")
        elif panel.status == UNSUPPORTED:
            if any(method.status == AUTHORITY for method in panel.methods):
                problems.append(f"{panel.id}: unsupported panel lists an authority-served method")
            if any(event.status == AUTHORITY for event in panel.events):
                problems.append(f"{panel.id}: unsupported panel lists a declared event")
    assert not problems, "\n".join(problems)


@pytest.mark.asyncio
async def test_runtime_describe_advertises_the_matrix(tmp_path, monkeypatch):
    """The client reads one reply and knows which panels it may drive."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("s", source="test")
        epoch = begin_runtime_epoch(db, instance_id="current")
        authority = SessionAuthority(SimpleNamespace(_draining=False), profile_id=str(tmp_path),
                                     instance_id="current", db=db, epoch=epoch)
        authority.sessions["s"] = LiveSession(None, "route")
        connection = AuthorityConnection(authority, SimpleNamespace(write=lambda frame: None),
                                         {"user_id": "owner"})
        try:
            reply = await connection.dispatch({"id": 0, "method": "runtime.describe", "params": {}})
            assert "result" in reply, reply
            CANONICAL_METHODS["runtime.describe"].result.model_validate(reply["result"])
            assert reply["result"]["panels"] == panel_inventory.capabilities()
            assert set(reply["result"]["panels"]) == set(panel_inventory.PANEL_IDS)
            # ADR-10 D6/P4: the same reply carries the transcript block table the client consumes.
            assert reply["result"]["transcript_blocks"] == panel_inventory.transcript_capabilities()
            assert {"advisor", "skill_invocation", "tool_execution", "notice"} <= set(
                reply["result"]["transcript_blocks"])
        finally:
            await connection.close()
            authority.db.close()


def test_ready_panels_name_only_served_methods(tmp_path):
    """A ready panel advertises exactly its served methods: authority-owned or adapter-served."""
    served = _served(tmp_path)
    advertised = panel_inventory.capabilities()
    for panel in panel_inventory.PANELS:
        expected = [method.name for method in panel.methods
                    if method.status in {AUTHORITY, SIDECAR}]
        assert advertised[panel.id]["methods"] == expected, panel.id
        # A = an authority handler; L = a declared sidecar handler the adapter executes.
        assert all(name in served or name in METHODS for name in expected), panel.id
