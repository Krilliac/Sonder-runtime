import embeddings
import memory_store
import server


def _seed(monkeypatch, tmp_path):
    db_path = tmp_path / "maintenance.db"
    monkeypatch.setattr(server, "_DB_PATH", str(db_path))
    monkeypatch.setattr(server.embeddings, "EXPECTED_DIMENSION", 2)
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

    def fake_embed(text, timeout=30, **kwargs):
        calls.append((text, timeout))
        return [0.25, 0.75]

    monkeypatch.setattr(server.embeddings, "embed", fake_embed)

    dry = server.memory_embedding_backfill(limit=1, apply=False)
    applied = server.memory_embedding_backfill(limit=1, apply=True)

    assert "mode: dry-run" in dry
    assert calls and len(calls) == 2  # live compatibility probe + one refresh
    assert "updated: 1" in applied
    conn = memory_store.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT embedding, embedding_model, embedding_dim FROM lessons "
            "ORDER BY ts ASC, rowid ASC LIMIT 1"
        ).fetchone()
        assert embeddings.from_blob(row[0]) == [0.25, 0.75]
        assert row[1] == server.embeddings.EMBED_IDENTITY
        assert row[2] == 2
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


def test_embedding_backfill_refuses_non_loopback_ollama(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    monkeypatch.setattr(server.embeddings, "BASE", "http://example.com:11434")
    calls = []
    monkeypatch.setattr(
        server.embeddings, "embed", lambda *args, **kwargs: calls.append(args),
    )
    network_calls = []
    monkeypatch.setattr(
        server.embeddings.urllib.request,
        "urlopen",
        lambda *args, **kwargs: network_calls.append(args),
    )

    result = server.memory_embedding_backfill(limit=1, apply=True)

    assert result.startswith("ERROR: embedding refresh is local-only")
    assert calls == []
    assert network_calls == []


def test_embedding_backfill_dry_run_uses_known_target_dimension(
    monkeypatch, tmp_path,
):
    db_path = tmp_path / "dimensions.db"
    monkeypatch.setattr(server, "_DB_PATH", str(db_path))
    monkeypatch.setattr(server.embeddings, "EXPECTED_DIMENSION", 3)
    conn = memory_store.connect(str(db_path))
    memory_store.add_lesson(
        conn, "wrong-dimension", "text", embeddings.to_blob([1.0, 0.0]), "seed",
        embedding_model=server.embeddings.EMBED_IDENTITY,
        embedding_revision=server.embeddings.EMBED_REVISION,
        embedding_dim=2,
    )
    conn.close()

    result = server.memory_embedding_backfill(limit=10, apply=False)

    assert "target dimension: 3" in result
    assert "selected stale/missing: 1" in result
    assert "wrong-dimension" in result


def test_embedding_backfill_refuses_probe_dimension_drift_before_row_text(
    monkeypatch, tmp_path,
):
    _seed(monkeypatch, tmp_path)
    monkeypatch.setattr(server.embeddings, "EXPECTED_DIMENSION", 3)
    calls = []

    def fake_embed(text, timeout=30, **kwargs):
        calls.append(text)
        return [0.25, 0.75]

    monkeypatch.setattr(server.embeddings, "embed", fake_embed)

    result = server.memory_embedding_backfill(limit=1, apply=True)

    assert result.startswith("ERROR: local embedding probe dimension 2")
    assert len(calls) == 1
    assert "hidden-value" not in calls[0]


def test_embedding_backfill_skips_a_concurrently_changed_lesson(
    monkeypatch, tmp_path,
):
    db_path = _seed(monkeypatch, tmp_path)
    calls = []

    def fake_embed(text, timeout=30, **kwargs):
        calls.append(text)
        if len(calls) == 2:
            concurrent = memory_store.connect(str(db_path))
            memory_store.refresh_lesson_embedding(
                concurrent, "private", embeddings.to_blob([0.9, 0.1]),
                server.embeddings.EMBED_IDENTITY, revision="concurrent", dimension=2,
            )
            concurrent.close()
        return [0.25, 0.75]

    monkeypatch.setattr(server.embeddings, "embed", fake_embed)

    result = server.memory_embedding_backfill(limit=1, apply=True)

    assert "updated: 0" in result
    assert "conflicted: 1" in result
    assert "skipped concurrently changed IDs: private" in result
    conn = memory_store.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT embedding_revision FROM lessons WHERE id='private'"
        ).fetchone()
        assert row[0] == "concurrent"
    finally:
        conn.close()


def test_lesson_backfill_uses_immutable_local_runtime_snapshot(
    monkeypatch, tmp_path,
):
    _seed(monkeypatch, tmp_path)
    approved_base = server.embeddings.BASE
    approved_model = server.embeddings.EMBED_MODEL
    calls = []

    def fake_embed(text, timeout=30, **kwargs):
        calls.append((text, kwargs.get("base"), kwargs.get("model")))
        if len(calls) == 1:
            monkeypatch.setattr(server.embeddings, "BASE", "https://example.com:11434")
            monkeypatch.setattr(server.embeddings, "EMBED_MODEL", "remote:cloud")
        return [0.25, 0.75]

    monkeypatch.setattr(server.embeddings, "embed", fake_embed)

    result = server.memory_embedding_backfill(limit=1, apply=True)

    assert "updated: 1" in result
    assert len(calls) == 2
    assert calls[1][1:] == (approved_base, approved_model)


