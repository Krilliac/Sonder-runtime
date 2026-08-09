"""Shadow-only capability metadata for Sonder's model-callable tools.

The existing MCP decorators, dispatch chain, and policy sets remain authoritative.
This module describes a deliberately small initial slice and detects drift when
tests or diagnostics explicitly ask it to.  Importing it never probes the host,
registers a tool, or changes an allow-list.
"""
from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Callable, Iterable, Mapping


class Effect(str, Enum):
    READ_ONLY = "read-only"
    MUTATION = "mutation"


class Visibility(str, Enum):
    DIRECT_MCP = "direct-mcp"
    REPOSITORY_AGENT = "repository-agent"
    FULL_AGENT = "full-agent"


class Permission(str, Enum):
    NONE = "none"
    GUARDED_READ = "guarded-read"
    GUARDED_WRITE = "guarded-write"
    HOST_EXECUTION = "host-execution"


class RootRequirement(str, Enum):
    NONE = "none"
    GUARDED_SCOPE = "project-agent-or-approved-direct-root"


class NetworkRequirement(str, Enum):
    NONE = "none"
    OPTIONAL = "optional"
    REQUIRED = "required"


class CloudRequirement(str, Enum):
    """Whether Sonder may place tool-derived data in a hosted model prompt."""

    LOCAL_ONLY = "local-only"
    PUBLIC_CONTEXT_ONLY = "public-context-only"
    ALLOWED = "allowed"


class SecretPolicy(str, Enum):
    NO_SECRET_INPUT = "no-secret-input"
    CALLER_MUST_REDACT = "caller-must-redact"
    OUTPUT_REDACTED = "output-redacted"


class ExecutionMode(str, Enum):
    IN_PROCESS = "in-process"
    BOUNDED_SUBPROCESS = "bounded-subprocess"
    MIXED = "mixed"


class ResourceClass(str, Enum):
    CPU = "cpu"
    GPU = "gpu"
    RAM = "ram"
    DISK = "disk"
    NETWORK = "network"


@dataclass(frozen=True, slots=True)
class ToolCapability:
    name: str
    effect: Effect
    visibility: frozenset[Visibility]
    permission: Permission
    root: RootRequirement
    network: NetworkRequirement
    cloud: CloudRequirement
    secret_policy: SecretPolicy
    execution_mode: ExecutionMode
    resources: frozenset[ResourceClass]
    deduplicated_inspection: bool = False
    counts_as_inspection: bool = False


@dataclass(frozen=True, slots=True)
class ShadowSurfaces:
    """Snapshots of the currently authoritative server surfaces."""

    direct_mcp_tools: frozenset[str]
    repository_read_only_tools: frozenset[str]
    project_bound_agent_tools: frozenset[str]
    project_scoped_tools: frozenset[str]
    dispatch_tools: frozenset[str]
    hosted_agent_tools: frozenset[str]
    deduplicated_inspection_tools: frozenset[str]
    work_inspection_tools: frozenset[str]
    full_agent_help: str
    repository_agent_help: str


_ALL_VISIBILITY = frozenset(Visibility)
_READ_RESOURCES = frozenset({ResourceClass.CPU, ResourceClass.RAM, ResourceClass.DISK})


def _read_tool(
    name: str,
    *,
    root: RootRequirement = RootRequirement.GUARDED_SCOPE,
    mode: ExecutionMode = ExecutionMode.IN_PROCESS,
    resources: frozenset[ResourceClass] = _READ_RESOURCES,
    deduplicated: bool = True,
    inspection: bool = True,
    secrets: SecretPolicy = SecretPolicy.CALLER_MUST_REDACT,
) -> ToolCapability:
    return ToolCapability(
        name=name,
        effect=Effect.READ_ONLY,
        visibility=_ALL_VISIBILITY,
        permission=Permission.NONE if root is RootRequirement.NONE else Permission.GUARDED_READ,
        root=root,
        network=NetworkRequirement.NONE,
        # Repository reads and host inventory can expose private source or
        # machine details.  Keep the initial shadow slice local-only; a future
        # descriptor may opt a specifically public tool into hosted context.
        cloud=CloudRequirement.LOCAL_ONLY,
        secret_policy=secrets,
        execution_mode=mode,
        resources=resources,
        deduplicated_inspection=deduplicated,
        counts_as_inspection=inspection,
    )


# Initial shadow slice.  Grow this only as each descriptor can be checked against
# every authoritative surface; absence from this mapping does not deny a tool.
_DESCRIPTORS = (
    _read_tool(
        "environment_status", root=RootRequirement.NONE,
        resources=frozenset({ResourceClass.CPU, ResourceClass.RAM}),
        inspection=False, secrets=SecretPolicy.NO_SECRET_INPUT,
    ),
    _read_tool(
        "hardware_profile", root=RootRequirement.NONE,
        mode=ExecutionMode.MIXED,
        resources=frozenset({ResourceClass.CPU, ResourceClass.GPU, ResourceClass.RAM}),
        inspection=False, secrets=SecretPolicy.NO_SECRET_INPUT,
    ),
    _read_tool(
        "file_policy", root=RootRequirement.NONE, inspection=True,
        resources=frozenset({ResourceClass.CPU, ResourceClass.RAM}),
        secrets=SecretPolicy.NO_SECRET_INPUT,
    ),
    _read_tool("workspace_inventory"),
    _read_tool("directory_tree"),
    _read_tool("file_find"),
    _read_tool("file_read"),
    _read_tool("file_read_range"),
    _read_tool("file_digest"),
    _read_tool("text_search"),
    _read_tool("repo_status", mode=ExecutionMode.BOUNDED_SUBPROCESS),
    _read_tool("repo_diff", mode=ExecutionMode.BOUNDED_SUBPROCESS),
)

