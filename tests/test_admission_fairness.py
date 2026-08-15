"""Per-owner admission fairness for the bounded HTTP concurrency gate.

Defect: ``RuntimeLifecycle.acquire_request_slot`` bounded only the *global*
slot count and queue depth.  A single authenticated principal could hold every
execution slot and every queue position, so all other accounts were rejected
with CAPACITY_EXHAUSTED (or timed out) while owning zero in-flight work --
total cross-account starvation with no per-owner bound anywhere.

The correction adds an opaque per-owner in-flight cap: an owner at its cap is
refused with an owner-scoped 429 *before* consuming a queue position, leaving
capacity for other principals.  Owner keys are in-memory only, never persisted
and never used as metric labels, and their accounting entries are removed at
zero so the map stays bounded by concurrently active owners.
"""

import threading
import time

import pytest

import sonder_lifecycle
from sonder_lifecycle import AdmissionRejected, RuntimeLifecycle


def _lifecycle(**kwargs):
    kwargs.setdefault("max_concurrent_requests", 1)
    kwargs.setdefault("queue_depth", 4)
    kwargs.setdefault("admission_timeout_seconds", 0.2)
    return RuntimeLifecycle(**kwargs)


def test_owner_at_cap_is_refused_with_owner_scoped_429():
    lifecycle = _lifecycle(owner_max_inflight=1)
    slot = lifecycle.acquire_request_slot(mutating=False, owner="ao-alice")
    with slot:
        with pytest.raises(AdmissionRejected) as excinfo:
            with lifecycle.acquire_request_slot(mutating=False, owner="ao-alice"):
                pass
    rejection = excinfo.value
    assert rejection.code == "OWNER_CAPACITY_EXHAUSTED"
    assert rejection.status == 429
    assert rejection.retryable is True


def test_saturating_owner_no_longer_starves_other_owners():
    """The demonstrated defect: owner A fills slot + queue, owner B starves.

    With queue_depth=1, owner A holds the slot and its second request would
    previously occupy the only queue position, so owner B's request was
    refused CAPACITY_EXHAUSTED despite having no in-flight work.  Now A's
    second request is refused at its owner cap without consuming the queue,
    and B admits as soon as A releases.
    """
    lifecycle = _lifecycle(queue_depth=1, owner_max_inflight=1,
                           admission_timeout_seconds=5.0)
    a_slot = lifecycle.acquire_request_slot(mutating=False, owner="ao-alice")
    a_slot.__enter__()
    try:
        # Owner A's second request must not take the queue position.
        with pytest.raises(AdmissionRejected) as excinfo:
            with lifecycle.acquire_request_slot(mutating=False, owner="ao-alice"):
                pass
        assert excinfo.value.code == "OWNER_CAPACITY_EXHAUSTED"

        b_admitted = threading.Event()
        b_error = []

        def b_request():
            try:
                with lifecycle.acquire_request_slot(mutating=False, owner="ao-bob"):
                    b_admitted.set()
            except AdmissionRejected as error:
                b_error.append(error)

        thread = threading.Thread(target=b_request)
        thread.start()
        try:
            # B is queued (owner A holds the only slot), not rejected.
            assert not b_admitted.wait(0.05)
        finally:
            a_slot.__exit__(None, None, None)
            thread.join(timeout=5.0)
        assert not thread.is_alive()
        assert b_error == []
        assert b_admitted.is_set()
    except BaseException:
        a_slot.__exit__(None, None, None)
        raise


def test_empty_owner_is_exempt_from_the_owner_cap():
    """Local-open deployments have one operator; only global bounds apply."""
    lifecycle = _lifecycle(max_concurrent_requests=3, owner_max_inflight=1)
    first = lifecycle.acquire_request_slot(mutating=False, owner="")
    second = lifecycle.acquire_request_slot(mutating=False, owner="")
    with first, second:
        pass


