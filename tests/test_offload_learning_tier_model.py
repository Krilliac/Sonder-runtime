"""A learned offload generates with the tier's runtime-policy binding.

Regression: the learning path resolved ``resolve_sonder_model`` instead of the
tier's bound model, so every ``offload(learn=True)`` on a local tier ran on the
``sonder:latest`` alias even when ``code`` was bound to a much larger coder.
Only ``learn=False`` honoured the binding. The strict alias contract belongs to
the default "sonder" chat route, which must keep behaving as before.
"""
import server


CODE_MODEL = "example-coder:30b"
FAST_MODEL = "example-small:3b"


def _route_learning_through_ollama(monkeypatch, *, strict=False):
    posted = []
    recorded = []
    learned = {}

    class Conn:
        def close(self):
            pass

    def fake_post(path, payload, timeout=None, **_kwargs):
        if path == "/api/chat":
            posted.append(payload)
            return {"message": {"content": "generated"}, "model": payload.get("model")}
        return {}

    def run_with_learning(conn, prompt, tier, gen, **kwargs):
        learned["tier"] = tier
        learned["kwargs"] = kwargs
        return gen(prompt), "iid-1"

    def record_model_call(**kwargs):
        recorded.append(kwargs.get("model"))

    monkeypatch.setattr(server, "_post", fake_post)
    monkeypatch.setattr(server, "_open_db", lambda: Conn())
    monkeypatch.setattr(server, "_should_learn", lambda tier, learn: True)
    monkeypatch.setattr(server, "_refresh_live_cloud_tiers", lambda: None)
    monkeypatch.setattr(server, "_bridge_provider_for_tier", lambda tier: None)
    monkeypatch.setattr(server, "_STRICT_DEFAULT", strict)
    monkeypatch.setitem(server.TIERS, "code", CODE_MODEL)
    monkeypatch.setitem(server.TIERS, "fast", FAST_MODEL)
    monkeypatch.setattr(
        server, "resolve_sonder_model",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("learned offload resolved the sonder alias instead of the tier binding")
        ),
    )
    monkeypatch.setattr(server.orchestrator, "run_with_learning", run_with_learning)
    monkeypatch.setattr(server.activity_tracker, "record_model_call", record_model_call)
    return posted, recorded, learned


def test_learned_code_offload_generates_with_the_code_tier_model(monkeypatch):
    posted, recorded, learned = _route_learning_through_ollama(monkeypatch)

    out = server._offload_impl("write a parser", tier="code", learn=True, num_ctx=4096)

    assert out.endswith("[interaction_id: iid-1]"), repr(out)
    assert learned["tier"] == "code"  # the learning loop still ran
    assert [p["model"] for p in posted] == [CODE_MODEL]
    # The activity record names the model that actually generated.
    assert recorded == [CODE_MODEL]


def test_learned_fast_offload_generates_with_the_fast_tier_model(monkeypatch):
    posted, recorded, _ = _route_learning_through_ollama(monkeypatch)

    server._offload_impl("rename a thing", tier="fast", learn=True, num_ctx=4096)

    assert [p["model"] for p in posted] == [FAST_MODEL]
    assert recorded == [FAST_MODEL]


def test_strict_env_no_longer_refuses_a_bound_learning_tier(monkeypatch):
    # SONDER_STRICT pins the default "sonder" route to the alias; a named tier
    # with its own binding is not that route and must not need the alias.
    posted, _, _ = _route_learning_through_ollama(monkeypatch, strict=True)

    server._offload_impl("write a parser", tier="code", learn=True, num_ctx=4096)

    assert [p["model"] for p in posted] == [CODE_MODEL]


def test_learned_and_plain_offload_use_the_same_model(monkeypatch):
    posted, _, _ = _route_learning_through_ollama(monkeypatch)
    server._offload_impl("a", tier="code", learn=True, num_ctx=4096)
    monkeypatch.setattr(server, "_should_learn", lambda tier, learn: False)
    server._offload_impl("b", tier="code", learn=False, num_ctx=4096)

    assert [p["model"] for p in posted] == [CODE_MODEL, CODE_MODEL]


def test_default_sonder_route_keeps_strict_alias_semantics(monkeypatch):
    monkeypatch.setattr(server, "_refresh_live_cloud_tiers", lambda: None)
    monkeypatch.setattr(server, "resolve_sonder_model", lambda strict=False: "sonder:latest" if strict else "fallback")

    assert server._serve_target("sonder", True) == ("sonder:latest", False, True, "sonder")
    monkeypatch.setattr(server, "resolve_sonder_model", lambda strict=False: None)
    assert server._serve_target("", True) == (None, False, True, "sonder")


def test_unlearned_offload_on_a_bridged_tier_reaches_the_bound_provider(monkeypatch):
    # Regression (2026-10-08): learn=False never bound the rung, so a tier bound
    # to Sonder Inference was posted to Ollama with num_ctx=0; Ollama sized the
    # context to 256 tokens, truncated the prompt and generated unrelated text.
    from types import SimpleNamespace

    seen = {}

    def fake_bridge_chat_request(gateway, payload, rung, *, context):
        seen["provider"], seen["tier"] = rung.provider, rung.tier
        seen["model"] = payload["model"]
        return {"message": {"content": "bridged"}, "model": payload["model"]}, "bridged"

    def no_ollama(*_a, **_k):
        raise AssertionError("bridged tier was posted to Ollama")

    monkeypatch.setattr(server, "_post", no_ollama)
    monkeypatch.setattr(server, "_should_learn", lambda tier, learn: False)
    monkeypatch.setattr(server, "_refresh_live_cloud_tiers", lambda: None)
    monkeypatch.setattr(server, "_bridge_provider_for_tier", lambda tier, cloud=False: "sonder_inference")
    monkeypatch.setattr(server, "_application", lambda: SimpleNamespace(model_gateway=object()))
    monkeypatch.setattr(server._legacy_chat_bridge, "chat_request", fake_bridge_chat_request)
    monkeypatch.setattr(server._legacy_chat_bridge, "ollama_only_reroute", lambda *a, **k: None)
    monkeypatch.setitem(server.TIERS, "code", CODE_MODEL)

    out = server._offload_impl("write a parser", tier="code", learn=False)

    assert out == "bridged"
    assert seen == {"provider": "sonder_inference", "tier": "code", "model": CODE_MODEL}
