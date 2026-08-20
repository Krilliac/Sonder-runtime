import json
import inspect
from concurrent.futures import ThreadPoolExecutor

import pytest

from sonder_runtime.adapters import evaluation_history_store as eval_history
from sonder_runtime.platform import paths as packaged_paths
import eval_models
import server
from sonder_runtime.__main__ import main as runtime_main


DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
SUITE_A = "c" * 64
SUITE_B = "d" * 64


def test_evaluation_history_uses_packaged_path_boundary():
    assert eval_history.sonder_paths is packaged_paths
    assert eval_history.default_path().parent == packaged_paths.default_home()


def _fields(**overrides):
    fields = {
        "model": "sonder:latest",
        "model_digest": DIGEST_A,
        "suite": "promotion-sql",
        "suite_version": "3",
        "suite_digest": SUITE_A,
        "passed": 4,
        "total": 5,
        "recorded_at": 1000.0,
        "source": "test",
    }
    fields.update(overrides)
    return fields


def test_record_round_trip_has_exact_identity_and_stable_ids(tmp_path):
    path = tmp_path / "history.jsonl"
    record = eval_history.record_result(path, **_fields())
    loaded = eval_history.load_history(path)
    assert loaded["records"] == [record]
    assert loaded["malformed"] == 0
    assert record["identity"] == {
        "model": "sonder:latest",
        "model_digest": DIGEST_A,
        "suite": "promotion-sql",
        "suite_version": "3",
        "suite_digest": SUITE_A,
    }
    assert len(record["identity_key"]) == 64
    assert len(record["record_id"]) == 64


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("model_digest", "short", "64-character"),
        ("suite_digest", "z" * 64, "64-character"),
        ("passed", 6, "0 <= passed"),
        ("total", 0, "total > 0"),
        ("recorded_at", float("inf"), "finite"),
    ],
)
def test_record_validation_rejects_ambiguous_evidence(field, value, match):
    with pytest.raises(eval_history.HistoryError, match=match):
        eval_history.make_record(**_fields(**{field: value}))


def test_status_never_mixes_digest_suite_or_version_identities(tmp_path):
    path = tmp_path / "history.jsonl"
    variants = [
        {},
        {"model_digest": DIGEST_B},
        {"suite_version": "4"},
        {"suite_digest": SUITE_B},
    ]
    for index, variant in enumerate(variants):
        eval_history.record_result(
            path, **_fields(recorded_at=1000 + index, **variant)
        )
    status = eval_history.history_status(path)
    assert status["matched_records"] == 4
    assert len(status["groups"]) == 4
    assert {group["identity_key"] for group in status["groups"]} == {
        record["identity_key"] for record in eval_history.load_history(path)["records"]
    }


def test_regression_is_only_against_prior_exact_identity(tmp_path):
    path = tmp_path / "history.jsonl"
    eval_history.record_result(
        path, **_fields(passed=5, total=5, recorded_at=1)
    )
    eval_history.record_result(
        path, **_fields(passed=3, total=5, recorded_at=2)
    )
    eval_history.record_result(
        path, **_fields(
            model_digest=DIGEST_B, passed=1, total=5, recorded_at=3,
        )
    )
    status = eval_history.history_status(path, model_digest=DIGEST_A)
    assert len(status["groups"]) == 1
    group = status["groups"][0]
    assert group["samples"] == 2
    assert group["prior_best_pass_rate"] == 1.0
    assert group["latest"]["pass_rate"] == 0.6
    assert group["regressed"] is True
    tolerant = eval_history.history_status(
        path, model_digest=DIGEST_A, tolerance=0.5,
    )
    assert tolerant["groups"][0]["regressed"] is False
    assert status["promotion_authority"] is False
    assert status["read_only"] is True


def test_malformed_tampered_and_truncated_jsonl_are_counted_not_raised(tmp_path):
    path = tmp_path / "history.jsonl"
    valid = eval_history.record_result(path, **_fields())
    tampered = dict(valid)
    tampered["result"] = dict(valid["result"], passed=0)
    with path.open("ab") as handle:
        handle.write(b"not-json\n")
        handle.write(json.dumps(tampered).encode("utf-8") + b"\n")
        handle.write(b'{"schema":"sonder.eval-history.v1"')
    loaded = eval_history.load_history(path)
    assert loaded["records"] == [valid]
    assert loaded["malformed"] == 3
    status = eval_history.history_status(path)
    assert status["valid_records"] == 1
    assert status["malformed_records"] == 3


