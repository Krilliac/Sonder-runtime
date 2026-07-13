"""Distill reusable lessons from good outcomes; dedup by embedding similarity."""
import re

import contribute
import embeddings
import memory_store

DUP_THRESHOLD = 0.92
DISTILL_SYSTEM = (
    "You extract ONE concrete, reusable engineering lesson from a solved coding "
    "task. The lesson must name the specific technique, data structure, API, "
    "algorithm, or pitfall that mattered — something a developer could act on "
    "without ever seeing this task. Output a single imperative sentence, no "
    "preamble, no markdown.\n"
    "BANNED (too vague to store): 'efficiently', 'effectively', 'properly', "
    "'appropriately', 'best practices', 'clean/readable code', 'use the standard "
    "library', 'manage state'. If you can only produce something that generic, "
    "output the single word NONE.\n"
    "Good: 'Use collections.deque for O(1) pops from both ends of a queue.'\n"
    "Bad:  'Use appropriate data structures efficiently.'"
)

# A distilled lesson is worthless if it's a platitude — better to store nothing
# than to pollute retrieval with "use classes efficiently". These patterns catch
# the generic shapes the weak distiller used to emit.
_VAGUE_MARKERS = re.compile(
    r"\b(efficien\w+|effectiv\w+|properly|appropriate\w*|correctly|"
    r"as needed|when necessary|where appropriate|best practices?|"
    r"clean code|readable code|good (structure|design|practices?)|"
    r"manage (its |their )?state)\b",
    re.I,
)
_GENERIC_TEMPLATES = re.compile(
    r"^use (the )?(standard library|classes|functions?|loops?|variables?|"
    r"appropriate data structures?)\b",
    re.I,
)
# A concrete "anchor" makes a lesson actionable even if it also uses filler words:
# a dotted call (re.search), backticked code, a CamelCase API (OrderedDict), a
# snake_case identifier (lru_cache), or a big-O bound all count.
_CONCRETE_ANCHOR = re.compile(
    r"`[^`]+`"                 # `re.search`
    r"|\b\w+\.\w+"             # functools.lru_cache
    r"|\b\w+_\w+"             # snake_case
    r"|[A-Za-z]+[A-Z][a-z]"    # OrderedDict, ZeroDivisionError
    r"|O\([^)]*\)",            # O(1), O(n log n)
)


def _has_concrete_anchor(text):
    return bool(_CONCRETE_ANCHOR.search(text or ""))


def _looks_vague(text):
    """True if the lesson is a non-actionable platitude that shouldn't be stored.

    Filler words ('efficiently', 'use classes', ...) only condemn a lesson when it
    has no concrete anchor — a specific API/technique keeps it even if it's wordy.
    """
    t = (text or "").strip().strip(".").strip()
    if not t:
        return True
    if t.upper() == "NONE":
        return True
    if _has_concrete_anchor(t):
        return False
    if _GENERIC_TEMPLATES.match(t):
        return True
    if _VAGUE_MARKERS.search(t):
        return True
    return False


def distill(task, response, signal, offload_fn):
    prompt = (
        "A coding task was completed with outcome '%s'.\n\n"
        "TASK:\n%s\n\nSOLUTION:\n%s\n\n"
        "Extract ONE concrete, reusable lesson (max 25 words) that names the "
        "specific technique, API, data structure, algorithm, or pitfall that made "
        "this solution work. It must be actionable on a DIFFERENT future task. "
        "If no such specific insight exists, output NONE. No preamble."
        % (signal, task, response)
    )
    text = offload_fn(
        prompt=prompt, tier="code", system=DISTILL_SYSTEM,
        temperature=0.0, num_predict=60,
    )
    text = (text or "").strip()
    if _looks_vague(text):
        return ""
    return text


def is_duplicate(
    new_emb,
    conn,
    threshold=DUP_THRESHOLD,
    *,
    embedding_model=None,
    embedding_revision=None,
    embedding_dim=None,
):
    """Return whether *new_emb* matches a compatible, current lesson vector.

    Semantic deduplication is deliberately fail-closed: a caller must identify
    the embedding model, revision, and dimension that produced ``new_emb``.
    Legacy or stale rows, corrupt blobs, and non-finite vectors are ignored so a
    model migration cannot suppress an otherwise distinct lesson.
    """
    if (
        not embeddings.valid_vector(new_emb)
        or not embedding_model
        or embedding_revision is None
        or embedding_dim != len(new_emb)
    ):
        return False
    for les in memory_store.all_lessons(conn):
        if (
            les.get("embedding_model") != embedding_model
            or (les.get("embedding_revision") or None)
            != (embedding_revision or None)
            or les.get("embedding_dim") != embedding_dim
        ):
            continue
        emb = les["embedding"]
        if not emb:
            continue
        try:
            stored = embeddings.from_blob(emb)
        except (TypeError, ValueError, OverflowError):
            continue
        if (
            len(stored) == embedding_dim
            and embeddings.valid_vector(stored)
            and embeddings.cosine(new_emb, stored) >= threshold
        ):
            return True
    return False


def _normalize_lesson_text(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def exact_text_exists(text, conn):
    needle = _normalize_lesson_text(text)
    if not needle:
        return False
    for les in memory_store.all_lessons(conn):
        if _normalize_lesson_text(les["text"]) == needle:
            return True
    return False


def maybe_add_lesson(conn, interaction_id, task, response, signal, offload_fn,
                     embed_fn=None, id_fn=memory_store.new_id):
    runtime_default = embed_fn is None
    embed_fn = embed_fn or embeddings.embed
    if memory_store.lesson_exists_for_interaction(conn, interaction_id):
        return None
    text = distill(task, response, signal, offload_fn)
    if not text:
        return None
    # Keep secret- and path-like material out of both the lesson row and its
    # FTS mirror. Use the shared privacy classifier so ingestion, maintenance,
    # and opt-in export enforce the same conservative boundary. The classifier
    # returns stable reason names only; never surface the matched text here.
    if contribute.private_reasons(text):
        return None
    if exact_text_exists(text, conn):
        return None
    emb = embed_fn(text)
    if not embeddings.valid_vector(emb):
        emb = None
    provenance = (
        embeddings.provenance(emb)
        if emb is not None and (runtime_default or embed_fn is embeddings.embed)
        else {}
    )
    if is_duplicate(
        emb,
        conn,
        embedding_model=provenance.get("model"),
        embedding_revision=provenance.get("revision"),
        embedding_dim=provenance.get("dimension"),
    ):
        return None
    lesson_id = id_fn()
    blob = embeddings.to_blob(emb) if emb else None
    memory_store.add_lesson(
        conn, lesson_id, text, blob, interaction_id,
        embedding_model=provenance.get("model"),
        embedding_revision=provenance.get("revision"),
        embedding_dim=provenance.get("dimension"),
    )
    return lesson_id
