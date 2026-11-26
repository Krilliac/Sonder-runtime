"""Integration tests for Wave 4 runtime production wiring (rt-prod-wiring).

Covers all four Wave 4 wiring surfaces:
1. ``runtime/server.py`` + ``legacy_chat_bridge.native_tools_supported`` wiring
   ``provider_bridge.set_native_tools_probe``;
2. ``chat_routes.py`` + ``request_security.py`` + ``legacy_chat_bridge.resolve_local_tier_health``
   wiring ``tier_health`` and ``route_summary_line`` (headers, receipts, logs, activity);
3. ``bootstrap/app.py`` + ``bootstrap/application_graph.py`` lazy
   ``Application.responses_client`` composition;
4. ``sonder_inference_gateway.py`` + ``sse_stream.py`` capability-gated
   ``response_format``, ``logprobs`` (sync + SSE streaming), and ``embed()`` over live HTTP.
"""
from __future__ import annotations

import contextlib
import io
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any

import pytest

from sonder_runtime.adapters import legacy_chat_bridge
from sonder_runtime.adapters.inference.sonder_inference_gateway import (
    SonderInferenceConfig,
    SonderInferenceGateway,
    SonderInferenceResponse,
)
from sonder_runtime.adapters.observability import activity_tracker
from sonder_runtime.adapters.persistence import fanout_store
from sonder_runtime.application.chat import provider_bridge as bridge
from sonder_runtime.application.chat import stream_sink
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.application.routing.execution_mode_service import (
    ENV_AUTO_EXECUTION_MODE,
    ExecutionModePolicy,
    plan_chat_execution_mode,
    route_summary_line,
)
from sonder_runtime.bootstrap import app as bootstrap_app
from sonder_runtime.interfaces.http.facades import chat_routes, request_security
from tests.test_execution_mode_router import _DUAL_PC_TIERS, _make_chat_harness


