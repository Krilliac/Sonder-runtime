import embeddings
import memory_store
import server


def _seed(monkeypatch, tmp_path):
    db_path = tmp_path / "maintenance.db"
    monkeypatch.setattr(server, "_DB_PATH", str(db_path))
    conn = memory_store.connect(str(db_path))
    memory_store.add_lesson(
        conn,
        "private",
        "Read C:\\Users\\alice\\private\\notes.txt with token=hidden-value",
        None,
        "seed",
    )
    memory_store.add_lesson(
        conn,
        "safe",
        "Use pathlib.Path for cross-platform path joins.",
        None,
        "seed",
    )
    conn.close()
    return db_path


def test_privacy_review_is_redacted_and_repair_is_explicit(monkeypatch, tmp_path):
    db_path = _seed(monkeypatch, tmp_path)

    review = server.memory_privacy_review(sample_limit=10)
    dry = server.memory_privacy_repair(["private", "safe"], apply=False)

    assert "private [windows_path,credential_assignment]" in review
    assert "hidden-value" not in review
    assert "eligible flagged lessons: 1" in dry
    assert "refused unflagged IDs: safe" in dry
    conn = memory_store.connect(str(db_path))
    try:
        assert memory_store.get_lesson_text(conn, "private") is not None
    finally:
        conn.close()

    applied = server.memory_privacy_repair(["private", "safe"], apply=True)
    assert "deleted: 1" in applied
    conn = memory_store.connect(str(db_path))
    try:
        assert memory_store.get_lesson_text(conn, "private") is None
        assert memory_store.get_lesson_text(conn, "safe") is not None
    finally:
        conn.close()


def test_embedding_backfill_is_local_bounded_and_dry_run_by_default(monkeypatch, tmp_path):
    db_path = _seed(monkeypatch, tmp_path)
    calls = []

    def fake_embed(text, timeout=30):
        calls.append((text, timeout))
        return [0.25, 0.75]

    monkeypatch.setattr(server.embeddings, "embed", fake_embed)

    dry = server.memory_embedding_backfill(limit=1, apply=False)
    applied = server.memory_embedding_backfill(limit=1, apply=True)

    assert "mode: dry-run" in dry
    assert calls and len(calls) == 1
    assert "updated: 1" in applied
    conn = memory_store.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT embedding FROM lessons ORDER BY ts ASC, rowid ASC LIMIT 1"
        ).fetchone()
        assert embeddings.from_blob(row[0]) == [0.25, 0.75]
    finally:
        conn.close()


def test_memory_maintenance_slash_commands(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    monkeypatch.setattr(
        server.embeddings, "embed", lambda text, timeout=30: [1.0, 0.0]
    )

    assert "memory privacy review" in server.control_command("/privacy 5")
    assert "mode: dry-run" in server.control_command("/privacyfix private")
    assert "mode: dry-run" in server.control_command("/embeddings 1")


def test_embedding_backfill_refuses_cloud_like_model(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    monkeypatch.setattr(server.embeddings, "EMBED_MODEL", "remote:cloud")

    result = server.memory_embedding_backfill(limit=1, apply=True)

    assert result.startswith("ERROR: embedding backfill requires a local model")


def test_privacy_repair_rejects_empty_or_oversized_id_sets():
    assert server.memory_privacy_repair([], apply=False).startswith("ERROR:")
    too_many = ["id-%d" % index for index in range(51)]
    assert "at most 50" in server.memory_privacy_repair(too_many, apply=False)
