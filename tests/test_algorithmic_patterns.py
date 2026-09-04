"""Tests for the 8 algorithmic pattern modules.

1. Circuit breaker
2. Token bucket rate limiting
3. Consistent hashing / rendezvous hashing
4. Work stealing
5. Adaptive batching
6. Bloom filter / sliding bloom filter
7. Deadline-aware scheduling
8. Backpressure propagation
"""
from __future__ import annotations

import pytest

from sonder_runtime.domain.circuit_breaker import (
    BreakerConfig,
    BreakerState,
    CircuitBreaker,
)
from sonder_runtime.domain.token_bucket import (
    AcquireResult,
    RateLimited,
    TokenBucket,
)
from sonder_runtime.domain.consistent_hash import (
    HashRing,
    RendezvousHash,
)
from sonder_runtime.domain.work_stealing import (
    WorkerDeque,
    WorkStealingScheduler,
)
from sonder_runtime.domain.adaptive_batcher import (
    AdaptiveBatcher,
    BatchConfig,
)
from sonder_runtime.domain.bloom_filter import (
    BloomFilter,
    SlidingBloomFilter,
)
from sonder_runtime.domain.deadline_scheduler import (
    DeadlineConstraint,
    DeadlineScoredNode,
    NodeEstimate,
    estimate_completion,
    filter_by_deadline,
    has_budget_for_retry,
    remaining_budget_fraction,
    score_with_deadline,
)
from sonder_runtime.domain.backpressure import (
    AdmissionDecision,
    BackpressureChain,
)
from sonder_runtime.domain.fleet_pressure import (
    BAND_CRITICAL,
    BAND_HIGH,
    BAND_LOW,
    BAND_MEDIUM,
)


# ---------------------------------------------------------------------------
# 1. Circuit Breaker
# ---------------------------------------------------------------------------

class TestCircuitBreaker:

    def test_starts_closed(self):
        cb = CircuitBreaker()
        assert cb.state == BreakerState.CLOSED
        assert cb.trip_count == 0

    def test_allows_calls_when_closed(self):
        cb = CircuitBreaker()
        assert cb.before_call(now=0.0) is True

    def test_trips_after_threshold_failures(self):
        cb = CircuitBreaker(BreakerConfig(failure_threshold=3, window_seconds=60.0))
        for i in range(3):
            cb.record_failure(now=float(i))
        assert cb.state == BreakerState.OPEN
        assert cb.trip_count == 1

    def test_open_rejects_calls(self):
        cb = CircuitBreaker(BreakerConfig(failure_threshold=2))
        cb.record_failure(now=1.0)
        cb.record_failure(now=2.0)
        assert cb.before_call(now=3.0) is False

    def test_transitions_to_half_open_after_recovery(self):
        cb = CircuitBreaker(BreakerConfig(
            failure_threshold=2, recovery_seconds=10.0
        ))
        cb.record_failure(now=1.0)
        cb.record_failure(now=2.0)
        assert cb.state == BreakerState.OPEN
        assert cb.before_call(now=5.0) is False
        assert cb.before_call(now=13.0) is True
        assert cb.state == BreakerState.HALF_OPEN

    def test_half_open_success_closes(self):
        cb = CircuitBreaker(BreakerConfig(
            failure_threshold=2, recovery_seconds=5.0
        ))
        cb.record_failure(now=1.0)
        cb.record_failure(now=2.0)
        cb.before_call(now=10.0)
        cb.record_success(now=10.0)
        assert cb.state == BreakerState.CLOSED

    def test_half_open_failure_reopens(self):
        cb = CircuitBreaker(BreakerConfig(
            failure_threshold=2, recovery_seconds=5.0
        ))
        cb.record_failure(now=1.0)
        cb.record_failure(now=2.0)
        cb.before_call(now=10.0)
        cb.record_failure(now=10.0)
        assert cb.state == BreakerState.OPEN
        assert cb.trip_count == 2

    def test_recovery_timeout_increases_exponentially(self):
        cb = CircuitBreaker(BreakerConfig(
            failure_threshold=1,
            recovery_seconds=10.0,
            recovery_multiplier=2.0,
            max_recovery_seconds=100.0,
        ))
        cb.record_failure(now=1.0)
        snap1 = cb.snapshot()
        assert snap1.recovery_deadline == 11.0

        cb.before_call(now=12.0)
        cb.record_failure(now=12.0)
        snap2 = cb.snapshot()
        assert snap2.recovery_deadline == 32.0

    def test_failures_outside_window_expire(self):
        cb = CircuitBreaker(BreakerConfig(
            failure_threshold=3, window_seconds=10.0
        ))
        cb.record_failure(now=1.0)
        cb.record_failure(now=2.0)
        cb.record_failure(now=15.0)
        assert cb.state == BreakerState.CLOSED

    def test_reset_clears_state(self):
        cb = CircuitBreaker(BreakerConfig(failure_threshold=1))
        cb.record_failure(now=1.0)
        assert cb.state == BreakerState.OPEN
        cb.reset()
        assert cb.state == BreakerState.CLOSED
        assert cb.before_call(now=2.0) is True

    def test_snapshot_captures_state(self):
        cb = CircuitBreaker()
        cb.record_success(now=1.0)
        cb.record_failure(now=2.0)
        snap = cb.snapshot()
        assert snap.success_count == 1
        assert snap.consecutive_failures == 1
        assert snap.state == BreakerState.CLOSED


