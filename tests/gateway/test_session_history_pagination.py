"""``session.history`` paging: ``since`` / ``cursor`` / ``limit`` over the display projection.

The full read keeps ``messages`` (the projected slice) and adds one ``HistoryEntry`` per projected
message carrying a durable ``row_id`` (tool rows ride the message slice), plus ``leaf_id``,
``next_cursor`` and ``total_messages``. ``cursor`` aliases ``since``; ``since`` wins when both are set.
"""
import pytest

from tests.gateway.test_session_mutation_model_busy import _owner


def _target(authority, ref):
    """The transcript the handler reads (a local session's receipt entry)."""
    from gateway.session_local_recovery import transcript_target
    return transcript_target(authority, ref)


def _seed(db, sid):
    """One turn with a tool exchange: five rows, four carrying a durable display id."""
    return (db.append_message(sid, 'user', 'first question'),
            db.append_message(sid, 'assistant', 'first answer'),
            db.append_message(sid, 'tool', 'tool output', tool_name='read', tool_call_id='call-1'),
            db.append_message(sid, 'assistant', 'after the tool'),
            db.append_message(sid, 'user', 'last question'))


async def _history(owner, sid, **params):
    reply = await owner.dispatch({'id': 1, 'method': 'session.history',
                                  'params': {'session_id': sid, **params}})
    assert 'result' in reply, reply
    return reply['result']


@pytest.mark.asyncio
async def test_session_history_entries_cover_every_row_with_a_durable_id(tmp_path, monkeypatch):
    async def handle(event):
        return 'ok'
    store, db, authority, owner, ref = _owner(tmp_path, monkeypatch, handle)
    try:
        ids = _seed(db, _target(authority, ref))
        result = await _history(owner, ref.session_id)
        assert [entry['entry_id'] for entry in result['entries']] == [ids[0], ids[1], ids[3], ids[4]]
        assert [entry['role'] for entry in result['entries']] == ['user', 'assistant', 'assistant', 'user']
        assert [entry['text'] for entry in result['entries']] == [
            'first question', 'first answer', 'after the tool', 'last question']
        assert all(isinstance(entry['timestamp'], float) and entry['timestamp'] > 0
                   for entry in result['entries'])
        assert all(entry['display_kind'] is None for entry in result['entries'])
        assert result['leaf_id'] == ids[4]
        assert result['total_messages'] == result['count'] == len(result['messages']) == 5
        assert result['next_cursor'] is None
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_session_history_pages_by_row_id_and_reports_the_next_cursor(tmp_path, monkeypatch):
    async def handle(event):
        return 'ok'
    store, db, authority, owner, ref = _owner(tmp_path, monkeypatch, handle)
    try:
        ids = _seed(db, _target(authority, ref))
        first = await _history(owner, ref.session_id, limit=2)
        assert [entry['entry_id'] for entry in first['entries']] == [ids[0], ids[1]]
        # The page's messages end before the next entry, so the turn's tool row travels with it.
        assert [message['role'] for message in first['messages']] == ['user', 'assistant', 'tool']
        assert first['next_cursor'] == ids[1] and first['leaf_id'] == ids[4]
        assert first['total_messages'] == 5 and first['count'] == 3

        second = await _history(owner, ref.session_id, since=first['next_cursor'])
        assert [entry['entry_id'] for entry in second['entries']] == [ids[3], ids[4]]
        assert [message['role'] for message in second['messages']] == ['assistant', 'user']
        assert 'last question' in [message.get('text') for message in second['messages']]
        assert second['next_cursor'] is None
        assert second['count'] == len(second['messages']) == 2
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_session_history_cursor_aliases_since_and_since_wins(tmp_path, monkeypatch):
    async def handle(event):
        return 'ok'
    store, db, authority, owner, ref = _owner(tmp_path, monkeypatch, handle)
    try:
        ids = _seed(db, _target(authority, ref))
        alias = await _history(owner, ref.session_id, cursor=ids[0], limit=1)
        assert [entry['entry_id'] for entry in alias['entries']] == [ids[1]]
        assert alias['next_cursor'] == ids[1]

        both = await _history(owner, ref.session_id, since=ids[1], cursor=ids[0])
        assert [entry['entry_id'] for entry in both['entries']] == [ids[3], ids[4]]
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_session_history_refuses_a_non_positive_limit(tmp_path, monkeypatch):
    async def handle(event):
        return 'ok'
    store, db, authority, owner, ref = _owner(tmp_path, monkeypatch, handle)
    try:
        _seed(db, _target(authority, ref))
        for limit in (0, -1):
            reply = await owner.dispatch({'id': 1, 'method': 'session.history',
                                          'params': {'session_id': ref.session_id, 'limit': limit}})
            assert reply['error']['data'] == {'reason': 'invalid_params', 'field': 'limit'}, reply
        clamped = await _history(owner, ref.session_id, limit=501)
        assert len(clamped['entries']) == 4 and clamped['next_cursor'] is None
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_session_history_of_an_empty_session_has_no_entries(tmp_path, monkeypatch):
    async def handle(event):
        return 'ok'
    store, _db, _authority, owner, ref = _owner(tmp_path, monkeypatch, handle)
    try:
        result = await _history(owner, ref.session_id, limit=10)
        assert result == {'count': 0, 'messages': [], 'entries': [], 'leaf_id': None,
                          'next_cursor': None, 'total_messages': 0}
    finally:
        await owner.close()
        store.close_all_db_handles()
