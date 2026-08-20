from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.operations.tracing_health import (
    BoundedTracer, HealthState, health_snapshot,
)


class Exporter:
    def __init__(self):
        self.records = []

    def export(self, record):
        self.records.append(record)


def test_trace_redacts_content_and_bounds_labels():
    exporter = Exporter()
    tracer = BoundedTracer(exporter)
    context = local_owner_context(correlation_id="corr-1")
    record = tracer.emit(context, operation="model.call", labels={"model": "qwen", "extra": "x"},
                         attributes={"prompt": "private prompt", "nested": {"token": "secret", "ok": "yes"}})
    assert record.redaction_applied is True
    assert record.attributes["prompt"] == "[redacted]"
    assert record.attributes["nested"]["token"] == "[redacted]"
    assert len(exporter.records) == 1


def test_cardinality_collapses_new_label_values():
    exporter = Exporter()
    tracer = BoundedTracer(exporter, cardinality=__import__(
        "sonder_runtime.application.operations.tracing_health", fromlist=["CardinalityLimiter"]
    ).CardinalityLimiter(2))
    context = local_owner_context(correlation_id="corr-2")
    for value in ("a", "b", "c"):
        tracer.emit(context, operation="job", labels={"job_id": value})
    assert exporter.records[-1].labels["job_id"] == "__cardinality_overflow__"


def test_health_distinguishes_readiness_dependency_drain_and_recovery():
    snapshot = health_snapshot(live=True, ready=False, dependencies={"ollama": "down"},
                               draining=True, recovery_required=True)
    assert HealthState.LIVE in snapshot.states
    assert HealthState.READY not in snapshot.states
    assert HealthState.DEPENDENCY_UNHEALTHY in snapshot.states
    assert HealthState.DRAINING in snapshot.states
    assert HealthState.RECOVERY_REQUIRED in snapshot.states
    assert snapshot.healthy is False
