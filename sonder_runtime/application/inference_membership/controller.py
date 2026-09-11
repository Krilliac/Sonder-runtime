"""One explicitly started runtime refresh loop, with no queued refresh backlog."""
import logging
import math
from threading import Condition
from time import monotonic

from ..ports.runtime_threads import Thread
from ..ports.inference_membership import MembershipSourceLimits
from ...domain.inference_membership import MembershipSnapshot, reconcile_membership

logger = logging.getLogger(__name__)


def _timeout(value, ceiling):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= ceiling:
        raise ValueError("timeout is outside its finite configured bound")
    return float(value)


class MembershipController:
    """Configured authority; the pool additionally enforces its endpoint policy.

    Construction performs no source/probe I/O. start() opts into the periodic
    loop; refresh() explicitly starts that same thread if needed. At most one
    refresh is pending/running. Timed-out work retains its thread until actual
    completion; close() reports incomplete cleanup instead of starting another.
    """

    def __init__(self, source, pool, *, clock, cluster_id, issuer_id,
                 refresh_interval_seconds=30, high_water_store=None, source_limits=None):
        self._interval = _timeout(refresh_interval_seconds, 86400)
        if self._interval < 1:
            raise ValueError("membership refresh interval must be at least one second")
        # Validate the injected aware clock and primitive authority before the
        # pool's admission state changes. This pure check contacts no source.
        reconcile_membership(None, cluster_id=cluster_id, issuer_id=issuer_id, clock=clock)
        self._source, self._pool, self._clock = source, pool, clock
        self._high_water_store = high_water_store
        self._source_limits = source_limits or MembershipSourceLimits(max_advertisements=pool.membership_limit)
        if type(self._source_limits) is not MembershipSourceLimits:
            raise ValueError("exact bounded membership source limits required")
        self._cluster, self._issuer = cluster_id, issuer_id
        self._condition = Condition()
        self._thread = None
        self._closed = self._running = False
        self._pending = None
        self._ticket = self._completed = 0
        self._result = None
        self._failure = None
        pool.configure_membership(cluster_id=cluster_id, issuer_id=issuer_id, clock=clock)

    def start(self):
        with self._condition:
            if self._closed:
                raise RuntimeError("membership controller is closed")
            if self._thread is None:
                self._thread = Thread(target=self._run, name="sonder-inference-membership", daemon=True)
                self._thread.start()

    def refresh(self, *, timeout_seconds=30, probe=True):
        timeout = _timeout(timeout_seconds, 30)
        if type(probe) is not bool:
            raise ValueError("probe must be a boolean")
        deadline = monotonic() + timeout
        self.start()
        with self._condition:
            if self._closed:
                raise RuntimeError("membership controller is closed")
            if self._running or self._pending is not None:
                raise RuntimeError("membership refresh is already running")
            self._ticket += 1
            ticket = self._ticket
            self._pending = (ticket, timeout, probe)
            self._condition.notify_all()
            while self._completed < ticket:
                if self._closed:
                    raise RuntimeError("membership controller is closed")
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError("membership refresh has not completed")
                self._condition.wait(remaining)
            if self._failure is not None:
                raise RuntimeError("membership refresh failed") from self._failure
            return self._result

    def _run(self):
        next_refresh = monotonic() + self._interval
        while True:
            with self._condition:
                while not self._closed and self._pending is None and monotonic() < next_refresh:
                    self._condition.wait(next_refresh - monotonic())
                if self._closed:
                    return
                ticket, timeout, probe = self._pending or (self._ticket, 30, True)
                self._pending = None
                self._running = True
            failure = None
            try:
                self._refresh_once(timeout, probe)
            except Exception as error:
                failure = error
                logger.warning("membership refresh failed: %s", type(error).__name__)
            with self._condition:
                self._failure = failure
                self._completed = ticket
                self._running = False
                next_refresh = monotonic() + self._interval
                self._condition.notify_all()

    def _refresh_once(self, timeout, probe):
        previous = self._result.roster if self._result is not None else None
        water = self._result.high_water if self._result is not None else None
        if self._high_water_store is not None:
            # State/source/probe I/O occurs outside both the pool condition and
            # publication lock. A persistence failure never mutates the roster.
            water = self._high_water_store.read()
        options = dict(cluster_id=self._cluster, issuer_id=self._issuer, clock=self._clock,
                       previous=previous, high_water=water, max_workers=self._pool.membership_limit)
        try:
            candidate = self._source.read_snapshot(limits=MembershipSourceLimits(
                max_advertisements=self._source_limits.max_advertisements,
                max_bytes=self._source_limits.max_bytes,
                timeout_seconds=min(timeout, self._source_limits.timeout_seconds)))
            if type(candidate) is not MembershipSnapshot:
                raise ValueError("source returned an unverified snapshot")
            self._pool.validate_membership_snapshot(candidate)
            result = reconcile_membership(candidate, **options)
        except Exception as error:
            logger.warning("membership source rejected: %s", type(error).__name__)
            result = reconcile_membership(None, **options)
            candidate = None
        if self._high_water_store is not None and candidate is not None:
            self._high_water_store.compare_and_advance(candidate)
        with self._condition:
            if self._closed:
                return
            self._pool.apply_membership(result)
            self._result = result
        if probe and result.roster is not None:
            self._pool.refresh_membership_capabilities()
            evidence = self._pool.membership_evidence(result.roster)
            result = reconcile_membership(None, **(options | dict(
                previous=result.roster, high_water=result.high_water, capability_evidence=evidence)))
            with self._condition:
                if not self._closed:
                    self._pool.apply_membership(result)
                    self._result = result

    def close(self, timeout=5):
        timeout = 5 if timeout is None else timeout
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 <= timeout <= 30:
            raise ValueError("close timeout must be within 0..30 seconds")
        deadline = monotonic() + timeout
        with self._condition:
            self._closed = True
            self._pending = None
            self._condition.notify_all()
            thread = self._thread
        self._pool.stop_membership()
        if thread is not None:
            thread.join(max(0, deadline - monotonic()))
        stopped = thread is None or not thread.is_alive()
        if self._high_water_store is not None:
            stopped = self._source.close(timeout=max(0, deadline - monotonic())) and stopped
        return stopped
