"""Client panel inventory: what each client panel needs and how far the gateway has got.

The omp-ui port starts from this matrix (rendered to ``docs/omp-ui-panels.json`` by
``scripts/gen_panel_inventory.py``). Runtime reads THIS module rather than ``docs/`` because docs
are not packaged; ``tests/tui_gateway/panels/test_panel_coverage.py`` is the gate that keeps the
module, the rendered file, the contract tables and the authority's handler map identical.

Statuses (``game plan §2``):

* ``A`` — the session authority serves the method (``gateway/*`` handler, ``METHODS`` or
  ``CANONICAL_METHODS`` declaration).
* ``L`` — the contract exists but only the legacy sidecar handler serves it; the client must not
  rely on the fallback.
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
           _m("session.mutate", "write", AUTHORITY), _m("model.save_key", "write", SIDECAR, "P"),
           _m("model.disconnect", "write", SIDECAR, "P")),
          note="model.options metadata (E) and the credential methods' promotion (P) are pending"),
    Panel("session_selector", "Session selector", PENDING,
          (_m("session.list", "read", AUTHORITY, "E"), _m("session.active_list", "read", AUTHORITY),
           _m("session.most_recent", "read", SIDECAR, "P"),
           _m("session.mutate", "write", AUTHORITY)),
          note="session.list query/pin/preview (E) and session.most_recent promotion (P) are pending"),
    Panel("tree_selector", "Tree selector", PENDING,
          (_m("session.history", "read", AUTHORITY, "E"), _m("session.info", "read", AUTHORITY),
           _m("session.mutate", "write", AUTHORITY, "E")),
          note="branch summarize/instructions payload (E) is pending"),
    Panel("rewind_selector", "Rewind selector", READY,
          (_m("session.history", "read", AUTHORITY), _m("session.mutate", "write", AUTHORITY))),
    Panel("copy_selector", "Copy selector", CLIENT),
    Panel("theme_selector", "Theme selector / skin", PENDING,
          (_m("skin.list", "read", MISSING, "N"), _m("config.get", "read", AUTHORITY),
           _m("config.set", "write", AUTHORITY)),
          note="skin.list does not exist yet (N); config.get/set are served"),
    Panel("settings_menu", "Settings menu", PENDING,
          (_m("config.schema", "read", MISSING, "N"), _m("config.get", "read", AUTHORITY),
           _m("config.set", "write", AUTHORITY)),
          note="config.schema does not exist yet (N)"),
    Panel("thinking_effort", "Thinking / effort", READY,
          (_m("model.options", "read", AUTHORITY), _m("session.mutate", "write", AUTHORITY))),
    Panel("usage_dashboard", "Usage dashboard", PENDING,
          (_m("session.usage", "read", SIDECAR, "P"), _m("session.stats", "read", AUTHORITY),
           _m("billing.state", "read", SIDECAR, "P"), _m("usage.bars", "read", SIDECAR, "P")),
          note="session.usage, billing.state and usage.bars are sidecar-only (P)"),
    Panel("jobs_panel", "Jobs panel / job tree", PENDING,
          (_m("process.list", "read", AUTHORITY, "E"), _m("spawn_tree.list", "read", SIDECAR, "P"),
           _m("process.stop", "write", AUTHORITY), _m("process.kill", "write", AUTHORITY)),
          (_e("process.started", MISSING), _e("process.updated", MISSING),
           _e("process.ended", MISSING), _e("spawn_tree.changed", MISSING)),
          note="process.list stats fields (E), spawn_tree promotion (P) and process events are pending"),
    Panel("agent_hub", "Agent Hub / dashboard / transcript", PENDING,
          (_m("subagent.list", "read", AUTHORITY), _m("subagent.tail", "read", AUTHORITY),
           _m("delegation.status", "read", SIDECAR, "P"), _m("subagent.steer", "write", AUTHORITY),
           _m("subagent.interrupt", "write", AUTHORITY), _m("delegation.pause", "write", AUTHORITY),
           _m("subagent.config", "write", MISSING, "N")),
          (_e("subagent.spawn_requested", AUTHORITY), _e("subagent.start", AUTHORITY),
           _e("subagent.thinking", AUTHORITY), _e("subagent.tool", AUTHORITY),
           _e("subagent.progress", AUTHORITY), _e("subagent.complete", AUTHORITY)),
          note="subagent.config (N) and delegation.status promotion (P) are pending"),
    Panel("mcp_manager", "MCP manager / add wizard", PENDING,
          (_m("mcp.catalog", "read", SIDECAR, "P"), _m("mcp.servers.list", "read", SIDECAR, "P"),
           _m("mcp.servers.status", "read", SIDECAR, "P"),
           _m("mcp.servers.add", "write", SIDECAR, "P"), _m("mcp.servers.remove", "write", SIDECAR, "P"),
           _m("mcp.servers.set_api_key", "write", SIDECAR, "P"),
           _m("mcp.servers.oauth.start", "write", SIDECAR, "P"),
           _m("mcp.servers.oauth.poll", "write", SIDECAR, "P"),
           _m("mcp.servers.oauth.callback", "write", SIDECAR, "P"),
           _m("mcp.servers.oauth.cancel", "write", SIDECAR, "P")),
          note="the whole mcp.* family is sidecar-only (P)"),
    Panel("extensions_center", "Extensions center", PENDING,
          (_m("plugins.list", "read", AUTHORITY), _m("hooks.list", "read", MISSING, "N"),
           _m("plugins.manage", "write", SIDECAR, "P"), _m("hooks.toggle", "write", MISSING, "N")),
          (_e("hooks.changed", MISSING),),
          note="hooks.list/toggle (N) and plugins.manage promotion (P) are pending; "
               "Hermes has no in-process extension registry"),
    Panel("git_tui", "Git TUI", PENDING,
          (_m("git.status", "read", MISSING, "N"), _m("git.diff", "read", MISSING, "N"),
           _m("git.log", "read", MISSING, "N"), _m("git.branches", "read", MISSING, "N"),
           _m("git.stage", "write", MISSING, "N"), _m("git.unstage", "write", MISSING, "N"),
           _m("git.commit", "write", MISSING, "N")),
          note="the git.* contract is not implemented yet (N)"),
    Panel("context_viewer", "Context viewer", READY,
          (_m("session.context_breakdown", "read", AUTHORITY), _m("session.history", "read", AUTHORITY))),
    Panel("hotkeys_sheet", "Hotkeys sheet", CLIENT),
    Panel("changelog", "Changelog", PENDING,
          (_m("runtime.describe", "read", AUTHORITY, "E"),),
          note="runtime.describe does not carry a version yet (E); the changelog content is client-side"),
    Panel("debug_selector", "Debug selector", READY,
          (_m("runtime.describe", "read", AUTHORITY), _m("session.info", "read", AUTHORITY))),
    Panel("btw_panel", "BTW panel / history", PENDING,
          (_m("btw.history", "read", MISSING, "N"), _m("prompt.btw", "write", AUTHORITY)),
          (_e("btw.complete", AUTHORITY),),
          note="btw.history does not exist yet (N)"),
    Panel("omfg", "OMFG", UNSUPPORTED,
          note="no Hermes backend: the omp rule-forging workflow has no gateway equivalent (P6)"),
    Panel("plan_review", "Plan review / save / TOC", PENDING,
          (_m("session.plan.read", "read", MISSING, "N"), _m("session.plan.save", "write", MISSING, "N"),
           _m("session.plan.approve", "write", MISSING, "N")),
          (_e("session.plan.updated", MISSING),),
          note="session.plan.* does not exist yet (N)"),
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
    Panel("skill_picker", "Skill picker", PENDING,
          (_m("skills.manage", "write", SIDECAR, "P"), _m("commands.catalog", "read", AUTHORITY),
           _m("complete.slash", "read", AUTHORITY)),
          note="skills.manage promotion (P) is pending"),
    Panel("autoresearch", "Autoresearch / cleanse / ps-top / live", PARTIAL,
          (_m("process.list", "read", AUTHORITY, "E"), _m("voice.record", "read", SIDECAR, "P"),
           _m("voice.toggle", "write", SIDECAR, "P"), _m("voice.tts", "write", SIDECAR, "P")),
          note="cleanse/autoresearch have no Hermes backend (P6); ps-top needs process.list stats (E)"),
    Panel("billing_subscription", "Billing / subscription", PENDING,
          (_m("billing.state", "read", SIDECAR, "P"), _m("billing.auto_reload", "read", SIDECAR, "P"),
           _m("billing.charge_status", "read", SIDECAR, "P"), _m("usage.bars", "read", SIDECAR, "P"),
           _m("subscription.state", "read", SIDECAR, "P"),
           _m("subscription.preview", "read", SIDECAR, "P"),
           _m("billing.charge", "write", SIDECAR, "P"),
           _m("billing.keep_going_on_credits", "write", SIDECAR, "P"),
           _m("billing.step_up", "write", SIDECAR, "P"),
           _m("subscription.change", "write", SIDECAR, "P"),
           _m("subscription.upgrade", "write", SIDECAR, "P"),
           _m("subscription.resume", "write", SIDECAR, "P")),
          note="billing, subscription and usage.bars are sidecar-only (P)"),
    Panel("connectors", "Connectors", PENDING,
          (_m("connectors.list", "read", SIDECAR, "P"), _m("connectors.catalog", "read", SIDECAR, "P"),
           _m("connectors.accounts", "read", SIDECAR, "P"), _m("connectors.tools", "read", SIDECAR, "P"),
           _m("connectors.policy.get", "read", SIDECAR, "P"),
           _m("connectors.operation.status", "read", AUTHORITY),
           _m("connectors.connect", "write", AUTHORITY),
           _m("connectors.accounts.remove", "write", SIDECAR, "P"),
           _m("connectors.accounts.rename", "write", SIDECAR, "P")),
          note="the account/policy methods are sidecar-only (P)"),
    Panel("vault", "Vault", PENDING,
          (_m("vault.list", "read", SIDECAR, "P"), _m("vault.sources", "read", SIDECAR, "P"),
           _m("vault.add", "write", SIDECAR, "P"), _m("vault.remove", "write", SIDECAR, "P"),
           _m("vault.unlock", "write", SIDECAR, "P"), _m("vault.lock", "write", SIDECAR, "P"),
           _m("vault.source.set", "write", SIDECAR, "P")),
          note="vault.* is sidecar-only (P)"),
    Panel("profiles", "Profiles / multiplex", PENDING,
          (_m("profiles.list", "read", AUTHORITY), _m("profiles.describe", "read", SIDECAR, "P"),
           _m("profiles.create", "write", SIDECAR, "P"), _m("profiles.configure", "write", SIDECAR, "P"),
           _m("profiles.set_asset", "write", SIDECAR, "P")),
          note="profiles.* except profiles.list is sidecar-only (P)"),
    Panel("rollback_checkpoints", "Rollback / checkpoints", READY,
          (_m("rollback.list", "read", AUTHORITY), _m("rollback.diff", "read", AUTHORITY),
           _m("rollback.restore", "write", AUTHORITY))),
    Panel("pets", "Pets", PENDING,
          (_m("pet.gallery", "read", SIDECAR, "P"), _m("pet.info", "read", SIDECAR, "P"),
           _m("pet.cells", "read", SIDECAR, "P"), _m("pet.select", "write", SIDECAR, "P"),
           _m("pet.hatch", "write", SIDECAR, "P"), _m("pet.generate", "write", SIDECAR, "P")),
          note="pet.* is sidecar-only (P)"),
    Panel("cron", "Cron", PENDING,
          (_m("cron.manage", "read", SIDECAR, "P"), _m("cron.status", "read", AUTHORITY),
           _m("cron.submit", "write", AUTHORITY), _m("cron.cancel", "write", AUTHORITY),
           _m("cron.manage", "write", SIDECAR, "P")),
          note="cron.manage promotion (P) is pending"),
    Panel("auth_login", "Login / logout / provider auth", PENDING,
          (_m("model.options", "read", AUTHORITY, "E"), _m("config.get", "read", AUTHORITY),
           _m("model.save_key", "write", SIDECAR, "P"), _m("model.disconnect", "write", SIDECAR, "P")),
          note="model.save_key/model.disconnect promotion (P) is pending"),
    Panel("approvals", "Approvals / ask / sudo / secret", READY,
          (_m("approval.respond", "write", AUTHORITY), _m("clarify.respond", "write", AUTHORITY))),
    Panel("todo_hud", "Todo HUD", READY, events=(_e("todo.updated", AUTHORITY),)),
    Panel("goal_loop", "Goal / loop bars", READY,
          (_m("session.control.read", "read", AUTHORITY), _m("session.control", "write", AUTHORITY),
           _m("session.automation", "write", AUTHORITY)),
          (_e("session.control.update", AUTHORITY),)),
    Panel("i18n", "i18n", PENDING,
          (_m("i18n.languages", "read", SIDECAR, "P"), _m("i18n.catalog", "read", SIDECAR, "P")),
          note="i18n.* is sidecar-only (P)"),
    Panel("shared_metrics", "Shared metrics consent", PENDING,
          (_m("shared_metrics.status", "read", SIDECAR, "P"),
           _m("shared_metrics.set", "write", SIDECAR, "P")),
          note="shared_metrics.* is sidecar-only (P)"),
    Panel("kanban_board", "Kanban (read-only board)", PENDING,
          (_m("kanban.run", "read", AUTHORITY), _m("kanban.board", "read", MISSING, "N")),
          (_e("kanban.changed", MISSING),),
          note="kanban.run is served; the kanban.board snapshot and kanban.changed are not (N)"),
    Panel("spawn_tree_replay", "Spawn-tree replay / diff", PENDING,
          (_m("spawn_tree.list", "read", SIDECAR, "P"), _m("spawn_tree.load", "read", SIDECAR, "P"),
           _m("spawn_tree.save", "read", SIDECAR, "P")),
          note="spawn_tree.* is sidecar-only (P)"),
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

    ``methods`` lists authority-served methods only: a pending panel's unserved methods must never
    be called (they answer ``-32601``). Full planned lists stay in the inventory/docs.
    """
    return {
        panel.id: {
            "supported": panel.status in SUPPORTED_STATUSES,
            "status": panel.status,
            "methods": [method.name for method in panel.methods if method.status == AUTHORITY],
            "reason": panel.note or None,
        }
        for panel in PANELS
    }
