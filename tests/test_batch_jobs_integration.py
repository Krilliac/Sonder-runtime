"""End to end over a real ``fanout.db``: resume, ordering, sealed answers, tokens, retention."""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

import server
from sonder_runtime.adapters.persistence import batch_store, fanout_store
from sonder_runtime.application.batch_jobs.runner import run_job
from sonder_runtime.domain.batch_jobs import JobStatus
from tests.support.batch_job_harness import ITEM_TEXT, MARKER, SimulatedCrash, build, two_stages


@pytest.fixture
def harness(monkeypatch, tmp_path):
    return build(monkeypatch, tmp_path)


def _results(harness, job_id, **kwargs):
    return harness.tool("batch_job_results", job_id=job_id, limit=50, **kwargs)


def _dead_pid():
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def test_three_items_two_stages_make_exactly_six_calls_and_job_tokens_are_the_item_sums(harness):
    created = harness.create(two_stages(), items=3, start=True)
    assert created["ok"] is True
    job_id = created["job"]["job_id"]
    harness.wait()

    assert len(harness.calls) == 6
    status = harness.tool("batch_job_status", job_id=job_id)["job"]
    assert status["status"] == "completed"
    assert status["progress"]["done"] == status["progress"]["total"] == 6
    page = _results(harness, job_id)
    assert page["total_items"] == 3 and page["next_offset"] is None
    per_item_in = sum(item["tokens_in_provider"] for item in page["items"])
    per_item_out = sum(item["tokens_out_provider"] for item in page["items"])
    assert (per_item_in, per_item_out) == (status["tokens"]["tokens_in_provider"], status["tokens"]["tokens_out_provider"])
    assert per_item_in == sum(len(call.prompt) for call in harness.calls) and per_item_in > 0
    assert status["tokens"]["rows_with_unknown_tokens"] == 0
    assert sum(entry["tokens_in"] for item in page["items"] for entry in item["steps"]) == per_item_in


def test_stage_major_order_one_model_switch_and_no_unload_for_child_runs(harness):
    harness.create(two_stages(), items=3, start=True)
    harness.wait()

    stages = ["R1" if call.prompt.startswith("R1") else "R2" for call in harness.calls]
    assert stages == ["R1"] * 3 + ["R2"] * 3
    models = [call.model for call in harness.calls]
    assert sum(1 for left, right in zip(models, models[1:], strict=False) if left != right) == 1
    assert not [post for post in harness.posts if post[0] == "/api/generate"], harness.posts
    first = harness.calls[0]
    assert (first.system, first.num_ctx, first.temperature, first.think) == ("judge", 2048, 0.0, False)


def test_a_downstream_prompt_carries_the_exact_upstream_answer(harness):
    harness.answer = lambda call: "UP[%s]" % call.prompt.split()[-1] if call.prompt.startswith("R1") else "done"
    harness.create(two_stages(), items=1, start=True)
    harness.wait()
    assert harness.calls[1].prompt == "R2 %s #0 || UP[#0]" % ITEM_TEXT


def test_a_failed_step_blocks_its_downstream_step_which_is_never_dispatched(harness):
    def fail(call):
        if call.prompt.startswith("R1") and call.prompt.endswith("#1"):
            raise server.ModelCallError("empty_response", "no answer")

    harness.fail = fail
    created = harness.create(two_stages(), items=3, start=True)
    harness.wait()

    assert len(harness.calls) == 5
    status = harness.tool("batch_job_status", job_id=created["job"]["job_id"])["job"]
    assert status["progress"]["blocked"] == 1 and status["progress"]["failed"] == 1
    assert status["blocked"] == [{"stage": "r2", "item_key": "item-1"}]
    assert status["status"] == "completed"
    assert status["failures"][0]["failure_class"] == "empty_response"


def test_served_model_is_reported_per_step_and_per_stage(harness):
    harness.served = lambda call: "m:9" if call.prompt.endswith("#2") else call.model
    created = harness.create(two_stages(), items=3, start=True)
    harness.wait()
    job = harness.tool("batch_job_status", job_id=created["job"]["job_id"])["job"]
    assert job["stages"][0]["requested_model"] == "m:1"
    assert job["stages"][0]["served_models"] == {"m:1": 2, "m:9": 1}
    steps = _results(harness, created["job"]["job_id"], stage="r1")["items"][2]["steps"]
    assert (steps[0]["requested_model"], steps[0]["served_model"]) == ("m:1", "m:9")


