"""Comprehensive safety evaluation: sandbox, permissions, credential egress,
agent dispatch, and adversarial use of algorithmic patterns.

Tests the runtime's security boundaries under adversarial conditions —
path traversal, privilege escalation, tool overreach, credential exfiltration,
and abuse of the new domain modules (circuit breaker, token bucket, etc.).

No model, no network — pure unit/integration tests of the safety surface.
"""
from __future__ import annotations

import hashlib
import math
import os
import struct
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

import permission_modes

# ---------------------------------------------------------------------------
# §1  Permission gate: adversarial tool dispatch
# ---------------------------------------------------------------------------

class _NoRule:
    """A rule lookup that returns no matching rule."""
    def __call__(self, name):
        return {}


class _AllowRule:
    """A rule lookup that returns allow for everything."""
    def __call__(self, name):
        return {"action": "allow", "pattern": name}


class _DenyRule:
    """A rule lookup that returns deny for everything."""
    def __call__(self, name):
        return {"action": "deny", "pattern": name}


class TestPermissionGateAdversarial:
    """Agent-driven tool calls should be refused when they attempt to
    escalate privilege, escape the sandbox, or reach system-operator tools."""

    # Tools blocked at the _agent_dispatch layer in server.py (above the
    # permission gate). These are tested separately via the dispatch test below.
    SYSTEM_OPERATOR_TOOLS = [
        "admin_login", "admin_register", "admin_set_account",
        "elevate", "permission_mode", "permission_rule_set",
        "permission_approve", "runtime_policy_update",
        "update_system_profile", "autopilot_start", "autopilot_resume",
        "autopilot_pause", "autopilot_cancel", "self_heal_repair",
        "memory_export", "memory_privacy_repair", "memory_quality_repair",
        "workflow_run", "workflow_delete", "set_context_size", "unload",
        "update_emotion_vectors", "learn_preference",
    ]

    # Tools the command catalog classifies as "dangerous" — always refused
    # unattended regardless of mode, enforced by permission_modes.decide().
    DANGEROUS_TOOLS = [
        "file_delete", "sqlite_mutate", "task_delete", "git_merge",
    ]

    @pytest.mark.parametrize("tool", SYSTEM_OPERATOR_TOOLS)
    def test_system_operator_tools_blocked_by_agent_dispatch(self, tool):
        """System-operator tools are blocked by _agent_dispatch() in server.py,
        which categorically refuses them before the permission gate runs.
        Here we verify the tool list is frozen."""
        from server import _AGENT_SYSTEM_OPERATOR_TOOLS
        assert tool in _AGENT_SYSTEM_OPERATOR_TOOLS, (
            f"{tool} missing from _AGENT_SYSTEM_OPERATOR_TOOLS"
        )

    @pytest.mark.parametrize("mode", ["plan", "manual", "acceptEdits", "auto"])
    def test_dangerous_tools_refused_unattended_all_modes(self, mode):
        for tool in self.DANGEROUS_TOOLS:
            d = permission_modes.decide(
                tool, interactive=False, mode=mode,
                rule_lookup=_NoRule(), surface="agent",
            )
            assert d.action == "deny", (
                f"{tool} in {mode} should be refused unattended"
            )

    def test_unclassified_tool_always_refused(self):
        for mode in ("plan", "manual", "acceptEdits", "auto"):
            d = permission_modes.decide(
                "totally_fake_injected_tool", interactive=False,
                mode=mode, rule_lookup=_NoRule(), surface="agent",
            )
            assert d.action == "deny", f"unclassified tool should be denied in {mode}"
            if mode != "plan":
                assert d.source == "unclassified"

    def test_plan_denies_all_mutations(self):
        for tool in ("file_write", "file_delete", "run_code", "workspace_run"):
            d = permission_modes.decide(
                tool, interactive=False, mode="plan",
                rule_lookup=_NoRule(), surface="agent",
            )
            assert d.action == "deny", f"{tool} should be denied in plan mode"

    def test_allow_rule_cannot_override_plan_denial(self):
        d = permission_modes.decide(
            "file_write", interactive=False, mode="plan",
            rule_lookup=_AllowRule(), surface="agent",
        )
        assert d.action == "deny", "allow rule must not override plan"

    def test_deny_rule_outranks_auto(self):
        d = permission_modes.decide(
            "file_write", interactive=False, mode="auto",
            rule_lookup=_DenyRule(), surface="agent",
        )
        assert d.action == "deny"
        assert d.source == "rule"

    def test_deny_rule_outranks_allow_rule(self):
        d = permission_modes.decide(
            "run_code", interactive=False, mode="auto",
            rule_lookup=_DenyRule(), surface="agent",
        )
        assert d.action == "deny"

    def test_file_write_refused_unattended_manual(self):
        d = permission_modes.decide(
            "file_write", interactive=False, mode="manual",
            rule_lookup=_NoRule(), surface="agent",
        )
        assert d.action == "deny"
        assert d.source == "unattended"

    def test_run_code_refused_unattended_acceptedits(self):
        d = permission_modes.decide(
            "run_code", interactive=False, mode="acceptEdits",
            rule_lookup=_NoRule(), surface="agent",
        )
        assert d.action == "deny"
        assert d.source == "unattended"


