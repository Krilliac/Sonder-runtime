"""Runtime v1 vocabulary: session, request and route events, content-free."""
import json
from datetime import datetime, timezone

import pytest

from sonder_runtime.adapters.observability.event_bridge import (
    EXPORTED_EVENT_CODES,
    EventSinkTelemetryBridge,
    TeeEventSink,
)
from sonder_runtime.adapters.observability.observatory_producer import ObservatoryProducer
from sonder_runtime.application.capabilities.observability import RedactingTelemetrySink
from sonder_runtime.application.observability import runtime_telemetry as rt
from sonder_runtime.application.session import provider_attempts as attempts
from sonder_runtime.platform.logging import Redactor

PROMPT = "PROMPT-TEXT-never-exported"
ANSWER = "ANSWER-TEXT-never-exported"


class ListSink:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)

    def types(self):
        return [event.event_code for event in self.events]


@pytest.fixture
def telemetry():
    sink = ListSink()
    clock = iter(range(0, 10_000))
    runtime = rt.RuntimeTelemetry(
        sink, session_id="rts-0123456789ab", version="0.9.0",
        clock=lambda: datetime(2026, 9, 26, tzinfo=timezone.utc),
        monotonic=lambda: next(clock) / 1000.0,
    )
    attempts.install_provider_attempt_observer(runtime)
    try:
        yield runtime, sink
    finally:
        attempts.clear_provider_attempt_observer(runtime)


def _send(provider, reply=None, error=None, model="requested-model"):
    def send():
        if error is not None:
            raise error
        return reply or {"model": model, "message": {"content": ANSWER},
                         "prompt_eval_count": 9, "eval_count": 3}

    return attempts.dispatch_provider(
        provider, "/api/chat",
        {"model": model, "messages": [{"role": "user", "content": PROMPT}]},
        send,
    )


def test_one_turn_yields_started_route_and_one_terminal_event(telemetry):
    runtime, sink = telemetry
    turn = runtime.begin_turn(turn_id="req-abc123", surface="http.chat_completions",
                              stream=False, requested_model="sonder")
    with runtime.activate(turn):
        _send("ollama")
    assert runtime.finish_turn(turn, outcome="completed", http_status=200) is True
    assert runtime.finish_turn(turn, outcome="failed", http_status=500) is False
    assert sink.types() == ["request.started", "route.selected", "request.completed"]
    for event in sink.events:
        assert event.correlation_id == "req-abc123"
        assert event.run_id == "req-abc123"
        assert event.session_id == "rts-0123456789ab"
    started, route, done = (event.fields for event in sink.events)
    assert started == {"surface": "http.chat_completions", "kind": "chat", "stream": False,
                       "requested_model": "sonder", "workload": "interactive_user"}
    assert route["provider"] == "ollama" and route["attempt"] == 1
    assert route["operation"] == "chat" and route["model"] == "requested-model"
    assert done["outcome"] == "completed" and done["http_status"] == 200
    assert done["attempts"] == 1 and done["provider"] == "ollama"
    assert done["prompt_tokens"] == 9 and done["completion_tokens"] == 3
    assert "error_code" not in done


def test_provider_switch_between_attempts_emits_route_changed(telemetry):
    runtime, sink = telemetry
    turn = runtime.begin_turn(turn_id="req-switch", surface="http.chat_completions",
                              stream=False, requested_model="sonder")
    with runtime.activate(turn):
        from sonder_runtime.domain.common.errors import DependencyUnavailable
        with pytest.raises(DependencyUnavailable):
            _send("sonder-inference", error=DependencyUnavailable("refused"))
        _send("ollama")
    runtime.finish_turn(turn, outcome="completed", http_status=200)
    assert sink.types() == ["request.started", "route.selected", "route.changed",
                            "route.selected", "request.completed"]
    first, changed, second = (e.fields for e in sink.events[1:4])
    assert first["provider"] == "sonder_inference" and first["status"] == "error"
    assert first["error_code"] == "DEPENDENCY_UNAVAILABLE"
    assert changed == {"from_provider": "sonder_inference", "to_provider": "ollama",
                       "reason_code": "DEPENDENCY_UNAVAILABLE", "attempt": 2}
    assert second["attempt"] == 2
    assert sink.events[-1].fields["attempts"] == 2


def test_pre_send_fallback_is_announced_once(telemetry):
    runtime, sink = telemetry
    turn = runtime.begin_turn(turn_id="req-fallback", surface="a2a", stream=False,
                              requested_model="sonder")
    with runtime.activate(turn):
        attempts.report_provider_fallback("sonder-inference", "ollama", "not_ready")
        _send("ollama")
    assert sink.types() == ["request.started", "route.changed", "route.selected"]
    assert sink.events[1].fields["to_provider"] == "ollama"


