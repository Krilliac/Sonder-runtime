# tests/test_reflection.py
import sonder_runtime.adapters.embeddings as e
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


def test_prepare_candidate_does_not_touch_sqlite_and_defers_dedupe():
    candidate = reflection.prepare_lesson_candidate(
        "task", "response", "tests_passed",
        offload_fn=lambda **kw: "Use collections.deque for O(1) queue pops.",
        embed_fn=lambda text: [1.0, 0.0],
        id_fn=lambda: "candidate-id",
    )

    assert candidate == {
        "status": "candidate",
        "lesson_id": "candidate-id",
        "text": "Use collections.deque for O(1) queue pops.",
        "embedding": [1.0, 0.0],
        "embedding_blob": e.to_blob([1.0, 0.0]),
        "embedding_model": None,
        "embedding_revision": None,
        "embedding_dim": None,
    }


def test_store_prepared_lesson_publishes_lesson_and_fts_in_active_transaction():
    c = ms.connect(":memory:")
    candidate = {
        "status": "candidate",
        "lesson_id": "prepared-id",
        "text": "Use collections.deque for O(1) queue pops.",
        "embedding": [1.0, 0.0],
        "embedding_blob": e.to_blob([1.0, 0.0]),
        "embedding_model": e.EMBED_IDENTITY,
        "embedding_revision": e.EMBED_REVISION,
        "embedding_dim": 2,
    }

    c.execute("BEGIN IMMEDIATE")
    result = reflection.store_prepared_lesson(c, "source-id", candidate)
    assert c.in_transaction
    c.commit()

    assert result == {
        "terminal_state": ms.DISTILLATION_STORED,
        "lesson_id": "prepared-id",
        "result": "stored",
    }
    assert ms.get_lesson_text(c, "prepared-id") == candidate["text"]
    assert c.execute(
        "SELECT text FROM lessons_fts WHERE lesson_id='prepared-id'"
    ).fetchone()[0] == candidate["text"]


def test_store_prepared_lesson_dedupes_inside_transaction_without_committing():
    c = ms.connect(":memory:")
    ms.add_lesson(c, "existing", "Use pathlib.Path.resolve() before comparison.", None, "old")
    candidate = {
        "status": "candidate",
        "lesson_id": "duplicate-id",
        "text": "  use   PATHLIB.path.resolve() before comparison. ",
        "embedding": None,
        "embedding_blob": None,
        "embedding_model": None,
        "embedding_revision": None,
        "embedding_dim": None,
    }

    c.execute("BEGIN IMMEDIATE")
    result = reflection.store_prepared_lesson(c, "new-source", candidate)
    assert c.in_transaction
    c.commit()

    assert result == {
        "terminal_state": ms.DISTILLATION_NO_LESSON,
        "result": "exact_duplicate",
    }
    assert len(ms.all_lessons(c)) == 1


def test_store_prepared_lesson_refuses_pruned_value_tombstone():
    c = ms.connect(":memory:")
    ms.add_lesson(
        c, "old", "Use pathlib.Path.resolve() before comparison.", None, "old-source",
    )
    assert ms.tombstone_lesson(c, "old") is True
    candidate = {
        "status": "candidate", "lesson_id": "reintroduced",
        "text": " use  pathlib.Path.resolve() before comparison. ",
        "embedding": None, "embedding_blob": None,
        "embedding_model": None, "embedding_revision": None, "embedding_dim": None,
    }
    c.execute("BEGIN IMMEDIATE")
    result = reflection.store_prepared_lesson(c, "new-source", candidate)
    c.commit()
    assert result == {"terminal_state": ms.DISTILLATION_NO_LESSON, "result": "rejected_value"}
    assert ms.all_lessons(c) == []


