from sonder_runtime.application.context_overflow_recovery import ContextOverflowRecovery


def _policy():
    return ContextOverflowRecovery(
        compact=lambda value: value[:-1] if len(value) > 2 else None,
        shrink=lambda value, factor: value[:max(1, int(len(value) * factor))],
        max_attempts=2,
        shrink_factor=0.5,
    )


def test_compacts_before_projected_overflow():
    policy = _policy()
    result = policy.prepare([1, 2, 3, 4], estimated_tokens=9, context_limit=10)

    assert result.action == "preflight_compacted"
    assert result.candidate == [1, 2, 3]


def test_preflight_leaves_safe_candidate_unchanged():
    policy = _policy()
    candidate = [1, 2, 3]

    result = policy.prepare(candidate, estimated_tokens=2, context_limit=10)

    assert result.action == "unchanged"
    assert result.candidate == candidate


def test_adaptive_recovery_is_bounded_and_records_last_good():
    policy = _policy()
    calls = []

    result = policy.recover(
        list(range(10)),
        overflow=True,
        fits=lambda value: calls.append(value) or len(value) <= 2,
    )

    assert result.action == "adaptively_shrunk"
    assert result.candidate == [0]
    assert result.attempts == 3  # one compact attempt plus two shrink attempts
    assert len(calls) == 3
    assert policy.last_good() == [0]


def test_persistent_overflow_returns_last_good_without_unbounded_retry():
    policy = _policy()
    policy.accept(["known", "good"])
    calls = []

    result = policy.recover(
        list(range(10)), overflow=True, fits=lambda value: calls.append(value) or False
    )

    assert result.action == "last_good"
    assert result.candidate == ["known", "good"]
    assert result.used_last_good is True
    assert result.attempts == 3
    assert len(calls) == 3


def test_non_overflow_is_never_retried_or_published():
    policy = _policy()
    result = policy.recover([1, 2], overflow=False, fits=lambda _: False)

    assert result.action == "not_overflow"
    assert result.candidate == [1, 2]
    assert policy.last_good() is None


def test_snapshots_are_isolated_from_caller_mutation():
    policy = _policy()
    candidate = {"messages": ["ok"]}
    policy.accept(candidate)
    candidate["messages"].append("changed")

    snapshot = policy.last_good()
    snapshot["messages"].append("local mutation")
    assert policy.last_good() == {"messages": ["ok"]}
