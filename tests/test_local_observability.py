import json
import threading

from sonder_logging import Redactor
from sonder_runtime.adapters.local_observability import LocalObservabilitySink
from sonder_runtime.bootstrap import app as bootstrap_app


class _Delegate:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def emit(self, event_code, **kwargs):
        if self.fail:
            raise OSError("unavailable")
        self.calls.append((event_code, kwargs))


def test_redacts_fields_locally_and_delegates_the_original_event_first():
    delegate = _Delegate()
    sink = LocalObservabilitySink(
        delegate,
        redactor=Redactor(env={"SONDER_API_KEY": "secret-value-1234"}),
        clock=lambda: 12.5,
    )

    sink.emit(
        "TOOL_DONE",
        summary="raw user prompt secret-value-1234",
        correlation_id="req_safe",
        detail={
            "category": "tool",
            "duration_ms": 7,
            "prompt": "raw private request",
            "env": {"SONDER_API_KEY": "secret-value-1234"},
            "result": "credential=secret-value-1234",
            "count": 3,
        },
    )

    event = sink.recent_events()[0]
    dumped = json.dumps(event)
    assert event["observed_at"] == 12.5
    assert event["correlation_id"] == "req_safe"
    assert "[REDACTED_FIELD]" in event["fields"].values()
    assert event["fields"]["count"] == 3
    assert "raw private request" not in dumped
    assert "secret-value-1234" not in dumped
    assert delegate.calls[0] == (
        "TOOL_DONE",
        {
            "summary": "raw user prompt secret-value-1234",
            "detail": {
                "category": "tool",
                "duration_ms": 7,
                "prompt": "raw private request",
                "env": {"SONDER_API_KEY": "secret-value-1234"},
                "result": "credential=secret-value-1234",
                "count": 3,
            },
            "severity": "INFO",
            "correlation_id": "req_safe",
            "operation_id": None,
        },
    )


def test_api_key_values_and_secret_bearing_key_names_never_reach_local_state():
    sink = LocalObservabilitySink(clock=lambda: 1.0, redactor=Redactor(env={}))
    sink.emit(
        "SAFE",
        summary="SAFE",
        detail={
            "api_key": "previously-unknown-secret",
            "password_hunter2": "anything",
            "ordinary": "kept",
        },
    )

    dumped = json.dumps(sink.recent_events())
    assert "previously-unknown-secret" not in dumped
    assert "password_hunter2" not in dumped
    assert "ordinary" in dumped
    assert sink.snapshot()["drops"]["redacted_fields"] == 2


def test_event_and_series_caps_have_explicit_drop_accounting():
    sink = LocalObservabilitySink(
        max_events=2, max_series=1, latency_window=2, clock=lambda: 1.0,
    )
    sink.emit("ONE", summary="ONE", detail={"category": "a"})
    sink.emit("TWO", summary="TWO", detail={"category": "b"})
    sink.emit("THREE", summary="THREE", detail={"category": "c"})

    snap = sink.snapshot()
    assert snap["total_observed"] == 3
    assert snap["retained_events"] == 2
    assert snap["drops"]["evicted_events"] == 1
    assert snap["drops"]["series_observations"] == 2
    assert [row["event_code"] for row in sink.recent_events(limit=100)] == [
        "THREE", "TWO",
    ]


def test_latency_summary_is_deterministic_over_bounded_window():
    sink = LocalObservabilitySink(latency_window=3, clock=lambda: 5.0)
    for duration in (10, 20, 30, 40):
        sink.emit(
            "MODEL_CALL",
            summary="MODEL_CALL",
            detail={"category": "model", "duration_ms": duration},
            severity="INFO",
            correlation_id="req_latency",
        )

    row = sink.snapshot()["series"][0]
    assert row["count"] == 4
    assert row["latency_ms"] == {
        "window_count": 3,
        "min": 20,
        "max": 40,
        "mean": 30,
        "p50": 30,
        "p95": 40,
    }
    assert sink.snapshot()["drops"]["latency_samples"] == 1


def test_invalid_local_event_is_still_delegated_and_call_exception_is_not_attestation():
    delegate = _Delegate()
    sink = LocalObservabilitySink(delegate, clock=lambda: 2.0)
    sink.emit("bad code with spaces", summary="ignored")

    snap = sink.snapshot()
    assert delegate.calls[0][0] == "bad code with spaces"
    assert snap["total_observed"] == 0
    assert snap["drops"]["invalid_events"] == 1
    assert snap["boundaries"]["delegate_delivery_attestation"] == "unavailable"

    failing = LocalObservabilitySink(_Delegate(fail=True), clock=lambda: 2.0)
    failing.emit("VALID", summary="VALID")
    assert failing.snapshot()["drops"]["delegate_call_exceptions"] == 1


