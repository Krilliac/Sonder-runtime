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

    def resolved(pool: dict, name: str, field: str, depth: int = 0, stack=()) -> object:
        entry = pool.get(name)
        if entry is None or depth > MAX_INHERIT_DEPTH or name in stack:
            return None
        preset = entry[0]
        if field in preset:
            return preset[field]
        parents = preset.get("inherits")
        if isinstance(parents, str):
            parents = [parents]
        for parent in (parents or ())[:16]:
            if isinstance(parent, str):
                value = resolved(pool, parent, field, depth + 1, stack + (name,))
                if value is not None:
                    return value
        return None

    def merged_cache(pool: dict, name: str, depth: int = 0, stack=()) -> dict[str, str]:
        entry = pool.get(name)
        if entry is None or depth > MAX_INHERIT_DEPTH or name in stack:
            return {}
        preset = entry[0]
        out: dict[str, str] = {}
        parents = preset.get("inherits")
        if isinstance(parents, str):
            parents = [parents]
        for parent in reversed(list(parents or ())[:16]):
            if isinstance(parent, str):
                out.update(merged_cache(pool, parent, depth + 1, stack + (name,)))
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