CAPABILITIES: Mapping[str, ToolCapability] = MappingProxyType(
    {descriptor.name: descriptor for descriptor in _DESCRIPTORS}
)


def dispatch_names(dispatch: Callable) -> frozenset[str]:
    """Extract literal ``tool_name`` branches from the authoritative dispatcher."""
    try:
        tree = ast.parse(inspect.getsource(dispatch))
    except (OSError, TypeError, SyntaxError):
        return frozenset()
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        if not isinstance(node.left, ast.Name) or node.left.id != "tool_name":
            continue
        comparator = node.comparators[0]
        if isinstance(node.ops[0], ast.Eq) and isinstance(comparator, ast.Constant):
            if isinstance(comparator.value, str):
                names.add(comparator.value)
        elif isinstance(node.ops[0], ast.In) and isinstance(comparator, (ast.Set, ast.Tuple, ast.List)):
            names.update(
                item.value for item in comparator.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            )
    return frozenset(names)


def _help_has(help_text: str, name: str) -> bool:
    return any(line.lstrip().startswith("- %s:" % name) for line in help_text.splitlines())


def validate_shadow(
    surfaces: ShadowSurfaces,
    descriptors: Iterable[ToolCapability] | None = None,
) -> tuple[str, ...]:
    """Return deterministic drift errors without changing runtime policy."""
    # Keep the source tuple here rather than the mapping values so an accidental
    # duplicate is reported in tests/diagnostics instead of being silently
    # hidden by dict key replacement (and without failing runtime import).
    rows = tuple(descriptors) if descriptors is not None else _DESCRIPTORS
    errors: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if row.name in seen:
            errors.append("%s: duplicate descriptor" % row.name)
            continue
        seen.add(row.name)
        expected_direct = Visibility.DIRECT_MCP in row.visibility
        expected_repository = Visibility.REPOSITORY_AGENT in row.visibility
        expected_full = Visibility.FULL_AGENT in row.visibility
        expected_hosted = (
            (expected_repository or expected_full)
            and row.cloud is not CloudRequirement.LOCAL_ONLY
        )
        checks = (
            ("direct MCP registration", expected_direct, row.name in surfaces.direct_mcp_tools),
            ("repository read-only allow-list", expected_repository and row.effect is Effect.READ_ONLY,
             row.name in surfaces.repository_read_only_tools),
            ("project-bound full-agent allow-list", expected_full, row.name in surfaces.project_bound_agent_tools),
            ("project root-scoping set", row.root is RootRequirement.GUARDED_SCOPE,
             row.name in surfaces.project_scoped_tools),
            ("repository help", expected_repository, _help_has(surfaces.repository_agent_help, row.name)),
            ("full-agent help", expected_full, _help_has(surfaces.full_agent_help, row.name)),
            ("agent dispatcher", expected_repository or expected_full, row.name in surfaces.dispatch_tools),
            ("hosted-agent exposure", expected_hosted,
             row.name in surfaces.hosted_agent_tools),
            ("deduplicated inspection set", row.deduplicated_inspection,
             row.name in surfaces.deduplicated_inspection_tools),
            ("work inspection set", row.counts_as_inspection,
             row.name in surfaces.work_inspection_tools),
        )
        for label, expected, actual in checks:
            if expected != actual:
                errors.append(
                    "%s: %s is %s but descriptor expects %s"
                    % (row.name, label, "present" if actual else "absent", "present" if expected else "absent")
                )
        if row.effect is Effect.MUTATION and row.permission is Permission.NONE:
            errors.append("%s: mutation cannot have permission=none" % row.name)
        if row.network is NetworkRequirement.REQUIRED and ResourceClass.NETWORK not in row.resources:
            errors.append("%s: required network is missing network resource class" % row.name)
        if row.root is RootRequirement.NONE and row.permission is Permission.GUARDED_READ:
            errors.append("%s: rootless tool cannot require a guarded project read" % row.name)
        if row.root is RootRequirement.GUARDED_SCOPE and row.permission is not Permission.GUARDED_READ:
            errors.append("%s: guarded root scope requires guarded-read permission" % row.name)
    return tuple(sorted(errors))


def assert_shadow_valid(surfaces: ShadowSurfaces) -> None:
    issues = validate_shadow(surfaces)
    if issues:
        raise AssertionError("tool capability shadow drift:\n- " + "\n- ".join(issues))


def format_shadow_report(surfaces: ShadowSurfaces) -> str:
    issues = validate_shadow(surfaces)
    if not issues:
        return "ok (%d descriptors; shadow-only)" % len(_DESCRIPTORS)
    return "ERROR %d drift issue(s): %s" % (len(issues), "; ".join(issues))