def test_crash_then_reconcile_interrupts_the_job_and_resume_replays_only_the_interrupted_step(harness):
    created = harness.create(two_stages(), items=3, start=False)
    job_id = created["job"]["job_id"]
    batch_store.start_job(job_id, max_active=4)
    harness.crash_on_call = 4  # the first stage-2 step dies mid-call

    with pytest.raises(SimulatedCrash):
        run_job(job_id, store=batch_store, executor=harness.service.executor(),
                owner_id="doomed-worker", owner_pid=os.getpid())
    assert len(harness.calls) == 4
    dead = _dead_pid()
    with fanout_store._write_transaction() as conn:
        for table in ("batch_jobs", "fanout_runs", "fanout_results"):
            conn.execute("UPDATE %s SET owner_pid=? WHERE owner_pid<>0" % table, (dead,))
    harness.crash_on_call = None

    assert fanout_store.reconcile_stale_runs() >= 1
    assert batch_store.get_job(job_id).status is JobStatus.INTERRUPTED
    interrupted = harness.tool("batch_job_status", job_id=job_id)["job"]
    assert interrupted["status"] == "interrupted" and interrupted["progress"]["unknown"] == 1

    resumed = harness.tool("batch_job_resume", job_id=job_id)
    assert resumed["ok"] is True
    harness.wait()

    assert len(harness.calls) == 7
    prompts = [call.prompt for call in harness.calls]
    # The interrupted step is the only one called twice, and its retry is the first call after resume.
    assert prompts[4] == prompts[3]
    assert all(prompts.count(prompt) == (2 if prompt == prompts[3] else 1) for prompt in prompts)
    final = harness.tool("batch_job_status", job_id=job_id)["job"]
    assert final["status"] == "completed" and final["progress"]["answered"] == 6


def test_resume_without_include_failed_does_not_rerun_a_failed_step(harness):
    def fail(call):
        if call.prompt.startswith("R1") and call.prompt.endswith("#0"):
            raise server.ModelCallError("empty_response", "no answer")

    harness.fail = fail
    job_id = harness.create(two_stages(), items=2, start=True)["job"]["job_id"]
    harness.wait()
    assert len(harness.calls) == 3
    harness.tool("batch_job_cancel", job_id=job_id)  # completed: unchanged
    again = harness.tool("batch_job_resume", job_id=job_id)
    assert again["ok"] is False and again["error"]["code"] == "CONFLICT"

    harness.fail = None
    retried = harness.tool("batch_job_resume", job_id=job_id, include_failed=True)
    assert retried["ok"] is True
    harness.wait()
    assert len(harness.calls) == 5
    assert harness.tool("batch_job_status", job_id=job_id)["job"]["progress"]["answered"] == 4


def test_a_quoted_input_span_comes_back_verbatim_and_the_answer_never_reaches_the_database_bytes(harness):
    harness.answer = lambda call: "QUOTE: %s %s" % (ITEM_TEXT[:40], MARKER) if call.prompt.startswith("R1") else "ok"
    created = harness.create(two_stages(), items=1, start=True)
    harness.wait()

    page = _results(harness, created["job"]["job_id"])
    answer = page["items"][0]["steps"][0]["answer"]
    assert answer == "QUOTE: %s %s" % (ITEM_TEXT[:40], MARKER)
    assert "<redacted" not in answer
    raw = b"".join(path.read_bytes() for path in harness.database.parent.glob("fanout.db*"))
    assert MARKER.encode() not in raw and ITEM_TEXT[:40].encode() not in raw


def test_plain_fanout_answers_still_go_through_prompt_echo_redaction(harness, monkeypatch):
    monkeypatch.setattr(server, "_make_generate", lambda *_a, **_k: lambda prompt: "echo: " + prompt)
    receipt = __import__("json").loads(server.model_fanout("private fanout prompt: echo-must-not-persist", scope="local"))
    assert "<redacted prompt>" in receipt["answers"][0]["answer"]


def test_batch_child_runs_stay_out_of_model_fanout_recent_and_the_child_survives_ordinary_pruning(harness):
    created = harness.create(two_stages(), items=2, start=True)
    job_id = created["job"]["job_id"]
    harness.wait()

    recent = __import__("json").loads(server.model_fanout_recent())
    assert recent["runs"] == []
    listing = harness.tool("batch_job_recent")
    assert [job["job_id"] for job in listing["jobs"]] == [job_id]
    assert set(listing["jobs"][0]) == {"job_id", "status", "pause_reason", "created_ts", "updated_ts", "finished_ts", "items"}

    later = time.time() + 10 * 86_400
    fanout_store.prune(ttl_seconds=7 * 86_400, now=later)
    assert len(batch_store.step_states(job_id)) == 4
    fanout_store.prune(ttl_seconds=7 * 86_400, now=time.time() + 15 * 86_400)
    assert batch_store.get_job(job_id) is None and batch_store.step_states(job_id) == {}


def test_the_job_worker_runs_without_a_narration_context_and_progress_has_no_invented_eta(harness):
    created = harness.create(two_stages(), items=1, start=False)
    job = harness.tool("batch_job_status", job_id=created["job"]["job_id"])["job"]
    assert job["status"] == "draft"
    assert job["progress"]["rate_per_min"] is None and job["progress"]["eta_s"] is None
    assert job["progress"]["eta_basis"] == "insufficient_samples"


def test_cancel_forwards_to_the_job_and_a_cancelled_job_dispatches_nothing_more(harness):
    created = harness.create(two_stages(), items=3, start=False)
    job_id = created["job"]["job_id"]
    cancelled = harness.tool("batch_job_cancel", job_id=job_id)
    assert cancelled["job"]["status"] == "cancelled"
    started = harness.tool("batch_job_start", job_id=job_id)
    assert started["ok"] is False and started["error"]["code"] == "CONFLICT"
    assert harness.calls == []
