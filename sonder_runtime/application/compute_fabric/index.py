"""Derived compute postings and bounded aggregate expiry; registry owns locking."""
from collections import Counter, defaultdict
from dataclasses import dataclass
import heapq
from uuid import uuid4

from ...domain.compute_fabric import NodeHealth, PlacementInventoryScope


@dataclass(frozen=True)
class IndexedCandidates:
    snapshots: tuple
    digests: dict
    scope: PlacementInventoryScope


class ComputeIndex:
    def __init__(self, nodes, ttl):
        self.generation = uuid4().hex
        self.revision = 0
        self.nodes = {node.node_id: node for node in nodes}
        self.ids = tuple(sorted(self.nodes))
        self.static = defaultdict(set)
        self.live = defaultdict(set)
        self.observations = {}
        self.digests = {}
        self.versions = {}
        self.ttl = ttl
        self.expiry = []
        self.stale = set()
        self.fresh_health = Counter()
        self.active_jobs = 0
        self.last_summary_at = None
        for node in nodes:
            for key in self._static_keys(node):
                self.static[key].add(node.node_id)

    @staticmethod
    def _static_keys(node):
        return (("local", node.local),
                *(("workload", value) for value in node.allowed_workloads),
                *(("cap", value) for value in node.configured_capabilities),
                *(("workspace", value) for value in node.workspace_mappings))

    @staticmethod
    def _live_keys(snapshot):
        return (("local", snapshot.node.local),
                *(("workload", value) for value in snapshot.effective_workloads),
                *(("cap", value) for value in snapshot.effective_capabilities),
                *(("model", value) for value in snapshot.models))

    def update(self, snapshot):
        digest = snapshot.digest()
        identity = snapshot.node.node_id
        prior = self.observations.get(identity)
        if prior is not None:
            for key in self._live_keys(prior):
                posting = self.live[key]
                posting.discard(identity)
                if not posting:
                    del self.live[key]
            self.active_jobs -= prior.active_jobs
            if identity not in self.stale:
                self.fresh_health[prior.health] -= 1
        self.stale.discard(identity)
        self.revision += 1
        self.observations[identity] = snapshot
        self.digests[identity] = digest
        self.versions[identity] = self.revision
        self.active_jobs += snapshot.active_jobs
        self.fresh_health[snapshot.health] += 1
        for key in self._live_keys(snapshot):
            self.live[key].add(identity)
        heapq.heappush(self.expiry, (snapshot.freshness_at + self.ttl, self.revision, identity))
        if len(self.expiry) > max(1, 2 * len(self.nodes)):
            self._rebuild_expiry()

    def _rebuild_expiry(self):
        self.expiry = [(item.freshness_at + self.ttl, self.versions[identity], identity)
                       for identity, item in self.observations.items() if identity not in self.stale]
        heapq.heapify(self.expiry)

    def summary(self, now):
        if self.last_summary_at is not None and now < self.last_summary_at:
            self.stale.clear()
            self.fresh_health = Counter(item.health for item in self.observations.values())
            self._rebuild_expiry()
        self.last_summary_at = now
        while self.expiry and self.expiry[0][0] < now:
            _, revision, identity = heapq.heappop(self.expiry)
            if self.versions.get(identity) == revision and identity not in self.stale:
                self.stale.add(identity)
                self.fresh_health[self.observations[identity].health] -= 1
        return dict(configured=len(self.nodes), live=len(self.observations) - len(self.stale),
                    healthy=self.fresh_health[NodeHealth.HEALTHY],
                    unhealthy=self.fresh_health[NodeHealth.UNHEALTHY],
                    stale=len(self.stale), active_jobs=self.active_jobs)

    def matching(self, request, *, observed, local=None):
        postings = self.live if observed else self.static
        sets = [postings.get(("workload", request.kind), set())]
        if local is not None:
            sets.append(postings.get(("local", local), set()))
        sets.extend(postings.get(("cap", value), set()) for value in request.required_capabilities)
        groups = ((request.any_capabilities,) if request.any_capabilities else ()) + request.any_capability_groups
        for group in groups:
            sets.append(set().union(*(postings.get(("cap", value), set()) for value in group)))
        if request.workspace_mapping:
            sets.append(self.static.get(("workspace", request.workspace_mapping), set()))
        if observed and request.required_model:
            sets.append(self.live.get(("model", request.required_model), set()))
        smallest, *others = sorted(sets, key=len)
        result = set(smallest)
        for posting in others:
            result.intersection_update(posting)
        result.difference_update(request.avoided_node_ids)
        return result

    def candidates(self, request, *, local=None):
        ids = sorted(self.matching(request, observed=True, local=local))
        configured_count = len(self.nodes) if local is None else len(self.static.get(("local", local), ()))
        observed_count = len(self.observations) if local is None else len(self.live.get(("local", local), ()))
        return IndexedCandidates(tuple(self.observations[identity] for identity in ids),
            {identity: self.digests[identity] for identity in ids},
            PlacementInventoryScope("indexed_structural_candidates", self.generation, self.revision,
                                    configured_count, observed_count, len(ids), observed_count - len(ids)))
