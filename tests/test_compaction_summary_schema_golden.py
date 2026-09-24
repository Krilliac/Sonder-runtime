"""Golden pin for compaction ``summary_schema`` 2.

Persisted schema-2 summaries are accepted only when they exactly equal the
canonical projection re-derived from their source events.  Any change to the
schema-2 projection or classification (``compaction_retention``) therefore
invalidates every persisted schema-2 summary unless ``SUMMARY_SCHEMA_VERSION``
is bumped and the old projection kept re-derivable.  If this test fails, do
NOT update the golden: bump the schema (see ``SUMMARY_SCHEMA_VERSION``).
"""
from __future__ import annotations

import json

from sonder_runtime.application.compaction.legacy import canonical_summary
from sonder_runtime.application.compaction_retention import SUMMARY_SCHEMA_VERSION
from sonder_runtime.application.ports.compaction import (
    CompactionRequest,
    SessionHistoryEvent,
    SourceRange,
)


TRACEBACK = "noise\n" * 300 + "Traceback (most recent call last):\nValueError: GOLDEN\n"

FIXTURE = (
    ("message.received", {"text": "build it", "requirements": ["REQ-1"],
                          "constraints": ["C-offline"]}),
    ("message.received", {"text": "plain user chatter"}),
    ("message.emitted", {"text": "plain assistant chatter"}),
    ("message.emitted", {"text": "decided", "decisions": ["D-sqlite"],
                         "facts": ["F-v3"], "confidence": 0.9}),
    ("model.response", {"content": "rationale: retry with backoff"}),
    ("tool.failed", {"call_id": "c1", "error": "E-timeout"}),
    ("tool.result", {"name": "run_tests", "output": "x" * 3000, "call_id": "c2",
                     "success": False, "error_code": None}),
    ("tool.result", {"content": "y" * 3000, "call_id": "c3", "name": "build",
                     "result": {"status": "failed", "exit_code": "2",
                                "stderr": TRACEBACK, "log": "z" * 3000}}),
    ("tool.result", {"call_id": "c4", "content": "ok " * 1000,
                     "tool_outcomes": ["T-ok"], "artifacts": ["A-report.json"]}),
    ("tool.completed", {"call_id": "c5", "status": "ok", "unresolved_tasks": ["U-docs"]}),
)

# stderr keeps its last 1024 bytes: a 60-byte truncation mark + 964-byte tail.
STDERR_TAIL = (
    "...[truncated; recover full value from the source reference]" + TRACEBACK[-964:]
)

GOLDEN = {'artifacts': ['A-report.json'],
          'confidence': 0.9,
          'decisions': ['D-sqlite'],
          'facts': ['F-v3'],
          'modalities': [{'event_id': 'g1',
                          'event_type': 'message.received',
                          'modality': 'text',
                          'payload': {'constraints': ['C-offline'],
                                      'requirements': ['REQ-1'],
                                      'text': 'build it'}},
                         {'event_id': 'g5',
                          'event_type': 'model.response',
                          'modality': 'text',
                          'payload': {'content': 'rationale: retry with backoff'}},
                         {'event_id': 'g6',
                          'event_type': 'tool.failed',
                          'modality': 'text',
                          'payload': {'call_id': 'c1', 'error': 'E-timeout'}},
                         {'event_id': 'g7',
                          'event_type': 'tool.result',
                          'modality': 'text',
                          'payload': {'call_id': 'c2',
                                      'name': 'run_tests',
                                      'reference_byte_count': 3081,
                                      'reference_event_id': 'g7',
                                      'reference_event_type': 'tool.result',
                                      'reference_sequence': 7,
                                      'reference_sha256': '86528f6ec7993ab1b98e65fc46424fd9d08af7d630c778bd98af245a6d4cfebe',
                                      'success': False}},
                         {'event_id': 'g8',
                          'event_type': 'tool.result',
                          'modality': 'text',
                          'payload': {'call_id': 'c3',
                                      'name': 'build',
                                      'reference_byte_count': 8266,
                                      'reference_event_id': 'g8',
                                      'reference_event_type': 'tool.result',
                                      'reference_sequence': 8,
                                      'reference_sha256': 'e076f8847a1a4a168940a267db4a4d5a52549f5b7318d899c479e0a2fb977b4d',
                                      'result.exit_code': '2',
                                      'result.status': 'failed',
                                      'result.stderr': STDERR_TAIL}},
                         {'event_id': 'g9',
                          'event_type': 'tool.result',
                          'modality': 'text',
                          'payload': {'call_id': 'c4',
                                      'reference_byte_count': 3084,
                                      'reference_event_id': 'g9',
                                      'reference_event_type': 'tool.result',
                                      'reference_sequence': 9,
                                      'reference_sha256': '92f221822e317f9e9354e3176f190ee5ea3636b13be9b5c708806c383fb9e076'}},
                         {'event_id': 'g10',
                          'event_type': 'tool.completed',
                          'modality': 'text',
                          'payload': {'call_id': 'c5',
                                      'status': 'ok',
                                      'unresolved_tasks': ['U-docs']}}],
          'tool_outcomes': ['T-ok'],
          'unresolved_tasks': ['U-docs']}


def _projection(summary) -> dict:
    return json.loads(json.dumps({
        "facts": list(summary.facts), "decisions": list(summary.decisions),
        "unresolved_tasks": list(summary.unresolved_tasks),
        "artifacts": list(summary.artifacts), "tool_outcomes": list(summary.tool_outcomes),
        "confidence": summary.confidence,
        "modalities": [
            {"event_id": item.event_id, "event_type": item.event_type,
             "modality": item.modality,
             "payload": json.loads(json.dumps(item.payload, default=dict))}
            for item in summary.modalities
        ],
    }, default=list, sort_keys=True))


def test_schema_2_projection_is_pinned():
    assert SUMMARY_SCHEMA_VERSION == 2, (
        "schema bumped: add a new golden for the new version and keep this one"
    )
    history = tuple(
        SessionHistoryEvent(f"g{index}", "golden", index, event_type, payload)
        for index, (event_type, payload) in enumerate(FIXTURE, 1)
    )
    request = CompactionRequest(
        "golden", history, SourceRange("golden", 1, len(history), "g1", f"g{len(history)}"),
    )
    assert _projection(canonical_summary(request, schema=2)) == GOLDEN