# ---------------------------------------------------------------------------
# 2. Token Bucket Rate Limiting
# ---------------------------------------------------------------------------

class TestTokenBucket:

    def test_initial_tokens_equal_capacity(self):
        tb = TokenBucket(capacity=10, refill_rate=1.0)
        assert tb.tokens == 10.0
        assert tb.capacity == 10.0

    def test_acquire_consumes_token(self):
        tb = TokenBucket(capacity=5, refill_rate=1.0)
        result = tb.try_acquire(now=0.0)
        assert result.allowed is True
        assert result.tokens_remaining == 4.0

    def test_exhausted_bucket_rejects(self):
        tb = TokenBucket(capacity=2, refill_rate=1.0, initial=0.0)
        result = tb.try_acquire(now=0.0)
        assert result.allowed is False
        assert result.retry_after > 0

    def test_refill_over_time(self):
        tb = TokenBucket(capacity=10, refill_rate=2.0, initial=0.0)
        result = tb.try_acquire(now=0.0)
        assert result.allowed is False
        result = tb.try_acquire(now=1.0)
        assert result.allowed is True
        assert result.tokens_remaining == pytest.approx(1.0, abs=0.01)

    def test_refill_caps_at_capacity(self):
        tb = TokenBucket(capacity=5, refill_rate=100.0, initial=3.0)
        result = tb.try_acquire(now=0.0)
        assert result.allowed is True
        result = tb.try_acquire(now=10.0)
        assert result.tokens_remaining == pytest.approx(4.0, abs=0.01)

    def test_acquire_raises_rate_limited(self):
        tb = TokenBucket(capacity=1, refill_rate=1.0, initial=0.0)
        with pytest.raises(RateLimited) as exc_info:
            tb.acquire(now=0.0)
        assert exc_info.value.retry_after > 0

    def test_custom_cost(self):
        tb = TokenBucket(capacity=10, refill_rate=1.0)
        result = tb.try_acquire(cost=5.0, now=0.0)
        assert result.allowed is True
        assert result.tokens_remaining == 5.0

    def test_invalid_capacity_raises(self):
        with pytest.raises(ValueError):
            TokenBucket(capacity=0, refill_rate=1.0)

    def test_invalid_rate_raises(self):
        with pytest.raises(ValueError):
            TokenBucket(capacity=10, refill_rate=0)


# ---------------------------------------------------------------------------
# 3. Consistent Hashing
# ---------------------------------------------------------------------------

