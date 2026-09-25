"""Pure build model: what a C/C++ build tree contains, without executing it.

A ``BuildModel`` is assembled by the pure parsers in this package (CMake File
API replies, ``compile_commands.json``, ``.sln``/``.vcxproj``) from bytes an
adapter read under guard. Nothing here opens a file, reads the environment or
the clock; ``created_at`` is supplied by the caller.

Wire rule: absolute paths never leave the model. ``source_root`` and
``build_dir`` are internal; every path shown on the wire is a label -- a path
relative to the source root, ``<build>/...`` for build-tree files, or
``<external>/<basename>`` for anything else.
"""
from __future__ import annotations

import hashlib
import json
import posixpath
import re
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Iterable

from ..common.errors import InvalidInput


MAX_TARGETS = 2000
MAX_UNITS = 50_000
MAX_TOOLCHAINS = 16
MAX_PRESETS = 256
MAX_NOTES = 32
MAX_NOTE_CHARS = 300
MAX_LABEL_CHARS = 1024
MAX_NAME_CHARS = 128
MAX_CONFIGS = 32
MAX_PLATFORMS = 64
MAX_DEPENDS = 64
MAX_ARTIFACTS = 16
MAX_PCH_HEADERS = 8
MAX_FORCED_INCLUDES = 16
MAX_PATH_SET = 20_000
MAX_JSON_DEPTH = 64
DEFAULT_VIEW_ITEMS = 100
MAX_VIEW_ITEMS = 500

TARGET_NAME_RE = re.compile(
    r"^[A-Za-z0-9_][A-Za-z0-9_.+-]{0,127}(?:\\[A-Za-z0-9_][A-Za-z0-9_.+-]{0,127}){0,8}$"
)
CONFIG_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
PLATFORM_RE = re.compile(r"^[A-Za-z0-9_.()+ -]{1,64}$")
PRESET_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:(?:/|$)")


