# tests/test_reflection.py
import embeddings as e
import memory_store as ms
import reflection


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
    ms.add_lesson(c, "existing", "Always release the lock.",
                  e.to_blob([1.0, 0.0]), "i0")
    # new interaction i1, different text, embedding cosine ~0.98 vs existing
    lid = reflection.maybe_add_lesson(
        c, "i1", "task", "resp", "tests_passed",
        offload_fn=lambda **kw: "Release locks promptly.",
        embed_fn=lambda t: [0.98, 0.199],
    )
    assert lid is None
    assert len(ms.all_lessons(c)) == 1


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