def test_owner_accounting_is_released_and_bounded():
    lifecycle = _lifecycle(owner_max_inflight=1)
    with lifecycle.acquire_request_slot(mutating=False, owner="ao-alice"):
        assert lifecycle._owner_inflight.get("ao-alice") == 1
    # Entries are removed at zero: the map stays bounded by active owners.
    assert lifecycle._owner_inflight == {}
    # And the owner can admit again after release.
    with lifecycle.acquire_request_slot(mutating=False, owner="ao-alice"):
        pass
    assert lifecycle._owner_inflight == {}


def test_owner_accounting_is_released_on_admission_timeout():
    lifecycle = _lifecycle(max_concurrent_requests=1, owner_max_inflight=4,
                           admission_timeout_seconds=0.05)
    holder = lifecycle.acquire_request_slot(mutating=False, owner="ao-alice")
    with holder:
        with pytest.raises(AdmissionRejected) as excinfo:
            with lifecycle.acquire_request_slot(mutating=False, owner="ao-bob"):
                pass
        assert excinfo.value.code == "ADMISSION_TIMEOUT"
        assert "ao-bob" not in lifecycle._owner_inflight
    assert lifecycle._owner_inflight == {}


def test_default_owner_cap_leaves_room_for_other_owners():
    """One owner must never be able to occupy every slot and queue position."""
    lifecycle = RuntimeLifecycle(max_concurrent_requests=4, queue_depth=32)
    assert 1 <= lifecycle._owner_max_inflight < 4 + 32


def test_owner_cap_env_override(monkeypatch):
    monkeypatch.setenv("SONDER_OWNER_MAX_INFLIGHT", "2")
    lifecycle = RuntimeLifecycle(max_concurrent_requests=4, queue_depth=32)
    assert lifecycle._owner_max_inflight == 2


class TestOwnerCapClamp:
    """The effective owner cap must stay strictly below total capacity.

    Defect: ``owner_max_inflight`` / ``SONDER_OWNER_MAX_INFLIGHT`` was taken
    verbatim, so an override at or above total global capacity
    (``max_concurrent_requests + queue_depth``) let one owner consume every
    slot and queue position again -- silently negating the fairness bound the
    cap exists to provide.  Whenever total capacity is greater than one, the
    effective cap is clamped to ``total capacity - 1`` so at least one
    admission position always remains for a different owner.
    """

    def test_huge_constructor_override_is_clamped_below_total_capacity(self):
        lifecycle = RuntimeLifecycle(
            max_concurrent_requests=4, queue_depth=32, owner_max_inflight=10_000
        )
        assert lifecycle._owner_max_inflight == 35  # (4 + 32) - 1

    def test_huge_env_override_is_clamped_below_total_capacity(self, monkeypatch):
        monkeypatch.setenv("SONDER_OWNER_MAX_INFLIGHT", "1000000")
        lifecycle = RuntimeLifecycle(max_concurrent_requests=2, queue_depth=3)
        assert lifecycle._owner_max_inflight == 4  # (2 + 3) - 1

    def test_override_equal_to_total_capacity_is_clamped(self):
        # A cap exactly at total capacity still lets one owner take every
        # slot and queue position; it must be reduced by one.
        lifecycle = RuntimeLifecycle(
            max_concurrent_requests=1, queue_depth=4, owner_max_inflight=5
        )
        assert lifecycle._owner_max_inflight == 4

    def test_normal_override_below_capacity_is_preserved(self):
        lifecycle = RuntimeLifecycle(
            max_concurrent_requests=4, queue_depth=32, owner_max_inflight=3
        )
        assert lifecycle._owner_max_inflight == 3

    def test_default_cap_is_unchanged_by_the_clamp(self):
        lifecycle = RuntimeLifecycle(max_concurrent_requests=4, queue_depth=32)
        assert lifecycle._owner_max_inflight == (4 + 32) // 4

    def test_minimum_capacity_deployment_keeps_a_cap_of_one(self):
        # Smallest reachable configuration (both bounds floor at 1): the
        # clamp cannot push the cap below one admission.
        lifecycle = RuntimeLifecycle(
            max_concurrent_requests=1, queue_depth=1, owner_max_inflight=99
        )
        assert lifecycle._owner_max_inflight == 1

    def test_clamped_cap_leaves_an_admission_position_for_another_owner(self):
        """Behavioral: under a huge override, a saturating owner is stopped
        one position short of total capacity, so another owner still admits.
        """
        lifecycle = RuntimeLifecycle(
            max_concurrent_requests=1,
            queue_depth=2,
            owner_max_inflight=999,
            admission_timeout_seconds=5.0,
        )
        assert lifecycle._owner_max_inflight == 2
        errors = []
        admitted = {"a2": threading.Event(), "b": threading.Event()}

        def request(owner, key):
            try:
                with lifecycle.acquire_request_slot(mutating=False, owner=owner):
                    admitted[key].set()
            except AdmissionRejected as error:
                errors.append((key, error.code))

        a_slot = lifecycle.acquire_request_slot(mutating=False, owner="ao-alice")
        a_slot.__enter__()
        a2 = threading.Thread(target=request, args=("ao-alice", "a2"))
        b = threading.Thread(target=request, args=("ao-bob", "b"))
        a2.start()
        try:
            # Wait for A's second request to be queued and counted.
            deadline = time.monotonic() + 2.0
            while (
                lifecycle._owner_inflight.get("ao-alice") != 2
                and time.monotonic() < deadline
            ):
                time.sleep(0.005)
            assert lifecycle._owner_inflight.get("ao-alice") == 2

            # A is at its clamped cap: the third request is refused
            # owner-scoped instead of taking the last queue position.
            with pytest.raises(AdmissionRejected) as excinfo:
                with lifecycle.acquire_request_slot(
                    mutating=False, owner="ao-alice"
                ):
                    pass
            assert excinfo.value.code == "OWNER_CAPACITY_EXHAUSTED"

            # The reserved position lets owner B queue instead of being
            # refused CAPACITY_EXHAUSTED.
            b.start()
            assert not admitted["b"].wait(0.05)
        finally:
            a_slot.__exit__(None, None, None)
            a2.join(timeout=5.0)
            if b.ident is not None:
                b.join(timeout=5.0)
        assert not a2.is_alive() and not b.is_alive()
        assert errors == []
        assert admitted["a2"].is_set() and admitted["b"].is_set()
        assert lifecycle._owner_inflight == {}


