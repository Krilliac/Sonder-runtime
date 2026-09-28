"""The chat loop applies the long-context overflow and states it.

A long turn on a local tier moves to the overflow model on the Ollama pool:
the system prompt names that model, the rung is not bound to the tier's
Sonder Inference provider, and the receipt says so.  When no pool worker
advertises the model, or the overflow attempt fails, the turn keeps its
route and the receipt says why.
"""
import http.client
import json
import threading
from types import SimpleNamespace

import pytest

import server
import sonder_runtime.interfaces.http.serve as serve
from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.application.routing import long_context_overflow as overflow

MOE = "qwen3.6:35b"
DENSE = "dense-27b:q3"
LONG = [{"role": "user", "content": "x" * 160_000}]


class _Built(Exception):
    """Stops the turn once the system prompt has been built."""


class _Pool:
    enabled = True
    has_remote_workers = True

    def __init__(self, *models):
        self._models = models

    def snapshots(self):
        return (
            SimpleNamespace(worker_id="127.0.0.1:11434", origin="http://127.0.0.1:11434",
                            state="ready", healthy=True, models=(DENSE,)),
            # A static roster worker's pool id is opaque; the notice names its origin.
            SimpleNamespace(worker_id="static-" + "f1" * 32, origin="https://10.77.0.2:8443",
                            state="ready", healthy=True, models=self._models),
        )


@pytest.fixture
def long_context(monkeypatch):
    for name in ("SONDER_MODEL_BACKEND", "SONDER_FAST_PROVIDER", "SONDER_GENERAL_PROVIDER",
                 "SONDER_CODE_PROVIDER", "SONDER_REASONING_PROVIDER", "SONDER_VISION_PROVIDER",
                 "SONDER_INFERENCE_TIER_MODELS", "SONDER_LONG_CONTEXT_THRESHOLD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SONDER_MODEL_BACKEND", "sonder-inference")
    monkeypatch.setenv("SONDER_INFERENCE_MODEL", DENSE)
    monkeypatch.setenv("SONDER_LONG_CONTEXT_OVERFLOW", "1")
    monkeypatch.setenv("SONDER_LONG_CONTEXT_MODEL", MOE)
    monkeypatch.setitem(server.TIERS, "general", DENSE)
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    # A live app graph left by an earlier test on this worker would win over
    # the env bindings above (CI saw provider=None in the full suite).
    monkeypatch.setattr(server, "_APP_GRAPH", None)
    monkeypatch.setattr(server, "OLLAMA_POOL", _Pool(MOE))
    return monkeypatch


def _capture_system(monkeypatch):
    seen = {}

    def fake_build_system(system, trace, persona, model="", cloud=False, provider=None):
        seen.update(model=model, provider=provider,
                    prompt=server._runtime_identity_block(model, cloud, provider))
        raise _Built()

    monkeypatch.setattr(server, "_build_system", fake_build_system)
    return seen


def test_long_turn_system_prompt_names_the_overflow_model(long_context):
    seen = _capture_system(long_context)
    with pytest.raises(_Built):
        server._answer_with_history_impl("hello", LONG, tier="general")
    assert seen["model"] == MOE
    assert seen["provider"] is None
    assert "`qwen3.6:35b`" in seen["prompt"]
    assert DENSE not in seen["prompt"]


def test_short_turn_keeps_the_tier_model(long_context):
    seen = _capture_system(long_context)
    with pytest.raises(_Built):
        server._answer_with_history_impl("hello", [], tier="general")
    assert seen["model"] == DENSE and seen["provider"] == "sonder_inference"


def test_unadvertised_overflow_model_keeps_the_route(long_context):
    long_context.setattr(server, "OLLAMA_POOL", _Pool("other:7b"))
    seen = _capture_system(long_context)
    with pytest.raises(_Built):
        server._answer_with_history_impl("hello", LONG, tier="general")
    assert seen["model"] == DENSE and seen["provider"] == "sonder_inference"