def test_store_prepared_lesson_refuses_semantic_pruned_tombstone():
    c = ms.connect(":memory:")
    vector = [1.0, 0.0]
    ms.add_lesson(
        c, "old", "Use pathlib.Path.resolve() before comparison.",
        e.to_blob(vector), "old-source",
        embedding_model=e.EMBED_IDENTITY,
        embedding_revision=e.EMBED_REVISION,
        embedding_dim=2,
    )
    assert ms.tombstone_lesson(c, "old") is True
    candidate = {
        "status": "candidate", "lesson_id": "reintroduced",
        "text": "Resolve a path before comparing it.",
        "embedding": vector, "embedding_blob": e.to_blob(vector),
        "embedding_model": e.EMBED_IDENTITY,
        "embedding_revision": e.EMBED_REVISION,
        "embedding_dim": 2,
    }
    c.execute("BEGIN IMMEDIATE")
    result = reflection.store_prepared_lesson(c, "new-source", candidate)
    c.commit()
    assert result == {"terminal_state": ms.DISTILLATION_NO_LESSON, "result": "rejected_value"}
    assert ms.all_lessons(c) == []


def test_store_prepared_lesson_requires_finalizer_transaction():
    c = ms.connect(":memory:")
    with pytest.raises(RuntimeError, match="active transaction"):
        reflection.store_prepared_lesson(
            c, "source", {"status": "no_lesson", "reason": "not_concrete"},
        )


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


def test_pitfall_refuses_fenced_or_multiline_output():
    """Small models answer the pitfall prompt with a before/after diff, which
    is useless once retrieved into a different task: it names no rule, only an
    edit to code the future task does not have."""
    assert reflection._one_sentence_lesson(
        "```powershell\nold\n```\nReplace with:\n```powershell\nnew\n```") == ""
    assert reflection._one_sentence_lesson("Do this.\nThen that.") == ""
    assert reflection._one_sentence_lesson("Use join.") == ""
    assert reflection._one_sentence_lesson("") == ""
    good = ("Wrap a pipeline in parentheses before applying -join, because "
            "-join otherwise binds to ForEach-Object.")
    assert reflection._one_sentence_lesson(good) == good


def test_distill_pitfall_needs_an_error_and_a_usable_sentence():
    calls = []

    def fake_offload(prompt, **options):
        calls.append(prompt)
        return "Declare PowerShell class methods with a return type so return "\
               "statements are allowed instead of failing as void."

    # No error text: never reaches the model at all.
    assert reflection.distill_pitfall("t", "r", "", fake_offload) == ""
    assert not calls

    lesson = reflection.distill_pitfall(
        "t", "r", "Invalid return statement within void method.", fake_offload)
    assert "return type" in lesson
    assert len(calls) == 1


def test_pitfall_refuses_a_verbatim_echo_of_the_worked_example():
    """The prompt carries a worked example and a small model will sometimes
    repeat its answer instead of reading the failure. A verbatim echo is
    refused outright: it carries no evidence the model looked at anything."""
    echoed = ("Parenthesise a pipeline before applying -join, because -join "
              "otherwise binds as an argument to ForEach-Object.")
    matching = "Cannot bind parameter 'RemainingScripts' from a -join call"
    unrelated = "Variable reference is not valid. ':' was not followed by a name."
    assert reflection._one_sentence_lesson(echoed, matching) == ""
    assert reflection._one_sentence_lesson(echoed, unrelated) == ""
    assert reflection._one_sentence_lesson(echoed) == ""


def test_pitfall_keeps_a_paraphrase_that_names_the_real_failure():
    """A lesson the model actually derived is kept when it names something the
    failure involved."""
    derived = ("Pipeline binding error with `-join`: wrap the pipeline in "
               "parentheses before joining its results.")
    matching = "Cannot bind parameter 'RemainingScripts' from a -join call"
    unrelated = "Variable reference is not valid. ':' was not followed by a name."
    assert reflection._one_sentence_lesson(derived, matching) == derived
    assert reflection._one_sentence_lesson(derived, unrelated) == ""


def test_pitfall_relevance_also_reads_the_attempted_code():
    """Wrong-output failures report a diff of values, not a description, so
    matching the error alone refused correct lessons: one about `print` in a
    loop was rejected for saying "outputs" while the error said "output;".
    The attempted code names the constructs the lesson is about."""
    lesson = ("The issue is using `print` inside a loop, which emits each item "
              "on its own line; join the values with spaces instead.")
    error = "wrong output; expected to contain '10 -1 30', got '10\\n-1\\n30'"
    code = "for r in results:\n    print(r)\n"
    assert reflection._one_sentence_lesson(lesson, error) == ""
    assert reflection._one_sentence_lesson(lesson, error, code) == lesson


