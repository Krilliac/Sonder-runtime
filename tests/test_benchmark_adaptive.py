"""Model-free tests for the adaptive checkpoint evidence comparator."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "benchmark_adaptive.py"
SPEC = importlib.util.spec_from_file_location("benchmark_adaptive", MODULE_PATH)
adaptive = importlib.util.module_from_spec(SPEC)
sys.modules["benchmark_adaptive"] = adaptive
SPEC.loader.exec_module(adaptive)

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()
HISTORY_DIGEST = hashlib.sha256(b"grounded-history-v1").hexdigest()


def _tasks(*, solve_second=False, regress_first=False):
    return [
        {
            "name": "task-a", "completed": not regress_first,
            "retries": 2 if regress_first else 1,
            "tokens_in": 100, "tokens_out": 20,
        },
        {
            "name": "task-b", "completed": solve_second,
            "retries": 0 if solve_second else 2,
            "tokens_in": 80, "tokens_out": 10,
        },
    ]


def _record(checkpoint, tasks=None, **overrides):
    fields = {
        "model": "sonder:latest",
        "model_digest": DIGEST_A,
        "suite": "adaptive-core",
        "suite_version": "1",
        "suite_digest": DIGEST_B,
        "hardware": "host-a/gpu-a/driver-a",
        "hardware_digest": DIGEST_A,
        "checkpoint": checkpoint,
        "checkpoint_label": checkpoint + "-checkpoint",
        "grounded_records": 0 if checkpoint == "fresh" else 12,
        "grounded_history_digest": (
            EMPTY_DIGEST if checkpoint == "fresh" else HISTORY_DIGEST
        ),
        "tasks": _tasks() if tasks is None else tasks,
        "source": "offline-test",
    }
    fields.update(overrides)
    return adaptive.make_record(**fields)


def test_comparison_tracks_completion_retries_tokens_and_identity():
    fresh = _record("fresh")
    accumulated = _record("accumulated", _tasks(solve_second=True))
    report = adaptive.compare_records(fresh, accumulated)
    assert report == adaptive.compare_records(fresh, accumulated)

    assert report["model_free"] is True
    assert report["causal_claim"] is False
    assert report["identity_key"] == fresh["identity_key"]
    assert report["deltas"] == {
        "completed": 1,
        "completion_rate": 0.5,
        "retries": -2,
        "tokens_in": 0,
        "tokens_out": 0,
        "tokens_total": 0,
    }
    assert report["assessment"]["completion"] == "improved"
    assert report["assessment"]["retries"] == "improved"
    assert report["assessment"]["tokens"] == "unchanged"
    assert report["assessment"]["improved_tasks"] == ["task-b"]
    assert report["assessment"]["regressed_tasks"] == []
    assert report["fresh"]["record_id"] == fresh["record_id"]
    assert report["accumulated"]["record_id"] == accumulated["record_id"]
    assert len(report["report_id"]) == 64
    unsigned_report = dict(report)
    report_id = unsigned_report.pop("report_id")
    assert adaptive._canonical_digest(unsigned_report) == report_id
    assert "does not run a model" in adaptive.render_report(report)


def test_task_swap_reports_regression_even_when_aggregate_completion_is_equal():
    fresh = _record("fresh")
    accumulated = _record(
        "accumulated", _tasks(solve_second=True, regress_first=True),
    )
    report = adaptive.compare_records(fresh, accumulated)
    assert report["deltas"]["completed"] == 0
    assert report["assessment"]["completion"] == "mixed"
    assert report["assessment"]["any_completion_regression"] is True
    assert report["assessment"]["improved_tasks"] == ["task-b"]
    assert report["assessment"]["regressed_tasks"] == ["task-a"]
    assert report["assessment"]["retry_regressed_tasks"] == ["task-a"]


def test_per_task_token_regression_is_not_hidden_by_better_aggregate():
    fresh = _record("fresh")
    tasks = _tasks()
    tasks[0]["tokens_in"] = 150
    tasks[1]["tokens_in"] = 20
    accumulated = _record("accumulated", tasks)
    report = adaptive.compare_records(fresh, accumulated)
    assert report["deltas"]["tokens_total"] == -10
    assert report["assessment"]["tokens"] == "improved"
    assert report["assessment"]["token_regressed_tasks"] == ["task-a"]
    assert "Tasks with more tokens: task-a" in adaptive.render_report(report)


def test_exact_hardware_model_and_suite_identity_is_required():
    fresh = _record("fresh")
    changed_hardware = _record("accumulated", hardware_digest=DIGEST_B)
    with pytest.raises(adaptive.AdaptiveBenchmarkError, match="match exactly"):
        adaptive.compare_records(fresh, changed_hardware)


def test_task_sets_must_match_exactly():
    fresh = _record("fresh")
    tasks = _tasks(solve_second=True) + [{
        "name": "task-c", "completed": True, "retries": 0,
        "tokens_in": 1, "tokens_out": 1,
    }]
    with pytest.raises(adaptive.AdaptiveBenchmarkError, match="task sets"):
        adaptive.compare_records(fresh, _record("accumulated", tasks))


def test_records_are_deterministic_bounded_and_tamper_evident():
    first = _record("fresh")
    second = _record("fresh", list(reversed(_tasks())))
    assert first == second
    assert adaptive.validate_record(first) == first

    tampered = json.loads(json.dumps(first))
    tampered["tasks"][0]["completed"] = False
    with pytest.raises(adaptive.AdaptiveBenchmarkError, match="summary"):
        adaptive.validate_record(tampered)

    extended = json.loads(json.dumps(first))
    extended["untracked_claim"] = "improved"
    with pytest.raises(adaptive.AdaptiveBenchmarkError, match="unsupported fields"):
        adaptive.validate_record(extended)
    extended[1] = "non-text key"
    with pytest.raises(adaptive.AdaptiveBenchmarkError, match="unsupported fields"):
        adaptive.validate_record(extended)

    too_many = [_tasks()[0] | {"name": "task-%03d" % i}
                for i in range(adaptive.MAX_TASKS + 1)]
    with pytest.raises(adaptive.AdaptiveBenchmarkError, match="at most"):
        _record("fresh", too_many)


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("grounded_records", True, "non-negative integer"),
        ("grounded_records", -1, "non-negative integer"),
        ("grounded_records", adaptive.MAX_COUNTER + 1, "no greater"),
        ("grounded_history_digest", "short", "64-character"),
        ("model", 7, "must be text"),
        ("model_digest", 7, "64-character"),
    ],
)
def test_invalid_checkpoint_evidence_is_rejected(field, value, match):
    with pytest.raises(adaptive.AdaptiveBenchmarkError, match=match):
        _record("accumulated", **{field: value})


def test_cli_creates_records_and_report_without_a_model(tmp_path, capsys):
    fresh_tasks = tmp_path / "fresh-tasks.json"
    warm_tasks = tmp_path / "warm-tasks.json"
    fresh_tasks.write_text(json.dumps(_tasks()), encoding="utf-8")
    warm_tasks.write_text(json.dumps(_tasks(solve_second=True)), encoding="utf-8")
    fresh_path = tmp_path / "fresh.json"
    warm_path = tmp_path / "warm.json"

    common = [
        "--model", "sonder:latest", "--model-digest", DIGEST_A,
        "--suite", "adaptive-core", "--suite-version", "1",
        "--suite-digest", DIGEST_B, "--hardware", "host-a/gpu-a/driver-a",
        "--hardware-digest", DIGEST_A, "--source", "test-cli",
    ]
    assert adaptive.main([
        "record", *common, "--checkpoint", "fresh",
        "--checkpoint-label", "clean", "--grounded-records", "0",
        "--grounded-history-digest", EMPTY_DIGEST,
        "--tasks", str(fresh_tasks), "--output", str(fresh_path),
    ]) == 0
    capsys.readouterr()
    assert adaptive.main([
        "record", *common, "--checkpoint", "accumulated",
        "--checkpoint-label", "after-12", "--grounded-records", "12",
        "--grounded-history-digest", HISTORY_DIGEST,
        "--tasks", str(warm_tasks), "--output", str(warm_path),
    ]) == 0
    capsys.readouterr()

    json_path = tmp_path / "report.json"
    markdown_path = tmp_path / "report.md"
    assert adaptive.main([
        "compare", "--fresh", str(fresh_path),
        "--accumulated", str(warm_path), "--json", str(json_path),
        "--markdown", str(markdown_path),
    ]) == 0
    stdout = capsys.readouterr().out
    assert "Adaptive improvement checkpoint comparison" in stdout
    assert json.loads(json_path.read_text(encoding="utf-8"))["model_free"] is True
    assert "Regressed tasks: none" in markdown_path.read_text(encoding="utf-8")

    fresh_before = fresh_path.read_bytes()
    assert adaptive.main([
        "compare", "--fresh", str(fresh_path),
        "--accumulated", str(warm_path), "--json", str(fresh_path),
    ]) == 2
    assert "must differ" in capsys.readouterr().err
    assert fresh_path.read_bytes() == fresh_before


def test_path_collisions_are_rejected_without_overwriting_evidence(tmp_path, capsys):
    tasks_path = tmp_path / "tasks.json"
    original = json.dumps(_tasks())
    tasks_path.write_text(original, encoding="utf-8")
    result = adaptive.main([
        "record", "--model", "sonder:latest", "--model-digest", DIGEST_A,
        "--suite", "adaptive-core", "--suite-version", "1",
        "--suite-digest", DIGEST_B, "--hardware", "host-a",
        "--hardware-digest", DIGEST_A, "--checkpoint", "fresh",
        "--checkpoint-label", "clean", "--grounded-records", "0",
        "--grounded-history-digest", EMPTY_DIGEST,
        "--tasks", str(tasks_path), "--output", str(tasks_path),
    ])
    assert result == 2
    assert "must differ" in capsys.readouterr().err
    assert tasks_path.read_text(encoding="utf-8") == original


def test_atomic_output_failure_preserves_existing_file(monkeypatch, tmp_path):
    output = tmp_path / "report.json"
    output.write_bytes(b"prior evidence\n")

    def fail_replace(_source, _destination):
        raise OSError("replace blocked")

    monkeypatch.setattr(adaptive.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace blocked"):
        adaptive._write_json(output, {"replacement": True})
    assert output.read_bytes() == b"prior evidence\n"
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize(
    "payload",
    [
        "[" * 2000 + "]" * 2000,
        '{"number":' + "9" * 5000 + "}",
    ],
)
def test_pathological_json_is_reported_as_bounded_input_error(tmp_path, payload):
    path = tmp_path / "bad.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(
        adaptive.AdaptiveBenchmarkError,
        match="cannot read|unsupported or missing",
    ):
        adaptive.load_record(path)


def test_oversize_json_is_rejected_before_parsing(tmp_path):
    path = tmp_path / "huge.json"
    path.write_bytes(b" " * (adaptive.MAX_JSON_BYTES + 1))
    with pytest.raises(adaptive.AdaptiveBenchmarkError, match="byte ceiling"):
        adaptive.load_record(path)
