"""``session.stats`` counters: token / context shapes plus the stored session's role counts, cost,
tool calls and routed models (``session_model_usage``), each read guarded on its own."""
from collections import deque
from types import SimpleNamespace

import pytest

from tests.gateway.test_session_mutation_model_busy import _owner


def _agent():
    compressor = SimpleNamespace(last_prompt_tokens=900, context_length=128000, _config_context_length=128000,
                                 compression_count=2)
    return SimpleNamespace(
        model='fake-model', provider='custom', session_id='sid',
        session_input_tokens=100, session_output_tokens=40, session_cache_read_tokens=10,
        session_cache_write_tokens=5, session_prompt_tokens=115, session_completion_tokens=40,
        session_total_tokens=155, session_reasoning_tokens=3, session_api_calls=2,
        context_compressor=compressor, _api_latency_history=deque([1.0, 3.0]),
        _api_output_history=deque([10, 30]), get_rate_limit_state=lambda: None,
        _session_messages=[], _turn_base_usage_anchor=None)


async def _stats(owner, sid):
    reply = await owner.dispatch({'id': 1, 'method': 'session.stats', 'params': {'session_id': sid}})
    assert 'result' in reply, reply
    return reply['result']


@pytest.mark.asyncio
async def test_session_stats_carries_stored_role_counts_cost_and_routes(tmp_path, monkeypatch):
    async def handle(event):
        return 'ok'
    store, db, authority, owner, ref = _owner(tmp_path, monkeypatch, handle)
    authority.runner._cached_agent_for = lambda route: _agent()
    sid = ref.session_id
    try:
        db.append_message(sid, 'user', 'hello')
        db.append_message(sid, 'assistant', 'hi', tool_calls=[
            {'id': 'call-1', 'type': 'function', 'function': {'name': 'read', 'arguments': '{}'}}])
        db.append_message(sid, 'tool', 'contents', tool_name='read', tool_call_id='call-1')
        db.append_message(sid, 'assistant', 'read it')
        db.update_token_counts(sid, model='alpha', input_tokens=100, output_tokens=40,
                               api_call_count=2, estimated_cost_usd=0.5)
        db.update_token_counts(sid, model='beta', api_call_count=1, estimated_cost_usd=0.25,
                               actual_cost_usd=0.3)

        result = await _stats(owner, sid)
        assert result['session_id'] == sid
        assert result['tokens'] == {'input': 100, 'output': 40, 'reasoning': 3, 'cache_read': 10,
                                    'cache_write': 5, 'total': 155}
        assert result['context_usage'] == {'tokens': 900, 'context_window': 128000, 'percent': 1}
        assert (result['user_messages'], result['assistant_messages'], result['tool_results']) == (1, 2, 1)
        assert result['total_messages'] == 4
        assert result['tool_calls'] == 1
        assert result['cost_usd'] == 0.3  # actual cost wins over the estimate
        assert result['routed_models'] == {'alpha': 2, 'beta': 1}
        from tui_gateway.contracts.registry import CANONICAL_METHODS
        CANONICAL_METHODS['session.stats'].result.model_validate(result)
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_session_stats_of_an_empty_session_reports_zero_counts(tmp_path, monkeypatch):
    async def handle(event):
        return 'ok'
    store, _db, _authority, owner, ref = _owner(tmp_path, monkeypatch, handle)
    try:
        result = await _stats(owner, ref.session_id)
        assert result['session_id'] == ref.session_id
        assert result['tokens'] == {'input': 0, 'output': 0, 'reasoning': 0, 'cache_read': 0,
                                    'cache_write': 0, 'total': 0}
        assert result['context_usage'] == {'tokens': 0, 'context_window': None, 'percent': 0}
        assert (result['user_messages'], result['assistant_messages'], result['tool_results']) == (0, 0, 0)
        assert result['total_messages'] == 0 and result['tool_calls'] == 0
        assert result['cost_usd'] is None and result['routed_models'] == {}
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_session_stats_survives_a_failing_db_counter(tmp_path, monkeypatch):
    async def handle(event):
        return 'ok'
    store, db, _authority, owner, ref = _owner(tmp_path, monkeypatch, handle)
    sid = ref.session_id

    def explode(session_id):
        raise RuntimeError('counter read failed')
    try:
        db.append_message(sid, 'user', 'hello')
        db.update_token_counts(sid, model='alpha', api_call_count=1, estimated_cost_usd=0.5)
        monkeypatch.setattr(db, 'message_role_counts', explode)

        result = await _stats(owner, sid)
        assert result['user_messages'] is None and result['total_messages'] is None
        assert result['tool_results'] is None
        # The other guarded reads still land.
        assert result['tool_calls'] == 0 and result['cost_usd'] == 0.5
        assert result['routed_models'] == {'alpha': 1}
    finally:
        await owner.close()
        store.close_all_db_handles()