def test_concurrent_writers_are_locked_and_each_record_survives(tmp_path):
    path = tmp_path / "history.jsonl"

    def write(index):
        return eval_history.record_result(
            path,
            **_fields(recorded_at=1000 + index, passed=index % 6, total=5),
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(pool.map(write, range(24)))
    loaded = eval_history.load_history(path)
    assert loaded["malformed"] == 0
    assert len(loaded["records"]) == 24
    assert {record["record_id"] for record in loaded["records"]} == {
        record["record_id"] for record in records
    }


def test_atomic_replace_failure_preserves_prior_history(monkeypatch, tmp_path):
    path = tmp_path / "history.jsonl"
    eval_history.record_result(path, **_fields())
    before = path.read_bytes()

    def fail_replace(source, destination):
        raise OSError("replace blocked")

    monkeypatch.setattr(eval_history.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace blocked"):
        eval_history.record_result(
            path, **_fields(recorded_at=2000, passed=3)
        )
    assert path.read_bytes() == before
    assert list(tmp_path.glob("*.tmp")) == []


def test_append_refuses_to_cross_history_size_ceiling(monkeypatch, tmp_path):
    path = tmp_path / "history.jsonl"
    eval_history.record_result(path, **_fields())
    before = path.read_bytes()
    monkeypatch.setattr(eval_history, "MAX_HISTORY_BYTES", len(before) + 1)
    with pytest.raises(eval_history.HistoryError, match="would exceed"):
        eval_history.record_result(
            path, **_fields(recorded_at=2000, passed=3)
        )
    assert path.read_bytes() == before


def test_bounded_reader_keeps_latest_valid_window(tmp_path):
    path = tmp_path / "history.jsonl"
    for index in range(6):
        eval_history.record_result(
            path, **_fields(recorded_at=index, passed=index % 6)
        )
    loaded = eval_history.load_history(path, max_records=2)
    assert [row["recorded_at"] for row in loaded["records"]] == [4.0, 5.0]
    assert loaded["discarded_valid"] == 4
    assert loaded["truncated"] is True


def test_cli_record_and_status_are_explicit_and_model_free(tmp_path, capsys):
    path = tmp_path / "history.jsonl"
    args = [
        "eval-history", "record", "--history", str(path),
        "--model", "sonder:latest", "--model-digest", DIGEST_A,
        "--suite", "unit", "--suite-version", "1",
        "--suite-digest", SUITE_A, "--passed", "7", "--total", "8",
        "--recorded-at", "123", "--source", "ci", "--json",
    ]
    assert runtime_main(args) == 0
    recorded = json.loads(capsys.readouterr().out)
    assert recorded["result"]["passed"] == 7

    assert runtime_main([
        "eval-history", "status", "--history", str(path), "--json",
    ]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["matched_records"] == 1
    assert status["promotion_authority"] is False


def test_cli_rejects_invalid_record_without_creating_history(tmp_path, capsys):
    path = tmp_path / "history.jsonl"
    result = runtime_main([
        "eval-history", "record", "--history", str(path),
        "--model", "x", "--model-digest", "bad",
        "--suite", "unit", "--suite-version", "1",
        "--suite-digest", SUITE_A, "--passed", "1", "--total", "1",
    ])
    assert result == 2
    assert "64-character" in capsys.readouterr().err
    assert not path.exists()


def test_mcp_status_is_read_only_advertised_and_dispatchable(monkeypatch):
    payload = {
        "schema": eval_history.SCHEMA,
        "groups": [],
        "promotion_authority": False,
    }
    calls = []

    def fake_status(**kwargs):
        calls.append(kwargs)
        return payload

    monkeypatch.setattr(server.eval_history, "history_status", fake_status)
    out = server._agent_dispatch(
        "evaluation_history_status",
        {"model": "sonder:latest", "suite": "unit"},
        read_only=True,
    )
    assert json.loads(out)["promotion_authority"] is False
    assert calls[0]["model"] == "sonder:latest"
    assert "evaluation_history_status" in server.REPOSITORY_READ_ONLY_TOOLS
    assert "evaluation_history_status" in server.tool_manifest()
    assert "- evaluation_history_status:" in server._agent_tool_help(read_only=True)


def test_mcp_status_signature_remains_legacy_compatible():
    signature = inspect.signature(server.evaluation_history_status)

    assert list(signature.parameters) == [
        "model", "model_digest", "suite", "suite_version", "suite_digest",
        "tolerance", "max_records",
    ]
    assert [parameter.default for parameter in signature.parameters.values()] == [
        "", "", "", "", "", 0.0, 10000,
    ]
    assert signature.return_annotation in (str, "str")


def test_mcp_status_maps_malformed_backend_exception_to_failed_activity(monkeypatch):
    events = []
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server, "_record_direct_tool", lambda *a, **k: events.append((a, k)))

    def fail_status(self, **_kwargs):
        raise RuntimeError("malformed backend")

    monkeypatch.setattr(
        server.eval_history_adapter.LegacyEvaluationHistoryReader,
        "status",
        fail_status,
    )

    assert server.evaluation_history_status() == "ERROR: malformed backend"
    assert events[-1][0][0] == "evaluation_history_status"
    assert events[-1][1]["ok"] is False
    assert events[-1][1]["summary"] == "malformed backend"


def test_mcp_status_treats_error_prefixed_field_as_success(monkeypatch):
    events = []
    payload = {"groups": [], "path": "ERROR: legitimate stored path"}
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server, "_record_direct_tool", lambda *a, **k: events.append((a, k)))
    monkeypatch.setattr(
        server.eval_history_adapter.LegacyEvaluationHistoryReader,
        "status",
        lambda self, **_kwargs: payload,
    )

    assert json.loads(server.evaluation_history_status()) == payload
    assert events[-1][1]["ok"] is True
    assert events[-1][1]["summary"] == "0 identity group(s)"


def test_eval_models_history_is_opt_in_and_does_not_replace_promotion_gate(
    monkeypatch, tmp_path, capsys,
):
    report = {
        "suite_version": "3", "suite_hash": SUITE_A,
        "base": {"model": "base", "score": 4, "total": 5, "tasks": []},
        "candidate": {
            "model": "candidate", "score": 5, "total": 5, "tasks": [],
        },
    }
    monkeypatch.setattr(eval_models.promotion_eval, "evaluate_pair", lambda *a, **k: report)
    monkeypatch.setattr(
        eval_models.promotion_eval, "promotion_decision", lambda *a, **k: (False, "task_regression:x"),
    )
    monkeypatch.setattr(
        eval_models.promotion_eval, "validate_model_report", lambda *a, **k: (True, "valid_report"),
    )
    digests = {"base": DIGEST_A, "candidate": DIGEST_B}
    monkeypatch.setattr(
        eval_models.promotion_eval, "local_model_digest", lambda model: digests[model],
    )
    recorded = []
    monkeypatch.setattr(
        eval_models.eval_history, "record_result",
        lambda path, **kwargs: recorded.append(kwargs) or kwargs,
    )

    assert eval_models.main(["base", "candidate"]) == 1
    assert recorded == []
    capsys.readouterr()
    assert eval_models.main([
        "base", "candidate", "--record-history",
        "--history-path", str(tmp_path / "history.jsonl"),
    ]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == "task_regression:x"
    assert payload["history"]["recorded"] == 2
    assert len(recorded) == 2


def test_eval_models_refuses_history_when_alias_digest_changes(
    monkeypatch, capsys,
):
    report = {
        "suite_version": "3", "suite_hash": SUITE_A,
        "base": {"model": "base", "score": 4, "total": 5, "tasks": []},
        "candidate": {
            "model": "candidate", "score": 5, "total": 5, "tasks": [],
        },
    }
    monkeypatch.setattr(eval_models.promotion_eval, "evaluate_pair", lambda *a, **k: report)
    monkeypatch.setattr(eval_models.promotion_eval, "promotion_decision", lambda *a, **k: (True, "ok"))
    values = iter([DIGEST_A, DIGEST_B, DIGEST_A, SUITE_B])
    monkeypatch.setattr(eval_models.promotion_eval, "local_model_digest", lambda model: next(values))
    recorded = []
    monkeypatch.setattr(
        eval_models.eval_history, "record_result",
        lambda *a, **k: recorded.append(k),
    )
    assert eval_models.main(["base", "candidate", "--record-history"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert "digest changed" in payload["history"]["error"]
    assert recorded == []
