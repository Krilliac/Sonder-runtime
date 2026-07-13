# tests/test_reflection.py
import embeddings as e
import memory_store as ms
import reflection
import pytest


def _off(**kw):
    # stub offload: echoes a fixed lesson regardless of prompt
    return "  Release the lock in a finally block.  "


def test_distill_strips_and_returns_text():
    out = reflection.distill("task", "resp", "tests_passed", _off)
    assert out == "Release the lock in a finally block."


def test_distill_uses_the_code_tier_not_the_weak_fast_tier():
    seen = {}

    def _capture(**kw):
        seen.update(kw)
        return "Use collections.deque for O(1) appends and pops from both ends."

    reflection.distill("task", "resp", "tests_passed", _capture)
    assert seen.get("tier") == "code"


def test_distill_rejects_vague_platitudes():
    # A generic, non-actionable "lesson" must be dropped rather than stored.
    for platitude in (
        "Use the standard library effectively.",
        "Use classes for game entities and manage their states efficiently.",
        "Use a grid-based approach for snake movement and collisions efficiently.",
        "Follow best practices and write clean, readable code.",
    ):
        out = reflection.distill("t", "r", "tests_passed", lambda **kw: platitude)
        assert out == "", "expected platitude to be rejected: %r" % platitude


def test_distill_keeps_specific_actionable_lessons():
    for good in (
        "Release the lock in a finally block.",
        "Use collections.deque for O(1) pops from both ends of a queue.",
        "Guard against ZeroDivisionError before dividing by a user-supplied value.",
    ):
        out = reflection.distill("t", "r", "tests_passed", lambda **kw: good)
        assert out == good


def test_looks_vague_flags_platitudes_and_passes_specifics():
    assert reflection._looks_vague("Use appropriate data structures efficiently.")
    assert reflection._looks_vague("Manage state properly.")
    assert not reflection._looks_vague("Memoize nth_fibonacci with functools.lru_cache.")


def test_maybe_add_lesson_writes_one_lesson():
    c = ms.connect(":memory:")
    lid = reflection.maybe_add_lesson(
        c, "i1", "task", "resp", "tests_passed",
        offload_fn=_off, embed_fn=lambda t: [1.0, 0.0],
    )
    assert lid is not None
    assert ms.get_lesson_text(c, lid) == "Release the lock in a finally block."


def test_maybe_add_lesson_dedupes_near_duplicate():
    c = ms.connect(":memory:")
    ms.add_lesson(c, "existing", "Release the lock in a finally block.",
                  e.to_blob([1.0, 0.0]), "i0")
    lid = reflection.maybe_add_lesson(
        c, "i1", "task", "resp", "tests_passed",
        offload_fn=_off, embed_fn=lambda t: [1.0, 0.0],  # identical vector -> dup
    )
    assert lid is None
    assert len(ms.all_lessons(c)) == 1


def test_maybe_add_lesson_dedupes_exact_text_without_embeddings():
    c = ms.connect(":memory:")
    ms.add_lesson(c, "existing", "Release the lock in a finally block.", None, "i0")
    lid = reflection.maybe_add_lesson(
        c, "i1", "task", "resp", "tests_passed",
        offload_fn=lambda **kw: "  release   the LOCK in a finally block. ",
        embed_fn=lambda t: None,
    )
    assert lid is None
    assert len(ms.all_lessons(c)) == 1


def test_maybe_add_lesson_skips_empty_distill():
    c = ms.connect(":memory:")
    lid = reflection.maybe_add_lesson(
        c, "i1", "task", "resp", "tests_passed",
        offload_fn=lambda **kw: "   ", embed_fn=lambda t: [1.0, 0.0],
    )
    assert lid is None


def test_maybe_add_lesson_dedupes_near_but_not_exact(monkeypatch):
    c = ms.connect(":memory:")
    ms.add_lesson(
        c,
        "existing",
        "Always release the lock.",
        e.to_blob([1.0, 0.0]),
        "i0",
        embedding_model=e.EMBED_IDENTITY,
        embedding_revision=e.EMBED_REVISION,
        embedding_dim=2,
    )
    monkeypatch.setattr(e, "embed", lambda _text: [0.98, 0.199])
    # new interaction i1, different text, embedding cosine ~0.98 vs existing
    lid = reflection.maybe_add_lesson(
        c, "i1", "task", "resp", "tests_passed",
        offload_fn=lambda **kw: "Release locks promptly.",
    )
    assert lid is None
    assert len(ms.all_lessons(c)) == 1


