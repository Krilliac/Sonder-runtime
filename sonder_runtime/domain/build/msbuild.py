"""Pure ``.sln``/``.vcxproj``/``.props`` readers -- no MSBuild evaluation.

The model a Windows engine tree needs (configs, custom console platforms,
toolsets, PCH and forced includes, Utility/Makefile projects) is read from
the XML directly. Conditions are matched only in the simple
``'$(Configuration)|$(Platform)'=='X|Y'`` and ``'$(Configuration)'=='X'``
shapes; anything richer is ignored with a note. ``Import`` elements are
followed only when the adapter supplied the imported bytes (inside the source
root, bounded depth and count).

XML is parsed only after DOCTYPE/ENTITY declarations are refused, so entity
expansion (billion laughs) and external entities cannot occur, and the
element count is capped while streaming.
"""
from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from typing import Mapping

from .model import (
    BuildModel,
    BuildSystem,
    BuildTarget,
    CompileUnit,
    Generator,
    ModelSource,
    PLATFORM_RE,
    PchMode,
    TargetType,
    bounded_notes,
    clip,
    finalize_model,
    norm_path,
    reject,
    safe_rel,
)
from .tool_targets import apply_safety, classify_targets


MAX_XML_BYTES = 4 * 1024 * 1024
MAX_XML_ELEMENTS = 200_000
MAX_SOLUTION_BYTES = 4 * 1024 * 1024
MAX_SOLUTION_PROJECTS = 512
MAX_IMPORT_DEPTH = 4
MAX_IMPORT_FILES = 64
SOLUTION_FOLDER_TYPE = "{2150E333-8FDC-42A3-9474-1A3956D46DE8}"
# MSBuild's ProjectInSolution.CleanseProjectName character set.
_CLEANSE_CHARS = frozenset("%$@;.()'")
_DECL_RE = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)
_XML_DECL_RE = re.compile(r"^\s*<\?xml[^>]{0,200}\?>")
_COND_PAIR_RE = re.compile(
    r"^\s*'\$\(Configuration\)\|\$\(Platform\)'\s*==\s*'([^'|]{1,64})\|([^']{1,64})'\s*$"
)
_COND_CONFIG_RE = re.compile(r"^\s*'\$\(Configuration\)'\s*==\s*'([^']{1,64})'\s*$")
_PROJECT_LINE_RE = re.compile(
    r'^Project\("(?P<type>\{[0-9A-Fa-f-]{36}\})"\)\s*=\s*"(?P<name>[^"]{1,256})"\s*,\s*'
    r'"(?P<path>[^"]{1,1024})"\s*,\s*"(?P<guid>\{[0-9A-Fa-f-]{36}\})"'
)
_NESTED_RE = re.compile(r"^\s*(\{[0-9A-Fa-f-]{36}\})\s*=\s*(\{[0-9A-Fa-f-]{36}\})\s*$")
_CONFIG_PLATFORM_RE = re.compile(r"^\s*([^|=]{1,64})\|([^=]{1,64}?)\s*=\s*")
_VS_VERSION_RE = re.compile(r"^#\s*Visual Studio Version\s+(\d+)", re.MULTILINE)
CONFIGURATION_TYPES = {
    "Application": TargetType.EXECUTABLE,
    "StaticLibrary": TargetType.STATIC_LIBRARY,
    "DynamicLibrary": TargetType.SHARED_LIBRARY,
    "Utility": TargetType.UTILITY,
    "Makefile": TargetType.MSBUILD_MAKEFILE,
}
_TRACKED_PROPS = frozenset({
    "ConfigurationType", "PlatformToolset", "PrecompiledHeader", "PrecompiledHeaderFile",
    "ForcedIncludeFiles", "LanguageStandard", "AdditionalIncludeDirectories",
    "PreprocessorDefinitions", "ExcludedFromBuild",
})


# --- XML ----------------------------------------------------------------------------

def _decode_xml(data: bytes, what: str) -> str:
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError as exc:
            raise reject("BUILD_TREE_REJECTED", "%s is not valid UTF-16" % what) from exc
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise reject("BUILD_TREE_REJECTED", "%s is not UTF-8" % what) from exc