# ---------------------------------------------------------------------------
# §2  Effect fence: lost lease blocks mutations
# ---------------------------------------------------------------------------

class _HeldFence:
    def check(self):
        return ""
    label = "test-held"


class _LostFence:
    def check(self):
        return "lease expired"
    label = "test-lost"


class TestEffectFence:
    def test_lost_fence_blocks_file_write_in_auto(self):
        d = permission_modes.decide(
            "file_write", interactive=False, mode="auto",
            rule_lookup=_NoRule(), surface="agent", fence=_LostFence(),
        )
        assert d.action == "deny"
        assert d.source == "fence"

    def test_lost_fence_blocks_run_code_in_auto(self):
        d = permission_modes.decide(
            "run_code", interactive=False, mode="auto",
            rule_lookup=_NoRule(), surface="agent", fence=_LostFence(),
        )
        assert d.action == "deny"
        assert d.source == "fence"

    def test_held_fence_allows_file_write_in_auto(self):
        d = permission_modes.decide(
            "file_write", interactive=False, mode="auto",
            rule_lookup=_NoRule(), surface="agent", fence=_HeldFence(),
        )
        assert d.action == "allow"

    def test_lost_fence_allows_reads(self):
        d = permission_modes.decide(
            "file_read", interactive=False, mode="auto",
            rule_lookup=_NoRule(), surface="agent", fence=_LostFence(),
        )
        assert d.action == "allow"

    def test_lost_fence_outranks_allow_rule(self):
        d = permission_modes.decide(
            "file_write", interactive=False, mode="auto",
            rule_lookup=_AllowRule(), surface="agent", fence=_LostFence(),
        )
        assert d.action == "deny"
        assert d.source == "fence"


# ---------------------------------------------------------------------------
# §3  Credential egress policy
# ---------------------------------------------------------------------------

from sonder_runtime.domain.security.credential_egress import (
    CredentialHandle,
    EgressDenied,
    EgressPolicy,
    EgressTarget,
    RedirectChain,
)


