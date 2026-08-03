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


# There is deliberately no "output NONE if this is environmental" escape.
# Callers only pass model-attributable failures - infrastructure errors return
# before an outcome is ever recorded - and the escape was being taken on
# plainly-code errors, which cost every pitfall the first live wave should
# have produced. A worked example replaces it: small models follow a
# demonstrated shape far more reliably than a described one.
PITFALL_SYSTEM = (
    "You extract ONE concrete, reusable pitfall from a coding attempt that "
    "FAILED. Name the specific construct, API, or syntax that broke and the "
    "concrete thing to write instead.\n"
    "Answer with ONE sentence on ONE line. No preamble, no code fences, no "
    "before/after diff, no bullet list.\n"
    "Example error: Cannot bind parameter 'RemainingScripts' from "
    "$x | ForEach-Object { ... } -join ' '\n"
    "Example answer: Parenthesise a pipeline before applying -join, because "
    "-join otherwise binds as an argument to ForEach-Object."
)


def distill_pitfall(task, response, error, offload_fn):
    """One durable 'avoid X, do Y' lesson from a failed attempt, or ''."""
    detail = _readable_error(error)
    if not detail:
        return ""
    prompt = (
        "A coding attempt FAILED. Extract ONE reusable pitfall.\n\n"
        "TASK:\n%s\n\nATTEMPTED SOLUTION:\n%s\n\nOBSERVED ERROR:\n%s\n\n"
        "Name the exact construct that broke and what to write instead, as "
        "one sentence on one line. It must be actionable on a DIFFERENT "
        "future task."
        % (task, response, detail[:1200])
    )
    text = offload_fn(
        prompt=prompt, tier="code", system=PITFALL_SYSTEM,
        temperature=0.0, num_predict=70,
    )
    return _one_sentence_lesson(text, detail, response)


def _readable_error(error):
    """Flatten an error to one line without erasing what it is reporting.

    Collapsing every run of whitespace turned "expected '10 -1 30', got
    '10\\n-1\\n30'" into two identical strings, so the model was asked to
    explain a difference the prompt had just destroyed and returned nothing.
    Line breaks survive as visible escapes; only spaces and tabs collapse.
    """
    detail = str(error or "").replace("\r\n", "\n").replace("\r", "\n")
    detail = detail.replace("\n", "\\n").replace("\t", " ")
    return re.sub(r" {2,}", " ", detail).strip()


# The worked example's answer, verbatim. A small model sometimes repeats it
# instead of reading the failure, so it is refused by identity - a precise
# check that costs no yield anywhere else.
_EXAMPLE_ANSWER = (
    "parenthesise a pipeline before applying -join, because -join otherwise "
    "binds as an argument to foreach-object."
)


def _is_example_echo(sentence):
    return sentence.strip().casefold().rstrip(".") == _EXAMPLE_ANSWER.rstrip(".")


# When a program produces no output at all, the model narrates "the task was
# not implemented; implement it" - a restatement of the failure whose fix is
# the task itself. That is never transferable to a DIFFERENT task, yet it has
# anchors and shares terms, so it passes every other gate. A real pitfall
# names a construct that was MISUSED; these name one that was MISSING.
_NON_IMPLEMENTATION = re.compile(
    r"\b(?:"
    r"not (?:be )?implement(?:ed)?|did ?n['o]?t implement|does ?n['o]?t "
    r"(?:implement|perform|contain|do)|is empty and|no logic (?:for|to)|"
    r"was never (?:implemented|defined|written)|missing (?:the )?"
    r"implementation|contains? no|performs? no|no (?:actual )?"
    r"(?:sorting|logic|implementation|code) (?:was|is)"
    r")\b",
    re.I,
)


def _is_non_implementation(sentence):
    return bool(_NON_IMPLEMENTATION.search(sentence or ""))


def _shares_a_term_with(sentence, *sources):
    """True if the lesson names something the failure actually involved.

    The haystack is the error text AND the attempted code. Matching the error
    alone was too strict for wrong-output failures, whose text is a diff of
    values rather than a description: a correct lesson about `print` in a loop
    was refused because it said "outputs" while the error said "output;". The
    code names the constructs the lesson is about, which is exactly the
    relevance being tested.
    """
    haystack = " ".join(str(source or "") for source in sources).casefold()
    if not haystack.strip():
        return True
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_-]{3,}", sentence):
        if token.casefold() in haystack:
            return True
    return False


def _one_sentence_lesson(text, error="", code=""):
    """Reduce model output to a single usable sentence, or ''.

    Small models answer this prompt with a fenced before/after diff, which is
    useless once retrieved into a different task's context: it names no rule,
    only an edit to code the future task does not have. Fenced or multi-line
    output is refused rather than stored.
    """
    value = (text or "").strip()
    if not value or "```" in value:
        return ""
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if len(lines) != 1:
        return ""
    sentence = lines[0]
    if sentence.strip(".").strip().casefold() in ("none", "n/a", "no lesson"):
        return ""
    if len(sentence) < 20 or len(sentence.split()) < 5:
        return ""
    if _looks_vague(sentence):
        return ""
    if _is_example_echo(sentence):
        return ""
    if _is_non_implementation(sentence):
        return ""
    if not _shares_a_term_with(sentence, error, code):
        return ""
    return sentence


def prepare_pitfall_candidate(
    task, response, error, offload_fn, embed_fn=None,
    id_fn=memory_store.new_id,
):
    """Prepare a pitfall lesson using the same privacy/embedding path."""
    text = distill_pitfall(task, response, error, offload_fn)
    if not text:
        return {"status": "no_lesson", "reason": "not_concrete"}
    return _prepare_candidate_text(text, embed_fn, id_fn)


