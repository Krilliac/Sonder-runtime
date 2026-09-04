"""Consistent hashing ring with virtual nodes.

Maps keys to nodes with minimal disruption when nodes join or leave.
Each physical node gets ``replicas`` virtual positions on the ring,
distributing load and reducing hotspots.  Adding or removing a node
only reassigns keys in the affected arc, not the entire keyspace.

Also provides rendezvous (highest random weight) hashing as an
alternative: for each key, every node gets a deterministic score and
the highest wins.  Rendezvous is simpler (no ring structure) and
handles heterogeneous weights naturally.

No I/O, no threading -- callers own synchronization.
"""
from __future__ import annotations

import hashlib
import struct
from bisect import bisect_right, insort


def _ring_hash(key: str) -> int:
    return struct.unpack(">I", hashlib.md5(key.encode("utf-8")).digest()[:4])[0]


def _rendezvous_score(node: str, key: str) -> float:
    h = hashlib.sha256(f"{node}:{key}".encode("utf-8")).digest()
    return struct.unpack(">Q", h[:8])[0]


class HashRing:
    """Consistent hashing ring with configurable virtual nodes."""

    def __init__(self, replicas: int = 150) -> None:
        if replicas < 1:
            raise ValueError("replicas must be at least 1")
        self._replicas = replicas
        self._ring: list[int] = []
        self._ring_to_node: dict[int, str] = {}
        self._nodes: set[str] = set()

    def add_node(self, node: str) -> None:
        if node in self._nodes:
            return
        self._nodes.add(node)
        for i in range(self._replicas):
            h = _ring_hash(f"{node}:{i}")
            self._ring_to_node[h] = node
            insort(self._ring, h)

    def remove_node(self, node: str) -> None:
        if node not in self._nodes:
            return
        self._nodes.discard(node)
        for i in range(self._replicas):
            h = _ring_hash(f"{node}:{i}")
            self._ring_to_node.pop(h, None)
            try:
                self._ring.remove(h)
            except ValueError:
                pass

    def get_node(self, key: str) -> str | None:
        if not self._ring:
            return None
        h = _ring_hash(key)
        idx = bisect_right(self._ring, h)
        if idx >= len(self._ring):
            idx = 0
        return self._ring_to_node[self._ring[idx]]

    def get_nodes(self, key: str, count: int = 1) -> list[str]:
        if not self._ring or count < 1:
            return []
        h = _ring_hash(key)
        idx = bisect_right(self._ring, h)
        result: list[str] = []
        seen: set[str] = set()
        for _ in range(len(self._ring)):
            if idx >= len(self._ring):
                idx = 0
            node = self._ring_to_node[self._ring[idx]]
            if node not in seen:
                seen.add(node)
                result.append(node)
                if len(result) >= count:
                    break
            idx += 1
        return result

    @property
    def nodes(self) -> frozenset[str]:
        return frozenset(self._nodes)

    @property
    def size(self) -> int:
        return len(self._nodes)


class RendezvousHash:
    """Rendezvous (HRW) hashing for weighted node selection."""

    def __init__(self) -> None:
        self._nodes: dict[str, float] = {}

    def add_node(self, node: str, weight: float = 1.0) -> None:
        if weight <= 0:
            raise ValueError("weight must be positive")
        self._nodes[node] = weight

    def remove_node(self, node: str) -> None:
        self._nodes.pop(node, None)

    def get_node(self, key: str) -> str | None:
        if not self._nodes:
            return None
        return max(
            self._nodes,
            key=lambda n: _rendezvous_score(n, key) * self._nodes[n],
        )

    def get_nodes(self, key: str, count: int = 1) -> list[str]:
        if not self._nodes or count < 1:
            return []
        scored = sorted(
            self._nodes,
            key=lambda n: _rendezvous_score(n, key) * self._nodes[n],
            reverse=True,
        )
        return scored[:count]

    @property
    def nodes(self) -> frozenset[str]:
        return frozenset(self._nodes)

    @property
    def size(self) -> int:
        return len(self._nodes)
