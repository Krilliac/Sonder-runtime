"""Integration tests for trilobite's conversation memory, with Ollama stubbed out."""
import embeddings
import pytest
import server


@pytest.fixture
def stub(monkeypatch, tmp_path):
    """Point the DB at a temp file, stub Ollama /api/chat, and disable embeddings."""
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    # No trilobite alias -> resolve to the base coder model (no network needed).
    monkeypatch.setattr(server, "_get", lambda path: {"models": [{"name": "qwen2.5:3b"}]})
    monkeypatch.setattr(embeddings, "embed", lambda text: None)  # no recall, no vectors

    calls = []

    def fake_post(path, payload):
        calls.append(payload)
        return {"message": {"content": "ECHO"}}

    monkeypatch.setattr(server, "_post", fake_post)
    return calls


def _answer_payload(calls, prompt):
    """The /api/chat payload for `prompt`'s answer.

    The final user message is the *augmented* prompt (facts/lessons/recalls prepended,
    task last), so it ends with the raw prompt rather than equalling it.
    """
    for p in calls:
        if p["messages"][-1]["content"].rstrip().endswith(prompt):
            return p
    raise AssertionError("no chat call for prompt %r" % prompt)


def _contents(payload):
    return [m["content"] for m in payload["messages"]]


def test_followup_sees_prior_turn(stub):
    server.trilobite("first question", session="S1")
    server.trilobite("second question", session="S1")
    p2 = _answer_payload(stub, "second question")
    contents = _contents(p2)
    assert "first question" in contents   # prior user turn threaded
    assert "ECHO" in contents             # prior assistant turn threaded


def test_memory_on_by_default_shared_session(stub):
    server.trilobite("q1")   # no session -> DEFAULT_SESSION
    server.trilobite("q2")
    p2 = _answer_payload(stub, "q2")
    assert "q1" in _contents(p2)


def test_session_none_is_single_turn(stub):
    server.trilobite("first", session="none")
    server.trilobite("solo", session="none")
    p = _answer_payload(stub, "solo")
    # Only the current user turn — no system (empty) and no prior history.
    contents = _contents(p)
    assert contents[-1] == "solo"
    assert "first" not in contents


def test_isolated_sessions_do_not_bleed(stub):
    server.trilobite("alpha", session="A")
    server.trilobite("beta", session="B")
    p = _answer_payload(stub, "beta")
    assert "alpha" not in _contents(p)


def test_first_turn_gets_a_title(stub):
    server.trilobite("build a fibonacci function", session="T")
    out = server.trilobite_sessions()
    assert "T" in out
    # title came from the stubbed model ("ECHO")
    conn = server._open_db()
    try:
        import memory_store
        sess = memory_store.get_session(conn, "T")
    finally:
        conn.close()
    assert sess["title"]  # non-empty


def test_remember_fact_is_injected_for_project(stub):
    server.trilobite_remember_fact("this project uses MSVC", project="proj")
    server.trilobite("compile it", session="X", project="proj")
    p = _answer_payload(stub, "compile it")
    joined = "\n".join(_contents(p))
    assert "this project uses MSVC" in joined


def test_learned_preference_is_injected_next_turn(stub):
    server.trilobite("I prefer concise status updates.", session="P")
    server.trilobite("what changed?", session="P")

    p = _answer_payload(stub, "what changed?")
    joined = "\n".join(_contents(p))
    assert "User preference: User prefers concise status updates." in joined


def test_fact_not_injected_when_project_none(stub):
    server.trilobite_remember_fact("secret fact", project="proj")
    server.trilobite("do it", session="Y", project="none")
    p = _answer_payload(stub, "do it")
    assert "secret fact" not in "\n".join(_contents(p))


def test_sessions_list_reflects_turns(stub):
    server.trilobite("one", session="Z")
    server.trilobite("two", session="Z")
    out = server.trilobite_sessions()
    assert "Z" in out
    assert "2 turns" in out


def test_trilobite_remember_fact_rejects_empty(stub):
    assert server.trilobite_remember_fact("   ").startswith("ERROR")


def test_long_thread_summarizes_overflow(stub, monkeypatch):
    import memory_store
    monkeypatch.setattr(server, "MAX_TURNS", 2)  # small cap so overflow triggers fast
    for i in range(4):
        server.trilobite("turn %d" % i, session="L")
    p = _answer_payload(stub, "turn 3")
    contents = _contents(p)
    # Oldest turns are folded into a summary system message instead of being sent raw.
    assert any(c.startswith("Earlier in this conversation:") for c in contents)
    # Only the last MAX_TURNS turns remain verbatim (turn 1 and turn 2), not turn 0.
    assert not any(c == "turn 0" for c in contents)
    conn = server._open_db()
    try:
        sess = memory_store.get_session(conn, "L")
    finally:
        conn.close()
    assert sess["summary"]
    assert sess["summarized_through"]
