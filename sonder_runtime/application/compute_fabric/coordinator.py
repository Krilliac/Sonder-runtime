"""Shared bounded probe admission and per-node single-flight refresh."""

from sonder_runtime.application.ports.runtime_threads import ThreadPoolExecutor as owned_runtime_pool
from concurrent.futures import Future, ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import timedelta
from threading import BoundedSemaphore, Condition

from ...domain.compute_fabric import NodeHealth, PlacementPolicy

# All composed coordinators share process admission; executors have no backlog
# beyond these admitted probes. This is not a fleet-wide or HTTP connection cap.
_PROBE_SLOTS = BoundedSemaphore(8)


class ComputeRefreshCoordinator:
    def __init__(self, registry, source, *, now, refresh_after):
        if not timedelta(0) < refresh_after <= registry.snapshot_ttl:
            raise ValueError("refresh interval must be within snapshot TTL")
        self.registry, self.source, self.now = registry, source, now
        self.refresh_after = refresh_after
        self._condition = Condition()
        self._inflight = {}
        self._executor = owned_runtime_pool(max_workers=8, thread_name_prefix="sonder-compute-probe")
        self._closed = False
        self._peak = 0

    def _admit(self, node, request_revision, force):
        with self._condition:
            if self._closed:
                raise RuntimeError("compute refresh coordinator is closed")
            existing = self._inflight.get(node.node_id)
            if existing is not None:
                return existing, False
            snapshot, revision = self.registry.observation_version(node.node_id)
            if revision > request_revision:
                return None, False
            if force:
                due = True
            else:
                due = (snapshot is None or snapshot.health is not NodeHealth.HEALTHY
                       or not timedelta(0) <= self.now() - snapshot.freshness_at < self.refresh_after)
            if not due:
                return None, False
            if not _PROBE_SLOTS.acquire(blocking=False):
                return None, True
            result = Future()
            self._inflight[node.node_id] = result
            self._peak = max(self._peak, len(self._inflight))
            try:
                self._executor.submit(self._probe, node, result)
            except BaseException:
                self._inflight.pop(node.node_id)
                _PROBE_SLOTS.release()
                raise
            return result, False

    def _probe(self, node, result):
        failure = None
        try:
            try:
                snapshot = self.source.snapshot(node, now=self.now())
            except Exception as error:
                self.registry.mark_probe_failed(node.node_id, received_at=self.now(),
                    evidence_ref="probe-failed:" + type(error).__name__)
            else:
                self.registry.observe(snapshot, received_at=self.now())
        except BaseException as error:
            failure = error
        finally:
            with self._condition:
                self._inflight.pop(node.node_id, None)
                if failure is None:
                    result.set_result(node.node_id)
                else:
                    result.set_exception(failure)
                _PROBE_SLOTS.release()
                self._condition.notify_all()

    def refresh(self, request=None, *, force=False):
        if type(force) is not bool:
            raise ValueError("force must be a boolean")
        if request is not None and (request.local_only or not request.allow_remote
                                    or request.placement_policy is PlacementPolicy.LOCAL_ONLY):
            return
        request_revision = self.registry.observation_revision
        # Static authority only: model/live capability absence must not prevent
        # a configured-possible host from acquiring new evidence.
        nodes = (self.registry.configured_candidates(request, local=False) if request is not None
                 else tuple(node for node in self.registry.configured_nodes() if not node.local))
        self._refresh_nodes(nodes, request_revision, force)

    def _refresh_nodes(self, nodes, request_revision, force):
        probed = set()
        for offset in range(0, len(nodes), 32):
            page = nodes[offset:offset + 32]
            cursor, pending = 0, set()
            while cursor < len(page) or pending:
                while cursor < len(page):
                    future, blocked = self._admit(page[cursor], request_revision, force)
                    if blocked:
                        break
                    cursor += 1
                    if future is not None:
                        pending.add(future)
                    if len(pending) == 8:
                        break
                if pending:
                    completed, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for future in completed:
                        probed.add(future.result())
                elif cursor < len(page):
                    with self._condition:
                        if self._closed:
                            raise RuntimeError("compute refresh coordinator is closed")
                        # Other coordinator instances share process slots.
                        self._condition.wait(timeout=.05)
        return len(probed)

    def refresh_page(self, *, limit=32, cursor=None):
        revision = self.registry.observation_revision
        page = self.registry.inventory_page(now=self.now(), limit=limit, cursor=cursor)
        nodes = tuple(self.registry.get_node(row["node_id"]) for row in page["nodes"] if not row["local"])
        probed = self._refresh_nodes(nodes, revision, True)
        result = self.registry.inventory_page(now=self.now(), limit=limit, cursor=cursor)
        result.update(probed_count=probed, selected_remote_count=len(nodes),
                      refresh_scope="page_only", partial_inventory=(cursor is not None or result["has_more"]))
        return result

    def state(self):
        with self._condition:
            return {"submitted": len(self._inflight), "peak_submitted": self._peak, "closed": self._closed}

    def close(self, timeout=None):
        with self._condition:
            self._closed = True
            pending = tuple(self._inflight.values())
            self._condition.notify_all()
        if timeout is None:
            self._executor.shutdown(wait=True)
            return
        _, unfinished = wait(pending, timeout=max(0, timeout))
        self._executor.shutdown(wait=False)
        if unfinished:
            raise TimeoutError("compute probes have not finished socket cleanup")