class TestHashRing:

    def test_empty_ring_returns_none(self):
        ring = HashRing()
        assert ring.get_node("key") is None

    def test_single_node_always_selected(self):
        ring = HashRing()
        ring.add_node("node-a")
        for i in range(20):
            assert ring.get_node(f"key-{i}") == "node-a"

    def test_keys_distribute_across_nodes(self):
        ring = HashRing(replicas=150)
        for n in ("a", "b", "c"):
            ring.add_node(n)
        counts: dict[str, int] = {"a": 0, "b": 0, "c": 0}
        for i in range(300):
            node = ring.get_node(f"key-{i}")
            assert node is not None
            counts[node] += 1
        for c in counts.values():
            assert c > 30, f"distribution too skewed: {counts}"

    def test_add_node_is_idempotent(self):
        ring = HashRing()
        ring.add_node("node-a")
        ring.add_node("node-a")
        assert ring.size == 1

    def test_remove_node(self):
        ring = HashRing()
        ring.add_node("a")
        ring.add_node("b")
        ring.remove_node("a")
        assert ring.size == 1
        for i in range(20):
            assert ring.get_node(f"key-{i}") == "b"

    def test_minimal_remapping_on_add(self):
        ring = HashRing(replicas=150)
        for n in ("a", "b", "c"):
            ring.add_node(n)
        before = {f"k{i}": ring.get_node(f"k{i}") for i in range(200)}
        ring.add_node("d")
        after = {f"k{i}": ring.get_node(f"k{i}") for i in range(200)}
        same = sum(1 for k in before if before[k] == after[k])
        assert same > 100, f"too many keys remapped: {200 - same}"

    def test_get_nodes_returns_multiple(self):
        ring = HashRing()
        for n in ("a", "b", "c"):
            ring.add_node(n)
        nodes = ring.get_nodes("some-key", count=2)
        assert len(nodes) == 2
        assert len(set(nodes)) == 2


class TestRendezvousHash:

    def test_empty_returns_none(self):
        rh = RendezvousHash()
        assert rh.get_node("key") is None

    def test_single_node(self):
        rh = RendezvousHash()
        rh.add_node("only")
        assert rh.get_node("any-key") == "only"

    def test_deterministic_selection(self):
        rh = RendezvousHash()
        for n in ("x", "y", "z"):
            rh.add_node(n)
        first = rh.get_node("test-key")
        second = rh.get_node("test-key")
        assert first == second

    def test_weighted_bias(self):
        rh = RendezvousHash()
        rh.add_node("heavy", weight=100.0)
        rh.add_node("light", weight=0.001)
        counts = {"heavy": 0, "light": 0}
        for i in range(100):
            node = rh.get_node(f"k{i}")
            assert node is not None
            counts[node] += 1
        assert counts["heavy"] > counts["light"]

    def test_remove_node(self):
        rh = RendezvousHash()
        rh.add_node("a")
        rh.add_node("b")
        rh.remove_node("a")
        assert rh.size == 1
        assert rh.get_node("key") == "b"

    def test_invalid_weight_raises(self):
        rh = RendezvousHash()
        with pytest.raises(ValueError):
            rh.add_node("bad", weight=0)

    def test_get_nodes_ordered(self):
        rh = RendezvousHash()
        for n in ("a", "b", "c"):
            rh.add_node(n)
        nodes = rh.get_nodes("key", count=3)
        assert len(nodes) == 3
        assert len(set(nodes)) == 3


# ---------------------------------------------------------------------------
# 4. Work Stealing
# ---------------------------------------------------------------------------

class TestWorkerDeque:

    def test_push_and_pop_lifo(self):
        dq: WorkerDeque[int] = WorkerDeque("w1")
        dq.push(1)
        dq.push(2)
        dq.push(3)
        assert dq.pop() == 3
        assert dq.pop() == 2
        assert dq.pop() == 1

    def test_steal_fifo(self):
        dq: WorkerDeque[int] = WorkerDeque("w1")
        dq.push(10)
        dq.push(20)
        dq.push(30)
        assert dq.steal() == 10
        assert dq.steal() == 20

    def test_empty_pop_returns_none(self):
        dq: WorkerDeque[str] = WorkerDeque("w1")
        assert dq.pop() is None

    def test_empty_steal_returns_none(self):
        dq: WorkerDeque[str] = WorkerDeque("w1")
        assert dq.steal() is None

    def test_max_size_enforced(self):
        dq: WorkerDeque[int] = WorkerDeque("w1", max_size=2)
        assert dq.push(1) is True
        assert dq.push(2) is True
        assert dq.push(3) is False
        assert dq.size == 2

    def test_stolen_from_counter(self):
        dq: WorkerDeque[int] = WorkerDeque("w1")
        dq.push(1)
        dq.push(2)
        dq.steal()
        assert dq.stolen_from == 1