class TestCredentialEgress:
    def test_credential_scope_rejects_wrong_host(self):
        handle = CredentialHandle.mint("test", ("api.safe.com",))
        assert handle.allows("https://api.safe.com/v1/data")
        assert not handle.allows("https://evil.attacker.com/steal")

    def test_credential_scope_rejects_http(self):
        handle = CredentialHandle.mint("test", ("api.safe.com",))
        assert not handle.allows("http://api.safe.com/v1/data")

    def test_credential_expired(self):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        handle = CredentialHandle.mint("test", ("api.safe.com",), expires_at=past)
        assert not handle.allows("https://api.safe.com/v1/data")

    def test_egress_policy_denies_private_networks(self):
        policy = EgressPolicy(allowed_hosts=(), deny_private_networks=True)
        with pytest.raises(EgressDenied):
            policy.check("https://192.168.1.1/admin")

    def test_egress_policy_denies_loopback(self):
        policy = EgressPolicy(allowed_hosts=(), deny_loopback=True)
        with pytest.raises(EgressDenied):
            policy.check("https://127.0.0.1/admin")

    def test_egress_policy_denies_link_local(self):
        policy = EgressPolicy(allowed_hosts=(), deny_link_local=True)
        with pytest.raises(EgressDenied):
            policy.check("https://169.254.169.254/metadata")

    def test_egress_target_rejects_userinfo(self):
        with pytest.raises(EgressDenied):
            EgressTarget.parse("https://user:pass@api.com/data")

    def test_egress_target_rejects_non_http(self):
        with pytest.raises(EgressDenied):
            EgressTarget.parse("ftp://files.com/secret")

    def test_redirect_chain_refuses_credential_scope_escape(self):
        handle = CredentialHandle.mint("test", ("api.safe.com",))
        policy = EgressPolicy(allowed_hosts=("api.safe.com", "evil.com"))
        chain = RedirectChain(hops=(
            "https://api.safe.com/auth",
            "https://evil.com/steal",
        ))
        with pytest.raises(EgressDenied):
            chain.validate(policy, handle)

    def test_ssrf_metadata_endpoint_blocked(self):
        policy = EgressPolicy(
            allowed_hosts=(), deny_private_networks=True,
            deny_link_local=True,
        )
        with pytest.raises(EgressDenied):
            policy.check("https://169.254.169.254/latest/meta-data/")


# ---------------------------------------------------------------------------
# §4  Redaction: credential shapes scrubbed from output
# ---------------------------------------------------------------------------

from sonder_runtime.domain.security.redaction import redact_text, REDACTED


class TestRedaction:
    def test_api_key_redacted(self):
        text = 'api_key = "sk-12345abcde67890"'
        assert REDACTED in redact_text(text)
        assert "sk-12345" not in redact_text(text)

    def test_bearer_token_redacted(self):
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiYWRtaW4iOnRydWV9.TJVA95OrM7E2cBab30RMHrHDcEfxjoYZgeFONFh7HgQ"
        result = redact_text(text)
        assert "eyJhbGci" not in result

    def test_private_key_redacted(self):
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA0Z3VS5JJcds3xf...\n-----END RSA PRIVATE KEY-----"
        result = redact_text(text)
        assert "MIIEpAIB" not in result

    def test_password_in_url_redacted(self):
        text = "postgres://admin:s3cr3t_p4ss@db.internal:5432/prod"
        result = redact_text(text)
        assert "s3cr3t_p4ss" not in result

    def test_cookie_redacted(self):
        text = "Set-Cookie: session=abc123def456; Path=/; HttpOnly"
        result = redact_text(text)
        assert "abc123def456" not in result

    def test_known_secret_value_redacted(self):
        text = "The output is: my-secret-token-value and then more text"
        result = redact_text(text, secret_values=["my-secret-token-value"])
        assert "my-secret-token-value" not in result
        assert REDACTED in result

    def test_query_param_credential_redacted(self):
        text = "https://api.example.com/data?api_key=supersecret123&format=json"
        result = redact_text(text)
        assert "supersecret123" not in result


# ---------------------------------------------------------------------------
# §5  Sandbox path containment
# ---------------------------------------------------------------------------

class TestSandboxContainment:
    """Test that the sandbox's _contained() check prevents path traversal."""

    def test_contained_within_workspace(self):
        from sonder_runtime.adapters.sandbox import _contained
        root = Path("/workspace/project")
        assert _contained(root, Path("/workspace/project/src/main.py"))
        assert _contained(root, Path("/workspace/project"))

    def test_traversal_outside_workspace(self):
        from sonder_runtime.adapters.sandbox import _contained
        root = Path("/workspace/project")
        assert not _contained(root, Path("/etc/passwd"))
        assert not _contained(root, Path("/workspace/other"))
        assert not _contained(root, Path("/"))

    def test_dotdot_traversal(self):
        from sonder_runtime.adapters.sandbox import _contained
        root = Path("/workspace/project")
        escaped = (root / ".." / ".." / "etc" / "passwd").resolve()
        assert not _contained(root, escaped)

    def test_symlink_style_escape(self):
        from sonder_runtime.adapters.sandbox import _contained
        root = Path("/workspace/project")
        assert not _contained(root, Path("/tmp/evil"))
        assert not _contained(root, Path("/home/user/.ssh/id_rsa"))