def test_readable_error_keeps_the_difference_it_is_reporting():
    """Collapsing every whitespace run turned "expected '10 -1 30', got
    '10\\n-1\\n30'" into two identical strings, so the model was asked to
    explain a difference the prompt had already destroyed."""
    readable = reflection._readable_error(
        "wrong output; expected to contain '10 -1 30', got '10\n-1\n30'")
    assert "\\n" in readable
    assert readable.count("10 -1 30") == 1
    assert "\n" not in readable
    assert reflection._readable_error("  spaced   out  ") == "spaced out"
    assert reflection._readable_error("") == ""


def test_pitfall_refuses_a_circular_restatement_of_the_task():
    """When a program produces no output, the model narrates "the task was not
    implemented; implement it" - a restatement whose fix is the task itself,
    never transferable to a different task. It has anchors and shares terms so
    every other gate passes it; this one catches the missing-vs-misused shape.
    A real pitfall names a construct that was misused, not one that was
    missing."""
    error = "wrong output; expected to contain 'd a b c', got ''"
    code = "int main() { return 0; }"
    circular = [
        "The attempt failed because it did not implement any logic for "
        "topological sorting.",
        "The function `main()` is empty and does not perform any operations, "
        "leading to empty output.",
        "The method for reversing a string was not implemented in the class.",
        "The code contains no sorting logic, so add a topological sort routine.",
    ]
    for sentence in circular:
        assert reflection._one_sentence_lesson(sentence, error, code) == "", sentence

    # A real pitfall naming a misused construct survives the same guard - shown
    # against a failure whose text it actually relates to.
    real = ("The issue is with using `print` inside a loop, which emits each "
            "item on its own line; join the values with spaces instead.")
    print_error = "wrong output; expected '1 2 3', got '1\\n2\\n3'"
    print_code = "for r in results:\n    print(r)\n"
    assert reflection._one_sentence_lesson(real, print_error, print_code) == real
    # And the guard alone keeps it regardless of relevance.
    assert reflection._is_non_implementation(real) is False


def test_pitfall_refuses_a_lesson_that_quotes_the_instances_calls():
    """A lesson spelling out the task's own call sequence - "call cache.get(1),
    cache.get(2), cache.get(3)" - is an edit to that code, not a rule another
    task can apply. It passes the anchor check precisely because those calls
    are concrete. Measured over 1042 stored lessons, requiring two or more
    literal integer arguments inside one backticked span matched exactly the
    two non-transferable lessons and nothing else."""
    error = "wrong output; expected exactly '10 -1 30', got '10\n-1\n30'"
    code = "for r in [cache.get(1), cache.get(2)]:\n    print(r)\n"
    instance_bound = (
        "Use `print(' '.join(map(str, [cache.get(1), cache.get(2), "
        "cache.get(3)])))` to fix this."
    )
    assert reflection._one_sentence_lesson(instance_bound, error, code) == ""

    # Advice that names a technique rather than the instance survives, even
    # when it is highly specific.
    for general in (
        "Use `collections.deque` for O(1) pops from both ends of a queue.",
        "Join the values with a single space instead of printing each on its "
        "own line.",
        "Use `range(start, end + 1)` when callers expect an inclusive end.",
    ):
        assert reflection._quotes_the_instances_calls(general) is False, general


def test_circular_guard_also_catches_the_present_tense_phrasing():
    """A candidate whose module never defined its exported function produces
    "the X function is not defined in module.py - define it", whose fix is the
    task itself. That is the same missing-symbol shape as "was never defined",
    and it reached the store once before the guard covered the present tense.
    Measured over 1042 lessons the extension matched only that one."""
    assert reflection._is_non_implementation(
        "The `with_defaults` function is not defined in `module.py`. Define it "
        "or remove its import from `test_module.py`.")
    assert reflection._is_non_implementation(
        "The required helpers are not defined in the module; add them.")

    # Lessons that merely contain similar words stay, including one whose
    # subject genuinely is validity rather than a missing symbol.
    for keeper in (
        "The ternary operator `? :` is not valid in PowerShell; use if/else.",
        "Use `collections.deque` for O(1) pops from both ends of a queue.",
        "Call `dict.copy()` before mutating so the caller's object is untouched.",
    ):
        assert reflection._is_non_implementation(keeper) is False, keeper