class BuildDomainError(InvalidInput):
    """A refusal with one of the stable build error codes."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def reject(code: str, message: str) -> BuildDomainError:
    return BuildDomainError(code, message)


class BuildSystem(str, Enum):
    CMAKE = "cmake"
    NINJA = "ninja"
    MAKE = "make"
    MSBUILD = "msbuild"
    PROFILE = "profile"


class Generator(str, Enum):
    NINJA = "Ninja"
    NINJA_MULTI = "Ninja Multi-Config"
    UNIX_MAKEFILES = "Unix Makefiles"
    NMAKE = "NMake Makefiles"
    VS2022 = "Visual Studio 17 2022"
    VS2019 = "Visual Studio 16 2019"
    XCODE = "Xcode"
    OTHER = "other"


MULTI_CONFIG_GENERATORS = frozenset({
    Generator.NINJA_MULTI, Generator.VS2022, Generator.VS2019, Generator.XCODE,
})
VS_GENERATORS = frozenset({Generator.VS2022, Generator.VS2019})


def generator_from_name(name: str) -> Generator:
    text = str(name or "").strip()
    for item in Generator:
        if item.value == text:
            return item
    return Generator.OTHER


class ModelSource(str, Enum):
    FILE_API = "file_api"
    COMPILE_DB = "compile_db"
    MSBUILD = "msbuild"
    NINJA_FILE = "ninja_file"
    MAKEFILE = "makefile"
    NONE = "none"


class TargetType(str, Enum):
    EXECUTABLE = "EXECUTABLE"
    STATIC_LIBRARY = "STATIC_LIBRARY"
    SHARED_LIBRARY = "SHARED_LIBRARY"
    MODULE_LIBRARY = "MODULE_LIBRARY"
    OBJECT_LIBRARY = "OBJECT_LIBRARY"
    INTERFACE_LIBRARY = "INTERFACE_LIBRARY"
    UTILITY = "UTILITY"
    MSBUILD_MAKEFILE = "MSBUILD_MAKEFILE"
    UNKNOWN = "UNKNOWN"


LIBRARY_TYPES = frozenset({
    TargetType.STATIC_LIBRARY, TargetType.SHARED_LIBRARY, TargetType.MODULE_LIBRARY,
    TargetType.OBJECT_LIBRARY, TargetType.INTERFACE_LIBRARY,
})


def target_type_from_name(name: str) -> TargetType:
    text = str(name or "").strip().upper()
    for item in TargetType:
        if item.value == text:
            return item
    return TargetType.UNKNOWN


class PchMode(str, Enum):
    NONE = "none"
    CREATE = "create"
    USE = "use"
    FORCED_INCLUDE = "forced_include"


class BuildAction(str, Enum):
    CONFIGURE = "configure"
    BUILD = "build"
    COMPILE_ONE = "compile_one"
    INCLUDE_TRACE = "include_trace"


class BuildWorld(str, Enum):
    HOST = "host"
    CONTAINER = "container"


class NetworkPolicy(str, Enum):
    ENFORCED_OFF = "enforced_off"
    ADVISORY_OFF = "advisory_off"
    ALLOWED = "allowed"


# EXEC-006 labels a build receipt may carry (application.execution's
# IsolationTruth values; a build never claims a verified security boundary).
ISOLATION_UNVERIFIED = "unverified"
ISOLATION_FAILURE_ONLY = "failure_isolation_only"
ISOLATION_LABELS = frozenset({ISOLATION_UNVERIFIED, ISOLATION_FAILURE_ONLY})

COMPILER_FAMILIES = frozenset({"gnu", "clang", "msvc", "clang_cl", "other"})


# --- validation helpers ---------------------------------------------------------

def check_text(value: object, name: str, *, limit: int, required: bool = False) -> str:
    """Validate one string field: a str, no NUL/CR/LF, bounded, non-empty if required."""
    if not isinstance(value, str):
        raise reject("BUILD_MODEL_UNAVAILABLE", "%s must be a string" % name)
    if "\x00" in value or "\n" in value or "\r" in value:
        raise reject("BUILD_MODEL_UNAVAILABLE", "%s contains control characters" % name)
    if len(value) > limit:
        raise reject("BUILD_MODEL_UNAVAILABLE", "%s exceeds %d characters" % (name, limit))
    if required and not value.strip():
        raise reject("BUILD_MODEL_UNAVAILABLE", "%s is required" % name)
    return value


def check_texts(values: object, name: str, *, limit: int, max_items: int) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise reject("BUILD_MODEL_UNAVAILABLE", "%s must be a tuple" % name)
    if len(values) > max_items:
        raise reject("BUILD_MODEL_UNAVAILABLE", "%s exceeds %d items" % (name, max_items))
    for item in values:
        check_text(item, name, limit=limit)
    return values


def clip(value: object, limit: int) -> str:
    """A single printable line, capped; used for host-derived text."""
    text = str(value if value is not None else "")
    text = text.replace("\x00", "").replace("\r", " ").replace("\n", " ")
    return text[: max(0, int(limit))]


def bounded_notes(notes: Iterable[object]) -> tuple[str, ...]:
    out: list[str] = []
    for note in notes:
        text = clip(note, MAX_NOTE_CHARS).strip()
        if text and text not in out:
            out.append(text)
        if len(out) >= MAX_NOTES:
            break
    return tuple(out)


# --- bounded JSON ---------------------------------------------------------------

_JSON_STRING_RE = re.compile(r'"(?:[^"\\\n]|\\.)*"')
_JSON_BRACKET_RE = re.compile(r"[\[\]{}]")


def json_depth(text: str) -> int:
    """Maximum bracket nesting of a JSON text, strings ignored (linear)."""
    stripped = _JSON_STRING_RE.sub('""', text)
    depth = deepest = 0
    for match in _JSON_BRACKET_RE.finditer(stripped):
        if match.group() in "[{":
            depth += 1
            deepest = max(deepest, depth)
        else:
            depth -= 1
    return deepest


def loads_bounded_json(data: bytes | str, *, max_bytes: int, what: str,
                       max_depth: int = MAX_JSON_DEPTH,
                       code: str = "BUILD_TREE_REJECTED") -> object:
    """Parse JSON after size and nesting checks; refusals carry ``code``."""
    if isinstance(data, str):
        raw = data.encode("utf-8", errors="replace")
    elif isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
    else:
        raise reject(code, "%s is not bytes" % what)
    if len(raw) > max_bytes:
        raise reject(code, "%s exceeds %d bytes" % (what, max_bytes))
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise reject(code, "%s is not UTF-8" % what) from exc
    if json_depth(text) > max_depth:
        raise reject(code, "%s nests deeper than %d" % (what, max_depth))
    try:
        return json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise reject(code, "%s is not valid JSON" % what) from exc


# --- paths ------------------------------------------------------------------------

def norm_path(path: object) -> str:
    """Lexically normalized ``/``-separated path; Windows drive/UNC preserved."""
    text = str(path or "").replace("\\", "/")
    if not text:
        return ""
    unc = text.startswith("//")
    text = posixpath.normpath(text)
    if unc and not text.startswith("//"):
        text = "/" + text
    if _DRIVE_RE.match(text):
        text = text[0].upper() + text[1:]
    return text


def is_absolute(path: str) -> bool:
    text = str(path or "").replace("\\", "/")
    return text.startswith("/") or bool(_DRIVE_RE.match(text))


def _case_fold_needed(path: str) -> bool:
    return bool(_DRIVE_RE.match(path)) or path.startswith("//")


def rel_under(path: str, root: str) -> str | None:
    """``path`` relative to ``root`` ('.' for equal), or None when outside.

    Both are normalized; Windows-shaped paths compare case-insensitively.
    """
    if not root:
        return None
    p = norm_path(path)
    r = norm_path(root)
    if not p or not r:
        return None
    cp, cr = (p.casefold(), r.casefold()) if _case_fold_needed(r) else (p, r)
    if cp == cr:
        return "."
    prefix = cr if cr.endswith("/") else cr + "/"
    if cp.startswith(prefix):
        return p[len(prefix):]
    return None


def safe_rel(rel: object) -> str | None:
    """A normalized relative path that stays inside its root, else None."""
    if not isinstance(rel, str) or not rel or "\x00" in rel:
        return None
    text = rel.replace("\\", "/")
    if is_absolute(text) or text.startswith("//"):
        return None
    normalized = posixpath.normpath(text)
    if normalized in (".", "") or normalized == ".." or normalized.startswith("../"):
        return None
    return normalized


def path_label(path: str, *, source_root: str, build_dir: str) -> tuple[str, str]:
    """(label, file_rel) for a path: rel-to-source, ``<build>/..``, or external."""
    text = str(path or "")
    if not text:
        return "", ""
    if not is_absolute(text):
        rel = safe_rel(text)
        return (rel or "<external>/" + posixpath.basename(norm_path(text))), (rel or "")
    in_build = rel_under(text, build_dir) if build_dir else None
    if in_build is not None:
        return ("<build>" if in_build == "." else "<build>/" + in_build), ""
    in_source = rel_under(text, source_root) if source_root else None
    if in_source is not None and in_source != ".":
        return in_source, in_source
    return "<external>/" + posixpath.basename(norm_path(text)), ""


# --- model types ------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Toolchain:
    language: str
    compiler_id: str
    version: str
    path_label: str
    target_arch: str = ""
    msvc_toolset: str = ""
    sysroot_label: str = ""

    def __post_init__(self) -> None:
        check_text(self.language, "language", limit=32, required=True)
        check_text(self.compiler_id, "compiler_id", limit=64)
        check_text(self.version, "version", limit=64)
        check_text(self.path_label, "path_label", limit=MAX_LABEL_CHARS)
        check_text(self.target_arch, "target_arch", limit=64)
        check_text(self.msvc_toolset, "msvc_toolset", limit=64)
        check_text(self.sysroot_label, "sysroot_label", limit=MAX_LABEL_CHARS)

    @property
    def family(self) -> str:
        return compiler_family_from_id(self.compiler_id)


def compiler_family_from_id(compiler_id: str, simulate_id: str = "") -> str:
    text = str(compiler_id or "").strip()
    if text == "MSVC":
        return "msvc"
    if text in ("Clang", "AppleClang", "IntelLLVM"):
        return "clang_cl" if simulate_id == "MSVC" else "clang"
    if text == "GNU":
        return "gnu"
    return "other"


@dataclass(frozen=True, slots=True)
class BuildTarget:
    name: str
    type: TargetType
    configs: tuple[str, ...] = ()
    source_count: int = 0
    artifacts: tuple[str, ...] = ()
    folder: str = ""
    project: str = ""
    depends: tuple[str, ...] = ()
    utility: bool = False
    build_time_tool: bool = False
    unity: bool = False
    pch_headers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        check_text(self.name, "target name", limit=MAX_NAME_CHARS * 2, required=True)
        if not TARGET_NAME_RE.fullmatch(self.name):
            raise reject("BUILD_MODEL_UNAVAILABLE", "invalid target name")
        if not isinstance(self.type, TargetType):
            raise reject("BUILD_MODEL_UNAVAILABLE", "target type must be TargetType")
        check_texts(self.configs, "configs", limit=64, max_items=MAX_CONFIGS)
        if type(self.source_count) is not int or self.source_count < 0:
            raise reject("BUILD_MODEL_UNAVAILABLE", "source_count must be >= 0")
        check_texts(self.artifacts, "artifacts", limit=MAX_LABEL_CHARS, max_items=MAX_ARTIFACTS)
        check_text(self.folder, "folder", limit=MAX_LABEL_CHARS)
        check_text(self.project, "project", limit=MAX_LABEL_CHARS)
        check_texts(self.depends, "depends", limit=MAX_NAME_CHARS * 2, max_items=MAX_DEPENDS)
        check_texts(self.pch_headers, "pch_headers", limit=MAX_LABEL_CHARS,
                    max_items=MAX_PCH_HEADERS)
        for flag in ("utility", "build_time_tool", "unity"):
            if type(getattr(self, flag)) is not bool:
                raise reject("BUILD_MODEL_UNAVAILABLE", "%s must be boolean" % flag)


@dataclass(frozen=True, slots=True)
class CompileUnit:
    file_label: str
    file_rel: str
    target: str = ""
    config: str = ""
    language: str = "CXX"
    family: str = "other"
    std: str = ""
    define_count: int = 0
    include_dir_count: int = 0
    pch: PchMode = PchMode.NONE
    pch_header: str = ""
    forced_includes: tuple[str, ...] = ()
    unity_blob_rel: str = ""
    flags_digest: str = ""

    def __post_init__(self) -> None:
        check_text(self.file_label, "file_label", limit=MAX_LABEL_CHARS, required=True)
        check_text(self.file_rel, "file_rel", limit=MAX_LABEL_CHARS)
        check_text(self.target, "target", limit=MAX_NAME_CHARS * 2)
        check_text(self.config, "config", limit=64)
        check_text(self.language, "language", limit=32)
        if self.family not in COMPILER_FAMILIES:
            raise reject("BUILD_MODEL_UNAVAILABLE", "unknown compiler family")
        check_text(self.std, "std", limit=32)
        for name in ("define_count", "include_dir_count"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise reject("BUILD_MODEL_UNAVAILABLE", "%s must be >= 0" % name)
        if not isinstance(self.pch, PchMode):
            raise reject("BUILD_MODEL_UNAVAILABLE", "pch must be PchMode")
        check_text(self.pch_header, "pch_header", limit=MAX_LABEL_CHARS)
        check_texts(self.forced_includes, "forced_includes", limit=MAX_LABEL_CHARS,
                    max_items=MAX_FORCED_INCLUDES)
        check_text(self.unity_blob_rel, "unity_blob_rel", limit=MAX_LABEL_CHARS)
        check_text(self.flags_digest, "flags_digest", limit=64)


@dataclass(frozen=True, slots=True)
class PresetInfo:
    name: str
    kind: str
    generator: str = ""
    binary_dir_label: str = ""
    hidden: bool = False
    binary_dir_resolvable: bool = True
    source_file_label: str = ""
    configure_preset: str = ""
    configuration: str = ""
    # Internal only (never on the wire): the resolved absolute binary dir.
    binary_dir: str = field(default="", repr=False)
    cache: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        check_text(self.name, "preset", limit=MAX_NAME_CHARS, required=True)
        if not PRESET_RE.fullmatch(self.name):
            raise reject("BUILD_MODEL_UNAVAILABLE", "invalid preset name")
        if self.kind not in ("configure", "build"):
            raise reject("BUILD_MODEL_UNAVAILABLE", "preset kind must be configure or build")
        check_text(self.generator, "generator", limit=64)
        check_text(self.binary_dir_label, "binary_dir_label", limit=MAX_LABEL_CHARS)
        check_text(self.source_file_label, "source_file_label", limit=MAX_LABEL_CHARS)
        check_text(self.configure_preset, "configure_preset", limit=MAX_NAME_CHARS)
        check_text(self.configuration, "configuration", limit=64)
        check_text(self.binary_dir, "binary_dir", limit=4096)
        for flag in ("hidden", "binary_dir_resolvable"):
            if type(getattr(self, flag)) is not bool:
                raise reject("BUILD_MODEL_UNAVAILABLE", "%s must be boolean" % flag)
        if not isinstance(self.cache, tuple) or len(self.cache) > 32:
            raise reject("BUILD_MODEL_UNAVAILABLE", "preset cache is bounded")


@dataclass(frozen=True, slots=True)
class BuildModel:
    project_label: str
    source_root: str
    build_dir: str
    system: BuildSystem
    generator: Generator = Generator.OTHER
    multi_config: bool = False
    configs: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ()
    targets: tuple[BuildTarget, ...] = ()
    toolchains: tuple[Toolchain, ...] = ()
    units: tuple[CompileUnit, ...] = ()
    presets: tuple[PresetInfo, ...] = ()
    source: ModelSource = ModelSource.NONE
    reply_client: str = ""
    compile_db_available: bool = False
    file_api_available: bool = False
    cmake_version: str = ""
    truncated: bool = False
    notes: tuple[str, ...] = ()
    digest: str = ""
    created_at: float = 0.0
    # Source-relative files the build treats as generated or as build inputs
    # (cmakeFiles-v1 inputs); both feed the edit-scope exclusions.
    generated_rel: tuple[str, ...] = ()
    build_inputs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        check_text(self.project_label, "project_label", limit=MAX_LABEL_CHARS, required=True)
        check_text(self.source_root, "source_root", limit=4096)
        check_text(self.build_dir, "build_dir", limit=4096)
        if not isinstance(self.system, BuildSystem):
            raise reject("BUILD_MODEL_UNAVAILABLE", "system must be BuildSystem")
        if not isinstance(self.generator, Generator):
            raise reject("BUILD_MODEL_UNAVAILABLE", "generator must be Generator")
        if not isinstance(self.source, ModelSource):
            raise reject("BUILD_MODEL_UNAVAILABLE", "source must be ModelSource")
        check_texts(self.configs, "configs", limit=64, max_items=MAX_CONFIGS)
        check_texts(self.platforms, "platforms", limit=64, max_items=MAX_PLATFORMS)
        for name, limit, kind in (("targets", MAX_TARGETS, BuildTarget),
                                  ("toolchains", MAX_TOOLCHAINS, Toolchain),
                                  ("units", MAX_UNITS, CompileUnit),
                                  ("presets", MAX_PRESETS, PresetInfo)):
            value = getattr(self, name)
            if not isinstance(value, tuple) or len(value) > limit:
                raise reject("BUILD_MODEL_UNAVAILABLE", "%s exceeds its bound" % name)
            if any(not isinstance(item, kind) for item in value):
                raise reject("BUILD_MODEL_UNAVAILABLE", "%s has a foreign item" % name)
        check_text(self.reply_client, "reply_client", limit=128)
        check_text(self.cmake_version, "cmake_version", limit=64)
        check_texts(self.notes, "notes", limit=MAX_NOTE_CHARS, max_items=MAX_NOTES)
        check_text(self.digest, "digest", limit=64)
        check_texts(self.generated_rel, "generated_rel", limit=MAX_LABEL_CHARS,
                    max_items=MAX_PATH_SET)
        check_texts(self.build_inputs, "build_inputs", limit=MAX_LABEL_CHARS,
                    max_items=MAX_PATH_SET)
        for flag in ("multi_config", "compile_db_available", "file_api_available", "truncated"):
            if type(getattr(self, flag)) is not bool:
                raise reject("BUILD_MODEL_UNAVAILABLE", "%s must be boolean" % flag)
        if type(self.created_at) not in (int, float):
            raise reject("BUILD_MODEL_UNAVAILABLE", "created_at must be a number")

    def target(self, name: str) -> BuildTarget | None:
        for item in self.targets:
            if item.name == name:
                return item
        return None

    def units_for(self, file_rel: str) -> tuple[CompileUnit, ...]:
        return tuple(unit for unit in self.units if unit.file_rel == file_rel)


def finalize_model(model: BuildModel) -> BuildModel:
    """Return ``model`` with its notes bounded and its digest computed."""
    model = replace(model, notes=bounded_notes(model.notes), digest="")
    return replace(model, digest=model_digest(model))


# --- wire -------------------------------------------------------------------------

def _target_wire(target: BuildTarget) -> dict:
    return {
        "name": target.name,
        "type": target.type.value,
        "configs": list(target.configs),
        "source_count": target.source_count,
        "artifacts": list(target.artifacts),
        "folder": target.folder,
        "project": target.project,
        "depends": list(target.depends),
        "utility": target.utility,
        "build_time_tool": target.build_time_tool,
        "unity": target.unity,
        "pch_headers": list(target.pch_headers),
    }


def _unit_wire(unit: CompileUnit) -> dict:
    return {
        "file": unit.file_label,
        "file_rel": unit.file_rel,
        "target": unit.target,
        "config": unit.config,
        "language": unit.language,
        "family": unit.family,
        "std": unit.std,
        "define_count": unit.define_count,
        "include_dir_count": unit.include_dir_count,
        "pch": unit.pch.value,
        "pch_header": unit.pch_header,
        "forced_includes": list(unit.forced_includes),
        "unity_blob": unit.unity_blob_rel,
        "flags_digest": unit.flags_digest,
    }


def _toolchain_wire(toolchain: Toolchain) -> dict:
    return {
        "language": toolchain.language,
        "compiler_id": toolchain.compiler_id,
        "family": toolchain.family,
        "version": toolchain.version,
        "path": toolchain.path_label,
        "target_arch": toolchain.target_arch,
        "msvc_toolset": toolchain.msvc_toolset,
        "sysroot": toolchain.sysroot_label,
    }


def _preset_wire(preset: PresetInfo) -> dict:
    return {
        "name": preset.name,
        "kind": preset.kind,
        "generator": preset.generator,
        "binary_dir": preset.binary_dir_label,
        "hidden": preset.hidden,
        "binary_dir_resolvable": preset.binary_dir_resolvable,
        "source_file": preset.source_file_label,
        "configure_preset": preset.configure_preset,
        "configuration": preset.configuration,
    }


def _canonical_wire(model: BuildModel) -> dict:
    return {
        "project": model.project_label,
        "system": model.system.value,
        "generator": model.generator.value,
        "multi_config": model.multi_config,
        "configs": list(model.configs),
        "platforms": list(model.platforms),
        "targets": [_target_wire(item) for item in model.targets],
        "toolchains": [_toolchain_wire(item) for item in model.toolchains],
        "units": [_unit_wire(item) for item in model.units],
        "presets": [_preset_wire(item) for item in model.presets],
        "source": model.source.value,
        "reply_client": model.reply_client,
        "compile_db_available": model.compile_db_available,
        "file_api_available": model.file_api_available,
        "cmake_version": model.cmake_version,
        "truncated": model.truncated,
        "notes": list(model.notes),
        "generated": list(model.generated_rel),
        "build_inputs": list(model.build_inputs),
    }


def model_digest(model: BuildModel) -> str:
    """sha256 of the canonical label-only wire form (``created_at`` excluded)."""
    payload = json.dumps(_canonical_wire(model), sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


VIEW_DETAILS = ("summary", "targets", "compile_units", "toolchain", "presets")


@dataclass(frozen=True, slots=True)
class BuildView:
    detail: str
    model: BuildModel
    items: tuple
    total: int
    truncated: bool


def build_view(model: BuildModel, *, detail: str = "summary", target: str = "",
               max_items: int = DEFAULT_VIEW_ITEMS) -> BuildView:
    """A bounded slice of the model for one ``build_model`` detail level."""
    if detail not in VIEW_DETAILS:
        raise reject("BUILD_MODEL_UNAVAILABLE", "unknown detail %r" % clip(detail, 32))
    try:
        limit = int(max_items)
    except (TypeError, ValueError):
        limit = DEFAULT_VIEW_ITEMS
    limit = max(1, min(limit, MAX_VIEW_ITEMS))
    if target and model.target(target) is None:
        raise reject("UNKNOWN_TARGET", "target is not in the build model")
    if detail == "targets":
        pool = tuple(item for item in model.targets if not target or item.name == target)
    elif detail == "compile_units":
        pool = tuple(item for item in model.units if not target or item.target == target)
    elif detail == "toolchain":
        pool = model.toolchains
    elif detail == "presets":
        pool = model.presets
    else:
        pool = ()
    return BuildView(detail=detail, model=model, items=pool[:limit], total=len(pool),
                     truncated=len(pool) > limit)


def _summary_wire(model: BuildModel) -> dict:
    counts: dict[str, int] = {}
    for item in model.targets:
        counts[item.type.value] = counts.get(item.type.value, 0) + 1
    return {
        "object": "build_model",
        "project": model.project_label,
        "system": model.system.value,
        "generator": model.generator.value,
        "multi_config": model.multi_config,
        "configs": list(model.configs),
        "platforms": list(model.platforms),
        "source": model.source.value,
        "reply_client": model.reply_client,
        "compile_db_available": model.compile_db_available,
        "file_api_available": model.file_api_available,
        "cmake_version": model.cmake_version,
        "target_count": len(model.targets),
        "target_types": counts,
        "utility_targets": [item.name for item in model.targets if item.utility][:32],
        "build_time_tools": [item.name for item in model.targets if item.build_time_tool][:32],
        "unit_count": len(model.units),
        "toolchains": [_toolchain_wire(item) for item in model.toolchains],
        "presets": [item.name for item in model.presets][:64],
        "truncated": model.truncated,
        "notes": list(model.notes),
        "digest": model.digest,
    }


def model_to_wire(model_or_view: BuildModel | BuildView) -> dict:
    """Label-only wire form; absolute roots never appear."""
    if isinstance(model_or_view, BuildModel):
        return _summary_wire(model_or_view)
    if not isinstance(model_or_view, BuildView):
        raise reject("BUILD_MODEL_UNAVAILABLE", "not a build model or view")
    view = model_or_view
    wire = _summary_wire(view.model)
    wire["detail"] = view.detail
    if view.detail != "summary":
        encoder = {
            "targets": _target_wire, "compile_units": _unit_wire,
            "toolchain": _toolchain_wire, "presets": _preset_wire,
        }[view.detail]
        wire["items"] = [encoder(item) for item in view.items]
        wire["total"] = view.total
        wire["items_truncated"] = view.truncated
    return wire


def build_context_summary(model: BuildModel, *, max_chars: int = 600) -> str:
    """One compact line for the model-context brief."""
    limit = max(80, min(int(max_chars), 2000))
    tools = [item.name for item in model.targets if item.build_time_tool]
    utility = [item.name for item in model.targets if item.utility]
    buildable = [item.name for item in model.targets if not item.utility]
    parts = [
        "build %s: %s/%s via %s" % (model.project_label, model.system.value,
                                     model.generator.value, model.source.value),
    ]
    if model.configs:
        parts.append("configs=" + ",".join(model.configs[:6]))
    if model.platforms:
        parts.append("platforms=" + ",".join(model.platforms[:4]))
    if buildable:
        parts.append("targets=" + ",".join(buildable[:12]))
    if tools:
        parts.append("build-time tools=" + ",".join(tools[:6]))
    if utility:
        parts.append("utility(refused)=" + ",".join(utility[:6]))
    families = sorted({item.family for item in model.toolchains})
    if families:
        parts.append("compilers=" + ",".join(families))
    pch = [item.name for item in model.targets if item.pch_headers]
    if pch:
        parts.append("pch=" + ",".join(pch[:4]))
    if model.truncated:
        parts.append("truncated")
    text = "; ".join(parts)
    return text if len(text) <= limit else text[: limit - 3] + "..."


__all__ = [
    "BuildAction", "BuildDomainError", "BuildModel", "BuildSystem", "BuildTarget",
    "BuildView", "BuildWorld", "CONFIG_RE", "CompileUnit", "Generator",
    "ISOLATION_FAILURE_ONLY", "ISOLATION_LABELS", "ISOLATION_UNVERIFIED",
    "LIBRARY_TYPES", "MULTI_CONFIG_GENERATORS", "ModelSource", "NetworkPolicy",
    "PLATFORM_RE", "PRESET_RE", "PchMode", "PresetInfo", "TARGET_NAME_RE",
    "TargetType", "Toolchain", "VIEW_DETAILS", "VS_GENERATORS",
    "build_context_summary", "build_view", "bounded_notes", "check_text",
    "clip", "compiler_family_from_id", "finalize_model", "generator_from_name",
    "is_absolute", "json_depth", "loads_bounded_json", "model_digest",
    "model_to_wire", "norm_path", "path_label", "rel_under", "reject",
    "safe_rel", "target_type_from_name",
]
