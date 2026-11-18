"""Sprint-5 acceptance: tool-card titles ride the live frames and the durable row (ADR-4 D13).

AS-2 and AS-4 reuse the ``TurnRunner`` harness that ``tests/gateway/test_local_viewer_events_turn.py``
already owns: a local-viewer turn whose ``_publish_execution`` appends ``(kind, payload)``. The
emitters under test are the real ``combined_tool_start_callback`` / ``combined_tool_complete_callback``
and the real ``gateway.local_viewer_events`` field builders; only the transport is captured.

The pending-card merge itself stays ``omp-ui``'s vitest territory (``#handleToolTitle`` →
``withEvalCellTitle``). What pytest owns is the gateway-side contract: the ordered frames, the
closed wire payloads, and the fact that titles never enter the model-visible tool result.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from gateway import local_viewer_events
from gateway.config import Platform
from gateway.run_turn_runner import TurnRunner
from tui_gateway import server
from tui_gateway.contracts.events import ToolStartPayload
from tui_gateway.contracts.registry import EVENTS

_EXECUTE_CELLS = {"cells": [
    {"code": "x = 1\nprint(x)"},
    {"code": "const y = 2", "language": "js", "title": "JS cell"},
]}


def _turn(platform, published):
    """The local-viewer TurnRunner harness (mirrors ``test_local_viewer_events_turn._turn``)."""
    turn = object.__new__(TurnRunner)
    turn._ctx = SimpleNamespace(
        source=SimpleNamespace(platform=platform, chat_id='chat'),
        mute_notification_reply=False, scheduled_heartbeat=False,
        streaming_tts_consumer_holder=[None], stream_consumer_holder=[None],
        interim_assistant_messages_enabled=False, user_config={},
        resolve_display_setting=lambda *a: None, _run_still_current=lambda: True,
        _voice_ack_guild=[None], _native_slack_task_cards=False, progress_mode='all',
        session_id='s', progress_queue=None, _live_status_adapter=None, _live_status_mode='off',
        log_queue=None,
    )
    turn._runner = SimpleNamespace(config=None, _delivery_adapter_for=lambda source: None)
    live = SimpleNamespace(tool_progress_mode=None)
    authority = SimpleNamespace(sessions={'s': live})
    turn._approval_owner = (authority, 's', 1)
    turn._publish_execution = lambda kind, payload: published.append((kind, payload)) or True
    turn._publish_api_tool = lambda *a, **k: None
    turn._schedule = lambda coro, _message: coro.close()
    return turn


def _frames(published, kind=None, tool_id=None):
    return [payload for name, payload in published
            if (kind is None or name == kind) and (tool_id is None or payload.get("tool_id") == tool_id)]


def test_as2_execute_code_start_carries_titles_and_repeats_them_per_cell():
    """AS-2: one ``tool.title`` per non-empty cell (with ``cell_index``), immediately after start,
    and no completion edge in between."""
    published = []
    turn = _turn(Platform.LOCAL, published)

    turn.combined_tool_start_callback('cell-call', 'execute_code', _EXECUTE_CELLS)
    start = _frames(published, 'tool.start', 'cell-call')[0]
    assert start['title'] == 'x = 1'
    assert start['cell_titles'] == ['x = 1', 'JS cell']

    assert _frames(published, 'tool.title', 'cell-call') == [
        {'tool_id': 'cell-call', 'name': 'execute_code', 'title': 'x = 1', 'cell_index': 0},
        {'tool_id': 'cell-call', 'name': 'execute_code', 'title': 'JS cell', 'cell_index': 1},
    ]
    assert [kind for kind, payload in published if payload.get('tool_id') == 'cell-call'] == [
        'tool.start', 'tool.title', 'tool.title']

    # A completion arriving afterwards does not retroactively sit between start and title.
    turn.combined_tool_complete_callback(
        'cell-call', 'execute_code', _EXECUTE_CELLS, json.dumps({'status': 'success', 'output': '1'}))
    assert [kind for kind, payload in published if payload.get('tool_id') == 'cell-call'] == [
        'tool.start', 'tool.title', 'tool.title', 'tool.complete']


def test_as2_ordinary_tool_emits_one_title_after_start():
    """AS-2: a single-title tool repeats ``tool.start.title`` in exactly one ``tool.title`` frame."""
    published = []
    turn = _turn(Platform.LOCAL, published)

    turn.combined_tool_start_callback('read-call', 'read_file', {'path': 'x'})
    start = _frames(published, 'tool.start', 'read-call')[0]
    assert start['title'] == 'x'
    assert 'cell_titles' not in start
    assert _frames(published, 'tool.title', 'read-call') == [
        {'tool_id': 'read-call', 'name': 'read_file', 'title': 'x'}]
    assert [kind for kind, payload in published if payload.get('tool_id') == 'read-call'] == [
        'tool.start', 'tool.title']


def test_as2_frames_fit_the_closed_wire_payloads_and_a_late_title_is_valid():
    """AS-2: both payloads accept the emitted fields and reject an unknown one; a later
    ``tool.title`` for a started call (``tool_id`` + ``cell_index``) is still a valid frame.

    ``tool_call_id``/``tool_name`` are gateway-internal aliases on the execution dict, not declared
    payload fields, so only the declared keys are handed to the model (the emitted wire payload is
    the same subset the clients read).
    """
    published = []
    turn = _turn(Platform.LOCAL, published)
    turn.combined_tool_start_callback('wire-call', 'execute_code', _EXECUTE_CELLS)

    start = _frames(published, 'tool.start', 'wire-call')[0]
    declared = {key: start[key] for key in ToolStartPayload.model_fields if key in start}
    validated = EVENTS['tool.start'].payload.model_validate(declared)
    assert validated.title == 'x = 1'
    assert validated.cell_titles == ['x = 1', 'JS cell']

    titles = _frames(published, 'tool.title', 'wire-call')
    for frame in titles:
        EVENTS['tool.title'].payload.model_validate(frame)

    with pytest.raises(ValidationError):
        EVENTS['tool.start'].payload.model_validate({**declared, 'unknown_field': 1})
    with pytest.raises(ValidationError):
        EVENTS['tool.title'].payload.model_validate({**titles[0], 'unknown_field': 1})

    # Late update: the same tool_id + cell_index merges into a pending card client-side.
    EVENTS['tool.title'].payload.model_validate(
        {'tool_id': 'wire-call', 'name': 'execute_code', 'title': 'Late', 'cell_index': 0})


def test_as2_titles_never_reach_the_model_visible_result():
    """AS-2: the tool result string carries no title, and the frame's title is display-only."""
    published = []
    turn = _turn(Platform.LOCAL, published)
    result = json.dumps({'status': 'success', 'output': '1'})

    turn.combined_tool_start_callback('leak-call', 'execute_code', _EXECUTE_CELLS)
    turn.combined_tool_complete_callback('leak-call', 'execute_code', _EXECUTE_CELLS, result)

    complete = _frames(published, 'tool.complete', 'leak-call')[0]
    assert complete['result'] == result
    assert 'x = 1' not in complete['result']
    assert 'card_title' not in complete['result']
    assert 'cell_titles' not in complete['result']


