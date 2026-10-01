"""Production fan-in parity and negative evidence regressions."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import sqlite3

import pytest

import server
from sonder_runtime.adapters.fanout_receipt import synthesis_rows
from sonder_runtime.application.artifacts.candidates import CandidateFanIn
from sonder_runtime.application.artifacts.fanin import decode_readiness, encode_readiness, readiness_error
from sonder_runtime.application.artifacts.readiness import ArtifactReadiness


def _row(model, answer, run_id="fan-test"):
    return {
        "model": model, "status": "answered", "answer": answer,
        "answer_truncation_known": 1, "answer_truncated": 0, "elapsed_ms": 1,
        "answer_chars": len(answer), "thinking_chars": 0, "done_reason": "stop",
        "readiness_json": encode_readiness(ArtifactReadiness.from_content(model, run_id, answer)),
    }


def _stage(monkeypatch, rows, expected=("a", "b")):
    run = {"id": "fan-test", "status": "completed", "models_json": json.dumps(expected)}
    monkeypatch.setattr(server.fanout_store, "list_results", lambda _run_id: rows)
    monkeypatch.setattr(server.fanout_store, "execution_prompt_ciphertext", lambda _run_id: "sealed")
    monkeypatch.setattr(server.fanout_prompt_vault, "decrypt_prompt", lambda _sealed: "question")
    return run


def test_complete_fanout_source_bytes_and_output_have_legacy_parity(monkeypatch):
    run = _stage(monkeypatch, [_row("a", "one"), _row("b", "two")])
    expected = ('{"question":"question","sources":['
                '{"answer":"one","answer_chars":3,"answer_truncated":false,"done_reason":"stop",'
                '"elapsed_ms":1,"model":"a","stored_answer_chars":3,"thinking_chars":0},'
                '{"answer":"two","answer_chars":3,"answer_truncated":false,"done_reason":"stop",'
                '"elapsed_ms":1,"model":"b","stored_answer_chars":3,"thinking_chars":0}]}')
    seen = []
    monkeypatch.setattr(server, "_fanout_synthesis_model", lambda _selector: "local")
    monkeypatch.setattr(server, "_fanout_synthesis_generate", lambda _model, bundle: seen.append(bundle) or "answer")
    result = server._fanout_synthesize_run(run)
    assert seen == [expected]
    assert result == {
        "run_id": "fan-test", "synth_model": "local", "answer": "answer",
        "provenance": {"source_count": 2, "source_previews": [
            {"model": model, "preview_sha256": hashlib.sha256(answer.encode()).hexdigest()}
            for model, answer in (("a", "one"), ("b", "two"))
        ]},
    }


@pytest.mark.parametrize("mutation", ["tampered", "truncated", "running", "missing", "legacy", "stale", "cross_run", "length"])
def test_invalid_slot_is_reported_in_actual_synthesis_without_its_bytes(monkeypatch, mutation):
    rows = [_row("a", "valid"), _row("b", "REJECTED_BYTES"), _row("c", "also valid")]
    bad = rows[1]
    if mutation == "tampered":
        bad["answer"] += " changed"
    elif mutation == "truncated":
        bad["answer_truncated"] = 1
    elif mutation == "running":
        bad["status"] = "running"
    elif mutation == "missing":
        rows.pop(1)
    elif mutation == "legacy":
        bad["readiness_json"] = ""
    elif mutation in ("stale", "cross_run"):
        evidence = decode_readiness(bad["readiness_json"])
        evidence = replace(evidence, **({"timestamp": datetime.now(timezone.utc) - timedelta(days=400)}
                                      if mutation == "stale" else {"run_id": "other"}))
        bad["readiness_json"] = encode_readiness(evidence)
    else:
        bad["done_reason"] = "length"
    run = _stage(monkeypatch, rows, expected=("a", "b", "c"))
    seen = []
    monkeypatch.setattr(server, "_fanout_synthesis_model", lambda _selector: "local")
    monkeypatch.setattr(server, "_fanout_synthesis_generate", lambda _model, bundle: seen.append(bundle) or "answer")
    result = server._fanout_synthesize_run(run)
    assert result["provenance"]["source_count"] == 2
    assert "REJECTED_BYTES" not in seen[0]
    sources = json.loads(seen[0])["sources"]
    rejected = [source for source in sources if source.get("status") == "not_ready"]
    assert len(sources) == 3 and len(rejected) == 1
    assert rejected[0]["model"] == "b" and rejected[0]["error"]


def test_fanout_synthesis_needs_two_validated_artifacts(monkeypatch):
    rows = [_row("a", "valid"), _row("b", "REJECTED_BYTES")]
    rows[1]["answer_truncated"] = 1
    run = _stage(monkeypatch, rows)
    seen = []
    monkeypatch.setattr(server, "_fanout_synthesis_model", lambda _selector: "local")
    monkeypatch.setattr(server, "_fanout_synthesis_generate", lambda _model, bundle: seen.append(bundle) or "answer")
    with pytest.raises(server.ModelCallError, match="at least two validated-complete"):
        server._fanout_synthesize_run(run)
    assert seen == []


def test_fanout_synthesis_accepts_sealed_answers_older_than_a_day(monkeypatch):
    rows = [_row("a", "one"), _row("b", "two")]
    for row in rows:
        evidence = decode_readiness(row["readiness_json"])
        row["readiness_json"] = encode_readiness(replace(
            evidence, timestamp=datetime.now(timezone.utc) - timedelta(days=3),
        ))
    run = _stage(monkeypatch, rows)
    monkeypatch.setattr(server, "_fanout_synthesis_model", lambda _selector: "local")
    monkeypatch.setattr(server, "_fanout_synthesis_generate", lambda _model, _bundle: "answer")
    assert server._fanout_synthesize_run(run)["provenance"]["source_count"] == 2


def test_store_seals_once_and_persistent_byte_tampering_is_rejected(monkeypatch, tmp_path):
    store = server.fanout_store
    database = tmp_path / "fanout.db"
    monkeypatch.setenv("SONDER_FANOUT_DB", str(database))
    store.reset_schema_cache_for_tests()
    run = store.create_run("question", ["a", "b"])
    store.claim_run(run["id"], "worker", owner_pid=os.getpid())
    first = store.claim_next_result(run["id"], "worker", owner_pid=os.getpid())
    store.record_result(run["id"], first["model"], "worker", "answered", answer="original")
    slots = synthesis_rows(run, store.list_results(run["id"]))
    assert sum(not error for _, error in slots) == 1
    assert any("incomplete" in error for _, error in slots)
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE fanout_results SET answer='changed' WHERE model=?", (first["model"],))
    assert all(error for _, error in synthesis_rows(run, store.list_results(run["id"])))


def _candidate(index=0):
    return {"index": index, "name": "candidate-1", "ok": True,
            "code": "print(1)", "output": "1", "seconds": 0}


def test_candidate_requires_matching_independent_verifier_receipt():
    fanin = CandidateFanIn("print one", "assert True")
    completed = fanin.produce(_candidate)
    assert fanin.consume(completed, 0) is completed.result
    for bad in (replace(completed, verifier_receipt=""),
                replace(completed, readiness=replace(completed.readiness, verifier_receipt=""))):
        rejected = fanin.consume(bad, 0)
        assert not rejected["ok"] and "NOT READY" in rejected["output"]
        assert "code" not in rejected


def test_candidate_mutated_after_producer_return_cannot_become_winner():
    fanin = CandidateFanIn("print one", "")
    completed = fanin.produce(_candidate)
    completed.result["code"] = "STILL_WRITING"
    result = fanin.consume(completed, 0)
    assert result["ok"] is False and "code" not in result


@pytest.mark.parametrize("multi_language", [False, True])
def test_parallel_generate_valid_output_exact_parity(monkeypatch, multi_language):
    monkeypatch.setattr(server, "_refresh_live_cloud_tiers", lambda: None)
    monkeypatch.setattr(server, "_make_generate", lambda *a, **k: lambda _prompt: "```python\nprint(1)\n```")
    monkeypatch.setattr(server.grounding, "run_code", lambda *a, **k: (True, "1\n"))
    monkeypatch.setattr(server.grounding, "run_language_code", lambda *a, **k: (True, "1\n"))
    monkeypatch.setattr(server.time, "time", lambda: 100.0)
    if multi_language:
        actual = server.parallel_generate_run_languages("print one", languages="python", variants_per_language=2)
        expected = ("parallel multi-language generate/run: 2/2 passed in 0.000s (tier=code, workers=2)\n"
                    "[PASS] python-1 [python]\n1\n[PASS] python-2 [python]\n1\nwinner code blocks:\n"
                    "```python\nprint(1)\n```\n```python\nprint(1)\n```")
    else:
        actual = server.parallel_generate_run("print one", variants=2)
        expected = ("parallel generate/run: 2/2 passed in 0.000s (tier=code, workers=2)\n"
                    "[PASS] candidate-1\n1\n[PASS] candidate-2\n1\nwinner code:\n```python\nprint(1)\n```")
    assert actual == expected


@pytest.mark.parametrize("multi_language", [False, True])
def test_parallel_generate_failed_candidate_output_exact_parity(monkeypatch, multi_language):
    # Failed candidates are never sealed and never become winners, so the
    # fan-in must pass their public diagnostics through byte-for-byte.
    monkeypatch.setattr(server, "_refresh_live_cloud_tiers", lambda: None)
    replies = iter(["no code here", "```python\nraise SystemExit(1)\n```"])
    monkeypatch.setattr(server, "_make_generate", lambda *a, **k: lambda _prompt: next(replies))
    monkeypatch.setattr(server.grounding, "run_code", lambda *a, **k: (False, "exit 1"))
    monkeypatch.setattr(server.grounding, "run_language_code", lambda *a, **k: (False, "exit 1"))
    monkeypatch.setattr(server.time, "time", lambda: 100.0)
    if multi_language:
        actual = server.parallel_generate_run_languages(
            "print one", languages="python", variants_per_language=2, max_workers=1,
        )
        expected = ("parallel multi-language generate/run: 0/2 passed in 0.000s (tier=code, workers=1)\n"
                    "[FAIL] python-1 [python]\nno python code block returned\n"
                    "[FAIL] python-2 [python]\nexit 1")
    else:
        actual = server.parallel_generate_run("print one", variants=2, max_workers=1)
        expected = ("parallel generate/run: 0/2 passed in 0.000s (tier=code, workers=1)\n"
                    "[FAIL] candidate-1\nno Python code block returned\n"
                    "[FAIL] candidate-2\nexit 1")
    assert actual == expected


def test_truncated_generated_candidate_never_reaches_verifier_or_winner(monkeypatch):
    def gen(_prompt):
        return "```python\nprint(1)\n```"
    gen.last_response_meta = {"done_reason": "length"}
    monkeypatch.setattr(server, "_refresh_live_cloud_tiers", lambda: None)
    monkeypatch.setattr(server, "_make_generate", lambda *a, **k: gen)
    monkeypatch.setattr(server.grounding, "run_code", lambda *a, **k: pytest.fail("truncated generation reached verifier"))
    result = server.parallel_generate_run("print one", variants=1)
    assert "[FAIL] candidate-1\nERROR: provider output is truncated" in result
    assert "0/1 passed" in result and "winner code" not in result


def test_bridged_tier_length_reply_never_reaches_verifier_or_winner(monkeypatch):
    # A tier bound to sonder-inference answers through the provider bridge,
    # which carries the reply's finish reason as done_reason (#616). Without
    # it the truncation guard has nothing to read and passes the reply.
    from types import SimpleNamespace

    from sonder_runtime.adapters import legacy_chat_bridge
    from sonder_runtime.adapters.inference.sonder_inference_gateway import SonderInferenceResponse

    class Gateway:
        def generate(self, request, _context):
            return SonderInferenceResponse(
                text="```python\nprint(1)\n```", model="bridged", tier=request.tier, finish_reason="length",
            )

    monkeypatch.setattr(server, "_refresh_live_cloud_tiers", lambda: None)
    monkeypatch.setattr(legacy_chat_bridge, "provider_for_tier", lambda *_args: "sonder_inference")
    monkeypatch.setattr(server, "_application", lambda: SimpleNamespace(model_gateway=Gateway()))
    monkeypatch.setattr(server.grounding, "run_code", lambda *a, **k: pytest.fail("truncated generation reached verifier"))
    result = server.parallel_generate_run("print one", variants=1)
    assert "[FAIL] candidate-1\nERROR: provider output is truncated" in result
    assert "0/1 passed" in result and "winner code" not in result


def test_configured_verifier_cannot_be_disabled_by_removing_its_receipt():
    evidence = ArtifactReadiness.from_content("worker", "run", "ok", deterministic_verifier="test")
    assert readiness_error(evidence, run_id="run", producer_id="worker", content="ok", require_verifier_receipt=True)


def _race_shared_generator(monkeypatch, build):
    """Both producers get their reply before either checks completion."""
    import threading
    from sonder_runtime.platform.runtime_threads import Thread

    def post(_path, payload, **_kwargs):
        name = payload["messages"][-1]["content"]
        return {"message": {"content": "```python\nprint(1)\n```"},
                "done_reason": "length" if name == "truncated" else "stop"}, 0

    monkeypatch.setattr(server, "_post_model", post)
    gen = build()
    CandidateFanIn.bind_generator(gen)
    barrier = threading.Barrier(2)
    observed = {}

    def one(name):
        try:
            gen(name)
            barrier.wait(timeout=10)
            CandidateFanIn.require_complete_generation(gen)
            observed[name] = "ready"
        except Exception as exc:
            observed[name] = str(exc)

    threads = [Thread(target=one, args=(name,)) for name in ("truncated", "complete")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    return observed


def test_shared_generator_completion_metadata_is_bound_to_each_producer(monkeypatch):
    observed = _race_shared_generator(monkeypatch, lambda: server._make_generate("local", "", 0.0, 10, 4096))
    assert observed == {"complete": "ready", "truncated": "provider output is truncated"}


def test_tier_wrapped_shared_generator_keeps_completion_per_producer(monkeypatch):
    # parallel_generate_run shares the wrapper that _make_tier_generate returns
    # (#610). The raw closure records each reply's completion, so the per-thread
    # slot must live on it: bound to the wrapper, the check fell back to the
    # shared last_response_meta and a truncated sibling passed as complete.
    observed = _race_shared_generator(monkeypatch, lambda: server._make_tier_generate(
        "code", server.TIERS.get("code") or "local", "", 0.0, 10, 4096, cloud=False,
    ))
    assert observed == {"complete": "ready", "truncated": "provider output is truncated"}