def test_composed_pre_send_fallback_emits_route_changed(telemetry):
    """The composed graph wires PreSendFallbackGateway to the telemetry observer.

    The primary refuses before any send (as cached not-ready health does), so
    only the fallback wrapper can announce the change.
    """
    import inspect

    from sonder_runtime.adapters.inference.sonder_inference_gateway import (
        SonderInferenceUnreachable,
    )
    from sonder_runtime.adapters.provider_dispatch.fallback import PreSendFallbackGateway
    from sonder_runtime.application.context import local_owner_context
    from sonder_runtime.application.ports.model_gateway import ModelRequest, ModelResponse
    from sonder_runtime.bootstrap import app as bootstrap_app

    class Refusing:
        def generate(self, request, context):
            raise SonderInferenceUnreachable("not ready")

    class Ollama:
        def generate(self, request, context):
            _send("ollama")
            return ModelResponse(text="ok", model="sonder:latest", tier=request.tier,
                                 duration_ms=1, tokens_in=1, tokens_out=1)

    assert "fallback_observer=_report_provider_fallback" in inspect.getsource(
        bootstrap_app.build_application)
    gateway = PreSendFallbackGateway(Refusing(), fallback=Ollama(),
                                     observer=bootstrap_app._report_provider_fallback)
    runtime, sink = telemetry
    turn = runtime.begin_turn(turn_id="req-composed", surface="http.chat_completions",
                              stream=False, requested_model="sonder")
    context = local_owner_context(correlation_id="req-composed", source="http",
                                  timeout_seconds=30)
    with runtime.activate(turn):
        gateway.generate(ModelRequest(prompt="hi", tier="general"), context)
    assert sink.types() == ["request.started", "route.changed", "route.selected"]
    assert sink.events[1].fields == {"from_provider": "sonder_inference",
                                     "to_provider": "ollama",
                                     "reason_code": "primary_unreachable", "attempt": 1}
    assert sink.events[2].fields["provider"] == "ollama"


def test_fallback_after_a_failed_send_does_not_announce_twice(telemetry):
    from sonder_runtime.domain.common.errors import DependencyUnavailable

    runtime, sink = telemetry
    turn = runtime.begin_turn(turn_id="req-fb2", surface="a2a", stream=False,
                              requested_model="sonder")
    with runtime.activate(turn):
        with pytest.raises(DependencyUnavailable):
            _send("sonder-inference", error=DependencyUnavailable("refused"))
        attempts.report_provider_fallback("sonder-inference", "ollama",
                                          "DEPENDENCY_UNAVAILABLE")
        _send("ollama")
    assert sink.types().count("route.changed") == 1