class TestWorkStealingScheduler:

    def test_submit_and_get_own_work(self):
        sched: WorkStealingScheduler[str] = WorkStealingScheduler()
        sched.add_worker("w1")
        sched.submit("w1", "task-a")
        result = sched.get_work("w1")
        assert result.item == "task-a"
        assert result.stolen is False

    def test_steal_from_busiest(self):
        sched: WorkStealingScheduler[str] = WorkStealingScheduler()
        sched.add_worker("idle")
        sched.add_worker("busy")
        for i in range(5):
            sched.submit("busy", f"task-{i}")
        result = sched.try_steal("idle")
        assert result.stolen is True
        assert result.source_worker == "busy"
        assert sched.total_steals == 1

    def test_steal_when_all_empty(self):
        sched: WorkStealingScheduler[str] = WorkStealingScheduler()
        sched.add_worker("w1")
        sched.add_worker("w2")
        result = sched.try_steal("w1")
        assert result.stolen is False
        assert result.item is None

    def test_remove_worker_returns_orphans(self):
        sched: WorkStealingScheduler[str] = WorkStealingScheduler()
        sched.add_worker("w1")
        sched.submit("w1", "a")
        sched.submit("w1", "b")
        orphans = sched.remove_worker("w1")
        assert len(orphans) == 2

    def test_get_work_falls_through_to_steal(self):
        sched: WorkStealingScheduler[int] = WorkStealingScheduler()
        sched.add_worker("thief")
        sched.add_worker("victim")
        sched.submit("victim", 42)
        result = sched.get_work("thief")
        assert result.item == 42
        assert result.stolen is True

    def test_snapshot(self):
        sched: WorkStealingScheduler[str] = WorkStealingScheduler()
        sched.add_worker("w1")
        sched.submit("w1", "task")
        snap = sched.snapshot()
        assert snap["workers"]["w1"]["queue_size"] == 1
        assert snap["total_submitted"] == 1


# ---------------------------------------------------------------------------
# 5. Adaptive Batching
# ---------------------------------------------------------------------------

class TestAdaptiveBatcher:

    def test_add_returns_none_below_threshold(self):
        batcher: AdaptiveBatcher[str] = AdaptiveBatcher(BatchConfig(max_batch_size=3))
        result = batcher.add("a", now=0.0)
        assert result is None
        assert batcher.pending == 1

    def test_add_returns_batch_at_threshold(self):
        batcher: AdaptiveBatcher[str] = AdaptiveBatcher(BatchConfig(max_batch_size=2))
        batcher.add("a", now=0.0)
        batch = batcher.add("b", now=0.001)
        assert batch is not None
        assert batch.size == 2
        assert batch.items == ("a", "b")
        assert batcher.pending == 0

    def test_flush_on_time_window(self):
        batcher: AdaptiveBatcher[str] = AdaptiveBatcher(BatchConfig(
            max_batch_size=100, max_window_ms=10.0
        ))
        batcher.add("x", now=0.0)
        assert batcher.flush(now=0.005) is None
        batch = batcher.flush(now=0.015)
        assert batch is not None
        assert batch.items == ("x",)

    def test_force_flush_ignores_thresholds(self):
        batcher: AdaptiveBatcher[str] = AdaptiveBatcher(BatchConfig(
            max_batch_size=100, max_window_ms=1000.0
        ))
        batcher.add("early", now=0.0)
        batch = batcher.force_flush(now=0.001)
        assert batch is not None
        assert batch.items == ("early",)

    def test_pressure_shrinks_window(self):
        batcher: AdaptiveBatcher[str] = AdaptiveBatcher(BatchConfig(
            min_window_ms=5.0, max_window_ms=50.0, pressure_alpha=1.0
        ))
        assert batcher.current_window_ms == 50.0
        batcher.update_pressure(1.0)
        assert batcher.current_window_ms == pytest.approx(5.0)

    def test_low_pressure_widens_window(self):
        batcher: AdaptiveBatcher[str] = AdaptiveBatcher(BatchConfig(
            min_window_ms=5.0, max_window_ms=50.0, pressure_alpha=1.0
        ))
        batcher.update_pressure(0.0)
        assert batcher.current_window_ms == pytest.approx(50.0)

    def test_force_flush_empty_returns_none(self):
        batcher: AdaptiveBatcher[str] = AdaptiveBatcher()
        assert batcher.force_flush(now=0.0) is None

    def test_batch_tracks_pressure(self):
        batcher: AdaptiveBatcher[str] = AdaptiveBatcher(BatchConfig(
            max_batch_size=1, pressure_alpha=1.0
        ))
        batcher.update_pressure(0.5)
        batch = batcher.add("item", now=0.0)
        assert batch is not None
        assert batch.pressure == 0.5


