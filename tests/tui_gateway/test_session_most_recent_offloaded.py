"""``session.most_recent`` must not pick a cold-mirrored (offloaded) session (ADR-16 D8)."""

from __future__ import annotations

from tui_gateway import server


def test_session_most_recent_skips_offloaded(monkeypatch):
    class _DB:
        def list_sessions_rich(self, *, source=None, limit=200, order_by_last_active=False, compact_rows=False):
            return [
                {"id": "offloaded-1", "source": "cli", "title": "cold", "started_at": 100, "offloaded": 1},
                {"id": "live-1", "source": "cli", "title": "warm", "started_at": 99, "offloaded": 0},
            ]

    monkeypatch.setattr(server, "_get_db", lambda: _DB())
    resp = server.handle_request({"id": "1", "method": "session.most_recent", "params": {}})
    assert resp["result"]["session_id"] == "live-1"
