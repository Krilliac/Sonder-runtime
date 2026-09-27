"""Export shareable distilled lessons from memory.db to lessons.jsonl.

The raw memory.db is a binary SQLite file (churns every interaction, and will
eventually hold interactions with private code) so it stays gitignored.  This
legacy convenience export is intentionally held to the same conservative
privacy boundary as ``contribute.py``: only short, generic lessons without
private markers are written.  The generated JSONL is therefore suitable for
review before committing, but it is never an export of the complete local
memory corpus. Run: python export_lessons.py
"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import contribute  # noqa
import sonder_runtime.adapters.memory_store as memory_store  # noqa
import sonder_paths  # noqa


def main(out="lessons.jsonl", db=None):
    # Same store every other tool uses (SONDER_DB/SONDER_HOME); see contribute.
    db = db or sonder_paths.memory_db_path()
    conn = memory_store.connect(db)
    try:
        # Do not create a second, weaker "safe export" policy here.  Lessons
        # can be distilled from private interactions, so reuse the explicitly
        # reviewed contribution filter (including non-identifying export IDs).
        lessons = contribute.scrubbed_lessons(conn)
    finally:
        conn.close()
    with io.open(out, "w", encoding="utf-8", newline="\n") as f:
        for l in sorted(lessons, key=lambda x: x["id"]):
            f.write(json.dumps({"id": l["id"], "text": l["text"]}, ensure_ascii=False) + "\n")
    print("exported %d lessons to %s" % (len(lessons), out))


if __name__ == "__main__":
    main()
