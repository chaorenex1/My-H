"""Client panel inventory: what each client panel needs and how far the gateway has got.

The omp-ui port starts from this matrix (rendered to ``docs/omp-ui-panels.json`` by
``scripts/gen_panel_inventory.py``). Runtime reads THIS module rather than ``docs/`` because docs
are not packaged; ``tests/tui_gateway/panels/test_panel_coverage.py`` is the gate that keeps the
module, the rendered file, the contract tables and the authority's handler map identical.

Statuses (``game plan §2``):

* ``A`` — the session authority serves the method itself (``gateway/*`` handler and a
  ``CANONICAL_METHODS`` declaration).
* ``L`` — the legacy sidecar handler serves it in-process through the gateway's adapter
  (``tui_gateway/ws_legacy_fallback.py::dispatch_legacy``) with the caller's profile bound.
  Supported; the gateway keeps one implementation instead of re-porting it.
* ``M`` — no contract and no handler yet.

Panel statuses are the client-facing verdict: ``ready`` (everything served, nothing pending),
``client`` (no gateway surface), ``pending`` (contract/work still missing), ``partial`` (some
capability exists but a design gap remains, P6), ``unsupported`` (no backend; never a fake entry).

``change`` tags mirror the plan: ``P`` promotion (sidecar → authority), ``N`` new method,
``E`` contract extension.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

AUTHORITY = "A"
SIDECAR = "L"
MISSING = "M"

READY = "ready"
CLIENT = "client"
PENDING = "pending"
PARTIAL = "partial"
UNSUPPORTED = "unsupported"

SUPPORTED_STATUSES = frozenset({READY, CLIENT})


@dataclass(frozen=True)
class PanelMethod:
    name: str
    access: Literal["read", "write"]
    status: str
    change: tuple[str, ...] = ()


@dataclass(frozen=True)
class PanelEvent:
    name: str
    status: str


@dataclass(frozen=True)
class Panel:
    id: str
    title: str
    status: str
    methods: tuple[PanelMethod, ...] = ()
    events: tuple[PanelEvent, ...] = ()
    note: str = ""


def _m(name: str, access: Literal["read", "write"], status: str, *change: str) -> PanelMethod:
    return PanelMethod(name, access, status, change)


def _e(name: str, status: str) -> PanelEvent:
    return PanelEvent(name, status)


PANELS: tuple[Panel, ...] = (
    Panel("model_hub", "Model hub / picker / browser", PENDING,
          (_m("model.options", "read", AUTHORITY, "E"), _m("config.get", "read", AUTHORITY),
           _m("session.mutate", "write", AUTHORITY), _m("model.save_key", "write", SIDECAR),
           _m("model.disconnect", "write", SIDECAR)),
          note="model.options metadata (E) is pending"),
    Panel("session_selector", "Session selector", PENDING,
          (_m("session.list", "read", AUTHORITY, "E"), _m("session.active_list", "read", AUTHORITY),
           _m("session.most_recent", "read", SIDECAR),
           _m("session.mutate", "write", AUTHORITY)),
          note="session.list query/pin/preview (E) is pending"),
    Panel("tree_selector", "Tree selector", PENDING,
          (_m("session.history", "read", AUTHORITY, "E"), _m("session.info", "read", AUTHORITY),
           _m("session.mutate", "write", AUTHORITY, "E")),
          note="branch summarize/instructions payload (E) is pending"),
    Panel("rewind_selector", "Rewind selector", READY,
          (_m("session.history", "read", AUTHORITY), _m("session.mutate", "write", AUTHORITY))),
    Panel("copy_selector", "Copy selector", CLIENT),
    Panel("theme_selector", "Theme selector / skin", READY,
          (_m("skin.list", "read", AUTHORITY), _m("config.get", "read", AUTHORITY),
           _m("config.set", "write", AUTHORITY)),
          (_e("skin.changed", AUTHORITY),)),
    Panel("settings_menu", "Settings menu", READY,
          (_m("config.schema", "read", AUTHORITY), _m("config.get", "read", AUTHORITY),
           _m("config.set", "write", AUTHORITY))),
    Panel("thinking_effort", "Thinking / effort", READY,
          (_m("model.options", "read", AUTHORITY), _m("session.mutate", "write", AUTHORITY))),
    Panel("usage_dashboard", "Usage dashboard", READY,
          (_m("session.usage", "read", AUTHORITY), _m("session.stats", "read", AUTHORITY),
           _m("billing.state", "read", SIDECAR), _m("usage.bars", "read", SIDECAR))),
    Panel("jobs_panel", "Jobs panel / job tree", PENDING,
          (_m("process.list", "read", AUTHORITY, "E"), _m("spawn_tree.list", "read", SIDECAR),
           _m("process.stop", "write", AUTHORITY), _m("process.kill", "write", AUTHORITY)),
          (_e("process.started", MISSING), _e("process.updated", MISSING),
           _e("process.ended", MISSING), _e("spawn_tree.changed", MISSING)),
          note="process.list stats fields (E) and process events are pending"),
    Panel("agent_hub", "Agent Hub / dashboard / transcript", READY,
          (_m("subagent.list", "read", AUTHORITY), _m("subagent.tail", "read", AUTHORITY),
           _m("delegation.status", "read", SIDECAR), _m("subagent.steer", "write", AUTHORITY),
           _m("subagent.interrupt", "write", AUTHORITY), _m("delegation.pause", "write", AUTHORITY),
           _m("subagent.config", "write", AUTHORITY)),
          (_e("subagent.spawn_requested", AUTHORITY), _e("subagent.start", AUTHORITY),
           _e("subagent.thinking", AUTHORITY), _e("subagent.tool", AUTHORITY),
           _e("subagent.progress", AUTHORITY), _e("subagent.complete", AUTHORITY))),
    Panel("mcp_manager", "MCP manager / add wizard", READY,
          (_m("mcp.catalog", "read", SIDECAR), _m("mcp.servers.list", "read", SIDECAR),
           _m("mcp.servers.status", "read", SIDECAR),
           _m("mcp.servers.add", "write", SIDECAR), _m("mcp.servers.remove", "write", SIDECAR),
           _m("mcp.servers.set_api_key", "write", SIDECAR),
           _m("mcp.servers.oauth.start", "write", SIDECAR),
           _m("mcp.servers.oauth.poll", "write", SIDECAR),
           _m("mcp.servers.oauth.callback", "write", SIDECAR),
           _m("mcp.servers.oauth.cancel", "write", SIDECAR))),
    Panel("extensions_center", "Extensions center", PENDING,
          (_m("plugins.list", "read", AUTHORITY), _m("hooks.list", "read", AUTHORITY),
           _m("plugins.manage", "write", SIDECAR), _m("hooks.toggle", "write", AUTHORITY)),
          (_e("hooks.changed", MISSING),),
          note="hooks.changed is a P4 event; Hermes has no in-process extension registry"),
    Panel("git_tui", "Git TUI", READY,
          (_m("git.status", "read", AUTHORITY), _m("git.diff", "read", AUTHORITY),
           _m("git.log", "read", AUTHORITY), _m("git.branches", "read", AUTHORITY),
           _m("git.stage", "write", AUTHORITY), _m("git.unstage", "write", AUTHORITY),
           _m("git.commit", "write", AUTHORITY))),
    Panel("context_viewer", "Context viewer", READY,
          (_m("session.context_breakdown", "read", AUTHORITY), _m("session.history", "read", AUTHORITY))),
    Panel("hotkeys_sheet", "Hotkeys sheet", CLIENT),
    Panel("changelog", "Changelog", PENDING,
          (_m("runtime.describe", "read", AUTHORITY, "E"),),
          note="runtime.describe does not carry a version yet (E); the changelog content is client-side"),
    Panel("debug_selector", "Debug selector", READY,
          (_m("runtime.describe", "read", AUTHORITY), _m("session.info", "read", AUTHORITY))),
    Panel("btw_panel", "BTW panel / history", READY,
          (_m("btw.history", "read", AUTHORITY), _m("prompt.btw", "write", AUTHORITY)),
          (_e("btw.complete", AUTHORITY),)),
    Panel("omfg", "OMFG", UNSUPPORTED,
          note="no Hermes backend: the omp rule-forging workflow has no gateway equivalent (P6)"),
    Panel("plan_review", "Plan review / save / TOC", PENDING,
          (_m("session.plan.read", "read", AUTHORITY), _m("session.plan.save", "write", AUTHORITY),
           _m("session.plan.approve", "write", AUTHORITY)),
          (_e("session.plan.updated", MISSING),),
          note="session.plan.updated is a P4 event"),
    Panel("annotation_overlay", "Annotation overlay", UNSUPPORTED,
          note="no annotation backend; storage and protocol are undesigned (P6)"),
    Panel("pause_screen", "Pause screen", PARTIAL,
          (_m("session.control.read", "read", AUTHORITY), _m("delegation.pause", "write", AUTHORITY)),
          note="delegation.pause only gates new spawns; freezing in-flight turns needs a new "
               "primitive (P6)"),
    Panel("session_info_overlay", "Session info overlay", READY,
          (_m("session.info", "read", AUTHORITY), _m("session.stats", "read", AUTHORITY))),
    Panel("transcript_browser", "Transcript browser / outline", PENDING,
          (_m("session.history", "read", AUTHORITY, "E"),),
          note="session.history pagination/outline extension (E) is pending"),
    Panel("skill_picker", "Skill picker", READY,
          (_m("skills.manage", "write", SIDECAR), _m("commands.catalog", "read", AUTHORITY),
           _m("complete.slash", "read", AUTHORITY)),
          (_e("commands.changed", AUTHORITY),)),
    Panel("autoresearch", "Autoresearch / cleanse / ps-top / live", PARTIAL,
          (_m("process.list", "read", AUTHORITY, "E"), _m("voice.record", "read", SIDECAR),
           _m("voice.toggle", "write", SIDECAR), _m("voice.tts", "write", SIDECAR)),
          note="cleanse/autoresearch have no Hermes backend (P6); ps-top needs process.list stats (E)"),
    Panel("billing_subscription", "Billing / subscription", READY,
          (_m("billing.state", "read", SIDECAR), _m("billing.auto_reload", "read", SIDECAR),
           _m("billing.charge_status", "read", SIDECAR), _m("usage.bars", "read", SIDECAR),
           _m("subscription.state", "read", SIDECAR),
           _m("subscription.preview", "read", SIDECAR),
           _m("billing.charge", "write", SIDECAR),
           _m("billing.keep_going_on_credits", "write", SIDECAR),
           _m("billing.step_up", "write", SIDECAR),
           _m("subscription.change", "write", SIDECAR),
           _m("subscription.upgrade", "write", SIDECAR),
           _m("subscription.resume", "write", SIDECAR))),
    Panel("connectors", "Connectors", READY,
          (_m("connectors.list", "read", AUTHORITY), _m("connectors.catalog", "read", SIDECAR),
           _m("connectors.accounts", "read", SIDECAR), _m("connectors.tools", "read", SIDECAR),
           _m("connectors.policy.get", "read", SIDECAR),
           _m("connectors.operation.status", "read", AUTHORITY),
           _m("connectors.connect", "write", AUTHORITY),
           _m("connectors.accounts.remove", "write", SIDECAR),
           _m("connectors.accounts.rename", "write", SIDECAR))),
    Panel("vault", "Vault", READY,
          (_m("vault.list", "read", SIDECAR), _m("vault.sources", "read", SIDECAR),
           _m("vault.add", "write", SIDECAR), _m("vault.remove", "write", SIDECAR),
           _m("vault.unlock", "write", SIDECAR), _m("vault.lock", "write", SIDECAR),
           _m("vault.source.set", "write", SIDECAR))),
    Panel("profiles", "Profiles / multiplex", READY,
          (_m("profiles.list", "read", AUTHORITY), _m("profiles.describe", "read", SIDECAR),
           _m("profiles.create", "write", SIDECAR), _m("profiles.configure", "write", SIDECAR),
           _m("profiles.set_asset", "write", SIDECAR))),
    Panel("rollback_checkpoints", "Rollback / checkpoints", READY,
          (_m("rollback.list", "read", AUTHORITY), _m("rollback.diff", "read", AUTHORITY),
           _m("rollback.restore", "write", AUTHORITY))),
    Panel("pets", "Pets", READY,
          (_m("pet.gallery", "read", SIDECAR), _m("pet.info", "read", SIDECAR),
           _m("pet.cells", "read", SIDECAR), _m("pet.select", "write", SIDECAR),
           _m("pet.hatch", "write", SIDECAR), _m("pet.generate", "write", SIDECAR))),
    Panel("cron", "Cron", READY,
          (_m("cron.manage", "read", SIDECAR), _m("cron.status", "read", AUTHORITY),
           _m("cron.submit", "write", AUTHORITY), _m("cron.cancel", "write", AUTHORITY),
           _m("cron.manage", "write", SIDECAR))),
    Panel("auth_login", "Login / logout / provider auth", PENDING,
          (_m("model.options", "read", AUTHORITY, "E"), _m("config.get", "read", AUTHORITY),
           _m("model.save_key", "write", SIDECAR), _m("model.disconnect", "write", SIDECAR)),
          note="model.options metadata (E) is pending"),
    Panel("approvals", "Approvals / ask / sudo / secret", READY,
          (_m("approval.respond", "write", AUTHORITY), _m("clarify.respond", "write", AUTHORITY))),
    Panel("todo_hud", "Todo HUD", READY, events=(_e("todo.updated", AUTHORITY),)),
    Panel("goal_loop", "Goal / loop bars", READY,
          (_m("session.control.read", "read", AUTHORITY), _m("session.control", "write", AUTHORITY),
           _m("session.automation", "write", AUTHORITY)),
          (_e("session.control.update", AUTHORITY),)),
    Panel("i18n", "i18n", READY,
          (_m("i18n.languages", "read", SIDECAR), _m("i18n.catalog", "read", SIDECAR))),
    Panel("shared_metrics", "Shared metrics consent", READY,
          (_m("shared_metrics.status", "read", SIDECAR),
           _m("shared_metrics.set", "write", SIDECAR))),
    Panel("kanban_board", "Kanban (read-only board)", PENDING,
          (_m("kanban.run", "read", AUTHORITY), _m("kanban.board", "read", MISSING, "N")),
          (_e("kanban.changed", MISSING),),
          note="kanban.run is served; the kanban.board snapshot and kanban.changed are not (N)"),
    Panel("spawn_tree_replay", "Spawn-tree replay / diff", READY,
          (_m("spawn_tree.list", "read", SIDECAR), _m("spawn_tree.load", "read", SIDECAR),
           _m("spawn_tree.save", "write", SIDECAR))),
    Panel("moa", "MoA display", READY,
          (_m("config.set", "write", AUTHORITY),),
          (_e("moa.aggregating", AUTHORITY), _e("moa.phase", AUTHORITY),
           _e("moa.progress", AUTHORITY), _e("moa.reference", AUTHORITY))),
    Panel("collab", "Collab / QR / LIVE", UNSUPPORTED,
          note="omp collab has no Hermes protocol mapping; groups.* hosted rooms are desktop-only (P6)"),
    Panel("review_summary", "Review summary", READY, events=(_e("review.summary", AUTHORITY),)),
)

PANEL_IDS = tuple(panel.id for panel in PANELS)


def capabilities() -> dict[str, dict]:
    """``runtime.describe.panels``: panel id → what the client may use today.

    ``methods`` lists served methods: authority-owned ones and the sidecar's, which the gateway
    runs in-process with the caller's profile bound. A pending panel's ``M`` methods must never be
    called (they answer ``-32601``). Full planned lists stay in the inventory/docs.
    """
    return {
        panel.id: {
            "supported": panel.status in SUPPORTED_STATUSES,
            "status": panel.status,
            "methods": [method.name for method in panel.methods
                        if method.status in (AUTHORITY, SIDECAR)],
            "reason": panel.note or None,
        }
        for panel in PANELS
    }
