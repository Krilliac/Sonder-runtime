from __future__ import annotations

from sonder_runtime.application.agents.retry_guard import (
    FailedToolRetryGuard,
    failure_fingerprint,
)


def test_failure_fingerprint_binds_tool_resource_scope_arguments_and_outcome():
    base = dict(
        tool="file_read",
        resource="C:/repo/README.md",
        scope="C:/repo",
        arguments={"path": "C:/repo/README.md"},
    )
    first = failure_fingerprint(**base, outcome="ERROR: missing")
    assert first != failure_fingerprint(**base, outcome="ERROR: locked")
    assert first != failure_fingerprint(**{**base, "scope": "C:/other"}, outcome="ERROR: missing")
    assert first != failure_fingerprint(**{**base, "arguments": {"path": "C:/repo/other.md"}}, outcome="ERROR: missing")


def test_guard_canary_blocks_unchanged_failure_and_allows_changed_recovery():
    guard = FailedToolRetryGuard(max_retries=2)
    identity = "file_read|C:/repo|README.md"
    call = dict(
        tool="file_read",
        resource="C:/repo/README.md",
        scope="C:/repo",
        arguments={"path": "C:/repo/README.md"},
    )

    assert guard.decision(identity).allowed
    assert guard.record_failure(identity, **call, outcome="ERROR: missing") == 1
    assert guard.decision(identity).allowed
    assert guard.record_failure(identity, **call, outcome="ERROR: missing") == 2
    blocked = guard.decision(identity)
    assert not blocked.allowed
    assert "unchanged failed tool call" in blocked.reason

    changed_identity = "file_read|C:/repo|other.md"
    assert guard.decision(changed_identity).allowed
    assert guard.record_failure(
        changed_identity,
        **{**call, "resource": "C:/repo/other.md", "arguments": {"path": "C:/repo/other.md"}},
        outcome="ERROR: missing",
    ) == 1

    guard.record_success(identity)
    assert guard.decision(identity).allowed

