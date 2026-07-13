"""Merge fleet-authored seed batches (seed/**/*.jsonl) into memory.db.

Each batch line is a lesson record: {"lesson": <text>, "source": <tag>, ...}.
Unlike pull_community.merge_lessons (exact-text dedup only), this applies the
full quality pipeline before a lesson is stored:

  1. reflection._looks_vague  -> drop platitudes ("use X efficiently")
  2. contribute.is_shareable   -> drop anything with a private marker (path,
     secret, email) as a privacy backstop for real-work lessons
  3. exact-text dedup (lowercased) vs the live store and within this run
  4. embedding-similarity dedup (reflection.is_duplicate) vs the live store

Lessons are stored with their batch `source` tag (e.g. seed:curriculum:strings:basic,
seed:realwork:mangos) so they are distinguishable from grounded interaction lessons.

Run: ./venv/Scripts/python.exe seed_merge.py            # merge seed/**/*.jsonl -> memory.db
     ./venv/Scripts/python.exe seed_merge.py --dry-run  # report only, write nothing
"""
import glob
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import contribute  # noqa: E402
import embeddings  # noqa: E402
import memory_store  # noqa: E402
import reflection  # noqa: E402

SEED_GLOB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seed", "**", "*.jsonl")


def load_records(files):
    """Yield (path, record) for every JSONL line in `files` (skips blank/bad lines)."""
    for path in files:
        with io.open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield path, json.loads(line)
                except json.JSONDecodeError:
                    continue


def _text_of(rec):
    return (rec.get("lesson") or rec.get("text") or "").strip()


def merge_records(conn, records, embed_fn=None, dry_run=False):
    """Merge lesson records into `conn`, applying the full quality pipeline.

    Returns a stats dict: {added, skipped_vague, skipped_private, skipped_dup_text,
    skipped_dup_embed, by_source}. `records` is an iterable of dicts with a
    'lesson'/'text' field and optional 'source'.
    """
    runtime_default = embed_fn is None
    embed_fn = embed_fn or embeddings.embed
    seen_text = {
        (lesson["text"] or "").strip().lower()
        for lesson in memory_store.all_lessons(conn)
    }
    stats = {
        "added": 0, "skipped_vague": 0, "skipped_private": 0,
        "skipped_dup_text": 0, "skipped_dup_embed": 0, "by_source": {},
    }
    for rec in records:
        text = _text_of(rec)
        if not text or reflection._looks_vague(text):
            stats["skipped_vague"] += 1
            continue
        if not contribute.is_shareable(text):
            stats["skipped_private"] += 1
            continue
        key = text.lower()
        if key in seen_text:
            stats["skipped_dup_text"] += 1
            continue
        emb = embed_fn(text) if embed_fn else None
        if not embeddings.valid_vector(emb):
            emb = None
        provenance = (
            embeddings.provenance(emb)
            if emb is not None and (runtime_default or embed_fn is embeddings.embed)
            else {}
        )
        if emb is not None and reflection.is_duplicate(
            emb,
            conn,
            embedding_model=provenance.get("model"),
            embedding_revision=provenance.get("revision"),
            embedding_dim=provenance.get("dimension"),
        ):
            stats["skipped_dup_embed"] += 1
            continue
        if not dry_run:
            blob = embeddings.to_blob(emb) if emb else None
            memory_store.add_lesson(
                conn, memory_store.new_id(), text, blob, rec.get("source", "seed"),
                embedding_model=provenance.get("model"),
                embedding_revision=provenance.get("revision"),
                embedding_dim=provenance.get("dimension"),
            )
        seen_text.add(key)
        src = rec.get("source", "seed")
        stats["by_source"][src] = stats["by_source"].get(src, 0) + 1
        stats["added"] += 1
    return stats


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    dry = "--dry-run" in argv
    files = sorted(glob.glob(SEED_GLOB, recursive=True))
    if not files:
        print("no seed batches found under seed/**/*.jsonl")
        return
    print("merging %d batch file(s):" % len(files))
    for f in files:
        print("  -", os.path.relpath(f, os.path.dirname(os.path.abspath(__file__))))

    db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory.db")
    conn = memory_store.connect(db)
    try:
        before = len(memory_store.all_lessons(conn))
        stats = merge_records(conn, (r for _, r in load_records(files)), dry_run=dry)
        after = len(memory_store.all_lessons(conn))
    finally:
        conn.close()

    print("\n%s" % ("DRY RUN (nothing written)" if dry else "merged"))
    print("  added:             %d" % stats["added"])
    print("  skipped vague:     %d" % stats["skipped_vague"])
    print("  skipped private:   %d" % stats["skipped_private"])
    print("  skipped dup(text): %d" % stats["skipped_dup_text"])
    print("  skipped dup(embed):%d" % stats["skipped_dup_embed"])
    print("  lessons %d -> %d" % (before, after))
    print("  by source:")
    for src in sorted(stats["by_source"]):
        print("    %-32s %d" % (src, stats["by_source"][src]))


if __name__ == "__main__":
    main()