def test_as4_prepare_and_complete_agree_and_history_rebuilds_the_same_details():
    """AS-4: ``prepare_fields``' top level is what lands in ``display_metadata``, ``complete_fields``
    repeats it, and ``session.history``'s projection returns the same ``details``."""
    published = []
    turn = _turn(Platform.LOCAL, published)
    view = {'kind': 'markdown', 'text': '3'}
    args = {'code': 'print(1 + 2)'}
    result = json.dumps({'status': 'success', 'output': '3',
                         'display': {'views': [{'text': '3', 'view': view}], 'images': []}})

    metadata = local_viewer_events.prepare_fields(turn, 'as4', 'execute_code', args, result)
    fields = local_viewer_events.complete_fields(turn, 'as4', 'execute_code', args, result)

    assert metadata['display'] == fields['display'] == {'views': [{'text': '3', 'view': view}],
                                                        'images': []}
    assert metadata['card_title'] == fields['card_title']
    assert metadata['cell_titles'] == fields['cell_titles']
    assert 'display' not in metadata['tool_result_metadata']

    row = {'role': 'tool', 'tool_call_id': 'as4', 'tool_name': 'execute_code', 'content': result,
           'timestamp': 10.0, '_row_id': 7,
           'display_metadata': {key: metadata[key] for key in ('display', 'card_title', 'cell_titles')}}
    entry = server._history_to_messages([row], include_tool_content='full')[0]
    assert entry['details'] == {'display': metadata['display'],
                                'card_title': metadata['card_title'],
                                'cell_titles': metadata['cell_titles']}
    # Model-facing content is byte-identical with and without the durable title fields.
    plain = {**row, 'display_metadata': {}}
    assert entry['content'] == server._history_to_messages(
        [plain], include_tool_content='full')[0]['content']
    assert 'card_title' not in entry['content']
    assert metadata['card_title'] not in entry['content']


def test_as4_legacy_sidecar_is_the_fallback_and_stamp_keeps_concurrent_keys():
    """AS-4: pre-fix rows keep their titles under ``tool_result_metadata``; stamping merges the
    card fields into ``display_metadata`` without rewriting a concurrent writer's keys."""
    from agent.message_metadata import stamp_tool_result_metadata
    display = {'views': [{'text': '3', 'view': {'kind': 'markdown', 'text': '3'}}], 'images': []}
    legacy = {'role': 'tool', 'tool_call_id': 'legacy', 'tool_name': 'execute_code',
              'content': 'body', 'timestamp': 10.0, '_row_id': 8,
              'display_metadata': {'tool_result_metadata': {
                  'display': display, 'card_title': 'Cell', 'cell_titles': ['Cell']}}}
    assert server._history_to_messages([legacy])[0]['details'] == {
        'display': display, 'card_title': 'Cell', 'cell_titles': ['Cell']}

    message = {'display_metadata': {'reactions': [{'emoji': 'x'}]}}
    stamp_tool_result_metadata(
        message, {'display': display, 'card_title': 'Cell', 'cell_titles': ['Cell']}, is_error=False)
    assert message['display_metadata'] == {
        'reactions': [{'emoji': 'x'}], 'display': display,
        'card_title': 'Cell', 'cell_titles': ['Cell'],
        'tool_result_metadata': {'is_error': False}}