def test_embedding_refresh_selection_checks_actual_vector_integrity():
    conn = memory_store.connect(":memory:")
    try:
        for lesson_id in (
            "valid", "malformed", "nonfinite", "zero", "dim-mismatch",
        ):
            memory_store.add_lesson(
                conn,
                lesson_id,
                "Embedding refresh candidate %s." % lesson_id,
                embeddings.to_blob([1.0, 0.0]),
                "seed:maintenance:test",
                embedding_model=embeddings.EMBED_IDENTITY,
                embedding_revision=embeddings.EMBED_REVISION,
                embedding_dim=2,
            )
        conn.execute(
            "UPDATE lessons SET embedding=? WHERE id='malformed'",
            (b"\x00" * 6,),
        )
        conn.execute(
            "UPDATE lessons SET embedding=? WHERE id='nonfinite'",
            (embeddings.to_blob([float("nan"), 0.0]),),
        )
        conn.execute(
            "UPDATE lessons SET embedding=? WHERE id='zero'",
            (embeddings.to_blob([0.0, 0.0]),),
        )
        conn.execute(
            "UPDATE lessons SET embedding=? WHERE id='dim-mismatch'",
            (embeddings.to_blob([1.0, 0.0, 0.0]),),
        )
        conn.commit()

        rows = memory_store.lessons_needing_embedding_refresh(
            conn,
            embeddings.EMBED_IDENTITY,
            revision=embeddings.EMBED_REVISION,
            dimension=2,
            limit=10,
        )
        count = memory_store.count_lessons_needing_embedding_refresh(
            conn,
            embeddings.EMBED_IDENTITY,
            revision=embeddings.EMBED_REVISION,
            dimension=2,
        )
    finally:
        conn.close()

    assert [row["id"] for row in rows] == [
        "malformed",
        "nonfinite",
        "zero",
        "dim-mismatch",
    ]
    assert count == 4


def _seed_interaction_embeddings(monkeypatch, tmp_path):
    db_path = tmp_path / "interaction-maintenance.db"
    monkeypatch.setattr(server, "_DB_PATH", str(db_path))
    monkeypatch.setattr(server.embeddings, "EXPECTED_DIMENSION", 2)
    conn = memory_store.connect(str(db_path))
    memory_store.log_interaction(
        conn,
        "interaction-private",
        "private task text that must not appear in maintenance output",
        "",
        "answer",
        "code",
        task_embedding=None,
    )
    memory_store.log_interaction(
        conn,
        "interaction-current",
        "already current task",
        "",
        "answer",
        "code",
        task_embedding=embeddings.to_blob([1.0, 0.0]),
        task_embedding_model=server.embeddings.EMBED_IDENTITY,
        task_embedding_revision=server.embeddings.EMBED_REVISION,
        task_embedding_dim=2,
    )
    conn.close()
    return db_path


def test_interaction_embedding_backfill_is_bounded_private_and_local(
    monkeypatch, tmp_path,
):
    db_path = _seed_interaction_embeddings(monkeypatch, tmp_path)
    monkeypatch.setattr(server.embeddings, "EXPECTED_DIMENSION", 2)
    calls = []

    def fake_embed(text, timeout=30, **kwargs):
        calls.append((text, timeout))
        return [0.25, 0.75]

    monkeypatch.setattr(server.embeddings, "embed", fake_embed)

    dry = server.memory_interaction_embedding_backfill(limit=1, apply=False)
    looped = server._loop_dispatch({
        "type": "memory_interaction_embedding_backfill",
        "limit": 1,
        "apply": False,
    })
    applied = server.memory_interaction_embedding_backfill(limit=1, apply=True)

    assert "mode: dry-run" in dry
    assert looped["ok"] is True
    assert looped["type"] == "memory_interaction_embedding_backfill"
    assert "target dimension: 2" in dry
    assert "interaction IDs: interaction-private" in dry
    assert "private task text" not in dry
    assert calls and len(calls) == 2  # compatibility probe + selected task
    assert "updated: 1" in applied
    assert "private task text" not in applied
    conn = memory_store.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT task_embedding, task_embedding_model, "
            "task_embedding_revision, task_embedding_dim FROM interactions "
            "WHERE id='interaction-private'"
        ).fetchone()
        assert embeddings.from_blob(row["task_embedding"]) == [0.25, 0.75]
        assert row["task_embedding_model"] == server.embeddings.EMBED_IDENTITY
        assert row["task_embedding_revision"] == (
            server.embeddings.EMBED_REVISION or None
        )
        assert row["task_embedding_dim"] == 2
    finally:
        conn.close()


