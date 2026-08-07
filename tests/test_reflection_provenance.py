# tests/test_reflection_provenance.py
"""Embedding provenance on the LIVE distillation path.

Measured 2026-08-06 against the live store (7865 interactions, 6563 positive
outcomes, 1061 lessons, distillation yield 0.081):

  * Every positive outcome has a lesson_distillations row, so distillation is
    attempted universally -- no positive interaction is silently skipped.
  * 853 distinct interactions reached state='stored', but the lesson is gone
    for 323 of them (37.9%): the nightly near-duplicate pruner
    (lesson_pruner.DEFAULT_THRESHOLD = 0.93) deleted them afterwards. In the
    most recent full week 235 of 317 stored lessons (74%) were pruned again.
  * That churn is impossible if the inline gate works, because the inline gate
    (reflection.DUP_THRESHOLD = 0.92) is STRICTER than the nightly pruner.

Root cause: server._prepare_lesson_candidate_bounded always wraps the embedder
in a closure to impose the distillation deadline, so `embed_fn is
embeddings.embed` was never true on the live path. Provenance was therefore
dropped, and is_duplicate() -- which is fail-closed on unknown provenance --
returned False before comparing anything. Semantic dedup was dead in
production; only exact-text dedup ran.

Residual footprint after the nightly pruner: of 1061 stored lessons, 13 (1.2%)
are >= 0.92 similar to an earlier lesson and 0 are >= 0.95 -- everything the
pruner covers (>= 0.93) was swept, and the survivors sit exactly in the
0.92-0.93 band that ONLY the inline gate protects. All 13 are
interaction-distilled; 0 are seeds.
"""
import embeddings as e
import memory_store as ms
import reflection
import pytest

LESSON = "Use `collections.deque` for O(1) pops from both ends of a queue."


@pytest.fixture(autouse=True)
def _clear_bound_embedding_state():
    """The bound-vector marker is thread-local; never leak it between tests."""
    yield
    for attr in ("vector", "revision", "model", "provider"):
        if hasattr(e._EMBED_STATE, attr):
            delattr(e._EMBED_STATE, attr)


def _bind_fake_embedder(monkeypatch, vector, provider="ollama"):
    """Make embeddings.embed behave like the real one: return AND bind.

    Every real embed path (ollama, cache, npu:*, cpu-reference) binds the
    vector it returns and sets a non-empty provider; only a foreign vector is
    left with an empty one.
    """
    def fake_embed(text, timeout=30, base=None, model=None):
        e._EMBED_STATE.vector = vector
        e._EMBED_STATE.revision = e.EMBED_REVISION
        e._EMBED_STATE.model = e.EMBED_IDENTITY
        e._EMBED_STATE.provider = provider
        e._EMBED_STATE.accelerated = False
        e._EMBED_STATE.simulated = False
        return vector

    monkeypatch.setattr(e, "embed", fake_embed)


class _BoundedEmbedDouble:
    """Stands in for server._prepare_lesson_candidate_bounded's closure.

    The real one calls embeddings.embed(text, timeout=...) and returns its
    vector: a different function object, but it yields the very vector the
    runtime embedder bound to this thread.
    """

    def __init__(self):
        self.calls = []

    def __call__(self, text):
        self.calls.append(text)
        return e.embed(text)


def test_bounded_embed_wrapper_keeps_provenance_so_dedup_can_run(monkeypatch):
    """The live path wraps embeddings.embed in a deadline closure.

    Before this fix the identity check `embed_fn is embeddings.embed` failed
    for that wrapper, so every candidate the running server produced was
    stored with embedding_model/revision/dim = None. is_duplicate() is
    fail-closed on unknown provenance, so it returned False without comparing
    anything and the 0.92 semantic gate never ran in production -- which is
    why the nightly 0.93 pruner had to delete 323 of the 853 stored lessons
    (235 of 317 in the most recent week) that the gate should have refused.
    """
    _bind_fake_embedder(monkeypatch, [0.6, 0.8])
    bounded = _BoundedEmbedDouble()

    candidate = reflection.prepare_lesson_candidate(
        "task", "response", "tests_passed",
        offload_fn=lambda **_kw: LESSON,
        embed_fn=bounded,
        id_fn=lambda: "cid",
    )

    assert bounded.calls == [LESSON]
    assert candidate["embedding_model"] == e.EMBED_IDENTITY
    assert candidate["embedding_revision"] == e.EMBED_REVISION
    assert candidate["embedding_dim"] == 2


def test_bounded_wrapper_candidate_is_deduped_against_the_store(monkeypatch):
    """End-to-end proof that the live path now refuses a near-duplicate.

    With provenance dropped this candidate was stored and left for the nightly
    pruner; with provenance restored is_duplicate() refuses it inline, which
    is the only gate covering the 0.92-0.93 band where all 13 surviving live
    near-duplicates sit.
    """
    _bind_fake_embedder(monkeypatch, [0.98, 0.199])  # ~0.98 vs [1.0, 0.0]
    conn = ms.connect(":memory:")
    ms.add_lesson(
        conn, "existing", "Always release the lock.", e.to_blob([1.0, 0.0]),
        "i0", embedding_model=e.EMBED_IDENTITY,
        embedding_revision=e.EMBED_REVISION, embedding_dim=2,
    )

    candidate = reflection.prepare_lesson_candidate(
        "task", "response", "tests_passed",
        offload_fn=lambda **_kw: "Release locks promptly in a finally block.",
        embed_fn=_BoundedEmbedDouble(),
        id_fn=lambda: "cid",
    )

    conn.execute("BEGIN IMMEDIATE")
    result = reflection.store_prepared_lesson(conn, "i1", candidate)
    conn.commit()

    assert result["result"] == "semantic_duplicate"
    assert result["terminal_state"] == ms.DISTILLATION_NO_LESSON
    assert len(ms.all_lessons(conn)) == 1


