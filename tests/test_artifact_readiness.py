"""Canaries for the fanout/fanin artifact readiness barrier."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace

import pytest

from sonder_runtime.application.artifacts import (
    ArtifactReadiness,
    ArtifactReadinessBarrier,
)
import master_orchestrator


def _pair(run_id="run-1", now=None):
    now = now or datetime.now(timezone.utc)
    contents = {"worker-a": "alpha", "worker-b": "bravo"}
    artifacts = tuple(
        ArtifactReadiness.from_content(producer, run_id, content, timestamp=now)
        for producer, content in contents.items()
    )
    return artifacts, contents, now


def test_barrier_accepts_complete_digest_bound_fanin():
    artifacts, contents, now = _pair()
    joined = ArtifactReadinessBarrier().join(
        artifacts,
        run_id="run-1",
        expected_producers=contents,
        content_by_producer=contents,
        now=now,
    )
    assert [item.producer_id for item in joined] == ["worker-a", "worker-b"]


@pytest.mark.parametrize("mutator", [
    lambda item: replace(item, completion_marker="partial"),
    lambda item: replace(item, validation_result="failed"),
])
def test_barrier_rejects_incomplete_or_invalid_validation(mutator):
    artifacts, contents, now = _pair()
    broken = (mutator(artifacts[0]), artifacts[1])
    with pytest.raises(ValueError):
        ArtifactReadinessBarrier().join(
            broken,
            run_id="run-1",
            expected_producers=contents,
            content_by_producer=contents,
            now=now,
        )


def test_barrier_rejects_partial_stale_cross_run_and_digest_mismatch():
    artifacts, contents, now = _pair()
    barrier = ArtifactReadinessBarrier(max_age=timedelta(seconds=5))
    with pytest.raises(ValueError, match="partial"):
        barrier.join(artifacts[:1], run_id="run-1", expected_producers=contents, now=now)
    with pytest.raises(ValueError, match="stale"):
        barrier.join(artifacts, run_id="run-1", expected_producers=contents, now=now + timedelta(seconds=6))
    with pytest.raises(ValueError, match="identity"):
        barrier.join(artifacts, run_id="other", expected_producers=contents, now=now)
    with pytest.raises(ValueError, match="digest"):
        barrier.join(
            artifacts,
            run_id="run-1",
            expected_producers=contents,
            content_by_producer={**contents, "worker-a": "tampered"},
            now=now,
        )
    with pytest.raises(ValueError, match="content map is incomplete"):
        barrier.join(
            artifacts,
            run_id="run-1",
            expected_producers=contents,
            content_by_producer={"worker-a": contents["worker-a"]},
            now=now,
        )


def test_default_barrier_window_allows_a_long_fanin_join():
    started = datetime.now(timezone.utc) - timedelta(minutes=20)
    artifacts, contents, _ = _pair(now=started)
    joined = ArtifactReadinessBarrier().join(
        artifacts,
        run_id="run-1",
        expected_producers=contents,
        content_by_producer=contents,
        now=datetime.now(timezone.utc),
    )
    assert len(joined) == 2


def test_deterministic_verifier_is_optional_but_fail_closed_when_declared():
    artifacts, contents, now = _pair()
    verifier = lambda text: text == "alpha" or text == "bravo"
    declared = tuple(
        ArtifactReadiness.from_content(
            item.producer_id,
            item.run_id,
            contents[item.producer_id],
            timestamp=now,
            deterministic_verifier="text-equals-known-values",
        )
        for item in artifacts
    )
    ArtifactReadinessBarrier().join(
        declared,
        run_id="run-1",
        expected_producers=contents,
        content_by_producer=contents,
        now=now,
        verifier=verifier,
    )
    with pytest.raises(ValueError, match="digest"):
        ArtifactReadinessBarrier().join(
            declared,
            run_id="run-1",
            expected_producers=contents,
            content_by_producer={**contents, "worker-a": "wrong"},
            now=now,
            verifier=verifier,
        )


def test_delegated_fanin_blocks_mutated_producer_manifest(monkeypatch):
    original = master_orchestrator._run_worker
    audited = []

    # Keep the producer output intact while mutating only the manifest carried
    # into the join, proving the barrier compares independent evidence.
    def mutate_manifest(*args, **kwargs):
        result = original(*args, **kwargs)
        if isinstance(result, master_orchestrator.ReadyWorkerOutput):
            broken = replace(result.readiness, completion_marker="partial")
            return master_orchestrator.ReadyWorkerOutput(result.output, broken)
        return result

    monkeypatch.setattr(master_orchestrator, "_run_worker", mutate_manifest)
    result = master_orchestrator.run_delegated(
        "compare options",
        worker_fn=lambda _prompt: "producer output",
        audit_fn=lambda prompt: audited.append(prompt) or "must not run",
        agents=1,
    )
    assert "artifact readiness barrier rejected fan-in" in result["output"]
    assert audited == []
