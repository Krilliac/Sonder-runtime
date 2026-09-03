"""Fanout receipts live in the adapters layer; the root name is a delegate."""
import json
import time

import server
from sonder_runtime.adapters import fanout_receipt
from sonder_runtime.adapters.persistence import fanout_store


def _run():
    return {
        "id": "fan-1", "status": "done", "scope": "local", "created_ts": 100.0, "finished_ts": 160.0,
        "limits_json": json.dumps({
            "plan_skipped": [
                {"model": "x", "reason": "cooldown", "retry_after_ts": time.time() + 30},
                "not eligible",
            ],
            "selection_profile": "healthy-local-chat",
        }),
        "models_json": json.dumps(["a:7b", "b:7b", "c:7b"]),
        "cloud_opt_in": False,
    }


def _rows():
    return [
        {"model": "a:7b", "status": "answered", "answer": "hi", "elapsed_ms": 10, "answer_chars": 2,
         "answer_truncation_known": 1, "answer_truncated": 0, "thinking_chars": 5, "done_reason": "stop",
         "error": None},
        {"model": "c:7b", "status": "unknown", "answer": "", "elapsed_ms": 30, "error": "unknown"},
        {"model": "b:7b", "status": "failed", "answer": "", "elapsed_ms": 20, "error": "ERROR: x",
         "failure_class": "timeout", "retry_after_ts": time.time() + 10},
        {"model": "d:7b", "status": "skipped", "answer": "", "elapsed_ms": 0, "error": "not resident"},
        {"model": "e:7b", "status": "pending", "answer": "", "elapsed_ms": 0, "error": None},
    ]


def _stage(monkeypatch):
    monkeypatch.setattr(fanout_store, "get_run", lambda run_id: _run() if run_id == "fan-1" else None)
    monkeypatch.setattr(fanout_store, "list_results", lambda _run_id: _rows())


def test_root_delegate_matches_the_packaged_receipt(monkeypatch):
    _stage(monkeypatch)
    expected = fanout_receipt.build_receipt("fan-1", admission=server._fanout_admission)
    actual = server._fanout_receipt("fan-1")
    for key in ("run_id", "models_selected", "models_failed", "usage", "admission", "answers"):
        assert actual[key] == expected[key]
    assert server._fanout_receipt("missing") is None


def test_receipt_counts_usage_skips_and_sorted_rows_without_the_prompt(monkeypatch):
    _stage(monkeypatch)
    receipt = fanout_receipt.build_receipt("fan-1", admission=lambda run, rows, limits: {"fake": True})
    assert receipt["run_id"] == "fan-1"
    assert receipt["selection_profile"] == "healthy-local-chat"
    assert (receipt["models_selected"], receipt["models_answered"], receipt["models_failed"]) == (5, 1, 1)
    assert (receipt["models_unknown"], receipt["models_pending"], receipt["models_running"]) == (1, 1, 0)
    assert receipt["models_skipped"] == 3
    plan_skip, plain_skip, execution_skip = receipt["skipped"]
    assert plan_skip["reason"] == "cooldown" and "retry_after_ts" not in plan_skip
    assert 0 < plan_skip["retry_after_ms"] <= 30_000
    assert plain_skip == {"reason": "not eligible"}
    assert execution_skip == {"model": "d:7b", "reason": "not resident"}
    assert receipt["total_elapsed_ms"] == 60_000
    assert receipt["usage"] == {
        "answer_chars": 2, "stored_answer_chars": 2, "answer_chars_known_models": 1,
        "thinking_chars": 5, "models_with_observed_thinking": 1,
    }
    assert receipt["admission"] == {"fake": True}
    assert [row["model"] for row in receipt["answers"]] == ["a:7b"]
    assert receipt["answers"][0]["answer_truncated"] is False
    assert [row["model"] for row in receipt["failures"]] == ["b:7b", "c:7b"]
    failed, unknown = receipt["failures"]
    assert failed["failure_class"] == "timeout"
    assert 0 < failed["retry_after_ms"] <= 10_000
    assert "retry_after_ms" not in unknown
    assert "prompt" not in json.dumps(receipt)