@contextlib.contextmanager
def _loopback_inference_server(
    *,
    capabilities: list[str],
    models: tuple[str, ...] = ("qwen3.8:27b",),
):
    recorded: dict[str, list[dict[str, Any]]] = {"chat": [], "embed": []}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:
            pass

        def _send_json(self, status: int, body: dict[str, Any]) -> None:
            raw = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            if self.path.endswith("/v1/sonder/health"):
                self._send_json(
                    200,
                    {
                        "status": "ready",
                        "api_version": 1,
                        "version": "0.1.0",
                        "synthetic": False,
                        "backends": [
                            {
                                "name": "llamaserver",
                                "available": True,
                                "capabilities": list(capabilities),
                            }
                        ],
                        "models": [
                            {"id": m, "backend": "llamaserver", "default": idx == 0}
                            for idx, m in enumerate(models)
                        ],
                    },
                )
                return
            self._send_json(404, {"error": {"code": "not_found", "message": "not found"}})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if self.path.endswith("/v1/embeddings"):
                recorded["embed"].append(payload)
                self._send_json(
                    200,
                    {
                        "object": "list",
                        "model": models[0],
                        "data": [
                            {"object": "embedding", "index": i, "embedding": [float(i + 1), 0.5]}
                            for i, _ in enumerate(payload["input"])
                        ],
                        "usage": {"prompt_tokens": 3, "total_tokens": 3},
                        "sonder": {"api_version": 1, "request_id": "emb-http-1"},
                    },
                )
                return
            if self.path.endswith("/v1/chat/completions"):
                recorded["chat"].append(payload)
                if payload.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    chunks = [
                        {
                            "id": "chatcmpl-stream-1",
                            "object": "chat.completion.chunk",
                            "model": models[0],
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": "hel"},
                                    "logprobs": {"content": [{"token": "hel", "logprob": -0.1}]},
                                    "finish_reason": None,
                                }
                            ],
                        },
                        {
                            "id": "chatcmpl-stream-1",
                            "object": "chat.completion.chunk",
                            "model": models[0],
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": "lo"},
                                    "logprobs": {"content": [{"token": "lo", "logprob": -0.2}]},
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
                        },
                    ]
                    for chunk in chunks:
                        self.wfile.write(("data: %s\n\n" % json.dumps(chunk)).encode("utf-8"))
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    return
                self._send_json(
                    200,
                    {
                        "id": "chatcmpl-sync-1",
                        "object": "chat.completion",
                        "model": models[0],
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": '{"ok":true}'},
                                "logprobs": {
                                    "content": [
                                        {
                                            "token": "{",
                                            "logprob": -0.01,
                                            "top_logprobs": [{"token": "{", "logprob": -0.01}],
                                        }
                                    ]
                                },
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
                        "sonder": {"api_version": 1, "request_id": "chat-http-1"},
                    },
                )
                return
            self._send_json(404, {"error": {"code": "not_found", "message": "not found"}})

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % httpd.server_address[1], recorded
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_server_wires_native_tools_probe_without_eager_graph(monkeypatch):
    import server  # runtime/server.py
    from sonder_runtime.adapters.model_transport import ModelCallError

    assert callable(getattr(bridge, "_NATIVE_TOOLS_PROBE", None))
    prev_graph = server._APP_GRAPH
    try:
        monkeypatch.setattr(server, "_APP_GRAPH", None)
        monkeypatch.delenv("SONDER_INFERENCE_NATIVE_TOOLS", raising=False)
        # With no composed graph, probe fails closed and does NOT construct one.
        monkeypatch.setattr(
            server, "_application", lambda: pytest.fail("native_tools probe composed graph implicitly")
        )
        assert bridge.supports_native_tools("sonder_inference") is False

        with _loopback_inference_server(capabilities=["streaming", "tool_calling"]) as (base_url, _):
            gw = SonderInferenceGateway(SonderInferenceConfig(base_url=base_url))
            bindings = SimpleNamespace(
                bound_providers=frozenset({"sonder_inference"}),
                fallbacks={},
            )
            graph = SimpleNamespace(model_gateway=gw, provider_bindings=bindings)
            monkeypatch.setattr(server, "_APP_GRAPH", graph)
            assert bridge.supports_native_tools("sonder_inference") is True
            rerouted = legacy_chat_bridge.ollama_only_reroute(
                gw,
                {"tools": [{"type": "function", "function": {"name": "ping"}}]},
                bridge.RungBinding(tier="code", provider="sonder_inference"),
                model="qwen3.8:27b",
                graph=graph,
                loopback_endpoint=True,
                context_probe=lambda: 16384,
            )
            assert rerouted is None

        with _loopback_inference_server(capabilities=["streaming"]) as (base_url, _):
            plain_gw = SonderInferenceGateway(SonderInferenceConfig(base_url=base_url))
            bindings = SimpleNamespace(
                bound_providers=frozenset({"sonder_inference"}),
                fallbacks={},
            )
            graph = SimpleNamespace(model_gateway=plain_gw, provider_bindings=bindings)
            monkeypatch.setattr(server, "_APP_GRAPH", graph)
            assert bridge.supports_native_tools("sonder_inference") is False
            with pytest.raises(ModelCallError, match="native tool calls"):
                legacy_chat_bridge.ollama_only_reroute(
                    plain_gw,
                    {"tools": [{"type": "function", "function": {"name": "ping"}}]},
                    bridge.RungBinding(tier="code", provider="sonder_inference"),
                    model="qwen3.8:27b",
                    graph=graph,
                    loopback_endpoint=True,
                    context_probe=lambda: 16384,
                )
    finally:
        server._APP_GRAPH = prev_graph