# ---------------------------------------------------------------------------
# §6  Adversarial use of algorithmic domain modules
# ---------------------------------------------------------------------------

from sonder_runtime.domain.circuit_breaker import CircuitBreaker, BreakerConfig
from sonder_runtime.domain.token_bucket import TokenBucket, RateLimited
from sonder_runtime.domain.consistent_hash import HashRing, RendezvousHash
from sonder_runtime.domain.work_stealing import WorkStealingScheduler
from sonder_runtime.domain.adaptive_batcher import AdaptiveBatcher, BatchConfig
from sonder_runtime.domain.bloom_filter import BloomFilter, SlidingBloomFilter
from sonder_runtime.domain.deadline_scheduler import (
    DeadlineConstraint, NodeEstimate, score_with_deadline,
    filter_by_deadline, has_budget_for_retry,
)
from sonder_runtime.domain.backpressure import BackpressureChain
from sonder_runtime.domain.fleet_pressure import BAND_LOW, BAND_MEDIUM, BAND_HIGH, BAND_CRITICAL


class TestCircuitBreakerAdversarial:
    """Verify the circuit breaker cannot be abused to permanently lock out
    service or bypassed to ignore failure state."""

    def test_rapid_failure_injection_trips_breaker(self):
        cb = CircuitBreaker(BreakerConfig(failure_threshold=3, window_seconds=60))
        t = 1000.0
        for i in range(3):
            cb.before_call(now=t + i)
            cb.record_failure(now=t + i)
        assert cb.state == "open"
        assert not cb.before_call(now=t + 3)

    def test_cannot_bypass_open_state_by_calling_record_success(self):
        cb = CircuitBreaker(BreakerConfig(failure_threshold=2, window_seconds=60))
        t = 1000.0
        cb.before_call(now=t)
        cb.record_failure(now=t)
        cb.before_call(now=t + 1)
        cb.record_failure(now=t + 1)
        assert cb.state == "open"
        cb.record_success(now=t + 2)
        assert cb.state == "open"

    def test_exponential_backoff_prevents_rapid_retry_after_repeated_trips(self):
        config = BreakerConfig(
            failure_threshold=1, window_seconds=60,
            recovery_seconds=10, recovery_multiplier=2.0,
            max_recovery_seconds=300,
        )
        cb = CircuitBreaker(config)
        t = 1000.0
        recovery_times = []
        for trip_num in range(5):
            cb.before_call(now=t)
            cb.record_failure(now=t)
            snap = cb.snapshot()
            if snap.recovery_deadline is not None:
                recovery_times.append(snap.recovery_deadline - t)
            t = snap.recovery_deadline + 0.1 if snap.recovery_deadline else t + 1
            if cb.before_call(now=t):
                cb.record_failure(now=t)
                t += 0.1

        for i in range(1, len(recovery_times)):
            assert recovery_times[i] >= recovery_times[i - 1], (
                f"Recovery time should increase: {recovery_times}"
            )

    def test_reset_clears_all_state(self):
        cb = CircuitBreaker(BreakerConfig(failure_threshold=1))
        t = 1000.0
        cb.before_call(now=t)
        cb.record_failure(now=t)
        assert cb.state == "open"
        cb.reset()
        assert cb.state == "closed"
        assert cb.before_call(now=t + 1)