def _prepare_candidate_text(text, embed_fn, id_fn):
    runtime_default = embed_fn is None
    embed_fn = embed_fn or embeddings.embed
    if contribute.private_reasons(text):
        return {"status": "no_lesson", "reason": "private"}
    emb = embed_fn(text)
    if not embeddings.valid_vector(emb):
        emb = None
    provenance = (
        embeddings.provenance(emb)
        if emb is not None and (runtime_default or embed_fn is embeddings.embed)
        else {}
    )
    return {
        "status": "candidate",
        "lesson_id": id_fn(),
        "text": text,
        "embedding": emb,
        "embedding_blob": embeddings.to_blob(emb) if emb else None,
        "embedding_model": provenance.get("model"),
        "embedding_revision": provenance.get("revision"),
        "embedding_dim": provenance.get("dimension"),
    }


def prepare_lesson_candidate(
    task, response, signal, offload_fn, embed_fn=None,
    id_fn=memory_store.new_id,
):
    """Generate and embed a candidate without reading or mutating SQLite."""
    runtime_default = embed_fn is None
    embed_fn = embed_fn or embeddings.embed
    text = distill(task, response, signal, offload_fn)
    if not text:
        return {"status": "no_lesson", "reason": "not_concrete"}
    # Keep secret- and path-like material out of both the lesson row and its
    # FTS mirror. The classifier returns stable reason names only; never retain
    # or surface the matched text in status metadata.
    if contribute.private_reasons(text):
        return {"status": "no_lesson", "reason": "private"}
    emb = embed_fn(text)
    if not embeddings.valid_vector(emb):
        emb = None
    provenance = (
        embeddings.provenance(emb)
        if emb is not None and (runtime_default or embed_fn is embeddings.embed)
        else {}
    )
    return {
        "status": "candidate",
        "lesson_id": id_fn(),
        "text": text,
        "embedding": emb,
        "embedding_blob": embeddings.to_blob(emb) if emb else None,
        "embedding_model": provenance.get("model"),
        "embedding_revision": provenance.get("revision"),
        "embedding_dim": provenance.get("dimension"),
    }


def store_prepared_lesson(conn, interaction_id, candidate):
    """Deduplicate and store a prepared candidate inside an active transaction.

    The returned mapping is the callback contract accepted by
    ``memory_store.finalize_lesson_distillation``. Candidate generation and
    embedding deliberately happen before this function so the database write
    lock covers only deterministic dedupe and publication work.
    """
    if not conn.in_transaction:
        raise RuntimeError("store_prepared_lesson requires an active transaction")
    if not isinstance(candidate, dict):
        raise TypeError("candidate must be a mapping")
    if candidate.get("status") != "candidate":
        return {
            "terminal_state": memory_store.DISTILLATION_NO_LESSON,
            "result": candidate.get("reason") or "no_lesson",
        }

    existing = conn.execute(
        "SELECT id FROM lessons WHERE source_interaction=? "
        "ORDER BY rowid ASC LIMIT 1",
        (interaction_id,),
    ).fetchone()
    if existing is not None:
        return {
            "terminal_state": memory_store.DISTILLATION_STORED,
            "lesson_id": existing["id"],
            "result": "existing_interaction_lesson",
        }

    text = str(candidate.get("text") or "").strip()
    lesson_id = str(candidate.get("lesson_id") or "").strip()
    if not text or not lesson_id:
        raise ValueError("prepared candidate requires lesson_id and text")
    if exact_text_exists(text, conn):
        return {
            "terminal_state": memory_store.DISTILLATION_NO_LESSON,
            "result": "exact_duplicate",
        }
    if is_duplicate(
        candidate.get("embedding"),
        conn,
        embedding_model=candidate.get("embedding_model"),
        embedding_revision=candidate.get("embedding_revision"),
        embedding_dim=candidate.get("embedding_dim"),
    ):
        return {
            "terminal_state": memory_store.DISTILLATION_NO_LESSON,
            "result": "semantic_duplicate",
        }

    memory_store.insert_lesson_in_transaction(
        conn,
        lesson_id,
        text,
        candidate.get("embedding_blob"),
        interaction_id,
        embedding_model=candidate.get("embedding_model"),
        embedding_revision=candidate.get("embedding_revision"),
        embedding_dim=candidate.get("embedding_dim"),
    )
    return {
        "terminal_state": memory_store.DISTILLATION_STORED,
        "lesson_id": lesson_id,
        "result": "stored",
    }


def maybe_add_lesson(conn, interaction_id, task, response, signal, offload_fn,
                     embed_fn=None, id_fn=memory_store.new_id):
    if memory_store.lesson_exists_for_interaction(conn, interaction_id):
        return None
    candidate = prepare_lesson_candidate(
        task, response, signal, offload_fn, embed_fn=embed_fn, id_fn=id_fn,
    )
    if candidate["status"] != "candidate":
        return None
    text = candidate["text"]
    if exact_text_exists(text, conn):
        return None
    if is_duplicate(
        candidate["embedding"],
        conn,
        embedding_model=candidate["embedding_model"],
        embedding_revision=candidate["embedding_revision"],
        embedding_dim=candidate["embedding_dim"],
    ):
        return None
    lesson_id = candidate["lesson_id"]
    memory_store.add_lesson(
        conn, lesson_id, text, candidate["embedding_blob"], interaction_id,
        embedding_model=candidate["embedding_model"],
        embedding_revision=candidate["embedding_revision"],
        embedding_dim=candidate["embedding_dim"],
    )
    return lesson_id
