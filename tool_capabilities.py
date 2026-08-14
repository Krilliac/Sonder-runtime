"""Shadow-only capability metadata for Sonder's model-callable tools.

The existing MCP decorators, dispatch chain, and policy sets remain authoritative.
This module describes a deliberately small initial slice and detects drift when
tests or diagnostics explicitly ask it to.  Importing it never probes the host,
registers a tool, or changes an allow-list.
"""
from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass, fields as dataclass_fields
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
    tool_manifest: str
    repository_read_only_tools: frozenset[str]
    project_bound_agent_tools: frozenset[str]
    project_scoped_tools: frozenset[str]
    dispatch_tools: frozenset[str]
    hosted_agent_tools: frozenset[str]
    deduplicated_inspection_tools: frozenset[str]
    work_inspection_tools: frozenset[str]
    full_agent_help: str
    repository_agent_help: str
    hosted_agent_help: str
    # Injected verbatim into the autopilot model transcript, so they advertise
    # tools exactly as the help texts do and are measured the same way.
    #
    # Deliberately NOT defaulted, like every field above: a default lets a
    # construction site omit a surface, and an omitted surface is indistinguishable
    # from one that genuinely advertises nothing. That is the same silence this
    # module was fixed for, moved from the descriptor slice to the snapshot.
    autopilot_observe_tools: frozenset[str]
    autopilot_workspace_tools: frozenset[str]


@dataclass(frozen=True, slots=True)
class SurfaceCoverage:
    """How much of one advertised surface the descriptor slice can validate."""

    surface: str
    advertised: int
    described: int

    @property
    def unvalidated(self) -> int:
        return self.advertised - self.described

    @property
    def described_fraction(self) -> float | None:
        """None when the surface advertises nothing -- 0 of 0 is not 100%.

        Returning 1.0 there says "fully validated" about a surface nothing was
        ever measured on, which is the exact shape of the defect this coverage
        report exists to expose.
        """
        return (self.described / self.advertised) if self.advertised else None


_ALL_VISIBILITY = frozenset(Visibility)
_READ_RESOURCES = frozenset({ResourceClass.CPU, ResourceClass.RAM, ResourceClass.DISK})


