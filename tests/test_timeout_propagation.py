import server


def test_make_generate_propagates_timeout(monkeypatch):
    seen = []
    monkeypatch.setattr(server, "_post", lambda p, d, timeout=None: seen.append(timeout) or {"message": {"content": "ok"}})
    assert server._make_generate("local", "", 0.2, 32, 2048, timeout=17)("hi") == "ok"
    assert seen == [17]


def test_plain_offload_bounds_timeout(monkeypatch):
    seen = []
    monkeypatch.setattr(server, "_post", lambda p, d, timeout=None: seen.append(timeout) or {"message": {"content": "ok"}})
    monkeypatch.setattr(server, "_should_learn", lambda tier, learn: False)
    assert server.offload("x", tier="fast", learn=False, timeout=0) == "ok"
    assert seen == [1]


def test_learning_offload_bounds_timeout(monkeypatch):
    seen = []
    class Conn:
        def close(self): pass
    monkeypatch.setattr(server, "_post", lambda p, d, timeout=None: seen.append(timeout) or {"message": {"content": "ok"}})
    monkeypatch.setattr(server, "_open_db", lambda: Conn())
    monkeypatch.setattr(server, "_should_learn", lambda tier, learn: True)
    monkeypatch.setattr(server, "resolve_trilobite_model", lambda strict=False: "trilobite")
    monkeypatch.setattr(server.orchestrator, "run_with_learning", lambda c, p, t, g, **k: (g(p), "abc123"))
    out = server.offload("x", tier="code", learn=True, timeout=server.TIMEOUT + 99)
    assert server.parse_interaction_id(out) == "abc123"
    assert seen == [server.TIMEOUT]