def test_huge_recursive_and_hostile_values_are_bounded_without_conversion_hooks():
    class Hostile:
        def __str__(self):
            raise AssertionError("must not stringify hostile values")

    class HostileDict(dict):
        def __iter__(self):
            raise AssertionError("must not iterate custom containers")

    recursive = {}
    recursive["self"] = recursive
    huge = {"item_%d" % index: index for index in range(10_000)}
    sink = LocalObservabilitySink(max_events=4, clock=lambda: 3.0)
    sink.emit(
        "BOUNDED",
        summary="BOUNDED",
        detail={
            "huge": huge,
            "list": list(range(10_000)),
            "recursive": recursive,
            "hostile": Hostile(),
            "hostile_container": HostileDict({"secret": "value"}),
        },
    )

    event = sink.recent_events()[0]
    dumped = json.dumps(event)
    assert len(dumped.encode("utf-8")) < 20_000
    assert "[RECURSION_TRUNCATED]" in dumped
    assert event["fields"]["hostile"] == "[UNSAFE_VALUE]"
    assert event["fields"]["hostile_container"] == "[UNSAFE_VALUE]"
    assert sink.snapshot()["drops"]["truncated_fields"] > 0


def test_long_secret_values_are_replaced_wholesale_before_prefix_truncation():
    secret = "secret-" + "x" * 300
    sink = LocalObservabilitySink(
        redactor=Redactor(secret_values=(secret,), env={}), clock=lambda: 3.5,
    )
    sink.emit(
        "SAFE",
        summary="SAFE",
        detail={"ordinary": "near-boundary-prefix:" + secret},
    )

    event = sink.recent_events()[0]
    dumped = json.dumps(event)
    assert event["fields"]["ordinary"] == "[TRUNCATED_VALUE]"
    assert secret not in dumped
    assert secret[:240] not in dumped
    assert sink.snapshot()["drops"]["truncated_fields"] == 1


def test_redaction_precedes_nul_display_normalization():
    secret = "credential\x00with-secret-material"
    sink = LocalObservabilitySink(
        redactor=Redactor(secret_values=(secret,), env={}), clock=lambda: 3.5,
    )
    sink.emit(
        "SAFE",
        summary="SAFE",
        detail={"ordinary": "prefix:" + secret + ":suffix"},
    )

    dumped = json.dumps(sink.recent_events()[0])
    assert secret.replace("\x00", "\\0") not in dumped
    assert "with-secret-material" not in dumped
    assert "[REDACTED]" in dumped


def test_secret_identifier_values_fall_back_before_retention():
    secret = "SecretIdentifier1234"
    sink = LocalObservabilitySink(
        redactor=Redactor(secret_values=(secret,), env={}), clock=lambda: 4.0,
    )
    sink.emit(
        "VALID",
        summary="VALID",
        correlation_id=secret,
        operation_id=secret,
        detail={"category": secret},
    )

    event = sink.recent_events()[0]
    assert secret not in json.dumps(event)
    assert event["category"] == "application"
    assert event["correlation_id"].startswith("local-")
    assert event["operation_id"] is None


def test_bootstrap_wraps_authoritative_sink_without_rewriting(monkeypatch):
    delegate = _Delegate()
    monkeypatch.setattr(bootstrap_app, "OperationsEventSink", lambda: delegate)

    application = bootstrap_app.build_application()
    application.events.emit(
        "invalid local code",
        summary="original summary",
        detail={"request_id": "exact"},
        severity="INFO",
    )

    assert isinstance(application.events, LocalObservabilitySink)
    assert delegate.calls == [(
        "invalid local code",
        {
            "summary": "original summary",
            "detail": {"request_id": "exact"},
            "severity": "INFO",
            "correlation_id": None,
            "operation_id": None,
        },
    )]


def test_concurrent_emit_keeps_exact_counters_and_bounded_recent_events():
    sink = LocalObservabilitySink(max_events=64, clock=lambda: 9.0)
    threads = []

    def produce(worker):
        for index in range(100):
            sink.emit(
                "WORK",
                summary="WORK",
                detail={"category": "agent", "worker": worker, "duration_ms": index},
                correlation_id="req-%d" % worker,
            )

    for worker in range(8):
        thread = threading.Thread(target=produce, args=(worker,))
        threads.append(thread)
        thread.start()
    for thread in threads:
        thread.join()

    snap = sink.snapshot()
    assert snap["total_observed"] == 800
    assert snap["retained_events"] == 64
    assert snap["drops"]["evicted_events"] == 736
    assert snap["series"][0]["count"] == 800
    assert len(sink.recent_events(limit=10_000)) == 64
    assert len(sink.recent_events(correlation_id="req-3")) <= 64