def _read_tool(
    name: str,
    *,
    visibility: frozenset[Visibility] = _ALL_VISIBILITY,
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
        visibility=visibility,
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
    # Image inspection returns a local path plus size, dimensions, and a
    # content-derived SHA-256.  That metadata can identify a private asset
    # even though the tool deliberately never returns pixel/OCR text, so it
    # must not enter a hosted-agent transcript.
    _read_tool("image_inspect"),
    _read_tool("repo_status", mode=ExecutionMode.BOUNDED_SUBPROCESS),
    _read_tool("repo_diff", mode=ExecutionMode.BOUNDED_SUBPROCESS),
    _read_tool(
        "artifact_risk_inspect",
        mode=ExecutionMode.IN_PROCESS,
        secrets=SecretPolicy.NO_SECRET_INPUT,
    ),
    _read_tool(
        "process_list",
        visibility=frozenset({Visibility.DIRECT_MCP, Visibility.FULL_AGENT}),
        root=RootRequirement.NONE,
        resources=frozenset({ResourceClass.CPU, ResourceClass.RAM}),
        secrets=SecretPolicy.NO_SECRET_INPUT,
    ),
    _read_tool(
        "process_memory_risk_inspect",
        visibility=frozenset({Visibility.DIRECT_MCP, Visibility.FULL_AGENT}),
        root=RootRequirement.NONE,
        resources=frozenset({ResourceClass.CPU, ResourceClass.RAM}),
        secrets=SecretPolicy.NO_SECRET_INPUT,
    ),
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


def _manifest_has(manifest_text: str, name: str) -> bool:
    return name in _manifest_names(manifest_text)


def _help_names(help_text: str) -> frozenset[str]:
    """Every tool name a help text advertises, by the shape ``_help_has`` reads."""
    names: set[str] = set()
    for line in (help_text or "").splitlines():
        stripped = line.lstrip()
        if not stripped.startswith("- "):
            continue
        name, separator, _ = stripped[2:].partition(":")
        if separator and name.isidentifier():
            names.add(name)
    return frozenset(names)


def _manifest_names(manifest_text: str) -> frozenset[str]:
    """Every tool name the manifest advertises, by ``_manifest_has``'s shape."""
    names: set[str] = set()
    for line in (manifest_text or "").splitlines():
        key, separator, _ = line.strip().partition(":")
        if not separator:
            continue
        names.update(part for part in key.split("/") if part.isidentifier())
    return frozenset(names)


# The only surfaces that are not already a set of names.  A field listed here
# is parsed into one; a text field missing from here raises rather than being
# skipped, because silently skipping what it lacks a handler for is exactly the
# defect this coverage report exists to expose.
_TEXT_SURFACE_PARSERS = {
    "tool_manifest": _manifest_names,
    "full_agent_help": _help_names,
    "repository_agent_help": _help_names,
    "hosted_agent_help": _help_names,
}


def surface_label(field_name: str) -> str:
    """The report label for a ShadowSurfaces field -- derived, not hand-written."""
    base = field_name[:-len("_tools")] if field_name.endswith("_tools") else field_name
    return base.replace("_", "-")


def advertised_surfaces(
    surfaces: ShadowSurfaces,
    text_parsers: Mapping[str, Callable[[str], frozenset[str]]] | None = None,
) -> tuple[tuple[str, frozenset[str]], ...]:
    """Every surface on the snapshot, enumerated FROM ``ShadowSurfaces`` itself.

    Hand-listing them reproduced this module's own defect one level up: the
    list covered every field that existed when it was written, and a fifteenth
    would have been silently unmeasured -- precisely as a tool without a
    descriptor is silently unchecked. Deriving the list means a new surface is
    measured the moment it is snapshotted, and the one hand-maintained part
    (which text fields need a name parser) fails loudly when it goes stale.
    """
    parsers = _TEXT_SURFACE_PARSERS if text_parsers is None else text_parsers
    rows = []
    for field in dataclass_fields(ShadowSurfaces):
        value = getattr(surfaces, field.name)
        if isinstance(value, str):
            parser = parsers.get(field.name)
            if parser is None:
                raise ValueError(
                    "no tool-name parser for text surface %r: coverage must not "
                    "silently skip a surface it cannot read" % field.name
                )
            names = parser(value)
        else:
            names = frozenset(value)
        rows.append((surface_label(field.name), names))
    return tuple(rows)


def shadow_coverage(
    surfaces: ShadowSurfaces,
    descriptors: Iterable[ToolCapability] | None = None,
) -> tuple[SurfaceCoverage, ...]:
    """Per-surface count of advertised tools this validator can actually check.

    ``validate_shadow`` is per-descriptor, so a tool with no descriptor is not
    checked -- it is *exempt*, silently.  These numbers are the difference
    between "no drift" and "no drift in the part I looked at".
    """
    rows = tuple(descriptors) if descriptors is not None else _DESCRIPTORS
    described = frozenset(row.name for row in rows)
    return tuple(
        SurfaceCoverage(label, len(names), len(names & described))
        for label, names in advertised_surfaces(surfaces)
    )


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
            ("tool manifest", expected_direct, _manifest_has(surfaces.tool_manifest, row.name)),
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
            ("hosted-agent help", expected_hosted,
             _help_has(surfaces.hosted_agent_help, row.name)),
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
    """The drift verdict, which may never be stated without its coverage.

    This validator was built to catch an advertised-but-uncallable tool and
    could not see a real 23-tool instance of exactly that, because every tool
    without a descriptor is skipped rather than reported.  Reporting "ok" over
    15 of ~184 advertised tools is how that stayed invisible, so the verdict
    now leads with the fraction it actually inspected.
    """
    issues = validate_shadow(surfaces)
    coverage = shadow_coverage(surfaces)
    advertised = frozenset().union(*(names for _label, names in advertised_surfaces(surfaces)))
    described = advertised & frozenset(CAPABILITIES)
    unvalidated = len(advertised) - len(described)
    if not advertised:
        # "complete: 0 of 0 (100.0%)" is this module's own defect one level up:
        # the absence of evidence reported as evidence of absence. A snapshot
        # that advertises nothing measured nothing.
        return (
            "unmeasured: this snapshot advertises no tool on any of its %d "
            "surface(s), so nothing was validated -- 0 of 0 is not coverage"
            % len(coverage)
        )
    verdict = "complete" if not unvalidated else "partial"
    scope = (
        "%s: %d descriptor(s) validate %d of %d advertised tool(s) (%.1f%%); "
        "%d unvalidated across %d surface(s)"
        % (
            verdict, len(_DESCRIPTORS), len(described), len(advertised),
            100.0 * len(described) / len(advertised) if advertised else 100.0,
            unvalidated, sum(1 for row in coverage if row.unvalidated),
        )
    )
    if issues:
        return "%s; ERROR %d drift issue(s) among the described: %s" % (
            scope, len(issues), "; ".join(issues),
        )
    return "%s; no drift among the described (shadow-only)" % scope


def format_coverage_report(surfaces: ShadowSurfaces) -> str:
    """Coverage for every advertised surface, so none can hide behind a total."""
    return "; ".join(
        "%s %d/%d" % (row.surface, row.described, row.advertised)
        for row in shadow_coverage(surfaces)
    )
