"""Which targets run arbitrary commands, and which sources the build executes.

``classify_targets`` is the F4/F5 policy input:

- *utility*: UTILITY/custom targets, VS Makefile/Utility projects, and the
  generator's packaging/install/test pseudo-targets. They run arbitrary
  commands (often with network access) and are refused unless an operator
  allowlists them.
- *build_time_tool*: an EXECUTABLE that another target depends on without
  linking it -- the code-generator pattern (shader compilers, reflection or
  header generators, asset cookers) -- plus any target whose artifact appears
  among the CMake build inputs.
- *tool_sources*: the sources of build-time tools, of every library such a
  tool links (transitively, conservatively), and every cmakeFiles input.
  build_fix never edits these, because the build itself executes them.
"""
from __future__ import annotations

import posixpath
from dataclasses import dataclass, replace

from .model import LIBRARY_TYPES, BuildModel, BuildTarget, TargetType, bounded_notes


# Pseudo-targets that package, install, deploy or run things.
UTILITY_TARGET_NAMES = frozenset({
    "install", "install/local", "install/strip", "package", "package_source", "test",
    "RUN_TESTS", "INSTALL", "PACKAGE", "ZERO_CHECK", "edit_cache", "rebuild_cache",
    "list_install_components", "Continuous", "Experimental", "Nightly", "NightlyMemoryCheck",
})
BUILD_ALIASES = frozenset({"all", "ALL_BUILD", "Build"})


@dataclass(frozen=True, slots=True)
class TargetSafety:
    utility: frozenset[str]
    build_time_tool: frozenset[str]
    tool_sources: frozenset[str]
    notes: tuple[str, ...] = ()

    def is_refused_target(self, name: str) -> bool:
        return name in self.utility


def _is_utility(target: BuildTarget) -> bool:
    if target.name in BUILD_ALIASES:
        return False
    if target.type in (TargetType.UTILITY, TargetType.MSBUILD_MAKEFILE):
        return True
    return target.name in UTILITY_TARGET_NAMES or target.utility


def classify_targets(model: BuildModel) -> TargetSafety:
    by_name = {target.name: target for target in model.targets}
    utility = {target.name for target in model.targets if _is_utility(target)}
    notes: list[str] = []

    inputs = set(model.build_inputs)
    input_basenames = {posixpath.basename(item) for item in inputs}
    tools: set[str] = set()
    for target in model.targets:
        if target.build_time_tool:
            tools.add(target.name)
            continue
        if target.type is TargetType.EXECUTABLE:
            if any(posixpath.basename(label) in input_basenames for label in target.artifacts):
                tools.add(target.name)
    for target in model.targets:
        for dependency in target.depends:
            dep = by_name.get(dependency)
            if dep is None or dep.type is not TargetType.EXECUTABLE:
                continue
            # An executable target dependency is never linked: the dependent
            # needs it to exist because it runs it (the code-generator shape).
            tools.add(dep.name)

    # Libraries a tool links are compiled into code the build executes.
    tainted = set(tools)
    frontier = list(tools)
    while frontier:
        current = by_name.get(frontier.pop())
        if current is None:
            continue
        for dependency in current.depends:
            dep = by_name.get(dependency)
            if dep is None or dep.name in tainted or dep.type not in LIBRARY_TYPES:
                continue
            tainted.add(dep.name)
            frontier.append(dep.name)
    linked_libraries = sorted(tainted - tools)
    if linked_libraries:
        notes.append("libraries linked by build-time tools are not editable: "
                     + ", ".join(linked_libraries[:8]))

    tool_sources = {unit.file_rel for unit in model.units
                    if unit.file_rel and unit.target in tainted}
    tool_sources.update(item for item in inputs if item)
    if tools:
        notes.append("build-time tools (run during the build): " + ", ".join(sorted(tools)[:8]))
    return TargetSafety(
        utility=frozenset(utility),
        build_time_tool=frozenset(tools),
        tool_sources=frozenset(tool_sources),
        notes=bounded_notes(notes),
    )


def apply_safety(model: BuildModel, safety: TargetSafety) -> tuple[BuildTarget, ...]:
    """The model's targets with ``utility``/``build_time_tool`` flags set."""
    return tuple(
        replace(target, utility=target.name in safety.utility,
                build_time_tool=target.name in safety.build_time_tool)
        for target in model.targets
    )


__all__ = ["BUILD_ALIASES", "TargetSafety", "UTILITY_TARGET_NAMES", "apply_safety",
           "classify_targets"]
