"""Read-only operator projections over durable jobs and child sessions."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class LineageNode:
    node_id: str
    parent_id: str | None
    root_id: str
    kind: str
    status: str
    depth: int
    operation_id: str | None = None
    parent_session_id: str | None = None
    # The durable source revision lets a caller distinguish a repeated read
    # from a node that changed while concurrent work was in flight.
    revision: int = 0


class JobLineageSource(Protocol):
    def list(self, *, include_terminal: bool = True, limit: int = 100) -> tuple[Any, ...]: ...

    def all(self, *, limit: int = 1000) -> tuple[Any, ...]: ...


class ChildLineageSource(Protocol):
    def list_all(self, *, limit: int = 1000) -> tuple[Any, ...]: ...


class DurableLineageQuery:
    """Bounded, non-mutating parent/child and operator query service.

    The service deliberately accepts durable adapters rather than owning a
    database.  It never returns prompts, output, credentials, or arbitrary
    metadata; this keeps operator discovery safe to expose to read-only UIs.
    """

    def __init__(self, jobs: JobLineageSource | None = None, children: ChildLineageSource | None = None) -> None:
        self._jobs, self._children = jobs, children

    def snapshot(self, *, limit: int = 1000) -> tuple[LineageNode, ...]:
        if isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be positive")
        nodes: list[LineageNode] = []
        if self._jobs is not None:
            loader = getattr(self._jobs, "all", None)
            values = loader(limit=limit) if loader is not None else self._jobs.list(include_terminal=True, limit=limit)
            for view in values:
                record = getattr(view, "record", view)
                identity = record.identity
                parent = identity.parent_job_id or identity.parent_session_id
                nodes.append(LineageNode(identity.job_id, parent, identity.job_id, "job",
                                         record.status.value, 0, identity.operation_id,
                                         identity.parent_session_id, record.revision))
        if self._children is not None:
            for child in self._children.list_all(limit=limit):
                request = child.request
                nodes.append(LineageNode(request.child_id, request.parent_id, request.child_id,
                                         "subagent", child.status.value, 0,
                                         revision=child.revision))
        by_id = {node.node_id: node for node in nodes}
        resolved: list[LineageNode] = []
        for node in nodes:
            parent = node.parent_id
            seen: set[str] = set()
            depth = 0
            root = node.node_id
            while parent and parent in by_id and parent not in seen:
                seen.add(parent)
                depth += 1
                root = parent
                parent = by_id[parent].parent_id
            resolved.append(LineageNode(node.node_id, node.parent_id, root, node.kind,
                                        node.status, depth, node.operation_id,
                                        node.parent_session_id, node.revision))
        return tuple(sorted(resolved, key=lambda item: (item.depth, item.node_id))[:limit])

    def descendants(self, node_id: str, *, include_root: bool = False, max_depth: int = 32,
                    limit: int = 100) -> tuple[LineageNode, ...]:
        if not node_id.strip() or isinstance(max_depth, bool) or max_depth < 0:
            raise ValueError("node_id and non-negative max_depth are required")
        if isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be positive")
        nodes = self.snapshot(limit=max(limit, 1000))
        by_parent: dict[str | None, list[LineageNode]] = {}
        for node in nodes:
            by_parent.setdefault(node.parent_id, []).append(node)
        found: list[LineageNode] = []
        queue: list[tuple[str, int]] = [(node_id, 0)]
        if include_root:
            found.extend(node for node in nodes if node.node_id == node_id)
        while queue and len(found) < limit:
            parent, depth = queue.pop(0)
            if depth >= max_depth:
                continue
            for child in by_parent.get(parent, ()):
                found.append(child)
                queue.append((child.node_id, depth + 1))
                if len(found) >= limit:
                    break
        return tuple(found[:limit])

    def operator_query(self, *, root_id: str | None = None, status: str | None = None,
                       kind: str | None = None, limit: int = 100) -> tuple[LineageNode, ...]:
        if status is not None and not status.strip():
            raise ValueError("status cannot be blank")
        nodes = self.snapshot(limit=max(limit, 1000))
        if root_id is not None:
            nodes = tuple(node for node in nodes if node.root_id == root_id)
        if status is not None:
            nodes = tuple(node for node in nodes if node.status == status)
        if kind is not None:
            nodes = tuple(node for node in nodes if node.kind == kind)
        return nodes[:limit]


__all__ = ["ChildLineageSource", "DurableLineageQuery", "JobLineageSource", "LineageNode"]
