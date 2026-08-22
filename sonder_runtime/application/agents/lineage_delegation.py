"""WP5 agent integration contracts for lineage, delegation, and workflows.

This module is an application boundary only.  It does not start workers or
write to a workspace.  Callers must supply an explicit workspace grant and
persist the immutable records returned here through their own adapter.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
from typing import Iterable, Mapping
from pathlib import Path

from sonder_runtime.application.agents.presets import AgentPreset, builtin_presets
from sonder_runtime.domain.agents.roles import AgentRole, role_budget


MAX_ID_CHARS = 128
MAX_PROMPT_CHARS = 16_000
MAX_PATH_CHARS = 512
MAX_EVIDENCE_CHARS = 4_000


class IntegrationError(ValueError):
    """Raised when an integration envelope is unsafe or incomplete."""


class DelegationStatus(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


def _required(value: str, field_name: str, *, limit: int = MAX_ID_CHARS) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IntegrationError(f"{field_name} must be non-empty")
    value = value.strip()
    if len(value) > limit:
        raise IntegrationError(f"{field_name} exceeds its bound")
    return value


def _paths(values: Iterable[str], field_name: str) -> tuple[str, ...]:
    result = tuple(sorted({_required(value, field_name, limit=MAX_PATH_CHARS) for value in values}))
    return result


@dataclass(frozen=True)
class WorkspaceAssignment:
    """Explicit read/write grant attached to every delegated child."""

    read_roots: tuple[str, ...]
    write_roots: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        reads = _paths(self.read_roots, "read root")
        writes = _paths(self.write_roots, "write root")
        if not reads and writes:
            raise IntegrationError("write roots require at least one read root")
        read_paths = tuple(Path(root).resolve(strict=False) for root in reads)
        if not all(any(_path_inside(Path(root).resolve(strict=False), read) for read in read_paths) for root in writes):
            raise IntegrationError("every write root must also be an explicit read root")
        object.__setattr__(self, "read_roots", reads)
        object.__setattr__(self, "write_roots", writes)

    @property
    def read_only(self) -> bool:
        return not self.write_roots

    def permits_write(self, root: str) -> bool:
        return _required(root, "write root", limit=MAX_PATH_CHARS) in self.write_roots

    def guard(self) -> "WorkspaceGuard":
        """Return the path-boundary guard for this explicit assignment."""
        return WorkspaceGuard(self)

    def permits(self, path: str | Path, *, write: bool = False) -> bool:
        """Check a concrete path, including symlink-resolved containment."""
        return self.guard().permits(path, write=write)


class WorkspaceGuard:
    """Executable read/write enforcement for one child workspace assignment."""

    def __init__(self, assignment: WorkspaceAssignment) -> None:
        self.assignment = assignment
        self._reads = tuple(Path(root).resolve(strict=False) for root in assignment.read_roots)
        self._writes = tuple(Path(root).resolve(strict=False) for root in assignment.write_roots)
        if not all(any(self._inside(root, parent) for parent in self._reads) for root in self._writes):
            raise IntegrationError("write workspace must be contained by a read workspace")

    @staticmethod
    def _inside(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
        except ValueError:
            return False
        return True

    def permits(self, path: str | Path, *, write: bool = False) -> bool:
        candidate = Path(path).resolve(strict=False)
        roots = self._writes if write else self._reads
        return any(self._inside(candidate, root) for root in roots)

    def require(self, path: str | Path, *, write: bool = False) -> Path:
        candidate = Path(path).resolve(strict=False)
        if not self.permits(candidate, write=write):
            mode = "write" if write else "read"
            raise IntegrationError(f"workspace {mode} denied outside explicit assignment: {candidate}")
        return candidate


@dataclass(frozen=True)
class LineageRecord:
    """Immutable parent/child identity recorded before dispatch."""

    lineage_id: str
    root_id: str
    parent_id: str
    child_id: str
    depth: int
    preset: str
    role: AgentRole
    workspace: WorkspaceAssignment
    sequence: int = 0

    def __post_init__(self) -> None:
        for name in ("lineage_id", "root_id", "parent_id", "child_id", "preset"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        if self.depth < 0 or self.sequence < 0:
            raise IntegrationError("lineage depth and sequence must be non-negative")
        if self.child_id == self.parent_id:
            raise IntegrationError("a child cannot be its own parent")
        if not isinstance(self.role, AgentRole):
            raise IntegrationError("lineage role must be an AgentRole")


@dataclass(frozen=True)
class DelegationRequest:
    """Validated request handed from one registered role to another."""

    delegation_id: str
    lineage: LineageRecord
    prompt: str
    preset: AgentPreset
    workspace: WorkspaceAssignment
    evidence_tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "delegation_id", _required(self.delegation_id, "delegation_id"))
        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise IntegrationError("prompt must be non-empty")
        if len(self.prompt) > MAX_PROMPT_CHARS:
            raise IntegrationError("prompt exceeds its bound")
        if self.lineage.preset != self.preset.name:
            raise IntegrationError("lineage preset and request preset disagree")
        if self.lineage.workspace != self.workspace:
            raise IntegrationError("lineage workspace and request workspace disagree")
        tags = tuple(sorted({_required(tag, "evidence tag") for tag in self.evidence_tags}))
        object.__setattr__(self, "evidence_tags", tags)


@dataclass(frozen=True)
class ResultEvidence:
    """Structured, bounded proof envelope for a delegated result."""

    delegation_id: str
    status: DelegationStatus
    output_digest: str = ""
    verification: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    failure_reason: str | None = None
    usage_steps: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "delegation_id", _required(self.delegation_id, "delegation_id"))
        if self.status in {DelegationStatus.SUCCEEDED, DelegationStatus.FAILED} and not self.output_digest:
            raise IntegrationError("terminal result evidence requires an output digest")
        if self.status is DelegationStatus.FAILED and not self.failure_reason:
            raise IntegrationError("failed result evidence requires a failure reason")
        if self.usage_steps < 0:
            raise IntegrationError("usage_steps must be non-negative")
        if len(self.failure_reason or "") > MAX_EVIDENCE_CHARS:
            raise IntegrationError("failure reason exceeds its bound")
        for name in ("verification", "artifacts"):
            values = tuple(sorted({_required(item, name) for item in getattr(self, name)}))
            object.__setattr__(self, name, values)

    @staticmethod
    def digest(output: str) -> str:
        if not isinstance(output, str) or len(output) > MAX_EVIDENCE_CHARS:
            raise IntegrationError("output exceeds evidence bound")
        return hashlib.sha256(output.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RoleWorkflowGraph:
    """A deterministic acyclic role graph used by orchestration adapters."""

    edges: tuple[tuple[AgentRole, AgentRole], ...]

    def __post_init__(self) -> None:
        normalized = tuple(sorted(set(self.edges), key=lambda edge: (edge[0].value, edge[1].value)))
        for source, target in normalized:
            if not isinstance(source, AgentRole) or not isinstance(target, AgentRole):
                raise IntegrationError("workflow edges require AgentRole values")
            if source is target:
                raise IntegrationError("workflow graph cannot contain self-edges")
        if self._has_cycle(normalized):
            raise IntegrationError("role workflow graph must be acyclic")
        object.__setattr__(self, "edges", normalized)

    @staticmethod
    def _has_cycle(edges: tuple[tuple[AgentRole, AgentRole], ...]) -> bool:
        graph: dict[AgentRole, set[AgentRole]] = {}
        for source, target in edges:
            graph.setdefault(source, set()).add(target)
            graph.setdefault(target, set())
        visiting: set[AgentRole] = set()
        visited: set[AgentRole] = set()

        def visit(node: AgentRole) -> bool:
            if node in visiting:
                return True
            if node in visited:
                return False
            visiting.add(node)
            if any(visit(child) for child in graph[node]):
                return True
            visiting.remove(node)
            visited.add(node)
            return False

        return any(visit(node) for node in graph)

    def successors(self, role: AgentRole) -> tuple[AgentRole, ...]:
        return tuple(target for source, target in self.edges if source is role)

    def topological_order(self) -> tuple[AgentRole, ...]:
        nodes = set(AgentRole)
        for source, target in self.edges:
            nodes.update((source, target))
        incoming = {node: 0 for node in nodes}
        for _, target in self.edges:
            incoming[target] += 1
        ready = sorted((node for node, count in incoming.items() if count == 0), key=lambda item: item.value)
        result: list[AgentRole] = []
        while ready:
            node = ready.pop(0)
            result.append(node)
            for child in self.successors(node):
                incoming[child] -= 1
                if incoming[child] == 0:
                    ready.append(child)
                    ready.sort(key=lambda item: item.value)
        if len(result) != len(nodes):
            raise IntegrationError("role workflow graph is cyclic")
        return tuple(result)


def complete_builtin_presets() -> tuple[AgentPreset, ...]:
    """Return every built-in role preset, including the integration role."""
    existing: dict[AgentRole, AgentPreset] = {}
    for preset in builtin_presets():
        existing.setdefault(preset.role, preset)
    existing.setdefault(
        AgentRole.INTEGRATOR,
        AgentPreset("integrator", AgentRole.INTEGRATOR, role_budget(AgentRole.INTEGRATOR), ("inspect", "integrate", "validate")),
    )
    return tuple(existing[role] for role in AgentRole)


def _path_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def default_role_workflow() -> RoleWorkflowGraph:
    """Default inspect -> design -> edit -> verify -> review -> integrate graph."""
    return RoleWorkflowGraph((
        (AgentRole.EXPLORER, AgentRole.ARCHITECT),
        (AgentRole.ARCHITECT, AgentRole.EDITOR),
        (AgentRole.EDITOR, AgentRole.VERIFIER),
        (AgentRole.VERIFIER, AgentRole.REVIEWER),
        (AgentRole.REVIEWER, AgentRole.INTEGRATOR),
    ))


def delegation_digest(request: DelegationRequest) -> str:
    """Create a stable digest for idempotency and evidence correlation."""
    payload: Mapping[str, object] = {
        "delegation_id": request.delegation_id,
        "lineage_id": request.lineage.lineage_id,
        "parent_id": request.lineage.parent_id,
        "child_id": request.lineage.child_id,
        "preset": request.preset.name,
        "role": request.preset.role.value,
        "prompt": request.prompt,
        "read_roots": request.workspace.read_roots,
        "write_roots": request.workspace.write_roots,
        "tags": request.evidence_tags,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


__all__ = [
    "DelegationRequest", "DelegationStatus", "IntegrationError", "LineageRecord",
    "ResultEvidence", "RoleWorkflowGraph", "WorkspaceAssignment", "WorkspaceGuard",
    "complete_builtin_presets", "default_role_workflow", "delegation_digest",
]