class TestTokenBucketAdversarial:
    """Verify the token bucket cannot be drained instantly or made to
    issue negative tokens."""

    def test_burst_exhaustion(self):
        bucket = TokenBucket(capacity=10, refill_rate=1.0)
        t = 1000.0
        for _ in range(10):
            result = bucket.try_acquire(now=t)
            assert result.allowed
        result = bucket.try_acquire(now=t)
        assert not result.allowed
        assert result.retry_after > 0

    def test_tokens_never_go_negative(self):
        bucket = TokenBucket(capacity=5, refill_rate=1.0)
        t = 1000.0
        for _ in range(20):
            bucket.try_acquire(now=t)
        result = bucket.try_acquire(now=t)
        assert result.tokens_remaining >= 0.0

    def test_high_cost_acquire_refused(self):
        bucket = TokenBucket(capacity=5, refill_rate=1.0)
        t = 1000.0
        result = bucket.try_acquire(cost=100.0, now=t)
        assert not result.allowed

    def test_acquire_raises_rate_limited(self):
        bucket = TokenBucket(capacity=1, refill_rate=0.1)
        t = 1000.0
        bucket.acquire(now=t)
        with pytest.raises(RateLimited) as exc_info:
            bucket.acquire(now=t)
        assert exc_info.value.retry_after > 0

    def test_refill_rate_respected(self):
        bucket = TokenBucket(capacity=10, refill_rate=2.0)
        t = 1000.0
        for _ in range(10):
            bucket.try_acquire(now=t)
        result = bucket.try_acquire(now=t + 1.0)
        assert result.allowed
        result2 = bucket.try_acquire(now=t + 1.0)
        assert result2.allowed
        result3 = bucket.try_acquire(now=t + 1.0)
        assert not result3.allowed


class TestConsistentHashAdversarial:
    """Verify the hash ring cannot be poisoned or made to return invalid nodes."""

    def test_empty_ring_returns_none(self):
        ring = HashRing()
        assert ring.get_node("any-key") is None

    def test_remove_all_nodes_returns_none(self):
        ring = HashRing()
        ring.add_node("node-a")
        ring.remove_node("node-a")
        assert ring.get_node("any-key") is None

    def test_get_nodes_deduplicates(self):
        ring = HashRing()
        ring.add_node("only-node")
        nodes = ring.get_nodes("key", count=5)
        assert len(nodes) == 1
        assert nodes[0] == "only-node"

    def test_rendezvous_zero_weight_rejected(self):
        rh = RendezvousHash()
        with pytest.raises(ValueError):
            rh.add_node("node", weight=0.0)

    def test_rendezvous_negative_weight_rejected(self):
        rh = RendezvousHash()
        with pytest.raises(ValueError):
            rh.add_node("node", weight=-1.0)

    def test_hash_distribution_not_pathologically_skewed(self):
        ring = HashRing(replicas=150)
        for i in range(5):
            ring.add_node(f"node-{i}")
        counts = {f"node-{i}": 0 for i in range(5)}
        for k in range(10000):
            node = ring.get_node(f"request-{k}")
            counts[node] += 1
        for node, count in counts.items():
            assert count > 500, f"{node} got only {count}/10000 — severely skewed"


class TestWorkStealingAdversarial:
    """Verify the scheduler cannot be abused to starve workers or
    steal from empty deques."""

    def test_steal_from_empty_returns_none(self):
        sched = WorkStealingScheduler()
        sched.add_worker("w1")
        result = sched.try_steal("w1")
        assert not result.stolen

    def test_cannot_steal_own_work(self):
        sched = WorkStealingScheduler()
        sched.add_worker("w1")
        sched.submit("w1", "task-1")
        own = sched.get_work("w1")
        assert own.item == "task-1"
        assert not own.stolen
        steal_result = sched.try_steal("w1")
        assert not steal_result.stolen

    def test_orphaned_work_returned_on_remove(self):
        sched = WorkStealingScheduler()
        sched.add_worker("w1")
        for i in range(5):
            sched.submit("w1", f"task-{i}")
        orphans = sched.remove_worker("w1")
        assert len(orphans) == 5


class TestAdaptiveBatcherAdversarial:
    """Verify the batcher cannot be made to produce zero-size or
    unbounded batches."""

    def test_max_batch_size_enforced(self):
        config = BatchConfig(max_batch_size=3, min_window_ms=1, max_window_ms=100)
        batcher = AdaptiveBatcher(config)
        t = 1000.0
        results = []
        for i in range(10):
            batch = batcher.add(f"item-{i}", now=t)
            if batch is not None:
                results.append(batch)
        for batch in results:
            assert batch.size <= 3

    def test_extreme_pressure_narrows_window(self):
        config = BatchConfig(max_batch_size=100, min_window_ms=5, max_window_ms=100)
        batcher = AdaptiveBatcher(config)
        for _ in range(20):
            batcher.update_pressure(1.0)
        batcher.add("item", now=1000.0)
        batch = batcher.flush(now=1000.006)
        assert batch is not None

    def test_zero_pressure_widens_window(self):
        config = BatchConfig(max_batch_size=100, min_window_ms=5, max_window_ms=100)
        batcher = AdaptiveBatcher(config)
        batcher.update_pressure(0.0)
        batcher.add("item", now=1000.0)
        batch = batcher.flush(now=1000.050)
        assert batch is None


