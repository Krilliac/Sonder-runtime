"""Merge community-contributed lessons into the local memory.db.

Community lessons never include a model or code — just distilled lesson
text aggregated (via CI, see .github/workflows/aggregate-lessons.yml) from
everyone's opt-in contributions. This script reads a local JSONL file (that
you fetched via `git pull` or copied from your file server) and merges any
lessons not already present into your local store, tagged with
source_interaction='community' so you can tell them apart from lessons you
grounded yourself.

Run: ./venv/Scripts/python.exe pull_community.py [src] [db]
"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import embeddings  # noqa: E402
import memory_store  # noqa


def merge_lessons(conn, lessons, embed_fn=None):
    existing = {
        (lesson["text"] or "").strip().lower()
        for lesson in memory_store.all_lessons(conn)
    }
    added = 0
    for lesson in lessons:
        text = lesson.get("text")
        if not text:
            continue
        key = text.strip().lower()
        if key in existing:
            continue
        result = embed_fn(text) if embed_fn else None
        vector = None
        embedding = None
        if isinstance(result, (bytes, bytearray, memoryview)):
            try:
                candidate = embeddings.from_blob(bytes(result))
            except (TypeError, ValueError, EOFError):
                candidate = None
            if embeddings.valid_vector(candidate):
                vector = candidate
                embedding = bytes(result)
        elif embeddings.valid_vector(result):
            vector = result
            embedding = embeddings.to_blob(vector)
        provenance = (
            embeddings.provenance(vector)
            if vector is not None and embed_fn is embeddings.embed else {}
        )
        memory_store.add_lesson(
            conn, memory_store.new_id(), text, embedding, "community",
            embedding_model=provenance.get("model"),
            embedding_revision=provenance.get("revision"),
            embedding_dim=provenance.get("dimension"),
        )
        existing.add(key)
        added += 1
    return added


def main(src="community_lessons.jsonl", db=None):
    db = db or os.path.join(os.path.dirname(__file__), "memory.db")
    lessons = []
    if os.path.isfile(src):
        with io.open(src, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                lessons.append(json.loads(line))
    else:
        print("no community lessons file found at %s (nothing to merge)" % src)

    conn = memory_store.connect(db)
    try:
        added = merge_lessons(conn, lessons)
    finally:
        conn.close()

    print("added %d new community lessons to %s" % (added, db))


if __name__ == "__main__":
    main()
