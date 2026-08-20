from sonder_runtime.adapters.observability.distillation_formatting import (
    _drain_backlog_text,
    _drain_summary_text,
)


def test_drain_backlog_reports_unknown_when_count_query_failed():
    assert _drain_backlog_text({"backlog": None}) == "unknown (count query failed)"


def test_drain_summary_preserves_healthy_wire_format():
    assert _drain_summary_text({
        "drained": 4, "stored": 3, "deferred": 1, "backlog": 8,
    }) == (
        "deferred distillations drained: 4 (lessons stored 3, still "
        "deferred in batch 1, backlog remaining 8)"
    )


def test_drain_summary_surfaces_failures():
    assert "failed 2" in _drain_summary_text({
        "drained": 4, "stored": 1, "deferred": 1,
        "failed": 2, "backlog": None,
    })
