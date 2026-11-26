"""Observatory telemetry wiring for execution_route, fanout/ensemble, and timings.

Verifies:
1. Content-free ``execution_route`` emission (``execution_route.decided`` and
   terminal ``request.completed``) matching Observatory's ``executionRoute.ts``
   schema, plus strict rejection of prompt/secret text.
2. Live ``fanout.*`` and ``ensemble.*`` event propagation from HTTP chat routes
   and ``fanout_execution`` helpers (both inside turns and on background fanout
   worker threads) with zero prompt/answer/vault leakage.
3. Measured inference timings (``ttft_ms``, ``prompt_cached_tokens`` /
   ``backend_cached_tokens``, ``prompt_uncached_tokens``, ``prompt_eval_ms``,
   ``eval_ms``, ``prompt_eval_tps``, ``decode_tps`` /
   ``backend_predicted_tokens_per_second``, ``load_ms``, ``queue_ms``,
   ``token_counts_from_backend``) flowing from both Sonder-Inference/OpenAI and
   Ollama provider replies with KV-cache-hit ``prompt_per_second`` inflation
   protection.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone

import pytest

from sonder_runtime.adapters import fanout_execution as fanout_ex
from sonder_runtime.adapters.inference.telemetry import (
    from_ollama,
    from_openai_compatible,
)
from sonder_runtime.adapters.observability.observatory_producer import (
    ObservatoryProducer,
)
from sonder_runtime.application.capabilities.observability import (
    RedactingTelemetrySink,
)
from sonder_runtime.application.observability import runtime_telemetry as rt
from sonder_runtime.application.routing.execution_mode_service import (
    ENV_AUTO_EXECUTION_MODE,
)
from sonder_runtime.application.session import provider_attempts as attempts
from sonder_runtime.domain.common.errors import DependencyUnavailable
from sonder_runtime.interfaces.http.facades import chat_routes
from sonder_runtime.platform.logging import Redactor
from tests.test_execution_mode_router import _make_chat_harness

SECRET_PROMPT = "TOP-SECRET-PROMPT-sk-live-1234567890abcdef"
SECRET_ANSWER = "TOP-SECRET-ANSWER-do-not-export"
SECRET_VAULT = "vault-token-super-secret-answer-blob"


def _make_observatory_stack():
    producer = ObservatoryProducer(
        version="0.9.0",
        node_id="test-host",
        instance_hex="aabbccddeeff",
    )
    sink = RedactingTelemetrySink(producer, Redactor())
    ticks = iter(range(1_000, 50_000, 25))
    telemetry = rt.RuntimeTelemetry(
        sink,
        session_id=producer.session_id,
        version="0.9.0",
        clock=lambda: datetime(2026, 11, 25, 8, 0, 0, tzinfo=timezone.utc),
        monotonic=lambda: next(ticks) / 1000.0,
    )
    return producer, telemetry


def _drain_envelopes(producer: ObservatoryProducer) -> list[dict]:
    sub = producer.subscribe()
    try:
        batch = sub.next_batch(0.05, limit=4096)
    finally:
        sub.close()
    return [json.loads(event.line) for event in batch.events]


@pytest.fixture
def live_telemetry():
    producer, telemetry = _make_observatory_stack()
    attempts.install_provider_attempt_observer(telemetry)
    try:
        yield producer, telemetry
    finally:
        attempts.clear_provider_attempt_observer(telemetry)


# -- 1. Cache-hit prompt_per_second inflation guard in adapters/inference/telemetry.py --


def test_openai_compatible_guards_against_cache_hit_prompt_tps_inflation():
    # 99% cache hit (9900 cached / 100 uncached over 50 ms):
    # backend mistakenly reports total_prompt / prompt_ms = 200,000 tok/s.
    # Uncached rate is 100 * 1000 / 50 = 2,000 tok/s.
    payload = {
        "usage": {
            "prompt_tokens": 10_000,
            "completion_tokens": 60,
            "prompt_tokens_details": {"cached_tokens": 9_900},
        },
        "timings": {
            "prompt_n": 10_000,
            "cache_n": 9_900,
            "prompt_ms": 50.0,
            "prompt_per_second": 200_000.0,
            "predicted_n": 60,
            "predicted_ms": 1_200.0,
            "predicted_per_second": 50.0,
            "ttft_ms": 62.5,
            "load_ms": 12.0,
        },
    }
    telemetry = from_openai_compatible(payload, with_usage=True)
    assert telemetry is not None
    assert telemetry.prompt_tokens == 10_000
    assert telemetry.prompt_cached_tokens == 9_900
    assert telemetry.prompt_uncached_tokens == 100
    assert telemetry.prompt_eval_ms == 50.0
    assert telemetry.prompt_tokens_per_second == pytest.approx(2_000.0)
    assert telemetry.output_tokens_per_second == pytest.approx(50.0)
    assert telemetry.ttft_ms == pytest.approx(62.5)
    assert telemetry.load_ms == pytest.approx(12.0)


def test_openai_compatible_returns_none_prompt_tps_on_100_percent_cache_hit():
    payload = {
        "usage": {
            "prompt_tokens": 4_096,
            "completion_tokens": 32,
            "prompt_tokens_details": {"cached_tokens": 4_096},
        },
        "timings": {
            "prompt_n": 4_096,
            "cache_n": 4_096,
            "prompt_ms": 2.0,
            "prompt_per_second": 2_048_000.0,
            "predicted_ms": 640.0,
            "predicted_per_second": 50.0,
        },
    }
    telemetry = from_openai_compatible(payload, with_usage=True)
    assert telemetry is not None
    assert telemetry.prompt_cached_tokens == 4_096
    assert telemetry.prompt_uncached_tokens == 0
    assert telemetry.prompt_tokens_per_second is None
    assert telemetry.output_tokens_per_second == pytest.approx(50.0)


def test_openai_compatible_drops_unverified_prompt_tps_when_cached_and_prompt_ms_missing():
    payload = {
        "usage": {
            "prompt_tokens": 1_000,
            "completion_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 800},
        },
        "timings": {
            "prompt_per_second": 50_000.0,
            "predicted_per_second": 45.0,
        },
    }
    telemetry = from_openai_compatible(payload, with_usage=True)
    assert telemetry is not None
    assert telemetry.prompt_tokens_per_second is None
    assert telemetry.output_tokens_per_second == pytest.approx(45.0)


def test_openai_compatible_reads_nested_usage_sonder_timings_fallback():
    payload = {
        "usage": {
            "prompt_tokens": 500,
            "completion_tokens": 25,
            "prompt_tokens_details": {"cached_tokens": 0},
            "sonder": {
                "timings": {
                    "ttft_ms": 88.0,
                    "prompt_ms": 100.0,
                    "predicted_ms": 500.0,
                }
            },
        }
    }
    telemetry = from_openai_compatible(payload, with_usage=True)
    assert telemetry is not None
    assert telemetry.ttft_ms == pytest.approx(88.0)
    assert telemetry.prompt_tokens_per_second == pytest.approx(5_000.0)
    assert telemetry.output_tokens_per_second == pytest.approx(50.0)


def test_ollama_prompt_tps_uses_uncached_tokens_and_omits_full_cache_hit():
    partial = from_ollama({
        "prompt_eval_count": 1_000,
        "prompt_eval_cached_count": 900,
        "prompt_eval_duration": 50_000_000,  # 50 ms
        "eval_count": 40,
        "eval_duration": 800_000_000,  # 800 ms
    })
    assert partial is not None
    assert partial.prompt_uncached_tokens == 100
    assert partial.prompt_tokens_per_second == pytest.approx(2_000.0)
    assert partial.output_tokens_per_second == pytest.approx(50.0)

    full_hit = from_ollama({
        "prompt_eval_count": 1_000,
        "prompt_eval_cached_count": 1_000,
        "prompt_eval_duration": 5_000_000,  # 5 ms
        "eval_count": 20,
        "eval_duration": 400_000_000,
    })
    assert full_hit is not None
    assert full_hit.prompt_uncached_tokens == 0
    assert full_hit.prompt_tokens_per_second is None
    assert full_hit.output_tokens_per_second == pytest.approx(50.0)


def test_openai_compatible_generic_peer_catches_cache_n_inflation():
    telemetry = from_openai_compatible({
        "timings": {
            "cache_n": 900,
            "prompt_n": 100,
            "prompt_ms": 50.0,
            "prompt_per_second": 20_000.0,
        }
    })
    assert telemetry is not None
    assert telemetry.prompt_tokens_per_second == pytest.approx(2_000.0)


# -- 2. Measured inference timings through dispatch_provider -> RuntimeTelemetry -> Observatory --


def test_sonder_inference_timings_propagate_to_route_and_completed_envelopes(live_telemetry):
    producer, telemetry = live_telemetry
    turn = telemetry.begin_turn(
        turn_id="req-si-timings",
        surface="http.chat_completions",
        stream=True,
        requested_model="sonder",
    )
    reply = {
        "model": "qwen3-coder-next-80b:latest",
        "choices": [{"message": {"content": SECRET_ANSWER}}],
        "usage": {
            "prompt_tokens": 2_000,
            "completion_tokens": 80,
            "prompt_tokens_details": {"cached_tokens": 1_800},
        },
        "timings": {
            "ttft_ms": 94.5,
            "prompt_n": 2_000,
            "cache_n": 1_800,
            "prompt_ms": 80.0,
            "prompt_per_second": 25_000.0,  # inflated! true uncached is 200 / 0.08 = 2500
            "predicted_n": 80,
            "predicted_ms": 1_600.0,
            "predicted_per_second": 50.0,
            "load_ms": 15.25,
            "queue_ms": 4.5,
            "total_ms": 1_699.75,
        },
    }
    with telemetry.activate(turn):
        attempts.dispatch_provider(
            "sonder-inference",
            "/v1/chat/completions",
            {"model": "reasoning", "messages": [{"role": "user", "content": SECRET_PROMPT}]},
            lambda: reply,
        )
    assert telemetry.finish_turn(turn, outcome="completed", http_status=200) is True

    envelopes = _drain_envelopes(producer)
    assert [e["event_type"] for e in envelopes] == [
        "request.started",
        "route.selected",
        "request.completed",
    ]
    route_attrs = envelopes[1]["attributes"]
    done_attrs = envelopes[2]["attributes"]

    for attrs in (route_attrs, done_attrs):
        assert attrs["ttft_ms"] == pytest.approx(94.5)
        assert attrs["prompt_cached_tokens"] == 1_800
        assert attrs["backend_cached_tokens"] == 1_800
        assert attrs["prompt_uncached_tokens"] == 200
        assert attrs["prompt_eval_ms"] == pytest.approx(80.0)
        assert attrs["eval_ms"] == pytest.approx(1_600.0)
        assert attrs["load_ms"] == pytest.approx(15.25)
        assert attrs["queue_ms"] == pytest.approx(4.5)
        assert attrs["prompt_eval_tps"] == pytest.approx(2_500.0)
        assert attrs["decode_tps"] == pytest.approx(50.0)
        assert attrs["backend_predicted_tokens_per_second"] == pytest.approx(50.0)

    assert done_attrs["prompt_tokens"] == 2_000
    assert done_attrs["completion_tokens"] == 80
    assert done_attrs["token_counts_from_backend"] is True

    dumped = json.dumps(envelopes)
    assert SECRET_PROMPT not in dumped
    assert SECRET_ANSWER not in dumped


def test_ollama_nanosecond_timings_propagate_without_cache_inflation(live_telemetry):
    producer, telemetry = live_telemetry
    turn = telemetry.begin_turn(
        turn_id="req-ollama-timings",
        surface="http.chat_completions",
        stream=False,
        requested_model="fast",
    )
    reply = {
        "model": "bonsai-9b-mtp",
        "message": {"content": SECRET_ANSWER},
        "prompt_eval_count": 500,
        "prompt_eval_cached_count": 400,
        "prompt_eval_duration": 25_000_000,  # 25 ms -> uncached 100 / 0.025 = 4000 tok/s
        "eval_count": 50,
        "eval_duration": 500_000_000,  # 500 ms -> 100 tok/s
        "load_duration": 10_000_000,  # 10 ms
        "total_duration": 535_000_000,
    }
    with telemetry.activate(turn):
        attempts.dispatch_provider(
            "ollama",
            "/api/chat",
            {"model": "bonsai-9b-mtp", "messages": [{"role": "user", "content": SECRET_PROMPT}]},
            lambda: reply,
        )
    telemetry.finish_turn(turn, outcome="completed", http_status=200)

    envelopes = _drain_envelopes(producer)
    done_attrs = envelopes[-1]["attributes"]
    assert done_attrs["prompt_tokens"] == 500
    assert done_attrs["prompt_cached_tokens"] == 400
    assert done_attrs["backend_cached_tokens"] == 400
    assert done_attrs["prompt_uncached_tokens"] == 100
    assert done_attrs["prompt_eval_ms"] == pytest.approx(25.0)
    assert done_attrs["eval_ms"] == pytest.approx(500.0)
    assert done_attrs["load_ms"] == pytest.approx(10.0)
    assert done_attrs["prompt_eval_tps"] == pytest.approx(4_000.0)
    assert done_attrs["decode_tps"] == pytest.approx(100.0)
    assert done_attrs["backend_predicted_tokens_per_second"] == pytest.approx(100.0)
    assert done_attrs["token_counts_from_backend"] is True


def test_hostile_non_finite_or_inconsistent_timings_are_dropped(live_telemetry):
    producer, telemetry = live_telemetry
    turn = telemetry.begin_turn(
        turn_id="req-bad-timings",
        surface="http.chat_completions",
        stream=False,
        requested_model="sonder",
    )
    reply = {
        "model": "m:1",
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 999},  # > prompt_tokens: invalid
        },
        "timings": {
            "ttft_ms": math.nan,
            "prompt_ms": math.inf,
            "predicted_ms": -5.0,
            "load_ms": True,
            "queue_ms": "not-a-number",
            "prompt_per_second": 9_999_999_999.0,
            "predicted_per_second": -1.0,
        },
    }
    with telemetry.activate(turn):
        attempts.dispatch_provider("sonder-inference", "/v1/chat/completions", {"model": "m:1"}, lambda: reply)
    telemetry.finish_turn(turn, outcome="completed", http_status=200)

    envelopes = _drain_envelopes(producer)
    assert producer.stats()["dropped_events"] == 0
    done_attrs = envelopes[-1]["attributes"]
    for dropped_key in (
        "ttft_ms",
        "prompt_cached_tokens",
        "backend_cached_tokens",
        "prompt_uncached_tokens",
        "prompt_eval_ms",
        "eval_ms",
        "load_ms",
        "queue_ms",
        "prompt_eval_tps",
        "decode_tps",
        "backend_predicted_tokens_per_second",
    ):
        assert dropped_key not in done_attrs


# -- 3. execution_route sanitization & emission --


def test_sanitize_execution_route_blocks_prompt_text_and_synthesizes_summary():
    route = rt.sanitize_execution_route({
        "mode": "ensemble",
        "band": "hard",
        "difficulty_score": 0.7421,
        "recommended_tiers": ["code", "reasoning", "bad tier with spaces!"],
        "ensemble_mode": "code",
        "fanout_scope": "local",
        "auto_selected": True,
        "reason": f"Prompt leaked: {SECRET_PROMPT}",
        "signals": ["explicit_hard_cue", "structured_subitems:3", f"bad {SECRET_PROMPT}"],
        "route_summary_line": f"Injected: {SECRET_PROMPT}",
        "prompt": SECRET_PROMPT,
    })
    assert route is not None
    assert route["mode"] == "ensemble"
    assert route["band"] == "hard"
    assert route["difficulty_score"] == 0.742
    assert route["recommended_tiers"] == ["code", "reasoning"]
    assert route["ensemble_mode"] == "code"
    assert route["fanout_scope"] == "local"
    assert route["auto_selected"] is True
    assert route["signals"] == ["explicit_hard_cue", "structured_subitems:3"]
    assert route["participant_count"] == 2
    assert "reason" not in route  # free-text reason dropped
    assert route["route_summary_line"] == "auto ensemble (hard 0.74) -> code, reasoning"
    assert SECRET_PROMPT not in json.dumps(route)

    # Known domain reasons map to canonical snake_case codes.
    known = rt.sanitize_execution_route({
        "mode": "ensemble",
        "band": "hard",
        "difficulty_score": 0.8,
        "recommended_tiers": ["code", "reasoning"],
        "auto_selected": True,
        "reason": "hard reasoning prompt auto-routed to parallel multi-model ensemble",
    })
    assert known is not None
    assert known["reason"] == "hard_auto_ensemble"

    # Invalid mode or out-of-range score rejected cleanly.
    assert rt.sanitize_execution_route({"mode": "invalid"}) is None
    out_of_range = rt.sanitize_execution_route({"mode": "single", "difficulty_score": 1.5})
    assert out_of_range is not None
    assert "difficulty_score" not in out_of_range


# -- 4. End-to-end HTTP chat_routes wiring for execution_route, ensemble.*, fanout.* --


def test_http_chat_auto_ensemble_emits_execution_route_and_ensemble_events(monkeypatch, live_telemetry):
    producer, telemetry = live_telemetry
    monkeypatch.setenv(ENV_AUTO_EXECUTION_MODE, "auto")
    monkeypatch.setattr(chat_routes, "run_chat_media_route", lambda **_kw: (None, None, None))
    monkeypatch.setattr(chat_routes, "rate_limit_and_meter", lambda *_a, **_kw: (True, None))

    handler, serve_mod, deps, payloads, ens_calls, _, _ = _make_chat_harness()
    prompt = (
        f"Think hard and analyze deadlock and race condition trade-offs in this distributed architecture. {SECRET_PROMPT}"
    )
    turn = telemetry.begin_turn(
        turn_id="req-auto-ens",
        surface="http.chat_completions",
        stream=False,
        requested_model="sonder",
    )
    with telemetry.activate(turn):
        chat_routes.handle_chat_completions(
            handler,
            serve_mod,
            {"model": "sonder", "messages": [{"role": "user", "content": prompt}]},
            {"role": "admin", "authorized": True, "account": None},
            model_operation="chat",
            request_temperature=None,
            facade=None,
            record_early_chat_metric=lambda _k: None,
            deps=deps,
        )
    telemetry.finish_turn(turn, outcome="completed", http_status=200)

    assert payloads[-1][0] == 200
    assert len(ens_calls) == 1

    envelopes = _drain_envelopes(producer)
    types = [e["event_type"] for e in envelopes]
    assert types == [
        "request.started",
        "execution_route.decided",
        "ensemble.started",
        "ensemble.completed",
        "request.completed",
    ]
    assert all(e["request_id"] == "req-auto-ens" and e["run_id"] == "req-auto-ens" for e in envelopes)

    decided = envelopes[1]["attributes"]
    assert decided["execution_mode"] == "ensemble"
    assert decided["difficulty_band"] == "hard"
    assert decided["execution_route"]["mode"] == "ensemble"
    assert decided["execution_route"]["band"] == "hard"
    assert decided["execution_route"]["recommended_tiers"] == ["code", "reasoning"]
    assert decided["execution_route"]["reason"] == "hard_auto_ensemble"
    assert decided["execution_route"]["auto_selected"] is True

    ens_start = envelopes[2]["attributes"]
    ens_done = envelopes[3]["attributes"]
    assert ens_start["operation"] == "ensemble"
    assert ens_start["ensemble"] == {"mode": "prose", "tiers": ["code", "reasoning"], "count": 2}
    assert ens_done["status"] == "ok"
    assert ens_done["elapsed_ms"] >= 0

    completed = envelopes[4]["attributes"]
    assert completed["execution_mode"] == "ensemble"
    assert completed["difficulty_band"] == "hard"
    assert completed["execution_route"]["mode"] == "ensemble"

    dumped = json.dumps(envelopes)
    assert SECRET_PROMPT not in dumped
    assert "ensemble-reply" not in dumped


def test_http_chat_auto_fanout_emits_execution_route_and_fanout_events(monkeypatch, live_telemetry):
    producer, telemetry = live_telemetry
    monkeypatch.setenv(ENV_AUTO_EXECUTION_MODE, "auto")
    monkeypatch.setattr(chat_routes, "run_chat_media_route", lambda **_kw: (None, None, None))
    monkeypatch.setattr(chat_routes, "rate_limit_and_meter", lambda *_a, **_kw: (True, None))

    handler, serve_mod, deps, payloads, _, fan_calls, _ = _make_chat_harness()
    prompt = (
        f"Audit auth, cache, and scheduling across multiple modules in parallel for threat model and race condition gaps. {SECRET_PROMPT}"
    )
    turn = telemetry.begin_turn(
        turn_id="req-auto-fan",
        surface="http.chat_completions",
        stream=False,
        requested_model="sonder",
    )
    with telemetry.activate(turn):
        chat_routes.handle_chat_completions(
            handler,
            serve_mod,
            {"model": "sonder", "messages": [{"role": "user", "content": prompt}]},
            {"role": "admin", "authorized": True, "account": None},
            model_operation="chat",
            request_temperature=None,
            facade=None,
            record_early_chat_metric=lambda _k: None,
            deps=deps,
        )
    telemetry.finish_turn(turn, outcome="completed", http_status=200)

    assert payloads[-1][0] == 200
    assert len(fan_calls) == 1

    envelopes = _drain_envelopes(producer)
    assert [e["event_type"] for e in envelopes] == [
        "request.started",
        "execution_route.decided",
        "fanout.started",
        "fanout.completed",
        "request.completed",
    ]
    fan_done = envelopes[3]["attributes"]
    assert fan_done["operation"] == "fanout"
    assert fan_done["fanout"]["scope"] == "local"
    assert fan_done["fanout"]["tiers"] == ["code", "reasoning"]
    assert fan_done["status"] == "ok"
    assert envelopes[4]["attributes"]["execution_route"]["mode"] == "fanout"
    assert SECRET_PROMPT not in json.dumps(envelopes)
    assert "fanout-reply" not in json.dumps(envelopes)


def test_http_chat_ensemble_error_reply_emits_ensemble_failed(monkeypatch, live_telemetry):
    producer, telemetry = live_telemetry
    monkeypatch.setenv(ENV_AUTO_EXECUTION_MODE, "auto")
    monkeypatch.setattr(chat_routes, "run_chat_media_route", lambda **_kw: (None, None, None))
    monkeypatch.setattr(chat_routes, "rate_limit_and_meter", lambda *_a, **_kw: (True, None))

    handler, serve_mod, deps, _, _, _, _ = _make_chat_harness()
    deps.run_natural_ensemble = lambda _nm, _proj: f"ERROR: backend timeout {SECRET_PROMPT}"

    turn = telemetry.begin_turn(
        turn_id="req-ens-err",
        surface="http.chat_completions",
        stream=False,
        requested_model="sonder",
    )
    with telemetry.activate(turn):
        chat_routes.handle_chat_completions(
            handler,
            serve_mod,
            {
                "model": "sonder",
                "execution_mode": "ensemble",
                "messages": [{"role": "user", "content": SECRET_PROMPT}],
            },
            {"role": "admin", "authorized": True, "account": None},
            model_operation="chat",
            request_temperature=None,
            facade=None,
            record_early_chat_metric=lambda _k: None,
            deps=deps,
        )
    telemetry.finish_turn(turn, outcome="failed", http_status=200, error_code="ERROR_REPLY")

    envelopes = _drain_envelopes(producer)
    assert [e["event_type"] for e in envelopes] == [
        "request.started",
        "execution_route.decided",
        "ensemble.started",
        "ensemble.failed",
        "request.failed",
    ]
    assert envelopes[3]["attributes"]["error_code"] == "ERROR_REPLY"
    assert SECRET_PROMPT not in json.dumps(envelopes)


# -- 5. Background fanout_execution heartbeat & result emission --


def test_fanout_lease_heartbeat_and_result_emit_content_free_events(live_telemetry):
    producer, _telemetry = live_telemetry

    class FakeStore:
        def heartbeat(self, run_id, owner_id, *, lease_seconds):
            return True

    with fanout_ex.fanout_lease_heartbeat(FakeStore(), "fanout-run-42", "owner-1", 30.0):
        fanout_ex.emit_fanout_result(
            "fanout-run-42",
            {
                "served_model": "bonsai-27b-pq2",
                "tokens_in": 320,
                "tokens_out": 64,
                "done_reason": "stop",
                "sealed_answer": SECRET_VAULT,
                "prompt": SECRET_PROMPT,
            },
        )

    envelopes = _drain_envelopes(producer)
    assert [e["event_type"] for e in envelopes] == [
        "fanout.started",
        "fanout.result",
        "fanout.completed",
    ]
    assert all(e["run_id"] == "fanout-run-42" for e in envelopes)
    res_attrs = envelopes[1]["attributes"]
    assert res_attrs["served_model"] == "bonsai-27b-pq2"
    assert res_attrs["tokens_in"] == 320
    assert res_attrs["tokens_out"] == 64
    assert res_attrs["done_reason"] == "stop"
    assert envelopes[2]["attributes"]["status"] == "ok"

    dumped = json.dumps(envelopes)
    assert SECRET_VAULT not in dumped
    assert SECRET_PROMPT not in dumped


def test_fanout_lease_heartbeat_failure_emits_fanout_failed_with_domain_code(live_telemetry):
    producer, _telemetry = live_telemetry

    class FakeStore:
        def heartbeat(self, run_id, owner_id, *, lease_seconds):
            return True

    with pytest.raises(DependencyUnavailable):
        with fanout_ex.fanout_lease_heartbeat(FakeStore(), "fanout-run-99", "owner-1", 30.0):
            raise DependencyUnavailable("worker down")

    envelopes = _drain_envelopes(producer)
    assert [e["event_type"] for e in envelopes] == [
        "fanout.started",
        "fanout.failed",
    ]
    assert envelopes[1]["attributes"]["status"] == "error"
    assert envelopes[1]["attributes"]["error_code"] == "DEPENDENCY_UNAVAILABLE"


def test_inconsistent_uncached_and_unverified_cached_prompt_tps_are_dropped(live_telemetry):
    producer, telemetry = live_telemetry
    turn = telemetry.begin_turn(
        turn_id="req-inconsistent-cache",
        surface="http.chat_completions",
        stream=False,
        requested_model="sonder",
    )
    with telemetry.activate(turn):
        handle = telemetry.provider_send_started("sonder-inference", "/v1/chat/completions", "m:1")
        telemetry.provider_send_finished(
            handle,
            model="m:1",
            prompt_tokens=100,
            completion_tokens=10,
            timings={
                "prompt_cached_tokens": 500,  # invalid (> 100)
                "prompt_uncached_tokens": 50,  # must be dropped alongside invalid cached
                "prompt_eval_tps": 99_999.0,
            },
        )
        handle2 = telemetry.provider_send_started("sonder-inference", "/v1/chat/completions", "m:1")
        telemetry.provider_send_finished(
            handle2,
            model="m:1",
            prompt_tokens=None,
            completion_tokens=5,
            timings={
                "prompt_cached_tokens": 400,  # positive cached, unknown uncached
                "prompt_eval_tps": 88_888.0,  # unverified -> must be dropped
            },
        )
    telemetry.finish_turn(turn, outcome="completed", http_status=200)

    envelopes = _drain_envelopes(producer)
    route1 = envelopes[1]["attributes"]
    route2 = envelopes[2]["attributes"]
    assert "prompt_cached_tokens" not in route1
    assert "prompt_uncached_tokens" not in route1
    assert "prompt_eval_tps" not in route2


def test_multi_attempt_turn_aggregates_timings_and_preserves_first_ttft(live_telemetry):
    producer, telemetry = live_telemetry
    turn = telemetry.begin_turn(
        turn_id="req-multi-attempt",
        surface="http.chat_completions",
        stream=True,
        requested_model="sonder",
    )
    with telemetry.activate(turn):
        with pytest.raises(DependencyUnavailable):
            attempts.dispatch_provider(
                "sonder-inference",
                "/v1/chat/completions",
                {"model": "reasoning"},
                lambda: (_ for _ in ()).throw(DependencyUnavailable("first attempt down")),
            )
        attempts.dispatch_provider(
            "ollama",
            "/api/chat",
            {"model": "code"},
            lambda: {
                "model": "bonsai-27b-pq2",
                "prompt_eval_count": 400,
                "prompt_eval_cached_count": 300,
                "prompt_eval_duration": 50_000_000,  # 50 ms -> uncached 100 / 0.05 = 2000
                "eval_count": 30,
                "eval_duration": 600_000_000,  # 600 ms -> 50 tok/s
                "ttft_ms": 77.5,
            },
        )
    telemetry.finish_turn(turn, outcome="completed", http_status=200)

    envelopes = _drain_envelopes(producer)
    assert [e["event_type"] for e in envelopes] == [
        "request.started",
        "route.selected",
        "route.changed",
        "route.selected",
        "request.completed",
    ]
    done = envelopes[-1]["attributes"]
    assert done["attempts"] == 2
    assert done["ttft_ms"] == pytest.approx(77.5)
    assert done["prompt_cached_tokens"] == 300
    assert done["backend_cached_tokens"] == 300
    assert done["prompt_uncached_tokens"] == 100
    assert done["prompt_eval_tps"] == pytest.approx(2_000.0)
    assert done["decode_tps"] == pytest.approx(50.0)
    assert done["backend_predicted_tokens_per_second"] == pytest.approx(50.0)


def test_http_chat_natural_model_ensemble_emits_events_without_execution_route(monkeypatch, live_telemetry):
    producer, telemetry = live_telemetry
    monkeypatch.setenv(ENV_AUTO_EXECUTION_MODE, "off")
    monkeypatch.setattr(chat_routes, "run_chat_media_route", lambda **_kw: (None, None, None))
    monkeypatch.setattr(chat_routes, "rate_limit_and_meter", lambda *_a, **_kw: (True, None))

    handler, serve_mod, deps, payloads, ens_calls, _, _ = _make_chat_harness()
    serve_mod.server.natural_model_request = lambda _prompt: {
        "kind": "ensemble",
        "prompt": SECRET_PROMPT,
        "tiers": "code,reasoning",
        "mode": "code",
    }
    turn = telemetry.begin_turn(
        turn_id="req-natural-ens",
        surface="http.chat_completions",
        stream=False,
        requested_model="sonder",
    )
    with telemetry.activate(turn):
        chat_routes.handle_chat_completions(
            handler,
            serve_mod,
            {"model": "sonder", "messages": [{"role": "user", "content": SECRET_PROMPT}]},
            {"role": "admin", "authorized": True, "account": None},
            model_operation="chat",
            request_temperature=None,
            facade=None,
            record_early_chat_metric=lambda _k: None,
            deps=deps,
        )
    telemetry.finish_turn(turn, outcome="completed", http_status=200)

    assert payloads[-1][0] == 200
    assert len(ens_calls) == 1
    envelopes = _drain_envelopes(producer)
    assert [e["event_type"] for e in envelopes] == [
        "request.started",
        "ensemble.started",
        "ensemble.completed",
        "request.completed",
    ]
    assert envelopes[1]["attributes"]["ensemble"] == {
        "mode": "code",
        "tiers": ["code", "reasoning"],
        "count": 2,
    }
    assert SECRET_PROMPT not in json.dumps(envelopes)

