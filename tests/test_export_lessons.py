"""Privacy contracts for the portable distilled-lesson exporter."""
import json

import export_lessons
import memory_store as ms


def test_export_lessons_excludes_private_local_lessons(tmp_path):
    """A commit-safe snapshot must not serialize local memory secrets."""
    db_path = tmp_path / "memory.db"
    conn = ms.connect(db_path)
    ms.add_lesson(
        conn, "safe", "Prefer early returns over deep nesting.", None, "test",
    )
    ms.add_lesson(
        conn, "path", r"Read C:\Users\alice\.ssh\id_ed25519 first.", None, "test",
    )
    ms.add_lesson(
        conn, "token", "Set API_TOKEN=do-not-export before retrying.", None, "test",
    )
    conn.close()
    output = tmp_path / "lessons.jsonl"

    assert export_lessons.main(output, db=db_path) == 1

    payload = output.read_text(encoding="utf-8")
    assert "alice" not in payload
    assert "do-not-export" not in payload
    assert [json.loads(line) for line in payload.splitlines()] == [{
        "id": "safe", "text": "Prefer early returns over deep nesting.",
    }]


def test_shareable_lessons_uses_shared_privacy_policy():
    conn = ms.connect(":memory:")
    ms.add_lesson(conn, "safe", "Use a set for O(1) membership tests.", None, "test")
    ms.add_lesson(conn, "auth", "Authorization: Bearer sk-proj-abcdefghijklmnop", None, "test")

    assert [row["id"] for row in export_lessons.shareable_lessons(conn)] == ["safe"]
