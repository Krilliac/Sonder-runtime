import hashlib

import sonder_runtime.adapters.embeddings as embeddings
import memory_store as ms
import seed_merge


def _emb(text):
    # deterministic 8-d "embedding": distinct texts hash to well-separated vectors
    # (cosine < 0.92) so only genuinely-similar text trips the embed-dedup.
    h = hashlib.sha256(text.encode("utf-8")).digest()
    return [b / 255.0 for b in h[:8]]


def test_merge_applies_quality_pipeline():
    c = ms.connect(":memory:")
    records = [
        {"lesson": "Use collections.Counter(x).most_common(1) to get the mode.", "source": "seed:curriculum:dicts:basic"},
        {"lesson": "Use appropriate data structures efficiently.", "source": "seed:curriculum:dicts:basic"},   # vague
        {"lesson": "Config lives at C:\\Users\\example\\secret.env", "source": "seed:realwork:x"},               # private path
        {"lesson": "Guard binary_search with lo<=hi and mid=(lo+hi)//2.", "source": "seed:curriculum:search:basic"},
    ]
    stats = seed_merge.merge_records(c, records, embed_fn=_emb)
    assert stats["added"] == 2
    assert stats["skipped_vague"] == 1
    assert stats["skipped_private"] == 1
    texts = {lesson["text"] for lesson in ms.all_lessons(c)}
    assert "Use collections.Counter(x).most_common(1) to get the mode." in texts
    assert all("secret" not in t for t in texts)


def test_merge_dedupes_exact_text_against_store():
    c = ms.connect(":memory:")
    ms.add_lesson(c, "L0", "Memoize with functools.lru_cache.", None, "prior")
    stats = seed_merge.merge_records(
        c,
        [{"lesson": "memoize with functools.lru_cache.", "source": "seed:x"}],  # case-only diff
        embed_fn=_emb,
    )
    assert stats["added"] == 0
    assert stats["skipped_dup_text"] == 1


def test_merge_dedupes_within_batch():
    c = ms.connect(":memory:")
    recs = [
        {"lesson": "Use str.rsplit(sep, 1) to split on the last separator.", "source": "s"},
        {"lesson": "use str.rsplit(sep, 1) to split on the last separator.", "source": "s"},
    ]
    stats = seed_merge.merge_records(c, recs, embed_fn=_emb)
    assert stats["added"] == 1
    assert len(ms.all_lessons(c)) == 1


def test_dry_run_writes_nothing():
    c = ms.connect(":memory:")
    stats = seed_merge.merge_records(
        c, [{"lesson": "Use bisect.insort to keep a list sorted on insert.", "source": "s"}],
        embed_fn=_emb, dry_run=True,
    )
    assert stats["added"] == 1
    assert len(ms.all_lessons(c)) == 0


def test_text_of_accepts_both_field_names():
    assert seed_merge._text_of({"lesson": " a "}) == "a"
    assert seed_merge._text_of({"text": " b "}) == "b"
    assert seed_merge._text_of({}) == ""


def test_merge_runtime_embed_records_provenance_and_dedupes_current(monkeypatch):
    c = ms.connect(":memory:")
    calls = []
    monkeypatch.setattr(
        embeddings,
        "embed",
        lambda text: calls.append(text) or [0.5, 0.5],
    )
    records = [
        {"lesson": "Use pathlib.Path.resolve() before containment checks.", "source": "s"},
        {"lesson": "Resolve paths with pathlib.Path.resolve() before comparison.", "source": "s"},
    ]

    stats = seed_merge.merge_records(c, records)

    assert stats["added"] == 1
    assert stats["skipped_dup_embed"] == 1
    assert calls == [record["lesson"] for record in records]
    stored = ms.all_lessons(c)[0]
    assert stored["embedding_model"] == embeddings.EMBED_IDENTITY
    assert stored["embedding_revision"] == embeddings.EMBED_REVISION
    assert stored["embedding_dim"] == 2


def test_merge_skips_non_dict_rows_and_tombstoned_lessons():
    c = ms.connect(":memory:")
    tombstoned_text = "Always validate UTF-8 byte offsets before slicing."
    ms.add_lesson(c, "L-tomb", tombstoned_text, None, "prior")
    ms.tombstone_lesson(c, "L-tomb")

    stats = seed_merge.merge_records(
        c,
        [
            "not a dict",
            42,
            None,
            {"lesson": tombstoned_text.upper(), "source": "seed:x"},
            {"lesson": "Check sqlite3 WAL checkpoint before backup copy.", "source": "seed:x"},
        ],
        embed_fn=_emb,
    )
    assert stats["skipped_vague"] == 3
    assert stats["skipped_tombstoned"] == 1
    assert stats["added"] == 1
    assert [row["text"] for row in ms.all_lessons(c)] == [
        "Check sqlite3 WAL checkpoint before backup copy."
    ]


def test_merge_skips_embedding_tombstoned_duplicate(monkeypatch):
    c = ms.connect(":memory:")
    vec = [0.6, 0.8]
    monkeypatch.setattr(embeddings, "embed", lambda _text: vec)
    blob = embeddings.to_blob(vec)
    ms.add_lesson(
        c, "L-tomb-emb", "Pin subprocess timeouts on every host tool call.", blob, "prior",
        embedding_model=embeddings.EMBED_IDENTITY,
        embedding_revision=embeddings.EMBED_REVISION,
        embedding_dim=2,
    )
    ms.tombstone_lesson(c, "L-tomb-emb")

    stats = seed_merge.merge_records(
        c,
        [{"lesson": "Set explicit subprocess timeouts on host tool invocations.", "source": "seed:x"}],
    )
    assert stats["added"] == 0
    assert stats["skipped_tombstoned"] == 1
    assert ms.all_lessons(c) == []