def test_injected_test_double_still_gets_no_fabricated_provenance():
    """Widening the check must not let a fake vector claim the real model.

    An injected embed_fn never touches the runtime embedder, so
    embeddings.provenance() reports an empty `provider` -- its documented
    "this is not the vector I bound" marker. Provenance stays refused and
    dedup stays fail-closed rather than comparing across embedding spaces.
    """
    candidate = reflection.prepare_lesson_candidate(
        "task", "response", "tests_passed",
        offload_fn=lambda **_kw: LESSON,
        embed_fn=lambda text: [1.0, 0.0],
        id_fn=lambda: "cid",
    )

    assert candidate["embedding"] == [1.0, 0.0]
    assert candidate["embedding_model"] is None
    assert candidate["embedding_revision"] is None
    assert candidate["embedding_dim"] is None


def test_wrapper_returning_a_stale_vector_is_refused(monkeypatch):
    """Fail closed when a wrapper returns something the embedder did not bind.

    Provenance is only honest for the vector the runtime embedder produced
    last on this thread. A wrapper handing back some other vector (a stale
    cache entry, a retry's discarded first result) must be treated like an
    injected double, not credited with the runtime's identity.
    """
    _bind_fake_embedder(monkeypatch, [0.6, 0.8])

    def stale_wrapper(text):
        e.embed(text)          # binds [0.6, 0.8]
        return [0.0, 1.0]      # ...but returns something else

    candidate = reflection.prepare_lesson_candidate(
        "task", "response", "tests_passed",
        offload_fn=lambda **_kw: LESSON,
        embed_fn=stale_wrapper,
        id_fn=lambda: "cid",
    )

    assert candidate["embedding"] == [0.0, 1.0]
    assert candidate["embedding_model"] is None


def test_pitfall_path_shares_the_same_provenance_resolution(monkeypatch):
    """prepare_pitfall_candidate duplicated prepare_lesson_candidate's body.

    Both now route through _prepare_candidate_text, so the pitfall path cannot
    drift back into dropping provenance the way the lesson path silently did.
    """
    _bind_fake_embedder(monkeypatch, [0.6, 0.8])

    candidate = reflection.prepare_pitfall_candidate(
        "task", "attempted code with -join and ForEach-Object",
        "Cannot bind parameter 'RemainingScripts'",
        offload_fn=lambda **_kw: (
            "Parenthesise the ForEach-Object pipeline before applying -join."
        ),
        embed_fn=_BoundedEmbedDouble(),
        id_fn=lambda: "cid",
    )

    assert candidate["status"] == "candidate"
    assert candidate["embedding_model"] == e.EMBED_IDENTITY
    assert candidate["embedding_dim"] == 2


def test_trusted_provenance_has_one_home(monkeypatch):
    """The identity check `embed_fn is embeddings.embed` existed in six files.
    Fixing reflection.py left five copies, and their fallbacks pointed opposite
    ways -- in retriever a missing model DISABLED cross-model filtering
    (permissive), in seed_merge it disabled dedup (restrictive). One
    implementation removes that divergence.

    Hermetic on purpose: provenance() is stubbed so this tests the trust
    routing, not whether an embedding server happens to be reachable.
    """
    import embeddings as emb
    import inspect

    bound = {"model": "m", "revision": "r", "dimension": 3, "provider": "ollama"}
    foreign = {"model": "m", "revision": "r", "dimension": 3, "provider": ""}

    def wrapper(text, **kw):
        return [0.1, 0.2, 0.3]

    # A closure that delegates yields the thread-bound vector, so provider is set.
    monkeypatch.setattr(emb, "provenance", lambda v=None: bound)
    assert emb.trusted_provenance([0.1, 0.2, 0.3], wrapper).get("provider") == "ollama"
    assert emb.trusted_provenance([0.1, 0.2, 0.3], None).get("model") == "m"

    # A vector the real embedder never produced has a blank provider, and an
    # injected double must not inherit this runtime's identity.
    monkeypatch.setattr(emb, "provenance", lambda v=None: foreign)
    assert emb.trusted_provenance([0.1, 0.2, 0.3], wrapper) == {}
    # ...but the runtime-default path is trusted by construction.
    assert emb.trusted_provenance([0.1, 0.2, 0.3], None).get("model") == "m"


def test_no_module_reintroduces_the_identity_check():
    """Five copies survived the first fix. This fails if a sixth appears."""
    import inspect

    for module in ("recall", "retriever", "seed_merge", "tune_min_sim",
                   "pull_community", "reflection"):
        src = inspect.getsource(__import__(module))
        offending = [
            line for line in src.splitlines()
            if "embed_fn is embeddings.embed" in line
            and not line.strip().startswith("#")
            and "if " in line
        ]
        # Docstrings may describe the old check; executable code may not use it.
        assert not offending, (
            "%s reintroduced the identity check; call "
            "embeddings.trusted_provenance instead" % module
        )