def _answer_with(monkeypatch, generate):
    """Run a full non-learning turn with ``generate(model)`` as the model."""
    monkeypatch.setattr(server, "_should_learn", lambda tier, learn: False)
    monkeypatch.setattr(server, "_auto_model_context", lambda model: 16_384)
    seen = []

    def fake_make_generate(model, system, temperature, num_predict, num_ctx, **kwargs):
        seen.append((model, num_ctx))
        return lambda prompt, history=None: generate(model)

    monkeypatch.setattr(server, "_make_generate", fake_make_generate)
    with overflow.notice_scope() as notes:
        reply = server._answer_with_history_impl("hello", LONG, tier="general")
    return reply, overflow.receipt_entry(notes), seen


def test_switched_turn_states_it_in_the_receipt(long_context):
    reply, receipt, seen = _answer_with(long_context, lambda model: "answer from " + model)
    assert reply.startswith("answer from qwen3.6:35b")
    assert "long-context overflow" not in reply
    assert seen[0][0] == MOE and seen[0][1] >= 40_960
    assert receipt["status"] == "switched" and receipt["worker"] == "10.77.0.2:8443"
    assert receipt["notice"].startswith(
        "long-context overflow: switched to qwen3.6:35b on 10.77.0.2:8443 — context 40k"
    )


def test_rejected_overflow_attempt_falls_back_and_says_so(long_context):
    def generate(model):
        if model == MOE:
            raise ModelCallError("connection", "Ollama worker pool rejected the request")
        return "answer from " + model

    reply, receipt, seen = _answer_with(long_context, generate)
    assert [model for model, _ctx in seen] == [MOE, DENSE]
    assert reply.startswith("answer from " + DENSE)
    assert receipt["status"] == "unavailable"
    assert receipt["notice"].startswith(
        "long-context overflow (qwen3.6:35b) unavailable, stayed on %s: overflow attempt "
        "failed (connection: Ollama worker pool rejected the request)" % DENSE
    )


def test_disabled_overflow_leaves_no_receipt(long_context):
    long_context.setenv("SONDER_LONG_CONTEXT_OVERFLOW", "0")
    reply, receipt, seen = _answer_with(long_context, lambda model: "answer from " + model)
    assert receipt is None and seen[0][0] == DENSE


def test_responses_http_envelope_includes_the_overflow_receipt(monkeypatch):
    monkeypatch.setattr(serve, "AUTH_MODE", "local-open")
    monkeypatch.setattr(serve, "API_KEY", "")
    monkeypatch.setattr(serve, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(serve, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(serve, "_capture_live_session_turn", lambda **kwargs: None)

    def answer(_self, *args, **kwargs):
        overflow.record(overflow.Decision(
            status="switched", from_model=DENSE, from_provider="sonder_inference",
            to_model=MOE, estimated_tokens=41_234, threshold_tokens=32_768,
        ))
        return SimpleNamespace(
            content="answer", iid="overflow-http", thinking="", cache="",
            resolved_model=MOE, resolved_tier="general", provider_capture=None,
        )

    monkeypatch.setattr(serve.Handler, "_run_streamable_prompt", answer)
    httpd = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=15)
        try:
            conn.request(
                "POST", "/v1/responses",
                body=json.dumps({"model": "sonder", "input": "hello"}),
                headers={"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            payload = json.loads(response.read())
        finally:
            conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)

    assert response.status == 200, payload
    assert payload["object"] == "response"
    assert payload["output_text"] == "answer"
    assert payload["sonder_receipt"]["overflow"]["status"] == "switched"
    assert payload["sonder_receipt"]["overflow"]["to_model"] == MOE


def test_runtime_overflow_command_toggles_the_policy(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime_policy.json"))
    for name in ("SONDER_LONG_CONTEXT_OVERFLOW", "SONDER_LONG_CONTEXT_THRESHOLD",
                 "SONDER_LONG_CONTEXT_MODEL"):
        monkeypatch.delenv(name, raising=False)
    assert "long-context overflow: off" in server._runtime_command("overflow status")
    assert "long-context overflow: on" in server._runtime_command("overflow on")
    assert "threshold 40000 tokens" in server._runtime_command("overflow threshold 40000")
    assert "-> qwen3.6:35b on the ollama pool" in server._runtime_command("overflow model qwen3.6:35b")
    assert "not changed" in server._runtime_command("overflow threshold 12")
    assert "not changed" in server._runtime_command("overflow model big:cloud")
    assert server._runtime_command("overflow sideways").startswith("usage: /runtime overflow")
    assert "long-context overflow: off" in server._runtime_command("overflow off")