def test_owner_capacity_envelope_is_a_rate_limit_error():
    envelope = sonder_lifecycle.error_envelope(
        "OWNER_CAPACITY_EXHAUSTED", "owner over cap", "req_test", retryable=True
    )
    assert envelope["error"]["type"] == "rate_limit_error"
    assert envelope["error"]["retryable"] is True


class TestServeWiring:
    """The chat endpoint must derive and pass the opaque admission owner."""

    def test_local_open_deployment_has_no_admission_owner(self, monkeypatch):
        import sonder_serve as ts

        for name in ("SONDER_AUTH_MODE", "SONDER_API_KEY", "SONDER_REQUIRE_ACCOUNT"):
            monkeypatch.delenv(name, raising=False)
        assert ts._admission_request_owner({}) == ""

    def test_authenticated_owner_is_an_opaque_stable_digest(self, monkeypatch):
        import sonder_serve as ts

        monkeypatch.setenv("SONDER_AUTH_MODE", "account")
        alice = {"account": {"username": "alice", "id": "a1"}}
        bob = {"account": {"username": "bob", "id": "b1"}}
        owner_alice = ts._admission_request_owner(alice)
        owner_bob = ts._admission_request_owner(bob)
        assert owner_alice and owner_bob
        assert owner_alice != owner_bob
        assert owner_alice == ts._admission_request_owner(alice)
        # Opaque: the client-controlled username never appears in the key.
        assert "alice" not in owner_alice

    def test_chat_admission_passes_the_owner(self):
        import pathlib

        import sonder_serve

        source = pathlib.Path(sonder_serve.__file__).read_text(encoding="utf-8")
        assert (
            "acquire_request_slot(\n"
            "                mutating=True, owner=_admission_request_owner(context)\n"
            "            )" in source
            or "acquire_request_slot(mutating=True, owner=_admission_request_owner(context))"
            in source
        ), "the chat endpoint must admit under the caller's opaque owner key"
