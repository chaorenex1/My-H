"""``session.history`` (``methods_session`` split).

Moved verbatim from ``tui_gateway/methods_session.py`` (file-line ratchet): the body closes over
server.py globals through ``method_ctx.bind_module`` exactly as before, and registration runs from
the parent's ``register()`` via ``HandlerRegistry``. The paging helper itself lives in
``tui_gateway/history_paging.py`` so the authority emitter uses the same projection.
"""

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method


@method("session.history")
def _(rid, params: dict) -> dict:
    session, err = _sess_nowait(params, rid)
    if err:
        return err
    limit = params.get("limit")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        return _err(rid, -32602, "invalid params: limit must be a positive integer")
    limit = min(limit, 500) if limit is not None else None
    include_tool_content = params.get("include_tool_content", "none")
    if include_tool_content not in ("none", "text", "full"):
        return _err(rid, -32602, "invalid params: include_tool_content must be none, text or full")
    include_outline = params.get("include_outline") is True
    since, cursor = params.get("since"), params.get("cursor")
    after = since if since is not None else cursor
    history = list(session.get("history", []))
    # Address the session the live agent writes to, not session_key: a compression rotation moves the
    # tip mid-session, and include_ancestors walks parent pointers, so a stale parent materializes
    # root..parent and NEVER the continuation — a reconnect in that window renders a transcript missing
    # every turn since the rotation (#123545).
    row_session = _submit_row_target_key(session)
    if row_session:
        with _session_db(session) as db:
            if db is not None:
                # include_row_ids: the durable row id is how clients address a persisted turn (reactions,
                # truncation targets); _history_to_messages forwards it.
                with contextlib.suppress(Exception):
                    # The projection in _history_to_messages only forwards row_id when the row carries a
                    # stamp, so an unstamped read here silently strips the one durable address clients can
                    # use. See #87059.
                    history = db.get_messages_as_conversation(
                        row_session, include_ancestors=True, include_row_ids=True)
    messages = _history_to_messages(history, profile_home=session.get("profile_home"),
                                    include_tool_content=include_tool_content,
                                    running=bool(session.get("running")))
    from tui_gateway.history_paging import history_page
    page, entries, next_cursor, outline = history_page(messages, after=after, limit=limit,
                                                       include_outline=include_outline)
    leaf_id = max((m["row_id"] for m in messages if isinstance(m.get("row_id"), int)), default=None)
    return _ok(rid, {"count": len(page), "messages": page, "entries": entries, "leaf_id": leaf_id,
                     "next_cursor": next_cursor, "total_messages": len(messages),
                     **({"outline": outline} if outline is not None else {})})


def register(server) -> None:
    bind_module(globals(), server, skip=("_",))
