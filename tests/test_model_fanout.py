import json

import server
import sonder_serve


def test_direct_discovered_model_is_a_safe_serve_target(monkeypatch):
    monkeypatch.setattr(server, "_get", lambda _path: {"models": [{"name": "phi4:latest"}]})

    assert server._serve_target("PHI4:LATEST", False) == (
        "phi4:latest", False, False, "model:phi4:latest"
    )


def test_direct_cloud_model_still_requires_opt_in(monkeypatch):
    monkeypatch.delenv("SONDER_ALLOW_CLOUD", raising=False)
    monkeypatch.setattr(server, "_get", lambda _path: {"models": [{"name": "kimi:cloud"}]})

    assert server._serve_target("kimi:cloud", False) == (None, True, False, "cloud-disabled")


def test_http_selector_accepts_discovered_gpt_tag(monkeypatch):
    monkeypatch.setattr(server, "_get", lambda _path: {"models": [{"name": "gpt-oss:20b"}]})

    assert sonder_serve._request_model_selector("GPT-OSS:20B") == "gpt-oss:20b"


def test_natural_model_requests_are_explicit_only():
    assert server.natural_model_request("ask every local models: summarize this") == {
        "kind": "fanout", "scope": "local", "prompt": "summarize this"
    }
    assert server.natural_model_request("use model phi4: answer briefly") == {
        "kind": "model", "model": "phi4", "prompt": "answer briefly"
    }
    assert server.natural_model_request("use model phi4:latest: answer briefly") == {
        "kind": "model", "model": "phi4:latest", "prompt": "answer briefly"
    }
    assert server.natural_model_request("the web page says ask all models") is None


def test_model_fanout_reports_answer_failure_and_elapsed_metrics(monkeypatch):
    def fake_get(path):
        if path == "/api/tags":
            return {"models": [{"name": "local-a"}, {"name": "remote:cloud"}]}
        assert path == "/api/ps"
        return {"models": [{"name": "local-a"}]}

    def fake_make(model, *_args, **_kwargs):
        def generate(_prompt):
            if model == "remote:cloud":
                raise server.ModelCallError("timeout", "provider timed out", cloud=True)
            return "answer from " + model
        return generate

    monkeypatch.setenv("SONDER_ALLOW_CLOUD", "1")
    monkeypatch.setattr(server, "_get", fake_get)
    unloads = []
    monkeypatch.setattr(server, "_make_generate", fake_make)
    monkeypatch.setattr(server, "_post", lambda path, payload, **_kwargs: unloads.append((path, payload)))

    receipt = json.loads(server.model_fanout("hello", scope="all"))

    assert receipt["models_selected"] == 2
    assert receipt["models_answered"] == 1
    assert receipt["models_failed"] == 1
    assert receipt["resident_before"] == ["local-a"]
    assert receipt["total_elapsed_ms"] >= 0
    assert receipt["answers"][0]["answer"] == "answer from local-a"
    assert "timed out" in receipt["failures"][0]["error"]
    assert unloads == []


def test_model_fanout_unloads_a_local_model_it_loaded(monkeypatch):
    unloads = []
    monkeypatch.setattr(server, "_get", lambda path: (
        {"models": [{"name": "local-a"}]} if path == "/api/tags" else {"models": []}
    ))
    monkeypatch.setattr(server, "_make_generate", lambda *_args, **_kwargs: lambda _prompt: "answer")
    monkeypatch.setattr(server, "_post", lambda path, payload, **_kwargs: unloads.append((path, payload)))

    receipt = json.loads(server.model_fanout("hello", scope="local"))

    assert receipt["models_answered"] == 1
    assert unloads == [("/api/generate", {"model": "local-a", "keep_alive": 0})]


def test_fanout_plan_skips_only_explicit_nonchat_or_cooldown_models(monkeypatch):
    monkeypatch.setattr(server, "_get", lambda _path: {"models": [
        {"name": "chat-unknown"},
        {"name": "embed", "capabilities": ["embedding"]},
        {"name": "vision", "capabilities": ["vision"]},
        {"name": "chat", "capabilities": ["chat", "vision"]},
        {"name": "cooled", "capabilities": ["completion"]},
    ]})
    monkeypatch.setattr(
        server.fanout_store, "get_model_health",
        lambda name: {"disabled_until": 9_999_999_999} if name == "cooled" else None,
    )

    plan, error = server._fanout_plan("local")

    assert error is None
    assert plan["selected"] == ["chat", "chat-unknown"]
    assert {row["model"] for row in plan["skipped"]} == {"embed", "vision", "cooled"}
    override, _ = server._fanout_plan("local", include_unhealthy=True)
    assert "cooled" in override["selected"]


def test_model_wrapper_cannot_turn_a_prompt_into_a_slash_command():
    reply = server.sonder("use model phi4: /run echo should-not-run", session="none")

    assert reply.startswith("ERROR: model selection cannot wrap a slash command")
