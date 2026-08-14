import json

import export_lessons
import sonder_runtime.adapters.memory_store as memory_store


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

    export_lessons.main(out=str(out), db=str(db))

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