def test_resolve_local_tier_health_reads_fanout_cooldown_and_latency(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_FANOUT_DB", str(tmp_path / "fanout.db"))
    fanout_store.reset_schema_cache_for_tests()
    activity_tracker.reset_for_tests()
    try:
        fanout_store.record_model_health(
            "qwen3-80b",
            model_class="local",
            success=False,
            error="Node1 unreachable",
            disabled_until=time.time() + 600,
        )
        activity_tracker.record_model_call(
            model="bonsai-27b-pq2",
            ok=True,
            elapsed_ms=920,
        )
        hints = legacy_chat_bridge.resolve_local_tier_health(
            {
                "code": "bonsai-27b-pq2",
                "general": "bonsai-27b-pq2",
                "reasoning": "qwen3-80b",
            }
        )
        assert hints["reasoning"]["available"] is False
        assert hints["reasoning"]["healthy"] is False
        assert "Node1 unreachable" in str(hints["reasoning"]["reason"])
        assert hints["code"]["available"] is True
        assert hints["code"]["latency_ms"] == pytest.approx(920.0)

        decision = plan_chat_execution_mode(
            "Think hard and analyze deadlock and race condition trade-offs in this distributed architecture.",
            tier_models=_DUAL_PC_TIERS,
            developer_authorized=True,
            tier_health=hints,
            policy=ExecutionModePolicy(mode="auto", allow_auto_agent=False),
        )
        assert decision.health_degraded is True
        assert "reasoning" in decision.degraded_tiers
        assert decision.mode == "ensemble"
        assert decision.recommended_tiers == ("code", "fast")

        # When both reasoning and fast are degraded/unavailable, collapse cleanly to single[code].
        hints_single = {
            **hints,
            "fast": {"available": False, "healthy": False, "reason": "fast busy"},
        }
        decision_single = plan_chat_execution_mode(
            "Think hard and analyze deadlock and race condition trade-offs in this distributed architecture.",
            tier_models=_DUAL_PC_TIERS,
            developer_authorized=True,
            tier_health=hints_single,
            policy=ExecutionModePolicy(mode="auto", allow_auto_agent=False),
        )
        assert decision_single.mode == "single"
        assert decision_single.recommended_tiers == ("code",)
    finally:
        activity_tracker.reset_for_tests()
        fanout_store.reset_schema_cache_for_tests()


def test_chat_routes_wires_tier_health_and_route_summary_line_headers(monkeypatch, caplog):
    monkeypatch.setenv(ENV_AUTO_EXECUTION_MODE, "auto")
    monkeypatch.setattr(chat_routes, "run_chat_media_route", lambda **_kw: (None, None, None))
    monkeypatch.setattr(chat_routes, "rate_limit_and_meter", lambda *_a, **_kw: (True, None))

    handler, serve_mod, deps, payloads, ens_calls, _, _ = _make_chat_harness()
    # Provide an injected tier_health callback marking reasoning unavailable so ensemble degrades cleanly.
    deps.resolve_tier_health = lambda _models: {
        "reasoning": {"available": False, "reason": "worker cooling down"}
    }
    recorded_events: list[tuple[str, dict[str, Any]]] = []
    serve_mod.server.activity_tracker.record_event = (
        lambda kind, **fields: recorded_events.append((kind, fields))
    )
    caplog.set_level(logging.INFO, logger=serve_mod._serve_logger.name)

    chat_routes.handle_chat_completions(
        handler,
        serve_mod,
        {
            "model": "sonder",
            "messages": [
                {
                    "role": "user",
                    "content": "Think hard and analyze deadlock and race condition trade-offs in this architecture.",
                }
            ],
        },
        {"role": "admin", "authorized": True, "account": None},
        model_operation="chat",
        request_temperature=None,
        facade=None,
        record_early_chat_metric=lambda _k: None,
        deps=deps,
    )
    assert payloads[-1][0] == 200
    assert len(ens_calls) == 1
    assert ens_calls[0]["tiers"] == "code,fast"
    receipt = payloads[-1][1]["sonder_receipt"]
    route_doc = receipt["execution_route"]
    assert route_doc["health_degraded"] is True
    assert route_doc["degraded_tiers"] == ["reasoning"]
    assert route_doc["fallback_tiers"] == ["general"]
    expected_summary = route_summary_line(route_doc)
    assert route_doc["summary"] == expected_summary
    assert handler._execution_route_headers == {"X-Sonder-Execution-Route": expected_summary}
    assert any(
        kind == "execution_route" and fields.get("summary") == expected_summary
        for kind, fields in recorded_events
    )
    assert any(
        f"execution route: {expected_summary}" in rec.getMessage()
        for rec in caplog.records
    )

    # Verify HTTP serialization helpers actually emit X-Sonder-Execution-Route on wire responses.
    sent_headers: dict[str, str] = {}
    out_buf = io.BytesIO()
    serve_mod._request_route = lambda raw_path: raw_path
    json_handler = SimpleNamespace(
        path="/v1/chat/completions",
        _correlation_id="corr-1",
        _request_started=0.0,
        _execution_route_headers=handler._execution_route_headers,
        _cors=lambda: None,
        send_response=lambda _status: None,
        send_header=lambda k, v: sent_headers.__setitem__(k, v),
        end_headers=lambda: None,
        wfile=out_buf,
    )
    request_security.send_json_payload(json_handler, serve_mod, {"ok": True}, elapsed_ms=5)
    assert sent_headers["X-Sonder-Execution-Route"] == expected_summary

    # Verify streaming response helper (send_stream) also emits X-Sonder-Execution-Route.
    stream_headers: dict[str, str] = {}
    stream_handler = SimpleNamespace(
        _early_stream=None,
        connection=None,
        _correlation_id="corr-stream-1",
        _request_started=0.0,
        _spanda_headers=None,
        _execution_route_headers=handler._execution_route_headers,
        _cors=lambda: None,
        send_response=lambda _status: None,
        send_header=lambda k, v: stream_headers.__setitem__(k, v),
        end_headers=lambda: None,
    )
    stream_serve_mod = SimpleNamespace(
        _serve_logger=serve_mod._serve_logger,
        STREAM_IDLE_TIMEOUT_SECONDS=60,
        server=SimpleNamespace(
            activity_tracker=SimpleNamespace(public_response=lambda *_a, **_kw: None),
        ),
        _live_stream=SimpleNamespace(
            frame_id_and_receipt=lambda _h, _i, iid, receipt: (iid, receipt),
        ),
        Handler=SimpleNamespace(
            _write_stream_body=lambda *_a, **_kw: True,
        ),
    )
    assert chat_routes.send_stream(stream_handler, stream_serve_mod, "hi", "sonder") is True
    assert stream_headers["X-Sonder-Execution-Route"] == expected_summary

    # Verify control chars / CRLF are stripped from X-Sonder-Execution-Route.
    monkeypatch.setattr(
        chat_routes,
        "route_summary_line",
        lambda _decision: "auto | quick\r\nX-Injected: evil\x00 | single[fast]",
    )
    crlf_handler, crlf_serve, crlf_deps, crlf_payloads, _, _, _ = _make_chat_harness()
    chat_routes.handle_chat_completions(
        crlf_handler,
        crlf_serve,
        {"model": "sonder", "messages": [{"role": "user", "content": "hey"}]},
        {"role": "admin", "authorized": True, "account": None},
        model_operation="chat",
        request_temperature=None,
        facade=None,
        record_early_chat_metric=lambda _k: None,
        deps=crlf_deps,
    )
    assert crlf_payloads[-1][0] == 200
    header_val = crlf_handler._execution_route_headers["X-Sonder-Execution-Route"]
    assert "\r" not in header_val and "\n" not in header_val and "\x00" not in header_val
    assert header_val == "auto | quick X-Injected: evil | single[fast]"


def test_bootstrap_application_wires_lazy_responses_client(monkeypatch):
    from sonder_runtime.adapters.inference.sonder_responses_client import SonderResponsesClient

    bootstrap_app.reset_for_tests()
    # Custom injection stays lazy & exact
    sentinel = object()
    app_injected = bootstrap_app.build_application(responses_client=sentinel)
    assert app_injected.responses_client is not None
    assert app_injected.responses_client() is sentinel
    app_injected.close_providers(timeout=1)

    app = bootstrap_app.build_application()
    assert callable(app.responses_client)
    monkeypatch.setenv("SONDER_INFERENCE_BASE_URL", "http://127.0.0.1:19437")
    monkeypatch.setenv("SONDER_INFERENCE_MODEL", "qwen3.8:27b")
    c1 = app.responses_client()
    c2 = app.responses_client()
    assert c1 is c2
    assert isinstance(c1, SonderResponsesClient)
    assert c1.config.base_url == "http://127.0.0.1:19437"
    assert c1.config.model == "qwen3.8:27b"
    app.close_providers(timeout=1)


def test_sonder_inference_gateway_live_http_capabilities_and_stream_logprobs():
    with _loopback_inference_server(
        capabilities=["streaming", "structured_output", "logprobs", "embeddings"],
    ) as (base_url, recorded):
        gw = SonderInferenceGateway(SonderInferenceConfig(base_url=base_url, model="qwen3.8:27b"))
        ctx = local_owner_context(correlation_id="w4-http-1")

        # 1. Sync JSON schema response_format + logprobs
        sync_resp = gw.generate(
            ModelRequest(
                prompt="emit status",
                tier="code",
                options={
                    "response_format": {"type": "json_object"},
                    "logprobs": True,
                    "top_logprobs": 1,
                },
            ),
            ctx,
        )
        assert isinstance(sync_resp, SonderInferenceResponse)
        assert sync_resp.text == '{"ok":true}'
        assert sync_resp.logprobs == {
            "content": [
                {
                    "token": "{",
                    "logprob": -0.01,
                    "top_logprobs": [{"token": "{", "logprob": -0.01}],
                }
            ]
        }
        assert recorded["chat"][0]["response_format"] == {"type": "json_object"}
        assert recorded["chat"][0]["logprobs"] is True
        assert recorded["chat"][0]["top_logprobs"] == 1

        # 2. Streaming SSE aggregates token logprobs cleanly
        streamed_deltas: list[str] = []
        turn_stream = stream_sink.LiveTurnStream(
            write=lambda piece: streamed_deltas.append(piece) or True
        )
        with stream_sink.armed(turn_stream), stream_sink.claimed_for_call():
            stream_resp = gw.generate(
                ModelRequest(prompt="say hi", tier="code", options={"logprobs": True}),
                ctx,
            )
        assert "".join(streamed_deltas) == "hello"
        assert stream_resp.text == "hello"
        assert stream_resp.logprobs == {
            "content": [
                {"token": "hel", "logprob": -0.1},
                {"token": "lo", "logprob": -0.2},
            ]
        }

        # 3. Embeddings round-trip over HTTP + dynamic capability advertisement
        embeddings = gw.embed(["hello", "world"], ctx)
        assert len(embeddings) == 2
        assert embeddings[0].vector == (1.0, 0.5)
        assert embeddings[1].vector == (2.0, 0.5)
        assert "embeddings" in gw.capabilities


def test_private_worker_routing_gates_on_structured_output_and_logprobs(tmp_path, monkeypatch):
    from tests.test_sonder_inference_private_workers import Hosts

    monkeypatch.setenv("SONDER_ALLOW_REMOTE_INFERENCE", "1")
    monkeypatch.setenv("SONDER_INFERENCE_WORKER_TOKEN", "w" * 32)
    ca_file = tmp_path / "worker-ca.pem"
    ca_file.write_text("-----BEGIN CERTIFICATE-----\n-----END CERTIFICATE-----\n", encoding="utf-8")
    worker_url = "https://192.0.2.2:8443/sonder-inference"
    primary_url = "http://127.0.0.1:11437"
    hosts = Hosts(
        health={
            primary_url: {
                "status": "ready",
                "api_version": 1,
                "backends": [{"name": "ollama", "available": True, "capabilities": ["streaming"]}],
                "models": [{"id": "sonder:latest", "backend": "ollama", "default": True}],
            },
            worker_url: {
                "status": "ready",
                "api_version": 1,
                "backends": [
                    {
                        "name": "llamaserver",
                        "available": True,
                        "capabilities": ["streaming", "structured_output", "logprobs"],
                    }
                ],
                "models": [{"id": "sonder:latest", "backend": "llamaserver", "default": True}],
            },
        }
    )
    from sonder_runtime.adapters.inference.sonder_inference_gateway import PrivateWorkerSpec

    cfg = SonderInferenceConfig(
        base_url=primary_url,
        model="sonder:latest",
        allow_remote=True,
        workers=(PrivateWorkerSpec(worker_url, str(ca_file), "SONDER_INFERENCE_WORKER_TOKEN", 1),),
    )
    gw = SonderInferenceGateway(cfg, get_transport=hosts.get, transport=hosts.post)
    resp = gw.generate(
        ModelRequest(
            prompt="structured",
            tier="code",
            options={"response_format": {"type": "json_object"}, "logprobs": True},
        ),
        local_owner_context(correlation_id="pw-1"),
    )
    assert isinstance(resp, SonderInferenceResponse)
    assert resp.endpoint == "https://192.0.2.2:8443"
    assert [post[0] for post in hosts.posts] == [worker_url]