def test_resolved_model_from_the_reply_is_the_route_model(telemetry):
    runtime, sink = telemetry
    turn = runtime.begin_turn(turn_id="req-model", surface="http.chat_completions",
                              stream=False, requested_model="sonder")
    with runtime.activate(turn):
        attempts.dispatch_provider(
            "openai-compatible", "/v1/chat/completions", {"model": "default"},
            lambda: {"model": "mock:tiny", "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
        )
    assert sink.events[-1].fields["model"] == "mock:tiny"
    assert sink.events[-1].fields["provider"] == "openai_compatible"


def test_sends_outside_a_turn_are_not_exported(telemetry):
    runtime, sink = telemetry
    _send("ollama")
    assert sink.events == []


def test_failed_and_cancelled_turns_carry_error_codes(telemetry):
    runtime, sink = telemetry
    for outcome in ("failed", "cancelled"):
        turn = runtime.begin_turn(turn_id="req-" + outcome, surface="http.chat_completions",
                                  stream=True, requested_model="general")
        runtime.finish_turn(turn, outcome=outcome, http_status=503, error_code="model_error")
    assert sink.types() == ["request.started", "request.failed",
                            "request.started", "request.cancelled"]
    assert sink.events[1].fields["error_code"] == "model_error"
    assert sink.events[1].level == "WARNING"


def test_free_text_selectors_are_not_exported_as_labels(telemetry):
    runtime, sink = telemetry
    runtime.begin_turn(turn_id="req-label", surface="http.chat_completions", stream=False,
                       requested_model="please summarise my secret plan")
    assert sink.events[0].fields["requested_model"] == "[unsafe-label]"


def test_session_events_describe_bindings_without_content(telemetry):
    runtime, sink = telemetry
    runtime.session_started({
        "default_generation_provider": "openai_compatible",
        "tier_providers": {"general": "openai_compatible"},
        "embedding_provider": "ollama",
    })
    runtime.session_ended(emitted_events=4, dropped_events=0)
    started, ended = sink.events
    assert started.event_code == "session.started"
    assert started.fields["role"] == "runtime"
    assert started.fields["text_capture"] == "none"
    assert started.fields["provider_bindings"]["fallbacks"] == {}
    assert ended.fields == {"emitted_events": 4, "dropped_events": 0}


def test_a_failing_sink_never_fails_the_turn():
    class Exploding:
        def emit(self, event):
            raise RuntimeError("sink down")

    runtime = rt.RuntimeTelemetry(Exploding(), session_id="rts-x", version="v")
    turn = runtime.begin_turn(turn_id="req-1", surface="a2a", stream=False,
                              requested_model="sonder")
    runtime.finish_turn(turn, outcome="completed", http_status=200)
    assert runtime.emit_failures == 2


@pytest.mark.parametrize("message_id,http_id,expected", [
    ("e2e-a2a-1", "corr-1", "e2e-a2a-1"),
    ("has spaces!", "corr-2", "corr-2"),
    ("x" * 129, "corr-3", "corr-3"),
])
def test_turn_id_prefers_a_join_safe_message_id(message_id, http_id, expected):
    assert rt.turn_id(message_id, http_id) == expected


def test_turn_id_never_returns_an_unjoinable_value():
    value = rt.turn_id("bad id", "also bad")
    assert rt.sanitize_correlation_id(value) == value


def test_exported_json_never_contains_prompt_or_answer_text():
    producer = ObservatoryProducer(version="0.9.0", node_id="host", instance_hex="0123456789ab")
    sink = RedactingTelemetrySink(producer, Redactor())
    runtime = rt.RuntimeTelemetry(sink, session_id=producer.session_id, version="0.9.0")
    attempts.install_provider_attempt_observer(runtime)
    try:
        runtime.session_started({"default_generation_provider": "ollama",
                                 "tier_providers": {}, "embedding_provider": "ollama"})
        turn = runtime.begin_turn(turn_id="req-scan", surface="http.chat_completions",
                                  stream=False, requested_model="sonder")
        with runtime.activate(turn):
            _send("ollama")
        runtime.finish_turn(turn, outcome="completed", http_status=200)
    finally:
        attempts.clear_provider_attempt_observer(runtime)
    batch = producer.subscribe().next_batch(0.1, limit=100)
    exported = "\n".join(event.line for event in batch.events)
    assert len(batch.events) == 4
    assert PROMPT not in exported and ANSWER not in exported
    for line in exported.splitlines():
        json.loads(line)


def test_event_sink_bridge_exports_only_allowlisted_codes_without_summary():
    sink = ListSink()
    bridge = EventSinkTelemetryBridge(sink, Redactor())
    bridge.emit("AUTH_FAILED", summary="login from 10.0.0.7",
                detail={"client": "10.0.0.7"})
    bridge.emit("model.escalation.decided", summary="free text " + PROMPT,
                detail={"tier": "general", "prompt": PROMPT, "attempt": 2},
                correlation_id="req-1", operation_id="run 1 invalid")
    assert [event.event_code for event in sink.events] == ["model.escalation.decided"]
    event = sink.events[0]
    assert PROMPT not in json.dumps(dict(event.fields))
    assert event.fields["tier"] == "general" and event.fields["attempt"] == 2
    assert event.fields["severity"] == "INFO"
    assert event.correlation_id == "req-1"
    assert event.run_id is None
    assert "AUTH_FAILED" not in EXPORTED_EVENT_CODES


def test_tee_event_sink_keeps_the_durable_sink_authoritative():
    calls = []

    class Primary:
        def emit(self, code, **kwargs):
            calls.append(("primary", code))

    class Sibling:
        def emit(self, code, **kwargs):
            calls.append(("sibling", code))
            raise RuntimeError("export failed")

    TeeEventSink(Primary(), Sibling(), None).emit("x.y", summary="s")
    assert calls == [("primary", "x.y"), ("sibling", "x.y")]


def test_composition_wires_producer_redaction_and_observer():
    from sonder_runtime.adapters.provider_bindings import ProviderBindings
    from sonder_runtime.bootstrap import app as bootstrap_app
    from sonder_runtime.platform.config import ObservabilityConfig, SonderConfig

    live = bootstrap_app._compose_live_telemetry(
        SonderConfig(), Redactor(), ProviderBindings.uniform("ollama"),
    )
    try:
        assert attempts._attempt_observer is live.telemetry
        first = json.loads(live.producer.subscribe().next_batch(0.05).events[0].line)
        assert first["event_type"] == "session.started"
        assert first["session_id"] == live.producer.session_id
        assert first["attributes"]["provider_bindings"]["fallbacks"] == {}
    finally:
        live.close()
    assert attempts._attempt_observer is None
    events = [json.loads(e.line)["event_type"]
              for e in live.producer.subscribe().next_batch(0.05).events]
    assert events[-1] == "session.ended"
    disabled = bootstrap_app._compose_live_telemetry(
        SonderConfig(observability=ObservabilityConfig(live_export=False)),
        Redactor(), ProviderBindings.uniform("ollama"),
    )
    assert (disabled.telemetry, disabled.producer, disabled.event_bridge, disabled.close) == (
        None, None, None, None,
    )
