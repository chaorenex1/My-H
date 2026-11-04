"""ADR-5 D2/D4: the compaction carrier's durable stamp and the live event record.

``compress()`` is the one producer both commit paths share, so the counters must be stamped
there (not at commit time) and must be idempotent: canonical engines compare successive
``compress()`` results for no-ops.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from agent.compaction_display import (
    compaction_carrier,
    compaction_summary_event,
    compaction_summary_stamp,
)
from agent.context_compressor import (
    COMPRESSED_SUMMARY_METADATA_KEY,
    SUMMARY_PREFIX,
    ContextCompressor,
)
from agent.conversation_compression import compress_context
from hermes_state import SessionDB


def _compressor():
    with patch("agent.context_compressor.get_model_context_length", return_value=100_000):
        compressor = ContextCompressor(
            model="test/main-model", provider="test-provider", threshold_percent=0.50, quiet_mode=True,
            config_context_length=100_000,
        )
    compressor.tail_token_budget = 10
    return compressor


def _messages():
    filler = " ".join(["context"] * 200)
    msgs = [{"role": "system", "content": "system prompt"}]
    for idx in range(10):
        msgs.append({"role": "user", "content": f"user message {idx} {filler}"})
        msgs.append({"role": "assistant", "content": f"assistant reply {idx} {filler}"})
    return msgs


def _carrier():
    return {
        "role": "user",
        "content": f"{SUMMARY_PREFIX}\nSUMMARY BODY",
        COMPRESSED_SUMMARY_METADATA_KEY: True,
        "display_metadata": {"reactions": [{"emoji": "x"}]},
    }


def test_stamp_records_the_attempt_and_preserves_other_metadata():
    compressor = _compressor()
    compressor._last_compression_made_progress = True
    compressor._active_compression_telemetry = {
        "tokens_before": 1200, "tokens_after": 300, "token_count_method": "estimate_rough",
        "method": "aux_fallback_main",
    }
    carrier = _carrier()

    compressor._stamp_compaction_display([carrier])

    assert compaction_summary_stamp(carrier) == {
        "tokens_before": 1200, "tokens_after": 300,
        "token_method": "estimate_rough", "method": "aux_fallback_main",
    }
    assert carrier["display_metadata"]["reactions"] == [{"emoji": "x"}]


def test_stamp_is_idempotent_so_successive_compress_results_compare_equal():
    compressor = _compressor()
    compressor._last_compression_made_progress = True
    compressor._active_compression_telemetry = {
        "tokens_before": 1200, "tokens_after": 300, "token_count_method": "estimate_rough",
        "method": "llm_summary",
    }
    carrier = _carrier()
    compressor._stamp_compaction_display([carrier])
    metadata = carrier["display_metadata"]

    compressor._stamp_compaction_display([carrier])

    assert carrier["display_metadata"] is metadata


def test_a_no_op_attempt_never_stamps_a_carrier():
    compressor = _compressor()
    compressor._last_compression_made_progress = False
    compressor._active_compression_telemetry = {
        "tokens_before": 1, "tokens_after": 1, "token_count_method": "estimate_rough", "method": "llm_summary",
    }
    carrier = _carrier()

    compressor._stamp_compaction_display([carrier])

    assert "compaction" not in carrier["display_metadata"]


def test_event_record_reads_the_stamp_and_the_display_body():
    carrier = _carrier()
    carrier["display_metadata"]["compaction"] = {
        "tokens_before": 1200, "tokens_after": 300,
        "token_method": "estimate_rough", "method": "llm_summary",
    }
    carrier["_row_id"] = 42
    carrier["timestamp"] = 99.0

    payload = compaction_summary_event(carrier, trigger="pre_api")

    assert payload == {
        "text": "SUMMARY BODY", "tokens_before": 1200, "tokens_after": 300,
        "token_method": "estimate_rough", "method": "llm_summary", "trigger": "pre_api",
        "timestamp": 99.0, "boundary_row_id": 42,
    }
    # The wire body never carries the model-facing prefix or the end marker.
    assert "[CONTEXT COMPACTION" not in payload["text"]


def test_a_pre_stamp_carrier_has_no_event():
    assert compaction_summary_event(_carrier(), trigger="manual") is None


def test_compress_stamps_the_committed_carrier_and_leaves_one_event_record():
    """The real in-place commit path: the stamped row, its durable id, and the agent's slot agree."""
    with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent

        db = SessionDB(db_path=Path(tmpdir) / "state.db")
        db.create_session("compaction-display-session", "cli", model="test/model")
        agent = AIAgent(
            api_key="test-key", base_url="https://openrouter.ai/api/v1", model="test/model", quiet_mode=True,
            session_db=db, session_id="compaction-display-session", skip_context_files=True, skip_memory=True,
        )
        agent.compression_in_place = True
        agent.context_compressor.tail_token_budget = 10
        with patch.object(agent.context_compressor, "_generate_summary", return_value="SUMMARY BODY"):
            compressed, _ = agent._compress_context(_messages(), "sys", approx_tokens=80_000, trigger="pre_api")

        carrier = compaction_carrier(compressed)
        stamp = compaction_summary_stamp(carrier)
        record = agent._last_compaction_display
        agent.close()
        db.close()

    assert isinstance(carrier, dict)
    assert isinstance(stamp, dict)
    assert stamp["tokens_before"] > stamp["tokens_after"] >= 0
    assert (stamp["token_method"], stamp["method"]) == ("estimate_rough", "llm_summary")
    assert carrier["_row_id"] is not None
    assert isinstance(record, dict)
    assert record["text"] == ContextCompressor.summary_body_for_display(carrier) == "SUMMARY BODY"
    assert record["boundary_row_id"] == carrier["_row_id"]
    assert record["trigger"] == "pre_api"
    assert record["tokens_before"] == stamp["tokens_before"]
    assert record["tokens_after"] == stamp["tokens_after"]


def test_a_failed_attempt_leaves_no_event_record():
    with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent

        db = SessionDB(db_path=Path(tmpdir) / "state.db")
        db.create_session("compaction-abort-session", "cli", model="test/model")
        agent = AIAgent(
            api_key="test-key", base_url="https://openrouter.ai/api/v1", model="test/model", quiet_mode=True,
            session_db=db, session_id="compaction-abort-session", skip_context_files=True, skip_memory=True,
        )
        agent.compression_in_place = True
        compressor = agent.context_compressor
        compressor.tail_token_budget = 10
        compressor.abort_on_summary_failure = True
        with patch.object(compressor, "_generate_summary", return_value=None):
            returned, _ = agent._compress_context(_messages(), "sys", approx_tokens=80_000, trigger="post_tool")
        record = agent._last_compaction_display
        agent.close()
        db.close()

    assert returned == _messages()
    assert record is None


def test_compress_context_shares_the_stamp_with_the_canonical_detached_compressor():
    """A detached compressor (``prepare_compress``) also stamps: it is the same ``compress()``."""
    compressor = _compressor()
    with patch.object(compressor, "_generate_summary", return_value="DETACHED BODY"):
        compressed = compressor.compress(_messages(), force=True)

    carrier = compaction_carrier(compressed)
    assert compaction_summary_stamp(carrier)["tokens_before"] > 0
    assert ContextCompressor.summary_body_for_display(carrier) == "DETACHED BODY"
