"""Non-GPU tests for eval_retrieval.py: pool shape and true hold-out only.
Must never call the model -- no GPU/Ollama needed to run this file.
"""
import eval_retrieval
import training_tasks
from sonder_runtime.adapters import evaluation_history_store


def test_heldout_pool_has_at_least_ten_tasks():
    assert len(eval_retrieval.HELDOUT) >= 10


def test_heldout_is_a_true_holdout_disjoint_from_training_pool():
    training_names = {t["name"] for t in training_tasks.TASKS}
    heldout_names = {t["name"] for t in eval_retrieval.HELDOUT}
    overlap = heldout_names & training_names
    assert overlap == set(), "held-out task names must not appear in training_tasks.TASKS: %s" % overlap


def test_heldout_tasks_well_formed():
    names = set()
    for t in eval_retrieval.HELDOUT:
        assert t["name"].strip()
        assert t["prompt"].strip()
        assert t["check"].strip()
        names.add(t["name"])
    # also distinct amongst themselves
    assert len(names) == len(eval_retrieval.HELDOUT)


def test_history_is_opt_in_and_stores_only_bounded_aggregates(tmp_path, monkeypatch):
    monkeypatch.setattr(eval_retrieval, "HELDOUT", [
        {"name": "one", "prompt": "PRIVATE PROMPT", "check": "PRIVATE CHECK"},
        {"name": "two", "prompt": "PRIVATE PROMPT 2", "check": "PRIVATE CHECK 2"},
    ])
    monkeypatch.setattr(eval_retrieval.server, "resolve_sonder_model",
                        lambda _allow_cloud: "mock-model")
    monkeypatch.setattr(eval_retrieval.promotion_eval, "local_model_digest",
                        lambda model: "a" * 64)
    monkeypatch.setattr(eval_retrieval, "run_task", lambda task, **kwargs: {
        "name": task["name"], "retrieval": task["name"] == "one",
        "baseline": False, "retrieval_detail": "secret response",
        "baseline_detail": "secret response 2",
    })
    history = tmp_path / "history.jsonl"

    assert eval_retrieval.main(["eval_retrieval.py", "0", "2"]) == 0
    assert not history.exists()
    assert eval_retrieval.main([
        "eval_retrieval.py", "0", "2", "--record-history",
        "--history-path", str(history), "--run-id", "run-001",
    ]) == 0
    loaded = evaluation_history_store.load_history(history)
    assert len(loaded["records"]) == 2
    assert {(r["identity"]["suite"], r["result"]["passed"])
            for r in loaded["records"]} == {
                ("eval-retrieval:retrieval", 1),
                ("eval-retrieval:baseline", 0),
            }
    raw = history.read_text(encoding="utf-8")
    assert "PRIVATE PROMPT" not in raw
    assert "secret response" not in raw


def test_history_recording_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(eval_retrieval.server, "resolve_sonder_model",
                        lambda _allow_cloud: "mock-model")
    results = [{"retrieval": True, "baseline": False}]
    history = tmp_path / "history.jsonl"
    first = eval_retrieval._record_history(
        results, history, model="mock-model", model_digest="a" * 64,
        suite_digest="b" * 64, run_id="run-001",
    )
    second = eval_retrieval._record_history(
        results, history, model="mock-model", model_digest="a" * 64,
        suite_digest="b" * 64, run_id="run-001",
    )
    assert [r["record_id"] for r in first] == [r["record_id"] for r in second]
    assert len(evaluation_history_store.load_history(history)["records"]) == 2
    eval_retrieval._record_history(
        results, history, model="mock-model", model_digest="a" * 64,
        suite_digest="b" * 64, run_id="run-002",
    )
    assert len(evaluation_history_store.load_history(history)["records"]) == 4


def test_record_history_requires_explicit_run_id(tmp_path, monkeypatch):
    monkeypatch.setattr(eval_retrieval, "HELDOUT", [
        {"name": "one", "prompt": "p", "check": "c"},
    ])
    monkeypatch.setattr(eval_retrieval.server, "resolve_sonder_model",
                        lambda _allow_cloud: "mock-model")
    assert eval_retrieval.main([
        "eval_retrieval.py", "--record-history", "--history-path",
        str(tmp_path / "history.jsonl"),
    ]) == 2


def test_history_failure_is_reported_as_nonzero(tmp_path, monkeypatch):
    monkeypatch.setattr(eval_retrieval, "HELDOUT", [
        {"name": "one", "prompt": "p", "check": "c"},
    ])
    monkeypatch.setattr(eval_retrieval.server, "resolve_sonder_model",
                        lambda _allow_cloud: "mock-model")
    monkeypatch.setattr(eval_retrieval.promotion_eval, "local_model_digest",
                        lambda model: "a" * 64)
    monkeypatch.setattr(eval_retrieval, "run_task", lambda task, **kwargs: {
        "name": task["name"], "retrieval": True, "baseline": True,
        "retrieval_detail": "", "baseline_detail": "",
    })
    monkeypatch.setattr(evaluation_history_store, "record_result_pair_idempotent",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            OSError("disk full")))
    assert eval_retrieval.main([
        "eval_retrieval.py", "--record-history", "--history-path",
        str(tmp_path / "history.jsonl"), "--run-id", "run-001",
    ]) == 2


def test_model_digest_change_aborts_without_recording(tmp_path, monkeypatch):
    monkeypatch.setattr(eval_retrieval, "HELDOUT", [
        {"name": "one", "prompt": "p", "check": "c"},
    ])
    monkeypatch.setattr(eval_retrieval.server, "resolve_sonder_model",
                        lambda _allow_cloud: "mock-model")
    digests = iter(["a" * 64, "b" * 64])
    monkeypatch.setattr(eval_retrieval.promotion_eval, "local_model_digest",
                        lambda model: next(digests))
    monkeypatch.setattr(eval_retrieval, "run_task", eval_retrieval.run_task)
    history = tmp_path / "history.jsonl"
    assert eval_retrieval.main([
        "eval_retrieval.py", "--record-history", "--history-path", str(history),
        "--run-id", "run-001",
    ]) == 2
    assert not history.exists()
