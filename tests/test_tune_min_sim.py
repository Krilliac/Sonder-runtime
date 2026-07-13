from pathlib import Path

import pytest

import embeddings
import memory_store
import tune_min_sim


class _Connection:
    def close(self):
        pass


def _run_and_capture_database(monkeypatch):
    opened = []
    monkeypatch.setattr(
        tune_min_sim.memory_store,
        "connect",
        lambda path: opened.append(path) or _Connection(),
    )
    monkeypatch.setattr(
        tune_min_sim,
        "top1_scores",
        lambda _conn, queries: [0.0] * len(queries),
    )

    tune_min_sim.main()

    return Path(opened[0])


def test_main_uses_shared_sonder_memory_database_by_default(monkeypatch, tmp_path):
    home = tmp_path / "state"
    monkeypatch.setenv("SONDER_HOME", str(home))
    monkeypatch.delenv("SONDER_DB", raising=False)

    assert _run_and_capture_database(monkeypatch) == home / "memory.db"


def test_main_honors_explicit_memory_database_override(monkeypatch, tmp_path):
    explicit = tmp_path / "calibration.db"
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SONDER_DB", str(explicit))

    assert _run_and_capture_database(monkeypatch) == explicit


def _add_lesson(
    conn,
    lesson_id,
    vector,
    *,
    model=embeddings.EMBED_IDENTITY,
    revision=embeddings.EMBED_REVISION,
    dimension=None,
):
    memory_store.add_lesson(
        conn,
        lesson_id,
        "Calibration lesson %s." % lesson_id,
        embeddings.to_blob(vector),
        "source-%s" % lesson_id,
        embedding_model=model,
        embedding_revision=revision,
        embedding_dim=dimension if dimension is not None else len(vector),
    )


def test_top1_scores_excludes_stale_legacy_mixed_and_non_finite_vectors():
    conn = memory_store.connect(":memory:")
    _add_lesson(conn, "current", [0.0, 1.0])
    _add_lesson(conn, "legacy", [1.0, 0.0], model=None, revision=None)
    _add_lesson(conn, "stale-model", [1.0, 0.0], model="old-model:latest")
    _add_lesson(conn, "stale-revision", [1.0, 0.0], revision="old-revision")
    _add_lesson(conn, "mixed-dimension", [1.0, 0.0, 0.0])
    _add_lesson(conn, "non-finite", [1.0, 0.0])
    conn.execute(
        "UPDATE lessons SET embedding=? WHERE id='non-finite'",
        (embeddings.to_blob([float("nan"), 0.0]),),
    )
    conn.execute(
        "INSERT INTO lessons(id, text, embedding, source_interaction, "
        "embedding_model, embedding_revision, embedding_dim) "
        "VALUES(?, ?, ?, ?, ?, ?, ?)",
        (
            "malformed",
            "Malformed calibration lesson.",
            b"x",
            "source-malformed",
            embeddings.EMBED_IDENTITY,
            embeddings.EMBED_REVISION,
            2,
        ),
    )
    conn.commit()

    assert tune_min_sim.top1_scores(
        conn,
        ["query"],
        embed_fn=lambda _query: [1.0, 0.0],
    ) == [0.0]


def test_top1_scores_fails_when_no_current_compatible_corpus_exists():
    conn = memory_store.connect(":memory:")
    _add_lesson(conn, "legacy", [1.0, 0.0], model=None, revision=None)
    _add_lesson(conn, "stale", [1.0, 0.0], model="old-model:latest")

    with pytest.raises(RuntimeError, match="no current compatible semantic corpus"):
        tune_min_sim.top1_scores(
            conn,
            ["query"],
            embed_fn=lambda _query: [1.0, 0.0],
        )


def test_top1_scores_resolves_runtime_embedding_function(monkeypatch):
    conn = memory_store.connect(":memory:")
    _add_lesson(conn, "current", [1.0, 0.0])
    calls = []
    monkeypatch.setattr(
        embeddings,
        "embed",
        lambda query: calls.append(query) or [1.0, 0.0],
    )

    assert tune_min_sim.top1_scores(conn, ["runtime query"]) == [1.0]
    assert calls == ["runtime query"]