# ---------------------------------------------------------------------------
# 6. Bloom Filter
# ---------------------------------------------------------------------------

class TestBloomFilter:

    def test_contains_after_add(self):
        bf = BloomFilter(expected_items=100, fp_rate=0.01)
        bf.add("hello")
        assert bf.contains("hello") is True

    def test_not_contains_before_add(self):
        bf = BloomFilter(expected_items=100, fp_rate=0.01)
        assert bf.contains("missing") is False

    def test_no_false_negatives(self):
        bf = BloomFilter(expected_items=1000, fp_rate=0.01)
        keys = [f"key-{i}" for i in range(500)]
        for k in keys:
            bf.add(k)
        for k in keys:
            assert bf.contains(k) is True

    def test_false_positive_rate_bounded(self):
        bf = BloomFilter(expected_items=1000, fp_rate=0.05)
        for i in range(1000):
            bf.add(f"present-{i}")
        fp = sum(1 for i in range(10000) if bf.contains(f"absent-{i}"))
        assert fp < 1000, f"false positive rate too high: {fp / 10000}"

    def test_add_returns_true_for_new(self):
        bf = BloomFilter()
        assert bf.add("new") is True

    def test_add_returns_false_for_duplicate(self):
        bf = BloomFilter()
        bf.add("dup")
        assert bf.add("dup") is False

    def test_count_tracks_unique_adds(self):
        bf = BloomFilter()
        bf.add("a")
        bf.add("b")
        bf.add("a")
        assert bf.count == 2


class TestSlidingBloomFilter:

    def test_contains_within_window(self):
        sbf = SlidingBloomFilter(window_seconds=10.0)
        sbf.add("key", now=0.0)
        assert sbf.contains("key", now=3.0) is True

    def test_expires_after_window(self):
        sbf = SlidingBloomFilter(window_seconds=10.0, expected_items=100)
        sbf.add("old-key", now=0.0)
        assert sbf.contains("old-key", now=6.0) is True, "still in previous after first rotation"
        assert sbf.contains("old-key", now=12.0) is False, "gone after second rotation"

    def test_rotation_preserves_recent(self):
        sbf = SlidingBloomFilter(window_seconds=10.0)
        sbf.add("recent", now=4.0)
        assert sbf.contains("recent", now=6.0) is True

    def test_total_added(self):
        sbf = SlidingBloomFilter()
        sbf.add("a", now=0.0)
        sbf.add("b", now=1.0)
        assert sbf.total_added == 2

    def test_invalid_window_raises(self):
        with pytest.raises(ValueError):
            SlidingBloomFilter(window_seconds=0)


# ---------------------------------------------------------------------------
# 7. Deadline-Aware Scheduling
# ---------------------------------------------------------------------------