def safe_xml_root(data: bytes, *, what: str = "project file", max_bytes: int = MAX_XML_BYTES,
                  max_elements: int = MAX_XML_ELEMENTS) -> ET.Element:
    """Parse bounded XML with DOCTYPE/ENTITY refused and an element cap."""
    if not isinstance(data, (bytes, bytearray)):
        raise reject("BUILD_TREE_REJECTED", "%s is not bytes" % what)
    if len(data) > max_bytes:
        raise reject("BUILD_TREE_REJECTED", "%s exceeds %d bytes" % (what, max_bytes))
    text = _decode_xml(bytes(data), what)
    if _DECL_RE.search(text):
        raise reject("BUILD_TREE_REJECTED", "%s declares a DOCTYPE or ENTITY" % what)
    text = _XML_DECL_RE.sub("", text, count=1)
    parser = ET.XMLPullParser(events=("start",))
    count = 0
    root: ET.Element | None = None
    try:
        for offset in range(0, len(text), 65536):
            parser.feed(text[offset:offset + 65536])
            for _event, element in parser.read_events():
                if root is None:
                    root = element
                count += 1
                if count > max_elements:
                    raise reject("BUILD_TREE_REJECTED",
                                 "%s exceeds %d elements" % (what, max_elements))
        parser.close()
        for _event, element in parser.read_events():
            if root is None:
                root = element
            count += 1
    except ET.ParseError as exc:
        raise reject("BUILD_TREE_REJECTED", "%s is not well-formed XML" % what) from exc
    if count > max_elements:
        raise reject("BUILD_TREE_REJECTED", "%s exceeds %d elements" % (what, max_elements))
    if root is None:
        raise reject("BUILD_TREE_REJECTED", "%s has no root element" % what)
    return root


def _local(tag: object) -> str:
    return str(tag).rsplit("}", 1)[-1]


def condition_key(condition: str | None) -> str | None:
    """``'Cfg|Plat'``, ``'Cfg|*'``, ``'*'`` (no condition) or None (unsupported)."""
    if condition is None or not condition.strip():
        return "*"
    match = _COND_PAIR_RE.match(condition)
    if match:
        return "%s|%s" % (match.group(1).strip(), match.group(2).strip())
    match = _COND_CONFIG_RE.match(condition)
    if match:
        return "%s|*" % match.group(1).strip()
    return None


# --- solution ---------------------------------------------------------------------

def cleanse_project_name(name: str) -> str:
    return "".join("_" if ch in _CLEANSE_CHARS else ch for ch in name)


def solution_target_name(name: str, folders: tuple[str, ...] = ()) -> str:
    """MSBuild's solution target name: cleansed folders and name joined by ``\\``."""
    return "\\".join([cleanse_project_name(item) for item in folders] + [cleanse_project_name(name)])


@dataclass(frozen=True, slots=True)
class SolutionProject:
    name: str
    path: str
    guid: str
    type_guid: str
    is_folder: bool
    folders: tuple[str, ...]
    target_name: str


@dataclass(frozen=True, slots=True)
class SolutionInfo:
    projects: tuple[SolutionProject, ...]
    configurations: tuple[tuple[str, str], ...]
    configs: tuple[str, ...]
    platforms: tuple[str, ...]
    vs_major: int
    truncated: bool
    notes: tuple[str, ...] = ()

    def buildable(self) -> tuple[SolutionProject, ...]:
        return tuple(item for item in self.projects if not item.is_folder)


