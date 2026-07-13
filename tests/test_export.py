import hashlib
import json
from pathlib import Path

import pytest

import memory_store as ms
import export_training_data as etd


def _conn():
    return ms.connect(":memory:")


def test_build_examples_returns_good_pairs_in_chat_shape():
    c = _conn()
    ms.log_interaction(c, "a", "task A", "", "resp A", "code")
    ms.log_interaction(c, "b", "task B", "", "resp B", "code")
    ms.log_interaction(c, "bad", "task bad", "", "resp bad", "code")
    ms.record_outcome_row(c, "a", "tests_passed", 1.0)
    ms.record_outcome_row(c, "b", "compiled", 0.7)
    ms.record_outcome_row(c, "bad", "failed", -1.0)

    examples = etd.build_examples(c)

    assert len(examples) == 2
    tasks = {ex["messages"][0]["content"] for ex in examples}
    assert tasks == {"task A", "task B"}
    for ex in examples:
        assert ex["messages"][0]["role"] == "user"
        assert ex["messages"][1]["role"] == "assistant"
        # response content matches the task's response
        if ex["messages"][0]["content"] == "task A":
            assert ex["messages"][1]["content"] == "resp A"
        else:
            assert ex["messages"][1]["content"] == "resp B"


def test_build_examples_dedups_repeated_task():
    c = _conn()
    ms.log_interaction(c, "a", "same task", "", "resp 1", "code")
    ms.log_interaction(c, "b", "same task", "", "resp 2", "code")
    ms.record_outcome_row(c, "a", "tests_passed", 1.0)
    ms.record_outcome_row(c, "b", "tests_passed", 1.0)

    examples = etd.build_examples(c)

    assert len(examples) == 1


def test_build_examples_vetoes_interaction_with_any_bad_or_unknown_outcome():
    c = _conn()
    ms.log_interaction(c, "failed-later", "task A", "", "unsafe A", "code")
    ms.log_interaction(c, "unknown", "task B", "", "unsafe B", "code")
    ms.log_interaction(c, "recovered", "task C", "", "still conflicted", "code")
    for interaction_id in ("failed-later", "unknown", "recovered"):
        ms.record_outcome_row(c, interaction_id, "tests_passed", 1.0)
    ms.record_outcome_row(c, "failed-later", "failed", -1.0)
    ms.record_outcome_row(c, "unknown", "future_signal", 99.0)
    ms.record_outcome_row(c, "recovered", "failed", -1.0)
    ms.record_outcome_row(c, "recovered", "tests_passed", 1.0)

    assert etd.build_examples(c) == []


def test_build_examples_normalizes_prompts_and_selects_strongest_then_newest():
    c = _conn()
    ms.log_interaction(c, "strong", "  SAME\n task ", "", "strong response", "code")
    ms.log_interaction(c, "newer-weak", "same task", "", "newer weak response", "code")
    ms.log_interaction(c, "old-tie", "other task", "", "old tie", "code")
    ms.log_interaction(c, "new-tie", "OTHER   TASK", "", "new tie", "code")
    ms.record_outcome_row(c, "strong", "tests_passed", 1.0)
    ms.record_outcome_row(c, "newer-weak", "compiled", 0.7)
    ms.record_outcome_row(c, "old-tie", "tests_passed", 1.0)
    ms.record_outcome_row(c, "new-tie", "tests_passed", 1.0)

    examples = etd.build_examples(c)
    responses = [row["messages"][1]["content"] for row in examples]

    assert responses == ["new tie", "strong response"]


def test_export_excludes_private_content_and_writes_non_sensitive_manifest(tmp_path):
    db_path = tmp_path / "memory.db"
    conn = ms.connect(db_path)
    ms.log_interaction(conn, "safe", "safe task", "", "safe response", "code")
    ms.log_interaction(
        conn, "private", "read C:\\Users\\alice\\private.txt", "",
        "token=do-not-export", "code",
    )
    ms.record_outcome_row(conn, "safe", "tests_passed", 1.0)
    ms.record_outcome_row(conn, "private", "tests_passed", 1.0)
    conn.close()
    out = tmp_path / "training.jsonl"

    assert etd.main(out, db_path=db_path) == 1

    payload = out.read_bytes()
    manifest = json.loads((tmp_path / "training.jsonl.manifest.json").read_text())
    assert b"alice" not in payload and b"do-not-export" not in payload
    assert "alice" not in repr(manifest) and "do-not-export" not in repr(manifest)
    assert manifest["accepted"] == 1
    assert manifest["rejected_by_reason"]["privacy"] == 1
    assert manifest["rejected_by_reason"]["privacy.windows_path"] == 1
    assert manifest["rejected_by_reason"]["privacy.credential_assignment"] == 1
    assert manifest["sha256"] == hashlib.sha256(payload).hexdigest()


