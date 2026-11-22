"""Pure ``CMakePresets.json`` reader with bounded include and inherits resolution.

The adapter supplies the bytes of every include it read inside the source
root (depth <= 4, <= 32 files); includes that escape the root or were not
supplied are skipped with a note. Only these macros are expanded in
``binaryDir``: ``${sourceDir}``, ``${sourceParentDir}``, ``${presetName}``,
``${generator}`` and ``${hostSystemName}``. A ``binaryDir`` that uses
``$env{}``/``$penv{}`` (or anything else) differs under Sonder's scrubbed
environment, so it is marked unresolvable and such a preset is refused for
configure.
"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from typing import Mapping

from .model import (
    MAX_PRESETS,
    PRESET_RE,
    PresetInfo,
    bounded_notes,
    clip,
    is_absolute,
    loads_bounded_json,
    norm_path,
    path_label,
    rel_under,
    safe_rel,
)


MAX_PRESET_FILE_BYTES = 1024 * 1024
MAX_PRESET_FILES = 32
MAX_INCLUDE_DEPTH = 4
MAX_INHERIT_DEPTH = 16
PRESET_CACHE_ALLOWLIST = frozenset({
    "CMAKE_BUILD_TYPE", "CMAKE_CONFIGURATION_TYPES", "CMAKE_UNITY_BUILD",
    "CMAKE_C_COMPILER_LAUNCHER", "CMAKE_CXX_COMPILER_LAUNCHER", "CMAKE_EXPORT_COMPILE_COMMANDS",
    "CMAKE_C_COMPILER", "CMAKE_CXX_COMPILER", "VCPKG_MANIFEST_INSTALL", "CMAKE_TOOLCHAIN_FILE",
})
_MACRO_RE = re.compile(r"\$(?:(env|penv|vendor)\{[^}]{0,256}\}|\{([A-Za-z]{1,32})\})")
_ALLOWED_MACROS = frozenset({"sourceDir", "sourceParentDir", "presetName", "generator",
                             "hostSystemName", "dollar"})


@dataclass(frozen=True, slots=True)
class PresetSet:
    configure: tuple[PresetInfo, ...]
    build: tuple[PresetInfo, ...]
    files: tuple[str, ...]
    truncated: bool
    notes: tuple[str, ...] = ()

    def find(self, name: str, kind: str = "configure") -> PresetInfo | None:
        pool = self.configure if kind == "configure" else self.build
        for preset in pool:
            if preset.name == name:
                return preset
        return None

    def all(self) -> tuple[PresetInfo, ...]:
        return self.configure + self.build


def _expand(value: str, *, source_root: str, preset: str, generator: str,
            host_system: str) -> str | None:
    """Expand the allowed macros; None when anything else is referenced."""
    unresolved = False

    def sub(match: re.Match) -> str:
        nonlocal unresolved
        if match.group(1):
            unresolved = True
            return ""
        name = match.group(2)
        if name not in _ALLOWED_MACROS:
            unresolved = True
            return ""
        return {
            "sourceDir": source_root,
            "sourceParentDir": posixpath.dirname(norm_path(source_root)),
            "presetName": preset,
            "generator": generator,
            "hostSystemName": host_system,
            "dollar": "\x01",
        }[name]

    expanded = _MACRO_RE.sub(sub, value)
    if unresolved or "$" in expanded or "\x00" in expanded:
        return None
    return expanded.replace("\x01", "$")


def _condition(raw: object, *, host_system: str, source_root: str, preset: str,
               depth: int = 0) -> bool | None:
    """Evaluate a preset condition; None when it cannot be decided."""
    if raw is None:
        return True
    if depth > 8 or not isinstance(raw, dict):
        return None
    kind = raw.get("type")
    if kind == "const":
        return bool(raw.get("value"))
    if kind in ("equals", "notEquals"):
        lhs = raw.get("lhs")
        rhs = raw.get("rhs")
        if not isinstance(lhs, str) or not isinstance(rhs, str):
            return None
        left = _expand(lhs, source_root=source_root, preset=preset, generator="",
                       host_system=host_system)
        right = _expand(rhs, source_root=source_root, preset=preset, generator="",
                        host_system=host_system)
        if left is None or right is None:
            return None
        return (left == right) if kind == "equals" else (left != right)
    if kind in ("allOf", "anyOf"):
        results = [_condition(item, host_system=host_system, source_root=source_root,
                              preset=preset, depth=depth + 1)
                   for item in (raw.get("conditions") or ())[:32]]
        if any(item is None for item in results):
            return None
        return all(results) if kind == "allOf" else any(results)
    if kind == "not":
        inner = _condition(raw.get("condition"), host_system=host_system,
                           source_root=source_root, preset=preset, depth=depth + 1)
        return None if inner is None else not inner
    return None


def _load(data: bytes, label: str) -> dict:
    document = loads_bounded_json(data, max_bytes=MAX_PRESET_FILE_BYTES, what=label)
    if not isinstance(document, dict):
        raise ValueError("preset file is not an object")
    return document


def parse_cmake_presets(
    data: bytes,
    *,
    includes: Mapping[str, bytes],
    source_root: str,
    source_label: str = "CMakePresets.json",
    user_data: bytes | None = None,
    user_label: str = "CMakeUserPresets.json",
    host_system: str = "Linux",
) -> PresetSet:
    """Configure and build presets, visible ones only, with includes followed."""
    notes: list[str] = []
    truncated = False
    files: list[tuple[str, dict]] = []
    seen: set[str] = set()

    def visit(label: str, raw: bytes, depth: int) -> None:
        nonlocal truncated
        if label in seen:
            return
        if len(files) >= MAX_PRESET_FILES:
            truncated = True
            notes.append("preset include count cap (%d) reached" % MAX_PRESET_FILES)
            return
        seen.add(label)
        try:
            document = _load(raw, label)
        except Exception:  # noqa: BLE001 - one bad include never fails the whole set
            truncated = True
            notes.append("preset file rejected: %s" % clip(label, 120))
            return
        files.append((label, document))
        for include in (document.get("include") or ())[:64]:
            if not isinstance(include, str):
                continue
            if depth + 1 > MAX_INCLUDE_DEPTH:
                truncated = True
                notes.append("preset include depth cap (%d) reached" % MAX_INCLUDE_DEPTH)
                continue
            base = posixpath.dirname(label)
            if is_absolute(include):
                rel = rel_under(include, source_root)
                rel = safe_rel(rel) if rel else None
            else:
                rel = safe_rel(posixpath.join(base, include) if base else include)
            if rel is None:
                truncated = True
                notes.append("preset include outside the source root refused: %s"
                             % clip(include, 120))
                continue
            payload = includes.get(rel)
            if payload is None:
                truncated = True
                notes.append("preset include not available: %s" % clip(rel, 120))
                continue
            visit(rel, payload, depth + 1)

    visit(norm_path(source_label) or "CMakePresets.json", data, 0)
    if user_data is not None:
        visit(norm_path(user_label) or "CMakeUserPresets.json", user_data, 0)

    raw_configure: dict[str, tuple[dict, str]] = {}
    raw_build: dict[str, tuple[dict, str]] = {}
    for label, document in files:
        for key, pool in (("configurePresets", raw_configure), ("buildPresets", raw_build)):
            for preset in (document.get(key) or ())[:MAX_PRESETS]:
                if not isinstance(preset, dict) or not isinstance(preset.get("name"), str):
                    continue
                name = preset["name"]
                if not PRESET_RE.fullmatch(name):
                    notes.append("preset with an unsupported name skipped")
                    continue
                if name in pool:
                    notes.append("duplicate preset %s ignored" % name)
                    continue
                pool[name] = (preset, label)

    # Memoised per (pool, name, field, depth): diamond inheritance in an untrusted
    # presets file would otherwise be walked once per path (exponential in depth).
    # Precompute cycle membership per pool in O(V+E) via iterative Tarjan SCC:
    # acyclic nodes (`name not in in_cycle`) cannot reach any ancestor on `stack`,
    # so their results stay memoised even above a cyclic descendant; only nodes
    # inside a cycle skip memoisation when a cycle cut occurs. A per-root step
    # A per-root step budget caps dense cyclic cliques (e.g. K_16) to
    # milliseconds: acyclic nodes are memoised and bounded by _MAX_RESOLVE_STEPS,
    # while cyclic SCC exploration is additionally capped by _MAX_CYCLE_STEPS.
    _MAX_RESOLVE_STEPS = 4096
    _MAX_CYCLE_STEPS = 256
    resolved_memo: dict[tuple[int, str, str, int], object] = {}
    merged_memo: dict[tuple[int, str, int], dict[str, str]] = {}
    cycle_sets: dict[int, frozenset[str]] = {}

    def _parents_of(preset: dict) -> list[str]:
        raw = preset.get("inherits")
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return []
        return [parent for parent in raw[:16] if isinstance(parent, str)]

    def _cycle_nodes(pool: dict) -> frozenset[str]:
        cached = cycle_sets.get(id(pool))
        if cached is not None:
            return cached
        adj = {
            name: [p for p in _parents_of(entry[0]) if p in pool]
            for name, entry in pool.items()
        }
        index_counter = 0
        indices: dict[str, int] = {}
        lowlink: dict[str, int] = {}
        on_stack: set[str] = set()
        tarjan_stack: list[str] = []
        in_cycle: set[str] = set()
        for start in adj:
            if start in indices:
                continue
            call_stack: list[tuple[str, int]] = [(start, 0)]
            indices[start] = lowlink[start] = index_counter
            index_counter += 1
            tarjan_stack.append(start)
            on_stack.add(start)
            while call_stack:
                node, edge_i = call_stack[-1]
                neighbors = adj[node]
                if edge_i < len(neighbors):
                    call_stack[-1] = (node, edge_i + 1)
                    nxt = neighbors[edge_i]
                    if nxt not in indices:
                        indices[nxt] = lowlink[nxt] = index_counter
                        index_counter += 1
                        tarjan_stack.append(nxt)
                        on_stack.add(nxt)
                        call_stack.append((nxt, 0))
                    elif nxt in on_stack:
                        if indices[nxt] < lowlink[node]:
                            lowlink[node] = indices[nxt]
                else:
                    call_stack.pop()
                    if call_stack:
                        parent_node = call_stack[-1][0]
                        if lowlink[node] < lowlink[parent_node]:
                            lowlink[parent_node] = lowlink[node]
                    if lowlink[node] == indices[node]:
                        comp: list[str] = []
                        while True:
                            member = tarjan_stack.pop()
                            on_stack.discard(member)
                            comp.append(member)
                            if member == node:
                                break
                        if len(comp) > 1 or node in adj[node]:
                            in_cycle.update(comp)
        result = frozenset(in_cycle)
        cycle_sets[id(pool)] = result
        return result

    def _resolved(
        pool: dict, name: str, field: str, depth: int, stack: tuple[str, ...],
        in_cycle: frozenset[str], budget: list[int],
    ) -> tuple[object, bool, bool]:
        entry = pool.get(name)
        if name in stack:
            return None, True, False
        if entry is None or depth > MAX_INHERIT_DEPTH:
            return None, False, False
        key = (id(pool), name, field, depth)
        if key in resolved_memo:
            return resolved_memo[key], False, False
        is_cyclic = name in in_cycle
        if budget[0] <= 0 or (is_cyclic and budget[1] <= 0):
            return None, True, True
        budget[0] -= 1
        if is_cyclic:
            budget[1] -= 1
        preset = entry[0]
        if field in preset:
            return preset[field], False, False
        found: object = None
        cycle_cut = False
        budget_hit = False
        for parent in _parents_of(preset):
            value, child_cut, child_budget = _resolved(
                pool, parent, field, depth + 1, stack + (name,), in_cycle, budget,
            )
            if child_cut:
                cycle_cut = True
            if child_budget:
                budget_hit = True
            if value is not None:
                found = value
                break
        if not budget_hit and (not cycle_cut or name not in in_cycle):
            resolved_memo[key] = found
        return found, cycle_cut, budget_hit

    def resolved(pool: dict, name: str, field: str, depth: int = 0, stack=()) -> object:
        value, _, _ = _resolved(
            pool, name, field, depth, stack, _cycle_nodes(pool), [_MAX_RESOLVE_STEPS, _MAX_CYCLE_STEPS],
        )
        return value

    def _merged_cache(
        pool: dict, name: str, depth: int, stack: tuple[str, ...],
        in_cycle: frozenset[str], budget: list[int],
    ) -> tuple[dict[str, str], bool, bool]:
        entry = pool.get(name)
        if name in stack:
            return {}, True, False
        if entry is None or depth > MAX_INHERIT_DEPTH:
            return {}, False, False
        memo_key = (id(pool), name, depth)
        cached = merged_memo.get(memo_key)
        if cached is not None:
            return dict(cached), False, False
        is_cyclic = name in in_cycle
        if budget[0] <= 0 or (is_cyclic and budget[1] <= 0):
            return {}, True, True
        budget[0] -= 1
        if is_cyclic:
            budget[1] -= 1
        preset = entry[0]
        out: dict[str, str] = {}
        cycle_cut = False
        budget_hit = False
        for parent in reversed(_parents_of(preset)):
            parent_cache, child_cut, child_budget = _merged_cache(
                pool, parent, depth + 1, stack + (name,), in_cycle, budget,
            )
            if child_cut:
                cycle_cut = True
            if child_budget:
                budget_hit = True
            out.update(parent_cache)
        own = preset.get("cacheVariables")
        if isinstance(own, dict):
            for key, value in list(own.items())[:256]:
                if key not in PRESET_CACHE_ALLOWLIST:
                    continue
                if isinstance(value, dict):
                    value = value.get("value")
                if isinstance(value, bool):
                    value = "ON" if value else "OFF"
                if isinstance(value, str):
                    out[key] = clip(value, 1024)
        if not budget_hit and (not cycle_cut or name not in in_cycle):
            merged_memo[memo_key] = dict(out)
        return out, cycle_cut, budget_hit

    def merged_cache(pool: dict, name: str, depth: int = 0, stack=()) -> dict[str, str]:
        out, _, _ = _merged_cache(
            pool, name, depth, stack, _cycle_nodes(pool), [_MAX_RESOLVE_STEPS, _MAX_CYCLE_STEPS],
        )
        return out

    configure: list[PresetInfo] = []
    for name, (preset, label) in raw_configure.items():
        if preset.get("hidden") is True:
            continue
        condition = _condition(resolved(raw_configure, name, "condition"),
                               host_system=host_system, source_root=source_root, preset=name)
        if condition is False:
            notes.append("preset %s is disabled on %s" % (name, host_system))
            continue
        generator = resolved(raw_configure, name, "generator")
        generator = clip(generator, 64) if isinstance(generator, str) else ""
        binary = resolved(raw_configure, name, "binaryDir")
        binary_dir = ""
        resolvable = False
        if isinstance(binary, str) and binary.strip():
            expanded = _expand(binary, source_root=norm_path(source_root), preset=name,
                               generator=generator, host_system=host_system)
            if expanded is not None and "\x00" not in expanded:
                binary_dir = norm_path(expanded if is_absolute(expanded)
                                       else norm_path(source_root) + "/" + expanded)
                resolvable = True
        label_text = ""
        if binary_dir:
            inside = rel_under(binary_dir, source_root)
            label_text = inside if inside is not None else path_label(
                binary_dir, source_root=source_root, build_dir="")[0]
        configure.append(PresetInfo(
            name=name, kind="configure", generator=generator, binary_dir_label=clip(label_text, 1024),
            hidden=False, binary_dir_resolvable=resolvable, source_file_label=clip(label, 1024),
            binary_dir=clip(binary_dir, 4096),
            cache=tuple(sorted(merged_cache(raw_configure, name).items()))[:32],
        ))
    by_name = {item.name: item for item in configure}
    build: list[PresetInfo] = []
    for name, (preset, label) in raw_build.items():
        if preset.get("hidden") is True:
            continue
        configure_name = resolved(raw_build, name, "configurePreset")
        configure_name = configure_name if isinstance(configure_name, str) else ""
        target = by_name.get(configure_name)
        configuration = resolved(raw_build, name, "configuration")
        build.append(PresetInfo(
            name=name, kind="build", generator=target.generator if target else "",
            binary_dir_label=target.binary_dir_label if target else "", hidden=False,
            binary_dir_resolvable=bool(target and target.binary_dir_resolvable),
            source_file_label=clip(label, 1024), configure_preset=clip(configure_name, 128)
            if PRESET_RE.fullmatch(configure_name or "-") else "",
            configuration=clip(configuration, 64) if isinstance(configuration, str) else "",
            binary_dir=target.binary_dir if target else "",
        ))
    return PresetSet(
        configure=tuple(configure[:MAX_PRESETS]), build=tuple(build[:MAX_PRESETS]),
        files=tuple(label for label, _ in files), truncated=truncated,
        notes=bounded_notes(notes),
    )


__all__ = ["MAX_INCLUDE_DEPTH", "MAX_PRESET_FILES", "PresetSet", "parse_cmake_presets"]