def parse_solution(data: bytes) -> SolutionInfo:
    if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_SOLUTION_BYTES:
        raise reject("BUILD_TREE_REJECTED", "solution file exceeds %d bytes" % MAX_SOLUTION_BYTES)
    text = _decode_xml(bytes(data), "solution file")
    if "\x00" in text:
        raise reject("BUILD_TREE_REJECTED", "solution file contains NUL")
    raw_projects: list[dict] = []
    nested: dict[str, str] = {}
    configurations: list[tuple[str, str]] = []
    section = ""
    truncated = False
    notes: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Project("):
            match = _PROJECT_LINE_RE.match(stripped)
            if match is None:
                notes.append("unparsed solution project line")
                continue
            if len(raw_projects) >= MAX_SOLUTION_PROJECTS:
                truncated = True
                continue
            raw_projects.append(match.groupdict())
            continue
        if stripped.startswith("GlobalSection("):
            section = stripped[len("GlobalSection("):].split(")", 1)[0]
            continue
        if stripped == "EndGlobalSection":
            section = ""
            continue
        if section == "SolutionConfigurationPlatforms":
            match = _CONFIG_PLATFORM_RE.match(line)
            if match:
                pair = (match.group(1).strip(), match.group(2).strip())
                if pair not in configurations and len(configurations) < 256:
                    configurations.append(pair)
        elif section == "NestedProjects":
            match = _NESTED_RE.match(line)
            if match:
                nested[match.group(1).upper()] = match.group(2).upper()
    by_guid = {item["guid"].upper(): item for item in raw_projects}

    def folders_of(guid: str) -> tuple[str, ...]:
        chain: list[str] = []
        current = nested.get(guid.upper())
        seen: set[str] = set()
        while current and current not in seen and len(chain) < 16:
            seen.add(current)
            parent = by_guid.get(current)
            if parent is None:
                break
            chain.append(parent["name"])
            current = nested.get(current)
        return tuple(reversed(chain))

    projects: list[SolutionProject] = []
    for item in raw_projects:
        is_folder = item["type"].upper() == SOLUTION_FOLDER_TYPE
        folders = folders_of(item["guid"])
        path = "" if is_folder else norm_path(item["path"])
        projects.append(SolutionProject(
            name=item["name"], path=path, guid=item["guid"].upper(),
            type_guid=item["type"].upper(), is_folder=is_folder, folders=folders,
            target_name=solution_target_name(item["name"], folders),
        ))
    configs: list[str] = []
    platforms: list[str] = []
    for config, platform in configurations:
        if config not in configs:
            configs.append(config)
        if platform not in platforms and PLATFORM_RE.fullmatch(platform):
            platforms.append(platform)
    version = _VS_VERSION_RE.search(text)
    return SolutionInfo(
        projects=tuple(projects), configurations=tuple(configurations),
        configs=tuple(configs[:32]), platforms=tuple(platforms[:64]),
        vs_major=int(version.group(1)) if version else 0, truncated=truncated,
        notes=bounded_notes(notes),
    )


# --- vcxproj / props ----------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ClCompileItem:
    include: str
    metadata: tuple[tuple[str, str, str], ...]  # (condition key, property, value)


@dataclass(frozen=True, slots=True)
class ImportRef:
    project: str
    condition: str


@dataclass(frozen=True, slots=True)
class VcxprojInfo:
    label: str
    project_name: str
    guid: str
    configurations: tuple[tuple[str, str], ...]
    settings: tuple[tuple[str, str, str], ...]  # (condition key, property, value) in order
    cl_compile: tuple[ClCompileItem, ...]
    imports: tuple[ImportRef, ...]
    notes: tuple[str, ...] = ()

    def resolve(self, config: str, platform: str) -> dict[str, str]:
        return _resolve(self.settings, config, platform)


def _applies(key: str, config: str, platform: str) -> bool:
    if key == "*":
        return True
    cfg, _, plat = key.partition("|")
    return cfg == config and (plat in ("*", platform))


def _resolve(entries: tuple[tuple[str, str, str], ...], config: str, platform: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, prop, value in entries:
        if _applies(key, config, platform):
            out[prop] = value
    return out


def _parse_props_like(root: ET.Element, label: str) -> VcxprojInfo:
    configurations: list[tuple[str, str]] = []
    settings: list[tuple[str, str, str]] = []
    items: list[ClCompileItem] = []
    imports: list[ImportRef] = []
    notes: list[str] = []
    project_name = guid = ""
    unsupported = 0

    def walk(element: ET.Element, inherited: str) -> None:
        nonlocal project_name, guid, unsupported
        for child in element:
            name = _local(child.tag)
            key = condition_key(child.get("Condition"))
            if key is None:
                unsupported += 1
                continue
            if inherited != "*" and key == "*":
                key = inherited
            elif inherited != "*" and key != inherited:
                unsupported += 1
                continue
            if name == "Import":
                if child.get("Project") and len(imports) < MAX_IMPORT_FILES * 4:
                    imports.append(ImportRef(project=clip(child.get("Project"), 1024),
                                             condition=key))
            elif name == "ImportGroup":
                walk(child, key)
            elif name == "ItemGroup":
                for item in child:
                    item_name = _local(item.tag)
                    if item_name == "ProjectConfiguration":
                        include = item.get("Include") or ""
                        cfg, sep, plat = include.partition("|")
                        if sep and (cfg, plat) not in configurations and len(configurations) < 256:
                            configurations.append((clip(cfg, 64), clip(plat, 64)))
                        continue
                    if item_name != "ClCompile" or not item.get("Include"):
                        continue
                    if condition_key(item.get("Condition")) != "*":
                        unsupported += 1
                        continue
                    metadata: list[tuple[str, str, str]] = []
                    for meta in item:
                        meta_key = condition_key(meta.get("Condition"))
                        meta_name = _local(meta.tag)
                        if meta_key is None or meta_name not in _TRACKED_PROPS:
                            continue
                        metadata.append((meta_key, meta_name, clip(meta.text or "", 4096).strip()))
                    for include in (item.get("Include") or "").split(";")[:64]:
                        include = include.strip()
                        if include and len(items) < 50_000:
                            items.append(ClCompileItem(include=clip(include, 1024),
                                                       metadata=tuple(metadata[:32])))
            elif name == "PropertyGroup":
                for prop in child:
                    prop_key = condition_key(prop.get("Condition"))
                    prop_name = _local(prop.tag)
                    if prop_key is None:
                        unsupported += 1
                        continue
                    effective = key if prop_key == "*" else prop_key
                    value = clip(prop.text or "", 4096).strip()
                    if prop_name == "ProjectName" and value:
                        project_name = value
                    elif prop_name == "ProjectGuid" and value:
                        guid = value.upper()
                    elif prop_name in _TRACKED_PROPS:
                        settings.append((effective, prop_name, value))
            elif name == "ItemDefinitionGroup":
                for tool in child:
                    if _local(tool.tag) != "ClCompile":
                        continue
                    for prop in tool:
                        prop_key = condition_key(prop.get("Condition"))
                        prop_name = _local(prop.tag)
                        if prop_key is None or prop_name not in _TRACKED_PROPS:
                            continue
                        effective = key if prop_key == "*" else prop_key
                        settings.append((effective, prop_name, clip(prop.text or "", 4096).strip()))

    walk(root, "*")
    if unsupported:
        notes.append("%d MSBuild conditions were not evaluated" % unsupported)
    return VcxprojInfo(
        label=clip(label, 1024), project_name=clip(project_name, 256), guid=clip(guid, 64),
        configurations=tuple(configurations), settings=tuple(settings[:20_000]),
        cl_compile=tuple(items), imports=tuple(imports), notes=bounded_notes(notes),
    )


def parse_vcxproj(data: bytes, *, label: str) -> VcxprojInfo:
    root = safe_xml_root(data, what=label or "vcxproj")
    if _local(root.tag) != "Project":
        raise reject("BUILD_TREE_REJECTED", "%s is not an MSBuild project" % (label or "vcxproj"))
    return _parse_props_like(root, label)


def resolve_import_path(importer_label: str, raw: str) -> str | None:
    """Source-relative path of an ``Import``, or None when it needs evaluation."""
    text = str(raw or "").replace("\\", "/").strip()
    base = posixpath.dirname(norm_path(importer_label))
    for macro in ("$(MSBuildThisFileDirectory)", "$(MSBuildProjectDirectory)/",
                  "$(MSBuildProjectDirectory)"):
        if text.startswith(macro):
            text = text[len(macro):].lstrip("/")
            break
    if "$(" in text or "%(" in text or "@(" in text or "*" in text or not text:
        return None
    return safe_rel(posixpath.join(base, text) if base else text)


def merge_props(vcxproj: VcxprojInfo, imports: Mapping[str, bytes], *,
                max_depth: int = MAX_IMPORT_DEPTH,
                max_files: int = MAX_IMPORT_FILES) -> VcxprojInfo:
    """Overlay imported ``.props`` settings under the project's own settings.

    ``imports`` maps a source-relative path to bytes the adapter read inside
    the root. Imports are applied in document order (later wins) and the
    project's own settings win over every import, as MSBuild's property-sheet
    ordering does for the common layout.
    """
    followed: list[str] = []
    unresolved = 0
    missing = 0
    merged: list[tuple[str, str, str]] = []

    def visit(info: VcxprojInfo, depth: int, inherited: str) -> None:
        nonlocal unresolved, missing
        for ref in info.imports:
            if len(followed) >= max_files:
                return
            rel = resolve_import_path(info.label, ref.project)
            if rel is None:
                unresolved += 1
                continue
            if rel in followed:
                continue
            data = imports.get(rel)
            if data is None:
                missing += 1
                continue
            if depth >= max_depth:
                unresolved += 1
                continue
            followed.append(rel)
            child = _parse_props_like(safe_xml_root(data, what=rel), rel)
            condition = ref.condition if ref.condition != "*" else inherited
            visit(child, depth + 1, condition)
            for key, prop, value in child.settings:
                merged.append((condition if key == "*" else key, prop, value))

    visit(vcxproj, 0, "*")
    notes = list(vcxproj.notes)
    if followed:
        notes.append("followed %d property sheet imports" % len(followed))
    if unresolved:
        notes.append("%d imports need MSBuild evaluation and were not followed" % unresolved)
    if missing:
        notes.append("%d in-root imports were not available" % missing)
    return replace(vcxproj, settings=tuple(merged + list(vcxproj.settings))[:40_000],
                   notes=bounded_notes(notes))


# --- model --------------------------------------------------------------------------

def _pch_mode(value: str) -> PchMode:
    text = (value or "").strip().lower()
    if text == "use":
        return PchMode.USE
    if text == "create":
        return PchMode.CREATE
    return PchMode.NONE


def _std(value: str) -> str:
    text = (value or "").strip().lower()
    match = re.match(r"^stdcpp(\d+|latest)$", text)
    if match:
        return "c++" + match.group(1)
    match = re.match(r"^stdc(\d+)$", text)
    return ("c" + match.group(1)) if match else ""


def _list_count(value: str) -> int:
    return len([item for item in (value or "").split(";") if item.strip() and "%(" not in item])


def _generator(vs_major: int, toolsets: set[str]) -> Generator:
    if vs_major >= 17 or "v143" in toolsets:
        return Generator.VS2022
    if vs_major == 16 or "v142" in toolsets:
        return Generator.VS2019
    return Generator.OTHER


def model_from_msbuild(
    *,
    solution: bytes | None,
    solution_label: str,
    projects: Mapping[str, bytes],
    props: Mapping[str, bytes] | None = None,
    source_root: str,
    project_label: str,
    build_dir: str = "",
    created_at: float = 0.0,
) -> BuildModel:
    """A model from a solution (or bare vcxproj files) without evaluation.

    ``projects`` and ``props`` map source-relative paths to bytes. Project
    paths in the solution are resolved relative to the solution's directory.
    """
    notes: list[str] = []
    truncated = False
    entries: list[tuple[str, str, str, tuple[str, ...]]] = []  # (label, target, name, folders)
    info: SolutionInfo | None = None
    if solution is not None:
        info = parse_solution(solution)
        truncated = truncated or info.truncated
        notes.extend(info.notes)
        base = posixpath.dirname(norm_path(solution_label))
        for item in info.buildable():
            if not item.path.lower().endswith(".vcxproj"):
                continue
            rel = safe_rel(posixpath.join(base, item.path) if base else item.path)
            if rel is None:
                notes.append("solution project outside the root skipped: %s" % clip(item.name, 64))
                continue
            entries.append((rel, item.target_name, item.name, item.folders))
    else:
        for rel in sorted(projects)[:MAX_SOLUTION_PROJECTS]:
            name = posixpath.splitext(posixpath.basename(rel))[0]
            entries.append((rel, cleanse_project_name(name), name, ()))
    configs = list(info.configs) if info else []
    platforms = list(info.platforms) if info else []
    targets: list[BuildTarget] = []
    units: list[CompileUnit] = []
    toolsets: set[str] = set()
    for rel, target_name, name, folders in entries:
        data = projects.get(rel)
        if data is None:
            truncated = True
            notes.append("project file not available: %s" % clip(rel, 120))
            continue
        vcx = merge_props(parse_vcxproj(data, label=rel), props or {})
        notes.extend(vcx.notes)
        project_configs = [cfg for cfg, _ in vcx.configurations]
        if not info:
            for cfg, plat in vcx.configurations:
                if cfg not in configs:
                    configs.append(cfg)
                if plat not in platforms and PLATFORM_RE.fullmatch(plat):
                    platforms.append(plat)
        first = vcx.configurations[0] if vcx.configurations else ("", "")
        resolved = vcx.resolve(*first)
        toolset = resolved.get("PlatformToolset", "")
        if toolset:
            toolsets.add(toolset.lower())
        family = "clang_cl" if toolset.lower().startswith("clangcl") or toolset.lower() == "llvm" \
            else "msvc"
        kind = CONFIGURATION_TYPES.get(resolved.get("ConfigurationType", ""), TargetType.UNKNOWN)
        project_dir = posixpath.dirname(rel)
        pch_headers: list[str] = []
        count = 0
        for item in vcx.cl_compile:
            file_rel = safe_rel(posixpath.join(project_dir, item.include) if project_dir
                                else item.include)
            if file_rel is None:
                continue
            settings = dict(resolved)
            settings.update(_resolve(tuple(item.metadata), *first))
            if settings.get("ExcludedFromBuild", "").lower() == "true":
                continue
            mode = _pch_mode(settings.get("PrecompiledHeader", ""))
            header = settings.get("PrecompiledHeaderFile", "") if mode is not PchMode.NONE else ""
            header_rel = ""
            if header:
                header_rel = safe_rel(posixpath.join(project_dir, header.replace("\\", "/"))
                                      if project_dir else header.replace("\\", "/")) or ""
                if header_rel and header_rel not in pch_headers:
                    pch_headers.append(header_rel)
            forced = tuple(
                safe_rel(posixpath.join(project_dir, value.strip().replace("\\", "/")))
                or clip(value.strip(), 256)
                for value in settings.get("ForcedIncludeFiles", "").split(";")
                if value.strip() and "%(" not in value
            )[:16]
            if forced and mode is PchMode.NONE:
                mode = PchMode.FORCED_INCLUDE
            count += 1
            if len(units) >= 50_000:
                truncated = True
                continue
            units.append(CompileUnit(
                file_label=file_rel, file_rel=file_rel, target=target_name, config="",
                language="C" if file_rel.lower().endswith(".c") else "CXX", family=family,
                std=_std(settings.get("LanguageStandard", "")),
                define_count=_list_count(settings.get("PreprocessorDefinitions", "")),
                include_dir_count=_list_count(settings.get("AdditionalIncludeDirectories", "")),
                pch=mode, pch_header=header_rel, forced_includes=forced,
            ))
        targets.append(BuildTarget(
            name=target_name, type=kind, configs=tuple(dict.fromkeys(project_configs))[:32],
            source_count=count, folder="/".join(folders)[:1024], project=rel,
            pch_headers=tuple(pch_headers[:8]),
        ))
    model = BuildModel(
        project_label=clip(project_label, 1024) or "project", source_root=norm_path(source_root),
        build_dir=norm_path(build_dir), system=BuildSystem.MSBUILD,
        generator=_generator(info.vs_major if info else 0, toolsets), multi_config=True,
        configs=tuple(configs[:32]), platforms=tuple(platforms[:64]), targets=tuple(targets),
        units=tuple(units), source=ModelSource.MSBUILD, truncated=truncated,
        notes=tuple(notes), created_at=float(created_at),
    )
    safety = classify_targets(model)
    model = replace(model, targets=apply_safety(model, safety),
                    notes=bounded_notes(list(model.notes) + list(safety.notes)))
    return finalize_model(model)


__all__ = [
    "CONFIGURATION_TYPES", "ClCompileItem", "ImportRef", "MAX_XML_ELEMENTS", "SolutionInfo",
    "SolutionProject", "VcxprojInfo", "cleanse_project_name", "condition_key", "merge_props",
    "model_from_msbuild", "parse_solution", "parse_vcxproj", "resolve_import_path",
    "safe_xml_root", "solution_target_name",
]
