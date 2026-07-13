import json

import embeddings
import memory_store
import pytest
import self_heal


def test_self_heal_rebuilds_missing_fts_and_removes_orphan(tmp_path):
    db = str(tmp_path / "mem.db")
    conn = memory_store.connect(db)
    try:
        memory_store.add_lesson(conn, "L1", "good lesson text", None, "i1")
        conn.execute("DELETE FROM lessons_fts WHERE lesson_id='L1'")
        conn.execute("INSERT INTO lessons_fts(lesson_id, text) VALUES('ghost', 'ghost text')")
        conn.commit()
    finally:
        conn.close()

    issues = self_heal.check(db)
    assert {i.code for i in issues} >= {"store_missing_fts", "store_orphan_fts"}
    after, actions = self_heal.repair(db, apply=True)
    assert not any(i.code in {"store_missing_fts", "store_orphan_fts"} for i in after)
    assert any("rebuilt FTS" in a for a in actions)
    assert any("removed orphan" in a for a in actions)


@pytest.mark.parametrize(
    "bad_embedding",
    [
        pytest.param(b"bad", id="malformed"),
        pytest.param(embeddings.to_blob([float("nan"), 1.0]), id="nan"),
        pytest.param(embeddings.to_blob([0.0, -0.0]), id="zero-norm"),
    ],
)
def test_self_heal_clears_bad_embedding_and_provenance(tmp_path, bad_embedding):
    db = str(tmp_path / "mem.db")
    conn = memory_store.connect(db)
    try:
        memory_store.add_lesson(conn, "L1", "lesson", None, "i1")
        conn.execute(
            "UPDATE lessons SET embedding=?, embedding_model=?, "
            "embedding_revision=?, embedding_dim=? WHERE id='L1'",
            (bad_embedding, "stale-model", "stale-revision", 999),
        )
        conn.commit()
    finally:
        conn.close()
    dry_after, dry_actions = self_heal.repair(db, apply="false")
    assert any(i.code == "store_bad_embedding" for i in dry_after)
    assert dry_actions == ["dry run: no repairs applied"]
    after, actions = self_heal.repair(db, apply=True)
    assert not any(i.code == "store_bad_embedding" for i in after)
    assert any("cleared bad embedding" in a for a in actions)
    conn = memory_store.connect(db)
    try:
        row = conn.execute(
            "SELECT embedding, embedding_model, embedding_revision, embedding_dim "
            "FROM lessons WHERE id='L1'"
        ).fetchone()
        assert tuple(row) == (None, None, None, None)
    finally:
        conn.close()


def test_self_heal_repairs_invalid_json_configs(monkeypatch, tmp_path):
    import emotion_vectors
    import workflow_store

    monkeypatch.setattr(emotion_vectors, "workspace_root", lambda: str(tmp_path))
    monkeypatch.setattr(workflow_store, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("SONDER_EMOTION_VECTORS", raising=False)
    monkeypatch.delenv("SONDER_WORKFLOWS", raising=False)
    (tmp_path / "emotion_vectors.json").write_text("{bad", encoding="utf-8")
    (tmp_path / "workflows.json").write_text("{bad", encoding="utf-8")
    db = str(tmp_path / "mem.db")

    issues = self_heal.check(db)
    assert "emotion_vectors_invalid" in {i.code for i in issues}
    assert "workflows_invalid" in {i.code for i in issues}
    after, actions = self_heal.repair(db, apply=True)
    assert not any(i.code.endswith("_invalid") for i in after)
    assert json.loads((tmp_path / "emotion_vectors.json").read_text(encoding="utf-8"))
    assert json.loads((tmp_path / "workflows.json").read_text(encoding="utf-8"))


def test_server_self_heal_tools(monkeypatch, tmp_path):
    import server

    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    out = server.self_heal_check()
    assert out.startswith("self-heal check:")
    dry = server.self_heal_repair(apply=False)
    assert "dry run" in dry
