import json

import pytest

from sonder_runtime.application.observability.trace_projection import project_trace
from sonder_runtime.domain.common.errors import InvalidInput


def _event(**overrides):
    row = {
        "sequence": 2,
        "observed_at": 4.5,
        "correlation_id": "req-1",
        "category": "agent",
        "severity": "INFO",
        "event_code": "MODEL_CALL",
        "duration_ms": 12,
        "operation_id": "op-1",
        "fields": {
            "prompt": "do not retain this",
            "stdout": "do not retain this either",
        },
    }
    row.update(overrides)
    return row


def test_projection_is_deterministic_bounded_and_otel_shaped():
    result = project_trace([_event(), _event(sequence=1, event_code="TOOL_CALL")])
    body = result.to_dict()
    assert body["schema"] == "sonder.trace-span.v1"
    assert [row["sequence"] for row in body["spans"]] == [1, 2]
    assert body["spans"][0]["operation"] == "execute_tool"
    assert body["spans"][1]["attributes"]["gen_ai.operation.name"] == "invoke_agent"
    assert body["redacted"] is True
    assert "do not retain" not in result.to_json()
    assert json.loads(result.to_json()) == body


def test_projection_hashes_correlation_and_omits_fields():
    row = _event(correlation_id="private-correlation", fields={"token": "secret"})
    body = project_trace([row]).to_dict()["spans"][0]
    assert body["trace_id"] != "private-correlation"
    assert "fields" not in body
    assert "secret" not in json.dumps(body)


def test_projection_rejects_malformed_or_oversized_input():
    with pytest.raises(InvalidInput):
        project_trace([_event(sequence=0)])
    with pytest.raises(InvalidInput):
        project_trace([_event()] * 257)
    with pytest.raises(InvalidInput):
        project_trace([_event()], max_spans=0)
