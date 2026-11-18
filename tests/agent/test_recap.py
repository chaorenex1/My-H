"""Idle recap generation (ADR-5 P3): the upstream prompt, the shared ephemeral fork, the reply cap."""

from unittest.mock import patch

import pytest

from agent import recap
from agent.ephemeral_prompt import run_ephemeral_prompt


class TestRecapPrompt:

    def test_body_is_the_upstream_instruction(self):
        prompt = recap.build_recap_prompt()
        assert prompt.startswith("<recap>\nUser stepped away; returning. Recap: <40 words, 1–2 plain sentences")
        assert "Skip: root-cause narrative, fix internals, secondary to-dos, em-dash tangents." in prompt
        assert prompt.endswith("</recap>")
        assert "Overall goal:" not in prompt and "Active task:" not in prompt

    def test_anchors_are_their_own_lines_in_order(self):
        prompt = recap.build_recap_prompt("ship P3", "run the smoke test")
        lines = prompt.splitlines()
        assert lines[-3:] == ["Overall goal: ship P3", "Active task: run the smoke test", "</recap>"]
        assert prompt.index("Overall goal:") < prompt.index("Active task:")


class TestReplyCleanup:

    def test_collapses_runs_over_three(self):
        text = "\n".join(["same"] * 5 + ["done"])
        assert recap.dedupe_recap_reply(text) == "same\n[…5×]\ndone"

    def test_keeps_runs_of_three(self):
        text = "\n".join(["same"] * 3)
        assert recap.dedupe_recap_reply(text) == text

    def test_truncates_on_a_character_boundary(self):
        text = "界" * 3000  # 9000 UTF-8 bytes
        out = recap.dedupe_recap_reply(text, max_bytes=100)
        encoded = out.encode("utf-8")
        assert len(encoded) <= 100
        assert out.endswith("\n[…truncated]")
        encoded.decode("utf-8")  # never split a multi-byte character


class TestGenerate:

    def test_runs_the_shared_side_turn_with_the_recap_budget(self):
        seen = {}

        def fake_run(parent, prompt, **kwargs):
            seen.update(parent=parent, prompt=prompt, **kwargs)
            return "  Fixing the parser; next: run the suite.  "

        parent = object()
        with patch("agent.ephemeral_prompt.run_ephemeral_prompt", side_effect=fake_run):
            text = recap.generate_recap(parent, goal="ship the parser", task="run tests")

        assert text == "Fixing the parser; next: run the suite."
        assert seen["parent"] is parent
        assert seen["task"] == "recap"
        assert seen["max_iterations"] == 1
        assert seen["max_output_tokens"] == 256
        assert recap._NO_TOOLS_REMINDER in seen["prompt"]
        assert "Overall goal: ship the parser" in seen["prompt"]
        assert "Active task: run tests" in seen["prompt"]

    def test_cleans_repeated_lines_once(self):
        with patch("agent.ephemeral_prompt.run_ephemeral_prompt",
                   return_value="\n".join(["same"] * 4)):
            assert recap.generate_recap(object()) == "same\n[…4×]"

    def test_failure_is_a_silent_empty_reply(self):
        with patch("agent.ephemeral_prompt.run_ephemeral_prompt",
                   side_effect=RuntimeError("provider down")):
            assert recap.generate_recap(object()) == ""

    def test_empty_reply_stays_empty(self):
        with patch("agent.ephemeral_prompt.run_ephemeral_prompt", return_value="   "):
            assert recap.generate_recap(object()) == ""


