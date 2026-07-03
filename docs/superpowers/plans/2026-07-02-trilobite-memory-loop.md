# Trilobite Memory + Reward Loop — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the `local-llm` MCP server so local coding traffic is memory-augmented, captured, scored by real outcomes, and distilled into retrievable lessons — a self-improving loop around a frozen Ollama model, branded `trilobite`.

**Architecture:** Flat sibling modules in `~/.claude/mcp-servers/local-llm/`, imported by `server.py`. Pure logic (store, retrieval, reward, reflection, orchestration) is isolated behind injectable functions so it is unit-testable without a live Ollama. `server.py` wires the Ollama HTTP calls into that logic and exposes three tool surfaces: the wrapped `offload` (learns transparently), a new `trilobite` tool (interactive front door), and `record_outcome` (feeds the reward signal back).

**Tech Stack:** Python 3.12 (stdlib only for core: `sqlite3`+FTS5, `array`, `urllib`, `re`, `os`, `threading`), `mcp` (FastMCP, already installed), Ollama HTTP API, `pytest` (dev dep), `nomic-embed-text` (Ollama embed model).

## Global Constraints

- **Repo root:** `~/Trilobite-model` — all paths below are relative to it. All commands run with cwd = repo root.
- **Python interpreter:** always `./venv/Scripts/python.exe` (the server's venv). Never bare `python`.
- **Core modules are stdlib-only** (plus `mcp` in `server.py`). No third-party runtime deps beyond `mcp`. `pytest` is dev-only.
- **Privacy:** the learning path is LOCAL tiers only. Cloud tiers (`cloud-code`, `cloud-general`) MUST NOT retrieve, capture, or reflect — they take the original non-learning path.
- **Return contract:** learning calls return the model's text with exactly one trailing footer line `\n\n[interaction_id: <hex>]`. Non-learning calls (cloud, `learn=False`) return pure text, no footer.
- **`offload` default:** `learn=True`.
- **IDs:** lowercase hex from `os.urandom(8).hex()` (16 chars).
- **DB file:** `memory.db` at repo root (gitignored). Server opens it with `check_same_thread=False` and serializes tool bodies with a module-level `threading.Lock`.
- **Names:** the model/tool/alias is `trilobite`. The Ollama alias is `trilobite` (`FROM qwen2.5-coder:7b`); tool falls back to the raw `code` tier model if the alias is absent.
- **Soft-fail retrieval:** if embeddings are unavailable, retrieval degrades to lexical-only; it never errors the generation call.

---

### Task 1: Project setup & test harness

**Files:**
- Create: `conftest.py`
- Create: `tests/__init__.py` (empty)
- Modify: `requirements-dev.txt` (create)

- [ ] **Step 1: Install pytest into the venv**

Run:
```bash
./venv/Scripts/python.exe -m pip install pytest
```
Expected: ends with `Successfully installed pytest-...`

- [ ] **Step 2: Pull the embedding model**

Run:
```bash
ollama pull nomic-embed-text
```
Expected: ends with `success`. (If offline, retrieval will still degrade gracefully to lexical-only; the pull can be retried later.)

- [ ] **Step 3: Create `requirements-dev.txt`**

```
pytest
```

- [ ] **Step 4: Create `conftest.py` (puts repo root on sys.path so tests can import the flat modules)**

```python
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
```

- [ ] **Step 5: Create empty `tests/__init__.py`**

(empty file)

- [ ] **Step 6: Verify pytest runs (collects zero tests)**

Run:
```bash
./venv/Scripts/python.exe -m pytest -q
```
Expected: `no tests ran` (exit code 5 is fine — nothing to collect yet).

- [ ] **Step 7: Commit**

```bash
git add conftest.py tests/__init__.py requirements-dev.txt
git commit -m "chore: add pytest harness and dev deps for trilobite loop"
```

---

### Task 2: Memory store (`memory_store.py`)

SQLite schema + CRUD + FTS5 lexical search. No ORM.

**Files:**
- Create: `memory_store.py`
- Test: `tests/test_memory_store.py`

**Interfaces:**
- Produces:
  - `connect(path=":memory:", check_same_thread=True) -> sqlite3.Connection`
  - `init_db(conn) -> None`
  - `new_id() -> str` (16-char hex)
  - `log_interaction(conn, interaction_id, task, retrieved_ctx, response, tier) -> None`
  - `get_interaction(conn, interaction_id) -> dict | None` (keys: id, task, retrieved_ctx, response, tier, ts)
  - `record_outcome_row(conn, interaction_id, signal, reward) -> None`
  - `add_lesson(conn, lesson_id, text, embedding: bytes | None, source_interaction) -> None`
  - `all_lessons(conn) -> list[dict]` (keys: id, text, embedding)
  - `get_lesson_text(conn, lesson_id) -> str | None`
  - `fts_search(conn, query, limit=10) -> list[str]` (lesson_ids, best first; `[]` for empty/only-stopword queries)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_memory_store.py
import memory_store as ms


def _conn():
    return ms.connect(":memory:")


def test_new_id_is_16_hex():
    i = ms.new_id()
    assert len(i) == 16
    int(i, 16)  # parses as hex


def test_log_and_get_interaction_roundtrip():
    c = _conn()
    ms.log_interaction(c, "abc", "do X", "ctx", "resp", "code")
    got = ms.get_interaction(c, "abc")
    assert got["task"] == "do X"
    assert got["response"] == "resp"
    assert got["tier"] == "code"


def test_get_missing_interaction_returns_none():
    assert ms.get_interaction(_conn(), "nope") is None


def test_record_outcome_row():
    c = _conn()
    ms.log_interaction(c, "abc", "t", "", "r", "code")
    ms.record_outcome_row(c, "abc", "tests_passed", 1.0)
    row = c.execute("SELECT signal, reward FROM outcomes WHERE interaction_id='abc'").fetchone()
    assert row[0] == "tests_passed"
    assert row[1] == 1.0


def test_add_lesson_and_read_back():
    c = _conn()
    ms.add_lesson(c, "L1", "always free the lock", b"\x00\x01", "abc")
    assert ms.get_lesson_text(c, "L1") == "always free the lock"
    lessons = ms.all_lessons(c)
    assert lessons[0]["id"] == "L1"
    assert lessons[0]["embedding"] == b"\x00\x01"


def test_fts_search_matches_tokens():
    c = _conn()
    ms.add_lesson(c, "L1", "use RRF fusion for hybrid retrieval", None, "a")
    ms.add_lesson(c, "L2", "close the sqlite connection", None, "b")
    hits = ms.fts_search(c, "hybrid retrieval fusion")
    assert "L1" in hits
    assert hits[0] == "L1"


def test_fts_search_empty_query_returns_empty():
    c = _conn()
    ms.add_lesson(c, "L1", "anything", None, "a")
    assert ms.fts_search(c, "a to") == []  # only short/stopword tokens -> no query
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/Scripts/python.exe -m pytest tests/test_memory_store.py -q`
Expected: FAIL / collection error — `No module named 'memory_store'`.

- [ ] **Step 3: Write `memory_store.py`**

```python
"""SQLite-backed memory for the trilobite learning loop. Stdlib only."""
import os
import re
import sqlite3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS interactions (
    id TEXT PRIMARY KEY,
    task TEXT,
    retrieved_ctx TEXT,
    response TEXT,
    tier TEXT,
    ts TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS outcomes (
    interaction_id TEXT,
    signal TEXT,
    reward REAL,
    ts TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS lessons (
    id TEXT PRIMARY KEY,
    text TEXT,
    embedding BLOB,
    source_interaction TEXT,
    ts TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE VIRTUAL TABLE IF NOT EXISTS lessons_fts USING fts5(lesson_id UNINDEXED, text);
"""


def connect(path=":memory:", check_same_thread=True):
    conn = sqlite3.connect(path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def init_db(conn):
    conn.executescript(_SCHEMA)
    conn.commit()


def new_id():
    return os.urandom(8).hex()


def log_interaction(conn, interaction_id, task, retrieved_ctx, response, tier):
    conn.execute(
        "INSERT INTO interactions(id, task, retrieved_ctx, response, tier) "
        "VALUES(?, ?, ?, ?, ?)",
        (interaction_id, task, retrieved_ctx, response, tier),
    )
    conn.commit()


def get_interaction(conn, interaction_id):
    row = conn.execute(
        "SELECT * FROM interactions WHERE id=?", (interaction_id,)
    ).fetchone()
    return dict(row) if row else None


def record_outcome_row(conn, interaction_id, signal, reward):
    conn.execute(
        "INSERT INTO outcomes(interaction_id, signal, reward) VALUES(?, ?, ?)",
        (interaction_id, signal, reward),
    )
    conn.commit()


def add_lesson(conn, lesson_id, text, embedding, source_interaction):
    conn.execute(
        "INSERT INTO lessons(id, text, embedding, source_interaction) VALUES(?, ?, ?, ?)",
        (lesson_id, text, embedding, source_interaction),
    )
    conn.execute(
        "INSERT INTO lessons_fts(lesson_id, text) VALUES(?, ?)", (lesson_id, text)
    )
    conn.commit()


def all_lessons(conn):
    rows = conn.execute("SELECT id, text, embedding FROM lessons").fetchall()
    return [dict(r) for r in rows]


def get_lesson_text(conn, lesson_id):
    row = conn.execute("SELECT text FROM lessons WHERE id=?", (lesson_id,)).fetchone()
    return row[0] if row else None


def _sanitize_fts(query):
    # FTS5 MATCH chokes on raw punctuation; reduce to quoted word tokens OR'd together.
    toks = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 2][:32]
    return " OR ".join('"%s"' % t for t in toks)


def fts_search(conn, query, limit=10):
    q = _sanitize_fts(query)
    if not q:
        return []
    rows = conn.execute(
        "SELECT lesson_id FROM lessons_fts WHERE lessons_fts MATCH ? "
        "ORDER BY rank LIMIT ?",
        (q, limit),
    ).fetchall()
    return [r[0] for r in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/Scripts/python.exe -m pytest tests/test_memory_store.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add memory_store.py tests/test_memory_store.py
git commit -m "feat: sqlite memory store with FTS5 lexical search"
```

---

### Task 3: Embeddings (`embeddings.py`)

Local Ollama embeddings + vector packing + cosine. Soft-fails to `None`.

**Files:**
- Create: `embeddings.py`
- Test: `tests/test_embeddings.py`

**Interfaces:**
- Produces:
  - `to_blob(vec: list[float]) -> bytes`
  - `from_blob(b: bytes) -> list[float]`
  - `cosine(a: list[float], b: list[float]) -> float` (0.0 on empty/zero/mismatched)
  - `embed(text, timeout=30) -> list[float] | None` (None on any failure)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_embeddings.py
import embeddings as e


def test_blob_roundtrip():
    v = [0.5, -1.25, 3.0]
    back = e.from_blob(e.to_blob(v))
    assert len(back) == 3
    assert abs(back[0] - 0.5) < 1e-6
    assert abs(back[1] + 1.25) < 1e-6


def test_cosine_identical_is_one():
    assert abs(e.cosine([1.0, 0.0], [1.0, 0.0]) - 1.0) < 1e-6


def test_cosine_orthogonal_is_zero():
    assert abs(e.cosine([1.0, 0.0], [0.0, 1.0])) < 1e-6


def test_cosine_handles_empty():
    assert e.cosine([], [1.0]) == 0.0
    assert e.cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_embed_soft_fails_to_none(monkeypatch):
    def boom(*a, **k):
        raise OSError("no ollama")

    monkeypatch.setattr(e.urllib.request, "urlopen", boom)
    assert e.embed("anything") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/Scripts/python.exe -m pytest tests/test_embeddings.py -q`
Expected: FAIL — `No module named 'embeddings'`.

- [ ] **Step 3: Write `embeddings.py`**

```python
"""Local Ollama embeddings + vector helpers. Stdlib only. Soft-fails to None."""
import array
import json
import os
import urllib.error
import urllib.request

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434").replace("http://", "")
BASE = "http://%s" % OLLAMA_HOST
EMBED_MODEL = os.environ.get("TRILOBITE_EMBED_MODEL", "nomic-embed-text")


def to_blob(vec):
    return array.array("f", vec).tobytes()


def from_blob(b):
    a = array.array("f")
    a.frombytes(b)
    return list(a)


def cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def embed(text, timeout=30):
    payload = json.dumps({"model": EMBED_MODEL, "prompt": text}).encode("utf-8")
    req = urllib.request.Request(
        "%s/api/embeddings" % BASE,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")).get("embedding")
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/Scripts/python.exe -m pytest tests/test_embeddings.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add embeddings.py tests/test_embeddings.py
git commit -m "feat: local Ollama embeddings with cosine + soft-fail"
```

---

### Task 4: Retriever (`retriever.py`)

Hybrid lexical + semantic retrieval fused with Reciprocal-Rank Fusion.

**Files:**
- Create: `retriever.py`
- Test: `tests/test_retriever.py`

**Interfaces:**
- Consumes: `memory_store.fts_search`, `memory_store.all_lessons`, `memory_store.get_lesson_text`, `embeddings.embed`, `embeddings.from_blob`, `embeddings.cosine`
- Produces:
  - `rrf(rank_lists: list[list], k=60) -> list` (items ranked by fused score)
  - `semantic_search(conn, task, embed_fn=embeddings.embed, limit=10) -> list[str]` (lesson_ids; `[]` if `embed_fn` returns None)
  - `retrieve(conn, task, k=5, embed_fn=embeddings.embed) -> list[str]` (lesson texts, best first)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_retriever.py
import embeddings as e
import memory_store as ms
import retriever as r


def test_rrf_rewards_agreement():
    # item "B" appears high in both lists -> should win.
    fused = r.rrf([["A", "B", "C"], ["B", "A", "D"]])
    assert fused[0] == "B"


def test_semantic_search_uses_embeddings():
    c = ms.connect(":memory:")
    ms.add_lesson(c, "near", "x", e.to_blob([1.0, 0.0]), "i")
    ms.add_lesson(c, "far", "y", e.to_blob([0.0, 1.0]), "i")
    hits = r.semantic_search(c, "query", embed_fn=lambda t: [0.9, 0.1])
    assert hits[0] == "near"


def test_semantic_search_empty_when_no_embeddings():
    c = ms.connect(":memory:")
    ms.add_lesson(c, "near", "x", e.to_blob([1.0, 0.0]), "i")
    assert r.semantic_search(c, "q", embed_fn=lambda t: None) == []


def test_retrieve_returns_texts_and_degrades_to_lexical():
    c = ms.connect(":memory:")
    ms.add_lesson(c, "L1", "always release the threading lock", None, "i")
    ms.add_lesson(c, "L2", "prefer RRF for hybrid ranking", None, "i")
    # embeddings unavailable -> lexical only, still finds the lock lesson.
    texts = r.retrieve(c, "threading lock release", embed_fn=lambda t: None)
    assert any("threading lock" in t for t in texts)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/Scripts/python.exe -m pytest tests/test_retriever.py -q`
Expected: FAIL — `No module named 'retriever'`.

- [ ] **Step 3: Write `retriever.py`**

```python
"""Hybrid lexical+semantic retrieval over distilled lessons. RRF fusion."""
import embeddings
import memory_store


def rrf(rank_lists, k=60):
    scores = {}
    for lst in rank_lists:
        for rank, item in enumerate(lst):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores, key=lambda i: -scores[i])


def semantic_search(conn, task, embed_fn=embeddings.embed, limit=10):
    qv = embed_fn(task)
    if qv is None:
        return []
    scored = []
    for les in memory_store.all_lessons(conn):
        emb = les["embedding"]
        if not emb:
            continue
        v = embeddings.from_blob(emb)
        scored.append((embeddings.cosine(qv, v), les["id"]))
    scored.sort(reverse=True)
    return [lid for _, lid in scored[:limit]]


def retrieve(conn, task, k=5, embed_fn=embeddings.embed):
    lexical = memory_store.fts_search(conn, task, limit=10)
    semantic = semantic_search(conn, task, embed_fn=embed_fn, limit=10)
    fused = rrf([lexical, semantic])[:k]
    texts = [memory_store.get_lesson_text(conn, lid) for lid in fused]
    return [t for t in texts if t]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/Scripts/python.exe -m pytest tests/test_retriever.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add retriever.py tests/test_retriever.py
git commit -m "feat: hybrid RRF retriever over lessons"
```

---

### Task 5: Reward scoring (`reward.py`)

Map outcome signals to scalar rewards + goodness threshold.

**Files:**
- Create: `reward.py`
- Test: `tests/test_reward.py`

**Interfaces:**
- Produces:
  - `SIGNAL_REWARDS: dict[str, float]`
  - `VALID_SIGNALS: set[str]`
  - `GOOD_THRESHOLD: float = 0.7`
  - `score(signal) -> float` (0.0 for unknown)
  - `is_good(signal) -> bool`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_reward.py
import reward


def test_known_signals_score():
    assert reward.score("tests_passed") == 1.0
    assert reward.score("failed") == -1.0


def test_unknown_signal_is_zero():
    assert reward.score("banana") == 0.0


def test_is_good_threshold():
    assert reward.is_good("tests_passed") is True
    assert reward.is_good("compiled") is True   # 0.7, at threshold
    assert reward.is_good("rejected") is False


def test_valid_signals_set():
    assert "accepted" in reward.VALID_SIGNALS
    assert "banana" not in reward.VALID_SIGNALS
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/Scripts/python.exe -m pytest tests/test_reward.py -q`
Expected: FAIL — `No module named 'reward'`.

- [ ] **Step 3: Write `reward.py`**

```python
"""Outcome signal -> scalar reward. Execution-grounded signals weighted highest."""

SIGNAL_REWARDS = {
    "tests_passed": 1.0,
    "accepted": 0.8,
    "compiled": 0.7,
    "rejected": -0.5,
    "failed": -1.0,
}
VALID_SIGNALS = set(SIGNAL_REWARDS)
GOOD_THRESHOLD = 0.7


def score(signal):
    return SIGNAL_REWARDS.get(signal, 0.0)


def is_good(signal):
    return score(signal) >= GOOD_THRESHOLD
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/Scripts/python.exe -m pytest tests/test_reward.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add reward.py tests/test_reward.py
git commit -m "feat: outcome->reward scoring"
```

---

### Task 6: Reflection (`reflection.py`)

Distill a one-line lesson from a good outcome; dedup by embedding similarity.

**Files:**
- Create: `reflection.py`
- Test: `tests/test_reflection.py`

**Interfaces:**
- Consumes: `memory_store.new_id`, `memory_store.add_lesson`, `memory_store.all_lessons`, `embeddings.embed`, `embeddings.to_blob`, `embeddings.from_blob`, `embeddings.cosine`
- Produces:
  - `DUP_THRESHOLD: float = 0.92`
  - `distill(task, response, signal, offload_fn) -> str` — calls `offload_fn(prompt=..., tier="fast", system=..., temperature=0.2, num_predict=60)`
  - `is_duplicate(new_emb, conn, threshold=DUP_THRESHOLD) -> bool`
  - `maybe_add_lesson(conn, interaction_id, task, response, signal, offload_fn, embed_fn=embeddings.embed, id_fn=memory_store.new_id) -> str | None` (returns new lesson id, or None if empty/duplicate)

- [ ] **Step 1: Write the failing tests**

```python
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


def test_maybe_add_lesson_skips_empty_distill():
    c = ms.connect(":memory:")
    lid = reflection.maybe_add_lesson(
        c, "i1", "task", "resp", "tests_passed",
        offload_fn=lambda **kw: "   ", embed_fn=lambda t: [1.0, 0.0],
    )
    assert lid is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/Scripts/python.exe -m pytest tests/test_reflection.py -q`
Expected: FAIL — `No module named 'reflection'`.

- [ ] **Step 3: Write `reflection.py`**

```python
"""Distill reusable lessons from good outcomes; dedup by embedding similarity."""
import embeddings
import memory_store

DUP_THRESHOLD = 0.92
DISTILL_SYSTEM = (
    "You distill a single reusable coding lesson. Output one short imperative "
    "sentence, no preamble, no markdown."
)


def distill(task, response, signal, offload_fn):
    prompt = (
        "A coding task was completed with outcome '%s'.\n\n"
        "TASK:\n%s\n\nSOLUTION:\n%s\n\n"
        "Write ONE short imperative lesson (max 25 words) capturing the reusable "
        "insight for similar future tasks. No preamble." % (signal, task, response)
    )
    text = offload_fn(
        prompt=prompt, tier="fast", system=DISTILL_SYSTEM,
        temperature=0.2, num_predict=60,
    )
    return (text or "").strip()


def is_duplicate(new_emb, conn, threshold=DUP_THRESHOLD):
    if new_emb is None:
        return False
    for les in memory_store.all_lessons(conn):
        emb = les["embedding"]
        if emb and embeddings.cosine(new_emb, embeddings.from_blob(emb)) >= threshold:
            return True
    return False


def maybe_add_lesson(conn, interaction_id, task, response, signal, offload_fn,
                     embed_fn=embeddings.embed, id_fn=memory_store.new_id):
    text = distill(task, response, signal, offload_fn)
    if not text:
        return None
    emb = embed_fn(text)
    if is_duplicate(emb, conn):
        return None
    lesson_id = id_fn()
    blob = embeddings.to_blob(emb) if emb else None
    memory_store.add_lesson(conn, lesson_id, text, blob, interaction_id)
    return lesson_id
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/Scripts/python.exe -m pytest tests/test_reflection.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add reflection.py tests/test_reflection.py
git commit -m "feat: reflection distills deduped lessons from good outcomes"
```

---

### Task 7: Orchestrator (`orchestrator.py`)

The pure learning flow: retrieve → augment → generate → capture. Injectable `generate_fn` so it is testable without Ollama.

**Files:**
- Create: `orchestrator.py`
- Test: `tests/test_orchestrator.py`

**Interfaces:**
- Consumes: `memory_store.log_interaction`, `memory_store.new_id`, `retriever.retrieve`
- Produces:
  - `MEMORY_HEADER: str`
  - `build_prompt(task, lessons: list[str]) -> str` (returns `task` unchanged if no lessons)
  - `run_with_learning(conn, task, tier, generate_fn, retrieve_fn=retriever.retrieve, id_fn=memory_store.new_id) -> (response: str, interaction_id: str)` — `generate_fn(augmented_prompt) -> str`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_orchestrator.py
import memory_store as ms
import orchestrator as o


def test_build_prompt_no_lessons_is_passthrough():
    assert o.build_prompt("do X", []) == "do X"


def test_build_prompt_prepends_lessons():
    p = o.build_prompt("do X", ["lessonA", "lessonB"])
    assert "lessonA" in p and "lessonB" in p and "do X" in p
    assert p.index("lessonA") < p.index("do X")  # memories come first


def test_run_with_learning_captures_and_returns_id():
    c = ms.connect(":memory:")
    seen = {}

    def gen(prompt):
        seen["prompt"] = prompt
        return "the answer"

    resp, iid = o.run_with_learning(
        c, "fix the bug", "code", gen,
        retrieve_fn=lambda conn, task: ["prefer RRF"],
        id_fn=lambda: "fixed123",
    )
    assert resp == "the answer"
    assert iid == "fixed123"
    assert "prefer RRF" in seen["prompt"]          # retrieval was injected
    row = ms.get_interaction(c, "fixed123")
    assert row["task"] == "fix the bug"
    assert row["response"] == "the answer"
    assert row["tier"] == "code"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/Scripts/python.exe -m pytest tests/test_orchestrator.py -q`
Expected: FAIL — `No module named 'orchestrator'`.

- [ ] **Step 3: Write `orchestrator.py`**

```python
"""Pure learning flow: retrieve -> augment -> generate -> capture."""
import memory_store
import retriever

MEMORY_HEADER = "# Relevant lessons from past work (may help):"


def build_prompt(task, lessons):
    if not lessons:
        return task
    block = "\n".join("- %s" % l for l in lessons)
    return "%s\n%s\n\n# Task:\n%s" % (MEMORY_HEADER, block, task)


def run_with_learning(conn, task, tier, generate_fn,
                      retrieve_fn=retriever.retrieve, id_fn=memory_store.new_id):
    lessons = retrieve_fn(conn, task)
    augmented = build_prompt(task, lessons)
    response = generate_fn(augmented)
    interaction_id = id_fn()
    memory_store.log_interaction(
        conn, interaction_id, task, "\n".join(lessons), response, tier
    )
    return response, interaction_id
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/Scripts/python.exe -m pytest tests/test_orchestrator.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator.py tests/test_orchestrator.py
git commit -m "feat: orchestrator ties retrieval + capture around generation"
```

---

### Task 8: Server wiring (`server.py`)

Wire the pure logic into Ollama HTTP calls; add `learn` to `offload`, add `trilobite` and `record_outcome` tools, footer helpers, thread-safe DB. Unit-test the pure helpers (footer, id, prompt); the tool bodies themselves are exercised by the manual smoke test in Task 9.

**Files:**
- Modify: `server.py`
- Test: `tests/test_server_helpers.py`

**Interfaces:**
- Consumes: all modules above; existing `_post`, `_get`, `TIERS`, `CLOUD_TIERS`, `KEEP_ALIVE`.
- Produces (module-level, importable for tests):
  - `FOOTER_PREFIX: str = "\n\n[interaction_id: "`
  - `with_footer(text, interaction_id) -> str`
  - `parse_interaction_id(text) -> str | None`
  - `resolve_trilobite_model() -> str` (returns `"trilobite"` if the alias exists in `/api/tags`, else `TIERS["code"]`)
  - tools: `offload(..., learn=True)`, `trilobite(...)`, `record_outcome(interaction_id, signal)`

- [ ] **Step 1: Write the failing tests (pure helpers only)**

```python
# tests/test_server_helpers.py
import server


def test_with_footer_and_parse_roundtrip():
    out = server.with_footer("here is code", "abc123def4567890")
    assert out.endswith("[interaction_id: abc123def4567890]")
    assert server.parse_interaction_id(out) == "abc123def4567890"


def test_parse_none_when_absent():
    assert server.parse_interaction_id("just some text") is None


def test_resolve_trilobite_falls_back(monkeypatch):
    # no alias present -> code tier model
    monkeypatch.setattr(server, "_get", lambda path: {"models": [{"name": "qwen2.5:3b"}]})
    assert server.resolve_trilobite_model() == server.TIERS["code"]


def test_resolve_trilobite_prefers_alias(monkeypatch):
    monkeypatch.setattr(server, "_get", lambda path: {"models": [{"name": "trilobite:latest"}]})
    assert server.resolve_trilobite_model() == "trilobite"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/Scripts/python.exe -m pytest tests/test_server_helpers.py -q`
Expected: FAIL — `AttributeError: module 'server' has no attribute 'with_footer'`.

- [ ] **Step 3: Edit `server.py` — add imports + helpers below the existing `TIERS`/`CLOUD_TIERS` block (after line ~42, before `mcp = FastMCP(...)`)**

```python
import re
import threading

import memory_store
import orchestrator
import reward
import reflection
import embeddings  # noqa: F401  (ensures module import side-effects/config load)

_DB_PATH = os.path.join(os.path.dirname(__file__), "memory.db")
_DB = None
_LOCK = threading.Lock()

FOOTER_PREFIX = "\n\n[interaction_id: "
_FOOTER_RE = re.compile(r"\[interaction_id: ([0-9a-f]+)\]\s*$")


def _db():
    global _DB
    if _DB is None:
        _DB = memory_store.connect(_DB_PATH, check_same_thread=False)
    return _DB


def with_footer(text, interaction_id):
    return "%s%s%s]" % (text, FOOTER_PREFIX, interaction_id)


def parse_interaction_id(text):
    m = _FOOTER_RE.search(text or "")
    return m.group(1) if m else None


def resolve_trilobite_model():
    try:
        tags = [m.get("name", "") for m in _get("/api/tags").get("models", [])]
    except Exception:
        tags = []
    if any(t.split(":")[0] == "trilobite" for t in tags):
        return "trilobite"
    return TIERS["code"]


def _make_generate(model, system, temperature, num_predict, num_ctx):
    def gen(prompt):
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        options = {"temperature": temperature, "num_predict": num_predict,
                   "num_ctx": num_ctx}
        payload = {"model": model, "messages": messages, "stream": False,
                   "options": options, "keep_alive": KEEP_ALIVE}
        out = _post("/api/chat", payload)
        return out.get("message", {}).get("content", "")
    return gen


def _generate_text(prompt, tier="fast", system="", temperature=0.2,
                   num_predict=256, num_ctx=2048):
    model = TIERS.get(tier, TIERS["fast"])
    return _make_generate(model, system, temperature, num_predict, num_ctx)(prompt)
```

- [ ] **Step 4: Edit `server.py` — replace the existing `offload` body to add `learn` + the learning path.**

Replace the whole `@mcp.tool() def offload(...)` function (currently lines ~62-128) with:

```python
@mcp.tool()
def offload(
    prompt: str,
    tier: str = "fast",
    system: str = "",
    temperature: float = 0.2,
    num_predict: int = 1024,
    num_ctx: int = 4096,
    learn: bool = True,
) -> str:
    """Offload a self-contained subtask to a local-GPU or Ollama-cloud model.

    Local tiers (fast/code/general) run privately on the 6 GB 4050. When learn=True
    (default) a local call is memory-augmented and captured: the response ends with
    a '[interaction_id: <id>]' footer you can pass to record_outcome once you know
    whether it compiled / passed tests. Cloud tiers never learn (data privacy).
    Set learn=False for throwaway work you don't want captured (pure text, no footer).

    Tiers: fast=3B (default), code=7B coder, general=7B instruct,
    cloud-code / cloud-general (METERED, prompt leaves this machine).
    Give a FULLY self-contained prompt (the model can't see this chat or your files).
    """
    model = TIERS.get(tier)
    if model is None:
        return "ERROR: unknown tier '%s'. Valid tiers: %s." % (tier, ", ".join(TIERS))

    # Cloud tiers and opt-out both take the plain, non-learning path.
    if tier in CLOUD_TIERS or not learn:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        options = {"temperature": temperature, "num_predict": num_predict}
        payload = {"model": model, "messages": messages, "stream": False,
                   "options": options}
        if tier not in CLOUD_TIERS:
            payload["keep_alive"] = KEEP_ALIVE
            options["num_ctx"] = num_ctx
        try:
            out = _post("/api/chat", payload)
        except urllib.error.URLError as e:
            return ("ERROR contacting Ollama at %s: %s. Is the Ollama server "
                    "running? (`ollama serve`)" % (BASE, e))
        msg = out.get("message", {}).get("content", "")
        return msg if msg else "(empty response) raw=%s" % json.dumps(out)[:500]

    # Learning path (local tiers only).
    gen = _make_generate(model, system, temperature, num_predict, num_ctx)
    try:
        with _LOCK:
            response, iid = orchestrator.run_with_learning(_db(), prompt, tier, gen)
    except urllib.error.URLError as e:
        return ("ERROR contacting Ollama at %s: %s. Is the Ollama server "
                "running? (`ollama serve`)" % (BASE, e))
    return with_footer(response, iid)
```

- [ ] **Step 5: Edit `server.py` — add the `trilobite` and `record_outcome` tools immediately after `offload`.**

```python
@mcp.tool()
def trilobite(
    prompt: str,
    tier: str = "code",
    system: str = "",
    temperature: float = 0.2,
    num_predict: int = 1024,
    num_ctx: int = 4096,
) -> str:
    """Ask 'trilobite', the local self-improving coding model, for help.

    This is the interactive front door to the same learning loop the fleet uses:
    the prompt is augmented with lessons distilled from past work, answered locally
    on the 4050, captured, and returned with a '[interaction_id: <id>]' footer.
    After you learn how it went, call record_outcome(<id>, "tests_passed" | "accepted"
    | "compiled" | "rejected" | "failed") so trilobite gets better over time.
    Defaults to the 7B coder / the 'trilobite' Ollama alias if it exists.
    """
    if tier == "code":
        model = resolve_trilobite_model()
    else:
        model = TIERS.get(tier, resolve_trilobite_model())
    gen = _make_generate(model, system, temperature, num_predict, num_ctx)
    try:
        with _LOCK:
            response, iid = orchestrator.run_with_learning(
                _db(), prompt, "trilobite", gen
            )
    except urllib.error.URLError as e:
        return ("ERROR contacting Ollama at %s: %s. Is the Ollama server "
                "running? (`ollama serve`)" % (BASE, e))
    return with_footer(response, iid)


@mcp.tool()
def record_outcome(interaction_id: str, signal: str) -> str:
    """Feed a real-world outcome back into trilobite's learning loop.

    Call this after a trilobite/offload response once you know how it went.
    signal is one of: tests_passed, accepted, compiled, rejected, failed.
    A good outcome triggers a distilled 'lesson' that future prompts will retrieve.
    Pass the id from the '[interaction_id: <id>]' footer of the response.
    """
    if signal not in reward.VALID_SIGNALS:
        return "ERROR: unknown signal '%s'. Valid: %s." % (
            signal, ", ".join(sorted(reward.VALID_SIGNALS)))
    with _LOCK:
        conn = _db()
        inter = memory_store.get_interaction(conn, interaction_id)
        if inter is None:
            return "ERROR: no interaction '%s' (already expired or wrong id)." % interaction_id
        r = reward.score(signal)
        memory_store.record_outcome_row(conn, interaction_id, signal, r)
        lesson_id = None
        if reward.is_good(signal):
            lesson_id = reflection.maybe_add_lesson(
                conn, interaction_id, inter["task"], inter["response"], signal,
                offload_fn=_generate_text, embed_fn=embeddings.embed,
            )
    msg = "Recorded '%s' (reward %+.2f) for %s." % (signal, r, interaction_id)
    if lesson_id:
        msg += " Distilled lesson %s." % lesson_id
    return msg
```

- [ ] **Step 6: Run helper tests to verify they pass**

Run: `./venv/Scripts/python.exe -m pytest tests/test_server_helpers.py -q`
Expected: all PASS.

- [ ] **Step 7: Run the full suite + import-smoke the server**

Run:
```bash
./venv/Scripts/python.exe -m pytest -q && ./venv/Scripts/python.exe -c "import server; print('server imports OK')"
```
Expected: all tests PASS, then `server imports OK`.

- [ ] **Step 8: Commit**

```bash
git add server.py tests/test_server_helpers.py
git commit -m "feat: wire trilobite learning loop into MCP server (offload learn, trilobite, record_outcome)"
```

---

### Task 9: Ollama alias + docs + live smoke test

Create the `trilobite` identity alias, document usage, and verify the loop end-to-end against the real Ollama.

**Files:**
- Create: `setup_alias.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: `ollama` CLI on PATH.
- Produces: `setup_alias.py` with `main()`; the `trilobite` Ollama model.

- [ ] **Step 1: Create `setup_alias.py`**

```python
"""One-time setup: pull the embed model and create the 'trilobite' Ollama alias.

The alias is a stable named identity (FROM the 7B coder). Sub-project #3's
fine-tune loop later republishes 'trilobite' with an ADAPTER; nothing else changes.
Run: ./venv/Scripts/python.exe setup_alias.py
"""
import os
import subprocess
import tempfile

MODELFILE = (
    "FROM qwen2.5-coder:7b\n"
    "PARAMETER temperature 0.2\n"
    'SYSTEM "You are trilobite, a local self-improving coding assistant. '
    'Be concise and correct; prefer working code."\n'
)


def main():
    print("Pulling embed model nomic-embed-text ...")
    subprocess.run(["ollama", "pull", "nomic-embed-text"], check=False)
    fd, path = tempfile.mkstemp(suffix=".Modelfile")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(MODELFILE)
        print("Creating 'trilobite' alias ...")
        subprocess.run(["ollama", "create", "trilobite", "-f", path], check=False)
    finally:
        os.unlink(path)
    print("Done. Verify with: ollama list | findstr trilobite")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the setup script**

Run:
```bash
./venv/Scripts/python.exe setup_alias.py
```
Expected: pulls embed model, prints `Creating 'trilobite' alias ...`, then `Done.`

- [ ] **Step 3: Verify the alias exists**

Run:
```bash
ollama list | grep trilobite
```
Expected: a `trilobite:latest` row.

- [ ] **Step 4: Live smoke test the whole loop (real Ollama, temp DB so it stays clean)**

Run:
```bash
./venv/Scripts/python.exe -c "
import os, tempfile, server
server._DB_PATH = os.path.join(tempfile.gettempdir(), 'trilobite_smoke.db')
r = server.trilobite('Write a Python one-liner that reverses a string.')
print('RESP:', r[:200])
iid = server.parse_interaction_id(r)
print('ID:', iid)
print(server.record_outcome(iid, 'tests_passed'))
print(server.record_outcome(iid, 'tests_passed'))  # 2nd time: lesson should dedupe
"
```
Expected: a real code response ending in an `[interaction_id: ...]` footer; first `record_outcome` prints `Recorded 'tests_passed' (reward +1.00) ... Distilled lesson <id>.`; the loop ran end-to-end against Ollama. (Delete the temp DB afterward: `rm "$TEMP/trilobite_smoke.db"`.)

- [ ] **Step 5: Update `README.md` — append a "Trilobite self-learning loop" section**

Add at the end of `README.md`:

```markdown
## Trilobite — self-learning coding loop

`trilobite` is a self-improving local coding model built on top of this bridge.
The learning lives in this MCP server (retrieval + reflection + reward); Ollama
only serves frozen weights.

- **Interactive use:** call `mcp__local-llm__trilobite("...")` — or just say
  "use trilobite". The reply ends with `[interaction_id: <id>]`.
- **Close the loop:** once you know how the answer did, call
  `record_outcome(<id>, "tests_passed" | "accepted" | "compiled" | "rejected" | "failed")`.
  Good outcomes distill a retrievable lesson that improves future answers.
- **Fleets:** the normal `offload(...)` tool learns automatically (`learn=True`);
  pass `learn=False` to skip capture. Cloud tiers never learn (privacy).
- **Setup (one-time):** `./venv/Scripts/python.exe setup_alias.py` (pulls
  `nomic-embed-text`, creates the `trilobite` Ollama alias).
- **State:** `memory.db` (gitignored) at the server root holds interactions,
  outcomes, and lessons.
```

- [ ] **Step 6: Commit**

```bash
git add setup_alias.py README.md
git commit -m "feat: trilobite Ollama alias, setup script, and docs"
```

---

## Self-Review

**Spec coverage:**
- Serving via Ollama / orchestrator around frozen model → Tasks 7–8. ✓
- Memory store (interactions/outcomes/lessons + FTS) → Task 2. ✓ (uses standalone `lessons_fts` instead of the spec's `interactions_fts`; retrieval corpus is the distilled lessons, which is what the spec's retriever section actually consumes.)
- Hybrid retrieval + RRF, lexical fallback → Task 4. ✓
- Local embeddings, soft-fail → Task 3. ✓
- Capture + footer + `learn` flag → Tasks 7–8. ✓
- Reward harvester `record_outcome` + execution-grounded scoring → Tasks 5, 8. ✓
- Reflection with dedup → Task 6. ✓
- `trilobite` general-session tool + Ollama alias identity + fallback → Tasks 8, 9. ✓
- Testing (mocked, no live Ollama in core) → Tasks 2–8; live smoke isolated to Task 9. ✓
- Privacy (cloud never learns) → Task 8 offload path. ✓

**Placeholder scan:** none — every code/test step is complete and runnable.

**Type consistency:** `offload_fn(prompt=, tier=, system=, temperature=, num_predict=)` matches `_generate_text` (Task 8) and the reflection stub (Task 6). `run_with_learning` return `(response, interaction_id)` consumed consistently in Task 8. `with_footer`/`parse_interaction_id` names match across Tasks 8–9. `resolve_trilobite_model` name consistent. DB helper `_db()` and `_LOCK` used uniformly.

**Note on concurrency:** `_LOCK` serializes tool bodies so the single shared `check_same_thread=False` connection is never touched by two FastMCP worker threads at once — simplest correct choice for slice #1's low call volume.