def test_export_rejects_bearer_tokens_that_are_not_assignments():
    conn = _conn()
    secret = "sk-proj-abcdefghijklmnop"
    ms.log_interaction(
        conn, "bearer", "call endpoint", "",
        "Use Authorization: Bearer %s" % secret, "code",
    )
    ms.record_outcome_row(conn, "bearer", "tests_passed", 1.0)

    examples, stats = etd._select_examples(conn)

    assert examples == []
    assert stats["rejected_by_reason"]["privacy.authorization_header"] == 1
    assert secret not in repr(stats)


def test_export_rejects_records_outside_shared_training_bounds(monkeypatch):
    conn = _conn()
    ms.log_interaction(
        conn, "large-field", "large field", "",
        "word " * (etd.MAX_TRAINING_FIELD_CHARS // 5 + 2), "code",
    )
    ms.log_interaction(conn, "first", "first task", "", "12345", "code")
    ms.log_interaction(conn, "second", "second task", "", "67890", "code")
    for interaction_id in ("large-field", "first", "second"):
        ms.record_outcome_row(conn, interaction_id, "tests_passed", 1.0)
    monkeypatch.setattr(etd, "MAX_TRAINING_TOTAL_CHARS", 16)

    examples, stats = etd._select_examples(conn)

    assert len(examples) == 1
    assert stats["rejected_by_reason"]["field_too_large"] == 1
    assert stats["rejected_by_reason"]["content_size_limit"] == 1


def test_export_stream_fails_closed_at_outcome_evidence_limit(monkeypatch):
    conn = _conn()
    ms.log_interaction(conn, "one", "task", "", "response", "code")
    ms.record_outcome_row(conn, "one", "compiled", 0.7)
    ms.record_outcome_row(conn, "one", "tests_passed", 1.0)
    monkeypatch.setattr(etd, "MAX_EXPORT_EVIDENCE_ROWS", 1)

    with pytest.raises(etd.training_data.TrainingDataError, match="evidence limit"):
        etd._select_examples(conn)


def test_export_capacity_keeps_highest_quality_candidate(monkeypatch):
    conn = _conn()
    ms.log_interaction(conn, "weak", "weak task", "", "weak response", "code")
    ms.log_interaction(conn, "strong", "strong task", "", "strong response", "code")
    ms.record_outcome_row(conn, "weak", "compiled", 0.7)
    ms.record_outcome_row(conn, "strong", "tests_passed", 1.0)
    monkeypatch.setattr(etd, "MAX_TRAINING_EXAMPLES", 1)

    examples, stats = etd._select_examples(conn)

    assert [row["messages"][1]["content"] for row in examples] == [
        "strong response"
    ]
    assert stats["rejected_by_reason"]["selection_capacity"] == 1


def test_manifest_failure_never_leaves_a_stale_manifest_for_new_data(
    monkeypatch, tmp_path,
):
    db_path = tmp_path / "memory.db"
    conn = ms.connect(db_path)
    ms.log_interaction(conn, "safe", "safe task", "", "safe response", "code")
    ms.record_outcome_row(conn, "safe", "tests_passed", 1.0)
    conn.close()
    out = tmp_path / "training.jsonl"
    manifest = tmp_path / "selection.json"
    out.write_bytes(b"old-data\n")
    manifest.write_text('{"sha256":"stale"}\n', encoding="utf-8")
    real_atomic_write = etd._atomic_write

    def fail_manifest(path, payload):
        if Path(path) == manifest:
            raise OSError("simulated manifest failure")
        return real_atomic_write(path, payload)

    monkeypatch.setattr(etd, "_atomic_write", fail_manifest)
    with pytest.raises(OSError, match="manifest failure"):
        etd.main(out, db_path=db_path, manifest_path=manifest)

    assert out.read_bytes() != b"old-data\n"
    assert not manifest.exists()


def test_atomic_write_preserves_existing_output_when_replace_fails(monkeypatch, tmp_path):
    destination = tmp_path / "training.jsonl"
    destination.write_bytes(b"trusted-old-data\n")

    def fail_replace(_source, _destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(etd.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        etd._atomic_write(destination, b"uncommitted-new-data\n")

    assert destination.read_bytes() == b"trusted-old-data\n"
    assert list(tmp_path.glob("training.jsonl.tmp-*")) == []