class TestDeadlineScheduler:

    def test_estimate_completion_basic(self):
        node = NodeEstimate(
            node_id="n1",
            estimated_completion_ms=0,
            queue_depth=0,
            load_fraction=0.0,
            round_trip_ms=10.0,
        )
        est = estimate_completion(node, base_processing_ms=100.0)
        assert est == pytest.approx(110.0)

    def test_load_increases_estimate(self):
        light = NodeEstimate("n1", 0, 0, 0.0, 0.0)
        heavy = NodeEstimate("n2", 0, 0, 1.0, 0.0)
        assert estimate_completion(heavy, 100.0) > estimate_completion(light, 100.0)

    def test_queue_depth_increases_estimate(self):
        empty = NodeEstimate("n1", 0, 0, 0.0, 0.0)
        busy = NodeEstimate("n2", 0, 5, 0.0, 0.0)
        assert estimate_completion(busy, 100.0) > estimate_completion(empty, 100.0)

    def test_score_with_deadline_meets(self):
        node = NodeEstimate("n1", 0, 0, 0.0, 10.0)
        constraint = DeadlineConstraint(deadline_at=100.0)
        scored = score_with_deadline(node, 50.0, constraint, 100.0, now=0.0)
        assert scored.meets_deadline is True
        assert scored.final_score > 50.0

    def test_score_with_deadline_misses(self):
        node = NodeEstimate("n1", 0, 10, 1.0, 500.0)
        constraint = DeadlineConstraint(deadline_at=1.0)
        scored = score_with_deadline(node, 50.0, constraint, 5000.0, now=0.0)
        assert scored.meets_deadline is False

    def test_filter_removes_late_nodes(self):
        fast = DeadlineScoredNode("fast", 50, 80, 130, True, 100, 900)
        slow = DeadlineScoredNode("slow", 50, -50, 0, False, 2000, -1000)
        constraint = DeadlineConstraint(deadline_at=10.0, reject_if_late=True)
        viable = filter_by_deadline([slow, fast], constraint)
        assert len(viable) == 1
        assert viable[0].node_id == "fast"

    def test_filter_keeps_all_when_not_rejecting(self):
        fast = DeadlineScoredNode("fast", 50, 80, 130, True, 100, 900)
        slow = DeadlineScoredNode("slow", 50, -50, 0, False, 2000, -1000)
        constraint = DeadlineConstraint(deadline_at=10.0, reject_if_late=False)
        viable = filter_by_deadline([slow, fast], constraint)
        assert len(viable) == 2

    def test_has_budget_for_retry(self):
        constraint = DeadlineConstraint(deadline_at=100.0)
        assert has_budget_for_retry(constraint, now=50.0) is True
        assert has_budget_for_retry(constraint, now=101.0) is False

    def test_remaining_budget_fraction(self):
        constraint = DeadlineConstraint(deadline_at=10.0)
        assert remaining_budget_fraction(constraint, started_at=0.0, now=5.0) == pytest.approx(0.5)
        assert remaining_budget_fraction(constraint, started_at=0.0, now=10.0) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 8. Backpressure Propagation
# ---------------------------------------------------------------------------

class TestBackpressureChain:

    def test_no_source_admits_all(self):
        chain = BackpressureChain()
        decision = chain.check("any-key")
        assert decision.admitted is True
        assert decision.reason == "no_pressure_source"

    def test_low_pressure_admits(self):
        chain = BackpressureChain(pressure_source=lambda: BAND_LOW)
        decision = chain.check("key-1")
        assert decision.admitted is True
        assert decision.band == BAND_LOW

    def test_critical_pressure_sheds(self):
        chain = BackpressureChain(pressure_source=lambda: BAND_CRITICAL)
        decision = chain.check("key-1")
        assert decision.admitted is False
        assert decision.reason == "critical_shed"

    def test_medium_pressure_probabilistic(self):
        chain = BackpressureChain(pressure_source=lambda: BAND_MEDIUM)
        admitted = sum(1 for i in range(1000) if chain.check(f"key-{i}").admitted)
        assert 500 < admitted < 900, f"expected ~70% admission, got {admitted / 10}%"

    def test_high_pressure_sheds_more(self):
        chain_med = BackpressureChain(pressure_source=lambda: BAND_MEDIUM)
        chain_high = BackpressureChain(pressure_source=lambda: BAND_HIGH)
        med_admitted = sum(1 for i in range(1000) if chain_med.check(f"key-{i}").admitted)
        high_admitted = sum(1 for i in range(1000) if chain_high.check(f"key-{i}").admitted)
        assert med_admitted > high_admitted

    def test_deterministic_for_same_key(self):
        chain = BackpressureChain(pressure_source=lambda: BAND_MEDIUM)
        first = chain.check("stable-key")
        chain2 = BackpressureChain(pressure_source=lambda: BAND_MEDIUM)
        second = chain2.check("stable-key")
        assert first.admitted == second.admitted

    def test_snapshot_tracks_counts(self):
        chain = BackpressureChain(pressure_source=lambda: BAND_LOW)
        chain.check("a")
        chain.check("b")
        snap = chain.snapshot()
        assert snap["admitted"] == 2
        assert snap["shed"] == 0
        assert snap["total"] == 2

    def test_shed_counter_increments(self):
        chain = BackpressureChain(pressure_source=lambda: BAND_CRITICAL)
        chain.check("x")
        assert chain.shed_count == 1
        assert chain.admitted_count == 0
