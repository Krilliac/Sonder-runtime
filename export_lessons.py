"""Export sonder's distilled lessons from memory.db to lessons.jsonl.

The raw memory.db is a binary SQLite file (churns every interaction, and will
eventually hold interactions with private code) so it stays gitignored. This
exports just the distilled *lessons* (id + text) as diffable, shareable JSONL
that CAN live in the repo. Run: ./venv/Scripts/python.exe export_lessons.py
"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import memory_store  # noqa


def main(out="lessons.jsonl", db=None):
    db = db or os.path.join(os.path.dirname(__file__), "memory.db")
    conn = memory_store.connect(db)
    try:
        lessons = memory_store.all_lessons(conn)
    finally:
        conn.close()
    with io.open(out, "w", encoding="utf-8", newline="\n") as f:
        for l in sorted(lessons, key=lambda x: x["id"]):
            f.write(json.dumps({"id": l["id"], "text": l["text"]}, ensure_ascii=False) + "\n")
    print("exported %d lessons to %s" % (len(lessons), out))


if __name__ == "__main__":
    main()