class TestEphemeralFork:
    """``run_ephemeral_prompt`` is the /btw+recap shared skeleton: deny tools, replay the snapshot."""

    def test_denies_tools_caps_output_and_releases_under_the_task(self):
        from agent.side_question import trim_snapshot_for_fork

        calls = {}

        class FakeFork:
            _ephemeral_max_output_tokens = 0

            def run_conversation(self, user_message, conversation_history):
                calls["history"] = conversation_history
                calls["cap"] = self._ephemeral_max_output_tokens
                return {"final_response": "  answer  "}

        def fake_build(parent, task_cfg, *, max_iterations, write_origin):
            calls.update(task_cfg=task_cfg, max_iterations=max_iterations, write_origin=write_origin)
            return FakeFork(), {"model": "m"}, False

        whitelists = []

        def fake_release(parent, fork, *, task):
            calls["release"] = (parent, task)

        parent = type("P", (), {"_session_messages": [
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "a"},
            {"role": "user", "content": "still running"},
        ]})()
        with patch("agent.background_review.build_cache_parity_fork", fake_build), \
             patch("agent.background_review.release_cache_parity_fork", fake_release), \
             patch("hermes_cli.plugins.set_thread_tool_whitelist",
                   side_effect=lambda allowed, **kw: whitelists.append(allowed)), \
             patch("hermes_cli.plugins.clear_thread_tool_whitelist"):
            answer = run_ephemeral_prompt(parent, "do it", task="recap", max_iterations=1,
                                          max_output_tokens=256)

        assert answer == "answer"
        assert whitelists == [set()]
        assert calls["cap"] == 256
        assert calls["task_cfg"] == {}
        assert calls["max_iterations"] == 1
        assert calls["write_origin"] == "recap"
        assert calls["history"] == trim_snapshot_for_fork(parent._session_messages)
        assert calls["release"] == (parent, "recap")

    def test_an_explicit_snapshot_wins_over_the_live_history(self):
        captured = {}

        class FakeFork:
            def run_conversation(self, user_message, conversation_history):
                captured["history"] = conversation_history
                return {"final_response": "ok"}

        with patch("agent.background_review.build_cache_parity_fork",
                   lambda parent, cfg, *, max_iterations, write_origin: (FakeFork(), {}, False)), \
             patch("agent.background_review.release_cache_parity_fork"), \
             patch("hermes_cli.plugins.set_thread_tool_whitelist"), \
             patch("hermes_cli.plugins.clear_thread_tool_whitelist"):
            parent = type("P", (), {"_session_messages": [{"role": "assistant", "content": "newer"}]})()
            run_ephemeral_prompt(parent, "p", task="t", max_iterations=1, history=[])

        assert captured["history"] == []

    def test_a_failed_turn_with_no_text_raises_and_still_releases(self):
        released = []

        class FakeFork:
            def run_conversation(self, user_message, conversation_history):
                return {"final_response": "", "error": "boom"}

        with patch("agent.background_review.build_cache_parity_fork",
                   lambda parent, cfg, *, max_iterations, write_origin: (FakeFork(), {}, False)), \
             patch("agent.background_review.release_cache_parity_fork",
                   side_effect=lambda parent, fork, *, task: released.append(task)), \
             patch("hermes_cli.plugins.set_thread_tool_whitelist"), \
             patch("hermes_cli.plugins.clear_thread_tool_whitelist"):
            with pytest.raises(RuntimeError, match="boom"):
                run_ephemeral_prompt(object(), "p", task="recap", max_iterations=1)

        assert released == ["recap"]


class TestIsolation:
    """A recap must not touch the conversation it reads (ADR-5 acceptance 10)."""

    def test_real_fork_replays_the_parent_without_writing_it(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        # Importing run_agent runs hermes_bootstrap's dependency activation, which probes the
        # checkout's parent for a PM manifest (the real home when the checkout lives under it).
        with patch("pm.environments.activate_dependencies"), \
             patch("hermes_cli._early_recovery.recover_if_needed"):
            from run_agent import AIAgent

        captured = {}

        with patch("model_tools.get_tool_definitions", return_value=[]), \
             patch("model_tools.check_toolset_requirements", return_value={}), \
             patch("agent.model_metadata.fetch_model_metadata", return_value={}):
            parent = AIAgent(api_key="k", base_url="https://openrouter.ai/api/v1", model="test/model",
                             quiet_mode=True, skip_context_files=True, skip_memory=True,
                             session_id="sess-recap")
            parent._cached_system_prompt = "You are helpful."
            parent._session_messages = [{"role": "user", "content": "u"},
                                        {"role": "assistant", "content": "a"}]
            before = [dict(message) for message in parent._session_messages]

            def fake_run(self, user_message, conversation_history=None, **kwargs):
                captured.update(history=conversation_history, persist_disabled=self._persist_disabled,
                                session_db=self._session_db, cap=self._ephemeral_max_output_tokens,
                                prompt=user_message)
                return {"final_response": "Recap line."}

            with patch.object(AIAgent, "run_conversation", fake_run):
                assert recap.generate_recap(parent) == "Recap line."

        assert parent._session_messages == before
        assert captured["history"] == before
        assert captured["persist_disabled"] is True
        assert captured["session_db"] is None
        assert captured["cap"] == 256
        assert "User stepped away" in captured["prompt"]