def test_is_duplicate_requires_exact_finite_current_provenance():
    c = ms.connect(":memory:")
    candidate = [1.0, 0.0]
    rows = (
        ("legacy", None, None, candidate),
        ("stale-model", "old-model:latest", e.EMBED_REVISION, candidate),
        ("stale-revision", e.EMBED_IDENTITY, "old-revision", candidate),
        ("non-finite", e.EMBED_IDENTITY, e.EMBED_REVISION, candidate),
    )
    for lesson_id, model, revision, vector in rows:
        ms.add_lesson(
            c,
            lesson_id,
            "Different lesson %s." % lesson_id,
            e.to_blob(vector),
            "source-%s" % lesson_id,
            embedding_model=model,
            embedding_revision=revision,
            embedding_dim=2,
        )
    c.execute(
        "UPDATE lessons SET embedding=? WHERE id='non-finite'",
        (e.to_blob([float("nan"), 0.0]),),
    )
    c.commit()

    provenance = e.provenance(candidate)
    assert not reflection.is_duplicate(
        candidate,
        c,
        embedding_model=provenance["model"],
        embedding_revision=provenance["revision"],
        embedding_dim=provenance["dimension"],
    )

    ms.add_lesson(
        c,
        "current",
        "A compatible current lesson.",
        e.to_blob(candidate),
        "source-current",
        embedding_model=provenance["model"],
        embedding_revision=provenance["revision"],
        embedding_dim=provenance["dimension"],
    )
    assert reflection.is_duplicate(
        candidate,
        c,
        embedding_model=provenance["model"],
        embedding_revision=provenance["revision"],
        embedding_dim=provenance["dimension"],
    )


def test_maybe_add_resolves_runtime_embed_and_stores_current_provenance(monkeypatch):
    c = ms.connect(":memory:")
    calls = []
    monkeypatch.setattr(e, "embed", lambda text: calls.append(text) or [0.25, 0.75])

    lesson_id = reflection.maybe_add_lesson(
        c,
        "runtime-embed",
        "task",
        "response",
        "tests_passed",
        offload_fn=lambda **_kwargs: "Use pathlib.Path.resolve() before comparison.",
    )

    assert lesson_id is not None
    assert calls == ["Use pathlib.Path.resolve() before comparison."]
    stored = ms.all_lessons(c)[0]
    assert stored["embedding_model"] == e.EMBED_IDENTITY
    assert stored["embedding_revision"] == e.EMBED_REVISION
    assert stored["embedding_dim"] == 2


def test_maybe_add_lesson_stores_when_embeddings_unavailable():
    c = ms.connect(":memory:")
    lid = reflection.maybe_add_lesson(
        c, "i9", "task", "resp", "tests_passed",
        offload_fn=lambda **kw: "A useful lesson.",
        embed_fn=lambda t: None,  # embeddings unavailable
    )
    assert lid is not None
    stored = ms.all_lessons(c)[0]
    assert stored["embedding"] is None


@pytest.mark.parametrize(
    "private_lesson",
    (
        r"Read C:\Users\example\secret.env with pathlib.Path.read_text().",
        "Set api_key=super-secret-value before calling the client.",
        "Send the failure report to developer@example.com.",
        "Use Authorization: Bearer sk-proj-abcdefghijklmnop for the request.",
        "Read /etc/ssh/id_rsa before connecting.",
    ),
)
def test_maybe_add_lesson_rejects_privacy_flagged_distillation(private_lesson):
    c = ms.connect(":memory:")
    embed_calls = []

    lid = reflection.maybe_add_lesson(
        c, "private-source", "task", "response", "tests_passed",
        offload_fn=lambda **kw: private_lesson,
        embed_fn=lambda text: embed_calls.append(text) or [1.0],
    )

    assert lid is None
    assert embed_calls == []
    assert ms.all_lessons(c) == []
    assert c.execute("SELECT COUNT(*) FROM lessons_fts").fetchone()[0] == 0
