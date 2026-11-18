"""``run_oneshot`` bills the slot named by an explicit ``task`` or by the template.

The Desktop asks for a commit message with ``template="commit_message"`` and no ``task``; before
this, run_oneshot's hard default (``title_generation``) silently billed the session-title slot, so
``auxiliary.commit_message`` was unreachable. Calling ``llm.oneshot`` with no template still lands
on ``title_generation`` — that is the cheap backend one-shots default to.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent import oneshot
from agent.oneshot import run_oneshot

_DIFF = {"diff": "diff --git a/x b/x\n+new"}


def _response(text: str = "feat: thing") -> MagicMock:
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = text
    resp.choices[0].message.reasoning = None
    resp.choices[0].message.reasoning_content = None
    resp.choices[0].message.reasoning_details = None
    return resp


@pytest.mark.parametrize(("kwargs", "expected"), [
    # The template name IS an auxiliary slot: route there without an explicit task.
    ({"template": "commit_message", "variables": _DIFF}, "commit_message"),
    # An explicit task always wins over the template name.
    ({"template": "commit_message", "variables": _DIFF, "task": "kanban_estimator"}, "kanban_estimator"),
    # A blank task is "unset", not a slot named "".
    ({"template": "commit_message", "variables": _DIFF, "task": "   "}, "commit_message"),
    # Ad-hoc prompts keep the cheap default backend.
    ({"instructions": "be brief", "user_input": "hi"}, "title_generation"),
    ({"instructions": "be brief", "user_input": "hi", "task": "side_question"}, "side_question"),
])
def test_task_resolution(kwargs, expected):
    with patch("agent.oneshot.call_llm", return_value=_response()) as llm:
        assert run_oneshot(**kwargs) == "feat: thing"
    assert llm.call_args.kwargs["task"] == expected


def test_a_template_that_is_not_an_auxiliary_slot_keeps_the_default_backend(monkeypatch):
    monkeypatch.setitem(oneshot.PROMPT_TEMPLATES, "project_ideas", lambda _vars: ("sys", "ideas"))
    with patch("agent.oneshot.call_llm", return_value=_response("1. idea")) as llm:
        assert run_oneshot(template="project_ideas") == "1. idea"
    assert llm.call_args.kwargs["task"] == "title_generation"
