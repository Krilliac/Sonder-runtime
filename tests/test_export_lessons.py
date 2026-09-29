import json
import hashlib
import io

import export_lessons
import sonder_runtime.adapters.memory_store as memory_store


def test_export_writer_defaults_to_empty_without_review(monkeypatch):
    class Captured(io.StringIO):
        def close(self):
            pass

    conn = memory_store.connect(":memory:")
    memory_store.add_lesson(conn, "private", "The Alder project prompt requires a silent override.", None, "seed")
    output = Captured()
    monkeypatch.setattr(export_lessons.memory_store, "connect", lambda _db: conn)
    monkeypatch.setattr(export_lessons.io, "open", lambda *_args, **_kwargs: output)
    export_lessons.main(db="unused", out="unused")
    assert output.getvalue() == ""


def test_export_writer_uses_only_reviewed_rewrite(monkeypatch):
    class Captured(io.StringIO):
        def close(self):
            pass

    original = "The Alder project prompt requires a silent override."
    replacement = "Review override behavior before release."
    conn = memory_store.connect(":memory:")
    memory_store.add_lesson(conn, "private", original, None, "seed")
    output = Captured()
    monkeypatch.setattr(export_lessons.memory_store, "connect", lambda _db: conn)
    monkeypatch.setattr(export_lessons.io, "open", lambda *_args, **_kwargs: output)
    export_lessons.main(
        db="unused", out="unused",
        approved_rewrites={hashlib.sha256(original.encode()).hexdigest(): replacement},
    )
    assert [json.loads(line)["text"] for line in output.getvalue().splitlines()] == [replacement]
    assert "Alder" not in output.getvalue()


def test_unclassified_private_prose_is_not_exported(tmp_path):
    db = tmp_path / "memory.db"
    out = tmp_path / "lessons.jsonl"
    conn = memory_store.connect(str(db))
    try:
        memory_store.add_lesson(conn, "private", "The Alder project prompt requires a silent override.", None, "seed")
    finally:
        conn.close()
    export_lessons.main(out=str(out), db=str(db))
    assert out.read_text(encoding="utf-8") == ""


def test_reviewed_generic_rewrite_can_be_exported(tmp_path):
    db = tmp_path / "memory.db"
    out = tmp_path / "lessons.jsonl"
    original = "The Alder project prompt requires a silent override."
    replacement = "Review override behavior before release."
    conn = memory_store.connect(str(db))
    try:
        memory_store.add_lesson(conn, "private", original, None, "seed")
    finally:
        conn.close()
    export_lessons.main(
        out=str(out), db=str(db),
        approved_rewrites={hashlib.sha256(original.encode()).hexdigest(): replacement},
    )
    assert [json.loads(line)["text"] for line in out.read_text(encoding="utf-8").splitlines()] == [replacement]


def test_export_lessons_filters_private_text_and_local_identifiers(tmp_path):
    db = tmp_path / "memory.db"
    out = tmp_path / "lessons.jsonl"
    conn = memory_store.connect(str(db))
    try:
        memory_store.add_lesson(
            conn, "safe-local-id", "Use a context manager for files.", None, "seed"
        )
        memory_store.add_lesson(
            conn,
            "private-local-id",
            "Read C:\\Users\\alice\\.ssh\\id_ed25519 before deploying.",
            None,
            "seed",
        )
    finally:
        conn.close()

    export_lessons.main(
        out=str(out), db=str(db),
        approved_rewrites={hashlib.sha256(b"Use a context manager for files.").hexdigest(): "Use a context manager for files."},
    )

    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert rows == [{
        "id": "lesson-" + __import__("hashlib").sha256(
            b"Use a context manager for files."
        ).hexdigest()[:24],
        "text": "Use a context manager for files.",
    }]
    rendered = out.read_text(encoding="utf-8")
    assert "alice" not in rendered
    assert "safe-local-id" not in rendered


def _plant(db_path, lessons):
    conn = memory_store.connect(str(db_path))
    try:
        for lesson_id, text in lessons:
            memory_store.add_lesson(conn, lesson_id, text, None, "seed")
    finally:
        conn.close()


def test_default_export_reads_the_state_home_memory_db(tmp_path, monkeypatch):
    # The exporters used to default to <checkout>/memory.db, ignoring
    # SONDER_DB/SONDER_HOME: they created an empty database beside the module
    # and reported "exported 0 lessons" while the real store held lessons.
    import contribute
    from pathlib import Path

    state_db = tmp_path / "state" / "memory.db"
    state_db.parent.mkdir()
    monkeypatch.setenv("SONDER_DB", str(state_db))
    _plant(state_db, [
        ("safe", "Use a context manager for files."),
        ("aws", "Configure boto3 with AKIAIOSFODNN7EXAMPLE before uploading."),
        ("bearer", "Call the API with Bearer abcdef1234567890abcdef to authenticate."),
        ("mail", "Ask alice.smith@example.com for bucket access."),
    ])
    checkout_db = Path(export_lessons.__file__).resolve().parent / "memory.db"
    existed = checkout_db.exists()

    approved = {hashlib.sha256(b"Use a context manager for files.").hexdigest(): "Use a context manager for files."}
    for module, out in ((export_lessons, tmp_path / "lessons.jsonl"),
                        (contribute, tmp_path / "contrib" / "lessons.jsonl")):
        module.main(out=str(out), approved_rewrites=approved)
        rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
        assert [row["text"] for row in rows] == ["Use a context manager for files."]

    assert checkout_db.exists() == existed