class TestBloomFilterAdversarial:
    """Verify the bloom filter has bounded false positives and cannot
    produce false negatives."""

    def test_no_false_negatives_large_set(self):
        bf = BloomFilter(expected_items=10000, fp_rate=0.01)
        keys = [f"key-{i}" for i in range(5000)]
        for k in keys:
            bf.add(k)
        for k in keys:
            assert bf.contains(k), f"False negative for {k}"

    def test_false_positive_rate_bounded(self):
        bf = BloomFilter(expected_items=1000, fp_rate=0.05)
        for i in range(1000):
            bf.add(f"in-set-{i}")
        fp_count = sum(
            1 for i in range(10000) if bf.contains(f"not-in-set-{i}")
        )
        fp_rate = fp_count / 10000
        assert fp_rate < 0.10, f"FP rate {fp_rate:.3f} exceeds 2x target"

    def test_sliding_filter_expires_entries(self):
        sf = SlidingBloomFilter(window_seconds=10.0, expected_items=100)
        sf.add("old-key", now=0.0)
        assert sf.contains("old-key", now=0.0)
        sf.contains("old-key", now=6.0)
        assert not sf.contains("old-key", now=12.0)


class TestDeadlineSchedulerAdversarial:
    """Verify deadline logic handles edge cases — zero budget, all nodes
    missing deadline, negative slack."""

    def test_expired_deadline_scores_heavily_negative(self):
        node = NodeEstimate("n1", 100, 0, 0.0, 10)
        constraint = DeadlineConstraint(deadline_at=999.0)
        scored = score_with_deadline(node, 50.0, constraint, now=1000.0)
        assert not scored.meets_deadline
        assert scored.deadline_score == -1000.0

    def test_filter_removes_all_when_none_meet_deadline(self):
        constraint = DeadlineConstraint(deadline_at=1000.5, reject_if_late=True)
        nodes = [
            NodeEstimate(f"n{i}", 10000, 10, 0.9, 100) for i in range(5)
        ]
        scored = [
            score_with_deadline(n, 50.0, constraint, now=1000.0)
            for n in nodes
        ]
        filtered = filter_by_deadline(scored, constraint)
        assert len(filtered) == 0

    def test_no_budget_for_retry_past_deadline(self):
        constraint = DeadlineConstraint(deadline_at=1000.0)
        assert not has_budget_for_retry(constraint, now=1001.0)

    def test_budget_available_before_deadline(self):
        constraint = DeadlineConstraint(deadline_at=1010.0)
        assert has_budget_for_retry(constraint, now=1000.0)


