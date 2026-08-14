"""Export sonder's distilled lessons from memory.db to lessons.jsonl.

The raw memory.db is a binary SQLite file (churns every interaction, and will
eventually hold interactions with private code) so it stays gitignored. This
exports just the distilled *lessons* (id + text) as diffable, shareable JSONL
that CAN live in the repo. Run: python export_lessons.py
"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import contribute
import sonder_runtime.adapters.memory_store as memory_store  # noqa


def shareable_lessons(conn):
    """Return only lessons safe to write into a portable JSONL snapshot.

    ``memory.db`` is a local working store and can contain lessons distilled
    from private interactions.  This exporter advertises its output as safe to
    commit, so it must apply the same conservative privacy policy as the
    explicit community outbox rather than serializing every local lesson.
    """
    return [
        lesson for lesson in memory_store.all_lessons(conn)
        if contribute.is_shareable(lesson.get("text"))
    ]


def main(out="lessons.jsonl", db=None):
    db = db or os.path.join(os.path.dirname(__file__), "memory.db")
    conn = memory_store.connect(db)
    try:
        lessons = shareable_lessons(conn)
    finally:
        conn.close()
    with io.open(out, "w", encoding="utf-8", newline="\n") as f:
        for l in sorted(lessons, key=lambda x: x["id"]):
            f.write(json.dumps({"id": l["id"], "text": l["text"]}, ensure_ascii=False) + "\n")
    print("exported %d shareable lessons to %s" % (len(lessons), out))
    return len(lessons)


if __name__ == "__main__":
    main()