def test_interaction_embedding_backfill_refuses_remote_endpoint_before_calls(
    monkeypatch, tmp_path,
):
    _seed_interaction_embeddings(monkeypatch, tmp_path)
    monkeypatch.setattr(server.embeddings, "BASE", "https://example.com:11434")
    calls = []
    monkeypatch.setattr(
        server.embeddings, "embed", lambda *args, **kwargs: calls.append(args),
    )
    network_calls = []
    monkeypatch.setattr(
        server.embeddings.urllib.request,
        "urlopen",
        lambda *args, **kwargs: network_calls.append(args),
    )

    result = server.memory_interaction_embedding_backfill(limit=1, apply=True)

    assert result.startswith("ERROR: interaction embedding refresh is local-only")
    assert calls == []
    assert network_calls == []


def test_interaction_embedding_backfill_refuses_cloud_model_before_calls(
    monkeypatch, tmp_path,
):
    _seed_interaction_embeddings(monkeypatch, tmp_path)
    monkeypatch.setattr(server.embeddings, "EMBED_MODEL", "embedder:cloud")
    calls = []
    monkeypatch.setattr(
        server.embeddings, "embed", lambda *args, **kwargs: calls.append(args),
    )

    result = server.memory_interaction_embedding_backfill(limit=1, apply=True)

    assert result.startswith(
        "ERROR: interaction embedding backfill requires a local model"
    )
    assert calls == []


def test_interaction_embedding_backfill_refuses_probe_dimension_drift(
    monkeypatch, tmp_path,
):
    _seed_interaction_embeddings(monkeypatch, tmp_path)
    monkeypatch.setattr(server.embeddings, "EXPECTED_DIMENSION", 3)
    calls = []

    def fake_embed(text, timeout=30, **kwargs):
        calls.append(text)
        return [0.25, 0.75]

    monkeypatch.setattr(server.embeddings, "embed", fake_embed)

    result = server.memory_interaction_embedding_backfill(limit=1, apply=True)

    assert result.startswith(
        "ERROR: local interaction embedding probe dimension 2"
    )
    assert len(calls) == 1
    assert "private task text" not in calls[0]


def test_interaction_embedding_backfill_reports_compare_and_swap_conflict(
    monkeypatch, tmp_path,
):
    db_path = _seed_interaction_embeddings(monkeypatch, tmp_path)
    calls = []

    def fake_embed(text, timeout=30, **kwargs):
        calls.append(text)
        if len(calls) == 2:
            concurrent = memory_store.connect(str(db_path))
            memory_store.refresh_interaction_task_embedding(
                concurrent,
                "interaction-private",
                embeddings.to_blob([0.9, 0.1]),
                server.embeddings.EMBED_IDENTITY,
                revision="concurrent",
                dimension=2,
            )
            concurrent.close()
        return [0.25, 0.75]

    monkeypatch.setattr(server.embeddings, "embed", fake_embed)

    result = server.memory_interaction_embedding_backfill(limit=1, apply=True)

    assert "updated: 0" in result
    assert "conflicted: 1" in result
    assert "skipped concurrently changed IDs: interaction-private" in result
    conn = memory_store.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT task_embedding_revision FROM interactions "
            "WHERE id='interaction-private'"
        ).fetchone()
        assert row[0] == "concurrent"
    finally:
        conn.close()


def test_interaction_backfill_uses_immutable_local_runtime_snapshot(
    monkeypatch, tmp_path,
):
    _seed_interaction_embeddings(monkeypatch, tmp_path)
    approved_base = server.embeddings.BASE
    approved_model = server.embeddings.EMBED_MODEL
    calls = []

    def fake_embed(text, timeout=30, **kwargs):
        calls.append((text, kwargs.get("base"), kwargs.get("model")))
        if len(calls) == 1:
            monkeypatch.setattr(server.embeddings, "BASE", "https://example.com:11434")
            monkeypatch.setattr(server.embeddings, "EMBED_MODEL", "remote:cloud")
        return [0.25, 0.75]

    monkeypatch.setattr(server.embeddings, "embed", fake_embed)

    result = server.memory_interaction_embedding_backfill(limit=1, apply=True)

    assert "updated: 1" in result
    assert len(calls) == 2
    assert calls[1][1:] == (approved_base, approved_model)


def test_privacy_repair_rejects_empty_or_oversized_id_sets():
    assert server.memory_privacy_repair([], apply=False).startswith("ERROR:")
    too_many = ["id-%d" % index for index in range(51)]
    assert "at most 50" in server.memory_privacy_repair(too_many, apply=False)


def test_string_false_never_enables_mutating_maintenance(monkeypatch, tmp_path):
    db_path = _seed(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(
        server.embeddings,
        "embed",
        lambda *args, **kwargs: calls.append(args) or [1.0, 0.0],
    )

    privacy = server.memory_privacy_repair(["private"], apply="false")
    embeddings_result = server.memory_embedding_backfill(
        limit=1, apply="false",
    )

    assert "mode: dry-run" in privacy
    assert "deleted: 0" in privacy
    assert "mode: dry-run" in embeddings_result
    assert calls == []
    conn = memory_store.connect(str(db_path))
    try:
        assert memory_store.get_lesson_text(conn, "private") is not None
    finally:
        conn.close()