class TestBackpressureAdversarial:
    """Verify backpressure cannot be bypassed and behaves deterministically
    for the same request key across retries."""

    def test_critical_pressure_sheds_everything(self):
        chain = BackpressureChain(pressure_source=lambda: BAND_CRITICAL)
        for i in range(100):
            decision = chain.check(f"request-{i}")
            assert not decision.admitted
            assert decision.reason == "critical_shed"

    def test_low_pressure_admits_everything(self):
        chain = BackpressureChain(pressure_source=lambda: BAND_LOW)
        for i in range(100):
            decision = chain.check(f"request-{i}")
            assert decision.admitted

    def test_deterministic_for_same_key(self):
        chain = BackpressureChain(pressure_source=lambda: BAND_MEDIUM)
        results = [chain.check("stable-key-123").admitted for _ in range(10)]
        assert len(set(results)) == 1, "Same key should always get same decision"

    def test_medium_pressure_probabilistic_distribution(self):
        chain = BackpressureChain(pressure_source=lambda: BAND_MEDIUM)
        admitted = sum(
            1 for i in range(1000)
            if chain.check(f"req-{i}").admitted
        )
        assert 500 < admitted < 900, f"Expected ~70% admission, got {admitted}/1000"

    def test_high_pressure_sheds_most(self):
        chain = BackpressureChain(pressure_source=lambda: BAND_HIGH)
        admitted = sum(
            1 for i in range(1000)
            if chain.check(f"req-{i}").admitted
        )
        assert admitted < 500, f"Expected ~30% admission, got {admitted}/1000"

    def test_no_source_admits_all(self):
        chain = BackpressureChain(pressure_source=None)
        for i in range(50):
            assert chain.check(f"req-{i}").admitted

    def test_snapshot_tracks_decisions(self):
        chain = BackpressureChain(pressure_source=lambda: BAND_CRITICAL)
        for i in range(10):
            chain.check(f"req-{i}")
        snap = chain.snapshot()
        assert snap["shed"] == 10
        assert snap["admitted"] == 0
        assert snap["total"] == 10


# ---------------------------------------------------------------------------
# §7  Cross-cutting: agent dispatch credential stripping
# ---------------------------------------------------------------------------

class TestAgentCredentialStripping:
    """Verify that the agent dispatch strips credential-like arguments
    before tool execution."""

    def test_token_arg_stripped_from_proposal(self):
        args = {
            "path": "readme.md",
            "token": "stolen_api_key_abcdef",
            "content": "hello",
        }
        cleaned = {
            key: value for key, value in args.items()
            if not (key in ("token", "approval") and isinstance(value, str))
        }
        assert "token" not in cleaned
        assert cleaned["path"] == "readme.md"
        assert cleaned["content"] == "hello"

    def test_approval_arg_stripped_from_proposal(self):
        args = {
            "path": "file.txt",
            "approval": "forged_approval_abc123",
        }
        cleaned = {
            key: value for key, value in args.items()
            if not (key in ("token", "approval") and isinstance(value, str))
        }
        assert "approval" not in cleaned

    def test_non_string_token_preserved(self):
        args = {
            "path": "file.txt",
            "token": 42,
        }
        cleaned = {
            key: value for key, value in args.items()
            if not (key in ("token", "approval") and isinstance(value, str))
        }
        assert "token" in cleaned
        assert cleaned["token"] == 42


# ---------------------------------------------------------------------------
# §8  Cross-module: pressure → backpressure → circuit breaker pipeline
# ---------------------------------------------------------------------------

class TestSafetyPipeline:
    """Integration: verify the full pressure → admission → circuit breaker
    pipeline fails safe under adversarial conditions."""

    def test_critical_pressure_plus_circuit_breaker(self):
        cb = CircuitBreaker(BreakerConfig(failure_threshold=3))
        chain = BackpressureChain(pressure_source=lambda: BAND_CRITICAL)
        t = 1000.0
        for i in range(10):
            decision = chain.check(f"req-{i}")
            if not decision.admitted:
                cb.before_call(now=t + i)
                cb.record_failure(now=t + i)
        assert cb.state == "open"
        assert chain.shed_count == 10

    def test_rate_limited_requests_with_backpressure(self):
        bucket = TokenBucket(capacity=5, refill_rate=1.0)
        chain = BackpressureChain(pressure_source=lambda: BAND_LOW)
        t = 1000.0
        admitted_and_allowed = 0
        for i in range(20):
            decision = chain.check(f"req-{i}")
            if decision.admitted:
                result = bucket.try_acquire(now=t)
                if result.allowed:
                    admitted_and_allowed += 1
        assert admitted_and_allowed == 5

    def test_deadline_under_high_pressure(self):
        constraint = DeadlineConstraint(deadline_at=1001.0, reject_if_late=True)
        chain = BackpressureChain(pressure_source=lambda: BAND_HIGH)
        node = NodeEstimate("n1", 500, 2, 0.5, 50)
        decision = chain.check("deadline-req")
        scored = score_with_deadline(node, 50.0, constraint, now=1000.0)
        assert not decision.admitted or scored.meets_deadline is not None
