import server


def _capture_posts(monkeypatch, seen):
    """Record (path, timeout) for every _post; answer as a chat completion.

    Assertions below select the generation call by path instead of assuming it
    is the only _post: the auto-context /api/show metadata probe (fixed 30s
    timeout, 300s cache) also goes through _post when its cache is cold, which
    made bare timeout-list assertions depend on which test ran first.
    """
    monkeypatch.setattr(
        server,
        "_post",
        lambda p, d, timeout=None: seen.append((p, timeout))
        or {"message": {"content": "ok"}},
    )


def _chat_timeouts(seen):
    return [timeout for path, timeout in seen if path == "/api/chat"]


def test_make_generate_propagates_timeout(monkeypatch):
    seen = []
    _capture_posts(monkeypatch, seen)
    assert server._make_generate("local", "", 0.2, 32, 2048, timeout=17)("hi") == "ok"
    assert _chat_timeouts(seen) == [17]


def test_plain_offload_bounds_timeout(monkeypatch):
    seen = []
    _capture_posts(monkeypatch, seen)
    monkeypatch.setattr(server, "_should_learn", lambda tier, learn: False)
    assert server.offload("x", tier="fast", learn=False, timeout=0) == "ok"
    assert _chat_timeouts(seen) == [1]


def test_learning_offload_bounds_timeout(monkeypatch):
    seen = []
    class Conn:
        def close(self): pass
    _capture_posts(monkeypatch, seen)
    monkeypatch.setattr(server, "_open_db", lambda: Conn())
    monkeypatch.setattr(server, "_should_learn", lambda tier, learn: True)
    monkeypatch.setattr(server, "resolve_sonder_model", lambda strict=False: "sonder")
    monkeypatch.setattr(server.orchestrator, "run_with_learning", lambda c, p, t, g, **k: (g(p), "abc123"))
    out = server.offload("x", tier="code", learn=True, timeout=server.TIMEOUT + 99)
    assert server.parse_interaction_id(out) == "abc123"
    assert _chat_timeouts(seen) == [server.TIMEOUT]
