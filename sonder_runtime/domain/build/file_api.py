"""Pure parsers for CMake File API replies (any client's).

The adapter lists ``<build>/.cmake/api/v1/reply/``, reads the newest
``index-*.json`` and then only the reply files that index (and the codemodel
it names) reference. Every object is size- and depth-bounded before parsing.
Nothing here writes: the only File API write is ``query_document()``'s fixed
bytes, which the adapter writes inside an approved configure.

Reading any client's codemodel matters: trees configured by Visual Studio,
CLion or VS Code share ``reply/``, so a model is available without a Sonder
configure.
"""
from __future__ import annotations

import hashlib
import posixpath
import re
from dataclasses import dataclass, replace
from typing import Iterable, Mapping

from .compile_db import SOURCE_SUFFIXES, compiler_name
from .model import (
    BuildModel,
    BuildSystem,
    BuildTarget,
    CompileUnit,
    ModelSource,
    PchMode,
    PresetInfo,
    Toolchain,
    bounded_notes,
    clip,
    compiler_family_from_id,
    finalize_model,
    generator_from_name,
    is_absolute,
    loads_bounded_json,
    MAX_TARGETS,
    MAX_UNITS,
    norm_path,
    path_label,
    reject,
    rel_under,
    target_type_from_name,
    TARGET_NAME_RE,
)
from .tool_targets import apply_safety, classify_targets


MAX_INDEX_BYTES = 1024 * 1024
MAX_OBJECT_BYTES = 8 * 1024 * 1024
MAX_REPLY_FILES = 4096
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_UNITY_BLOB_BYTES = 1024 * 1024
SONDER_CLIENT = "client-sonder"
INDEX_NAME_RE = re.compile(r"^index-[0-9A-Za-z.:_-]{1,64}\.json$")
REPLY_NAME_RE = re.compile(r"^[A-Za-z0-9_.@+-]{1,240}\.json$")
_UNITY_BLOB_RE = re.compile(r"/Unity/unity_\d+_(?:c|cxx|cu|objc|objcxx)\.(?:c|cxx|cu|m|mm)$")
_UNITY_INCLUDE_RE = re.compile(r'^\s*#\s*include\s+"([^"\n]{1,4096})"', re.MULTILINE)
CACHE_ALLOWLIST = frozenset({
    "CMAKE_BUILD_TYPE", "CMAKE_CONFIGURATION_TYPES", "CMAKE_GENERATOR",
    "CMAKE_GENERATOR_PLATFORM", "CMAKE_GENERATOR_TOOLSET", "CMAKE_GENERATOR_INSTANCE",
    "CMAKE_C_COMPILER_LAUNCHER", "CMAKE_CXX_COMPILER_LAUNCHER", "CMAKE_CUDA_COMPILER_LAUNCHER",
    "CMAKE_UNITY_BUILD", "CMAKE_EXPORT_COMPILE_COMMANDS", "CMAKE_PROJECT_NAME",
    "CMAKE_C_COMPILER", "CMAKE_CXX_COMPILER", "CMAKE_MAKE_PROGRAM", "CMAKE_TOOLCHAIN_FILE",
    "VCPKG_MANIFEST_INSTALL", "VCPKG_TARGET_TRIPLET", "FETCHCONTENT_FULLY_DISCONNECTED",
    "FETCHCONTENT_UPDATES_DISCONNECTED", "CMAKE_HOME_DIRECTORY",
})
MAX_CACHE_TEXT_BYTES = 4 * 1024 * 1024


def query_document() -> bytes:
    """The fixed stateful query Sonder writes into ``query/client-sonder/``."""
    return (b'{"requests":[{"kind":"codemodel","version":2},{"kind":"cache","version":2},'
            b'{"kind":"toolchains","version":1},{"kind":"cmakeFiles","version":1}]}\n')


@dataclass(frozen=True, slots=True)
class ReplyObject:
    kind: str
    major: int
    minor: int
    json_file: str
    client: str = ""


@dataclass(frozen=True, slots=True)
class ReplyIndex:
    name: str
    cmake_version: str
    generator: str
    multi_config: bool
    objects: tuple[ReplyObject, ...]
    clients: tuple[str, ...]
    notes: tuple[str, ...] = ()

    def best(self, kind: str, major: int) -> ReplyObject | None:
        """Highest minor of ``kind``/``major`` over every client; Sonder's wins ties."""
        candidates = [item for item in self.objects if item.kind == kind and item.major == major]
        if not candidates:
            return None
        return sorted(candidates, key=lambda item: (
            -item.minor,
            0 if item.client == SONDER_CLIENT else (2 if not item.client else 1),
            item.client,
        ))[0]

    def files(self) -> tuple[str, ...]:
        seen: list[str] = []
        for item in self.objects:
            if item.json_file not in seen:
                seen.append(item.json_file)
        return tuple(seen)


def newest_index_name(names: Iterable[str]) -> str | None:
    """The newest ``index-*.json`` (CMake names sort chronologically)."""
    candidates = [name for name in names if isinstance(name, str) and INDEX_NAME_RE.fullmatch(name)]
    return max(candidates) if candidates else None


def _object(raw: object, client: str) -> ReplyObject | None:
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    json_file = raw.get("jsonFile")
    version = raw.get("version")
    if not isinstance(kind, str) or not isinstance(json_file, str) or not isinstance(version, dict):
        return None
    if not REPLY_NAME_RE.fullmatch(json_file) or len(kind) > 32:
        return None
    major, minor = version.get("major"), version.get("minor")
    if type(major) is not int or type(minor) is not int:
        return None
    return ReplyObject(kind=kind, major=major, minor=minor, json_file=json_file, client=client)


def parse_reply_index(data: bytes, *, name: str = "") -> ReplyIndex:
    document = loads_bounded_json(data, max_bytes=MAX_INDEX_BYTES, what="File API index")
    if not isinstance(document, dict):
        raise reject("BUILD_TREE_REJECTED", "File API index is not an object")
    cmake = document.get("cmake") if isinstance(document.get("cmake"), dict) else {}
    version = cmake.get("version") if isinstance(cmake.get("version"), dict) else {}
    generator = cmake.get("generator") if isinstance(cmake.get("generator"), dict) else {}
    objects: list[ReplyObject] = []
    notes: list[str] = []
    for raw in document.get("objects") or ():
        item = _object(raw, "")
        if item is not None:
            objects.append(item)
    clients: list[str] = []
    reply = document.get("reply") if isinstance(document.get("reply"), dict) else {}
    for client, body in list(reply.items())[:64]:
        if not isinstance(client, str) or not client.startswith("client-") or len(client) > 128:
            continue
        clients.append(client)
        responses: list[object] = []
        if isinstance(body, dict):
            for key, value in list(body.items())[:64]:
                if key == "query.json" and isinstance(value, dict):
                    responses.extend(value.get("responses") or ())
                elif isinstance(value, dict) and "jsonFile" in value:
                    responses.append(value)
        for raw in responses[:64]:
            item = _object(raw, client)
            if item is not None:
                objects.append(item)
    if len(objects) > MAX_REPLY_FILES:
        raise reject("BUILD_TREE_REJECTED", "File API index lists too many objects")
    return ReplyIndex(
        name=clip(name, 128), cmake_version=clip(version.get("string") or "", 64),
        generator=clip(generator.get("name") or "", 64),
        multi_config=bool(generator.get("multiConfig")),
        objects=tuple(objects), clients=tuple(clients), notes=bounded_notes(notes),
    )


def _version_tuple(text: str) -> tuple[int, ...]:
    parts = []
    for piece in str(text or "").split(".")[:3]:
        digits = re.match(r"\d+", piece)
        parts.append(int(digits.group()) if digits else 0)
    return tuple(parts)


# --- individual objects -------------------------------------------------------------

def parse_cache_v2(data: bytes) -> dict[str, str]:
    """Allowlisted cache entries only."""
    document = loads_bounded_json(data, max_bytes=MAX_OBJECT_BYTES, what="cache-v2 reply")
    entries = document.get("entries") if isinstance(document, dict) else None
    out: dict[str, str] = {}
    for entry in (entries or ())[:20_000]:
        if not isinstance(entry, dict):
            continue
        name, value = entry.get("name"), entry.get("value")
        if name in CACHE_ALLOWLIST and isinstance(value, str):
            out[name] = clip(value, 1024)
    return out


_CACHE_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.+-]{0,127})(?::[A-Z]+)?=(.*)$")


def parse_cmake_cache_text(data: bytes) -> dict[str, str]:
    """``CMakeCache.txt``: first 4 MiB, allowlisted keys only."""
    text = bytes(data[:MAX_CACHE_TEXT_BYTES]).decode("utf-8", errors="replace")
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line[0] in "#/":
            continue
        match = _CACHE_LINE_RE.match(line)
        if match and match.group(1) in CACHE_ALLOWLIST:
            out[match.group(1)] = clip(match.group(2), 1024)
    return out


def parse_toolchains_v1(data: bytes) -> tuple[Toolchain, ...]:
    document = loads_bounded_json(data, max_bytes=MAX_OBJECT_BYTES, what="toolchains-v1 reply")
    raw = document.get("toolchains") if isinstance(document, dict) else None
    out: list[Toolchain] = []
    for item in (raw or ())[:16]:
        if not isinstance(item, dict) or not isinstance(item.get("compiler"), dict):
            continue
        compiler = item["compiler"]
        path = compiler.get("path") if isinstance(compiler.get("path"), str) else ""
        compiler_id = clip(compiler.get("id") or "", 64)
        out.append(Toolchain(
            language=clip(item.get("language") or "?", 32) or "?",
            compiler_id=compiler_id,
            version=clip(compiler.get("version") or "", 64),
            path_label=("<external>/" + posixpath.basename(norm_path(path))) if path else "",
            target_arch=clip(compiler.get("target") or "", 64)
            if isinstance(compiler.get("target"), str) else "",
        ))
    return tuple(out)


@dataclass(frozen=True, slots=True)
class CMakeFilesInfo:
    inputs_rel: tuple[str, ...]
    external_count: int
    generated_count: int


def parse_cmakefiles_v1(data: bytes, *, cm_source: str, source_root: str,
                        build_dir: str) -> CMakeFilesInfo:
    """Project-owned build inputs (CMakeLists, included .cmake, preset files...)."""
    document = loads_bounded_json(data, max_bytes=MAX_OBJECT_BYTES, what="cmakeFiles-v1 reply")
    inputs = document.get("inputs") if isinstance(document, dict) else None
    rels: list[str] = []
    external = generated = 0
    for item in (inputs or ())[:20_000]:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            continue
        if item.get("isExternal") or item.get("isCMake"):
            external += 1
            continue
        if item.get("isGenerated"):
            generated += 1
            continue
        absolute = _absolute(item["path"], cm_source)
        _label, rel = path_label(absolute, source_root=source_root, build_dir=build_dir)
        if rel and rel not in rels:
            rels.append(rel)
    return CMakeFilesInfo(inputs_rel=tuple(rels), external_count=external,
                          generated_count=generated)


def _absolute(path: str, base: str) -> str:
    return norm_path(path) if is_absolute(path) else norm_path(base + "/" + path)


# --- codemodel ------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class _Group:
    language: str
    std: str
    include_count: int
    define_count: int
    fragments: tuple[str, ...]
    pch_headers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Source:
    path: str
    group: int
    generated: bool


@dataclass(frozen=True, slots=True)
class _Target:
    name: str
    target_id: str
    type: str
    artifacts: tuple[str, ...]
    depends_ids: tuple[str, ...]
    folder: str
    sources: tuple[_Source, ...]
    groups: tuple[_Group, ...]


def _parse_target(document: object) -> _Target | None:
    if not isinstance(document, dict):
        return None
    name, target_id, kind = document.get("name"), document.get("id"), document.get("type")
    if not isinstance(name, str) or not isinstance(target_id, str) or not isinstance(kind, str):
        return None
    groups: list[_Group] = []
    for group in (document.get("compileGroups") or ())[:4096]:
        if not isinstance(group, dict):
            continue
        standard = group.get("languageStandard")
        fragments = tuple(
            clip(fragment.get("fragment") or "", 8192)
            for fragment in (group.get("compileCommandFragments") or ())[:256]
            if isinstance(fragment, dict)
        )
        headers = tuple(
            clip(item.get("header") or "", 4096)
            for item in (group.get("precompileHeaders") or ())[:8]
            if isinstance(item, dict) and isinstance(item.get("header"), str)
        )
        groups.append(_Group(
            language=clip(group.get("language") or "", 32),
            std=clip(standard.get("standard") or "", 32) if isinstance(standard, dict) else "",
            include_count=len(group.get("includes") or ()),
            define_count=len(group.get("defines") or ()),
            fragments=fragments, pch_headers=headers,
        ))
    sources: list[_Source] = []
    for source in (document.get("sources") or ())[:100_000]:
        if not isinstance(source, dict) or not isinstance(source.get("path"), str):
            continue
        index = source.get("compileGroupIndex")
        sources.append(_Source(
            path=clip(source["path"], 4096),
            group=index if type(index) is int and 0 <= index < len(groups) else -1,
            generated=bool(source.get("isGenerated")),
        ))
    artifacts = tuple(
        clip(posixpath.basename(norm_path(item.get("path") or "")), 256)
        for item in (document.get("artifacts") or ())[:16]
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    )
    depends = tuple(
        item.get("id") for item in (document.get("dependencies") or ())[:256]
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    )
    folder = document.get("folder")
    return _Target(
        name=name, target_id=target_id, type=kind, artifacts=artifacts, depends_ids=depends,
        folder=clip(folder.get("name") or "", 256) if isinstance(folder, dict) else "",
        sources=tuple(sources), groups=tuple(groups),
    )


def codemodel_target_files(codemodel: bytes) -> tuple[str, ...]:
    """Reply file names a codemodel references (what the adapter must read next)."""
    document = loads_bounded_json(codemodel, max_bytes=MAX_OBJECT_BYTES, what="codemodel-v2 reply")
    names: list[str] = []
    for config in (document.get("configurations") or ()) if isinstance(document, dict) else ():
        if not isinstance(config, dict):
            continue
        for target in (config.get("targets") or ()):
            if isinstance(target, dict) and isinstance(target.get("jsonFile"), str) \
                    and REPLY_NAME_RE.fullmatch(target["jsonFile"]) and target["jsonFile"] not in names:
                names.append(target["jsonFile"])
    return tuple(names[:MAX_REPLY_FILES])


@dataclass(frozen=True, slots=True)
class CodemodelInfo:
    source: str
    build: str
    configs: tuple[str, ...]
    target_files: tuple[str, ...]
    missing: tuple[str, ...]
    target_names: tuple[str, ...]


def parse_codemodel_v2(codemodel: bytes, targets: Mapping[str, bytes]) -> CodemodelInfo:
    """Paths, configs and target reply files of one codemodel (bounded).

    ``model_from_file_api`` assembles the full model; this is the cheap view
    an adapter uses to decide what to read and whether anything is missing.
    """
    document = loads_bounded_json(codemodel, max_bytes=MAX_OBJECT_BYTES, what="codemodel-v2 reply")
    if not isinstance(document, dict):
        raise reject("BUILD_TREE_REJECTED", "codemodel-v2 is not an object")
    paths = document.get("paths") if isinstance(document.get("paths"), dict) else {}
    configs = tuple(
        clip(item.get("name") or "", 64) for item in (document.get("configurations") or ())[:32]
        if isinstance(item, dict)
    )
    files = codemodel_target_files(codemodel)
    names: list[str] = []
    for config in (document.get("configurations") or ())[:32]:
        for ref in (config.get("targets") or ()) if isinstance(config, dict) else ():
            name = ref.get("name") if isinstance(ref, dict) else None
            if isinstance(name, str) and TARGET_NAME_RE.fullmatch(name) and name not in names:
                names.append(name)
    return CodemodelInfo(
        source=norm_path(paths.get("source") or ""), build=norm_path(paths.get("build") or ""),
        configs=configs, target_files=files,
        missing=tuple(name for name in files if name not in targets),
        target_names=tuple(names[:MAX_TARGETS]),
    )


def _forced_includes(fragments: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for fragment in fragments:
        tokens = fragment.split()
        for index, token in enumerate(tokens):
            value = ""
            if token in ("-include", "/FI", "-FI") and index + 1 < len(tokens):
                value = tokens[index + 1]
            elif (token.startswith("/FI") or token.startswith("-FI")) and len(token) > 3:
                value = token[3:]
            if value:
                out.append(value.strip('"'))
    return out


def _flags_digest(fragments: tuple[str, ...]) -> str:
    material = "\x1f".join(
        token for fragment in fragments for token in fragment.split()
        if not (is_absolute(token) or "/" in token.replace("\\", "/")[1:])
    )
    return hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()[:16]


def _unity_mapping(blobs: list[str], members: list[str],
                   blob_texts: Mapping[str, bytes], build_dir: str) -> dict[str, str]:
    """source absolute path -> blob absolute path."""
    if not blobs:
        return {}
    if len(blobs) == 1:
        return {member: blobs[0] for member in members}
    mapping: dict[str, str] = {}
    for blob in blobs:
        rel = rel_under(blob, build_dir) if build_dir else None
        data = blob_texts.get(rel or "") if rel else None
        if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_UNITY_BLOB_BYTES:
            continue
        for included in _UNITY_INCLUDE_RE.findall(bytes(data).decode("utf-8", "replace")):
            for member in members:
                if norm_path(included) == member or (
                        re.match(r"^[A-Za-z]:", member)
                        and norm_path(included).casefold() == member.casefold()):
                    mapping[member] = blob
    return mapping


def model_from_file_api(
    *,
    index: ReplyIndex,
    objects: Mapping[str, bytes],
    source_root: str,
    build_dir: str,
    project_label: str,
    created_at: float = 0.0,
    unity_blobs: Mapping[str, bytes] | None = None,
    presets: tuple[PresetInfo, ...] = (),
    compile_db_available: bool = False,
    extra_notes: tuple[str, ...] = (),
) -> BuildModel:
    """Assemble a ``BuildModel`` from the reply files an index references.

    ``objects`` maps reply file name to bytes; a referenced name that is
    missing marks the model truncated. ``unity_blobs`` maps a unity blob path
    relative to ``build_dir`` to its text (only needed when a target has more
    than one blob).
    """
    if len(objects) > MAX_REPLY_FILES:
        raise reject("BUILD_TREE_REJECTED", "too many File API reply files")
    total = sum(len(value) for value in objects.values() if isinstance(value, (bytes, bytearray)))
    if total > MAX_TOTAL_BYTES:
        raise reject("BUILD_TREE_REJECTED", "File API reply exceeds %d bytes" % MAX_TOTAL_BYTES)
    notes: list[str] = list(extra_notes)
    truncated = False
    codemodel_ref = index.best("codemodel", 2)
    if codemodel_ref is None:
        raise reject("BUILD_MODEL_UNAVAILABLE", "the File API reply has no codemodel-v2")
    raw_codemodel = objects.get(codemodel_ref.json_file)
    if raw_codemodel is None:
        raise reject("BUILD_MODEL_UNAVAILABLE", "the codemodel-v2 reply file is missing")
    codemodel = loads_bounded_json(raw_codemodel, max_bytes=MAX_OBJECT_BYTES,
                                   what="codemodel-v2 reply")
    if not isinstance(codemodel, dict):
        raise reject("BUILD_TREE_REJECTED", "codemodel-v2 is not an object")
    paths = codemodel.get("paths") if isinstance(codemodel.get("paths"), dict) else {}
    cm_source = norm_path(paths.get("source") or source_root)
    cm_build = norm_path(paths.get("build") or build_dir)
    if rel_under(cm_source, source_root) is None and rel_under(source_root, cm_source) is None:
        raise reject("BUILD_TREE_REJECTED", "the build tree was configured for another source tree")
    if build_dir and rel_under(cm_build, build_dir) != ".":
        notes.append("codemodel build path differs from the requested build dir")

    for ref in index.objects:
        if ref.json_file not in objects and ref.kind in ("codemodel", "cache", "toolchains",
                                                          "cmakeFiles") \
                and index.best(ref.kind, ref.major) == ref:
            truncated = True
            notes.append("reply file listed by the index is missing: %s" % ref.json_file)

    toolchains: tuple[Toolchain, ...] = ()
    toolchain_ref = index.best("toolchains", 1)
    if toolchain_ref is not None and toolchain_ref.json_file in objects:
        toolchains = parse_toolchains_v1(objects[toolchain_ref.json_file])
    elif _version_tuple(index.cmake_version) < (3, 20):
        notes.append("toolchains-v1 needs CMake >= 3.20 (have %s)" % (index.cmake_version or "?"))
    else:
        notes.append("toolchains-v1 reply not present")
    cache: dict[str, str] = {}
    cache_ref = index.best("cache", 2)
    if cache_ref is not None and cache_ref.json_file in objects:
        cache = parse_cache_v2(objects[cache_ref.json_file])
    inputs: tuple[str, ...] = ()
    files_ref = index.best("cmakeFiles", 1)
    if files_ref is not None and files_ref.json_file in objects:
        inputs = parse_cmakefiles_v1(objects[files_ref.json_file], cm_source=cm_source,
                                     source_root=source_root, build_dir=build_dir).inputs_rel

    family_by_language: dict[str, str] = {}
    for toolchain in toolchains:
        family = toolchain.family
        if family == "clang" and compiler_name(toolchain.path_label) == "clang-cl":
            family = "clang_cl"
        family_by_language[toolchain.language] = family
    compiler_path = cache.get("CMAKE_CXX_COMPILER", "")
    if compiler_name(compiler_path) == "clang-cl":
        family_by_language["CXX"] = "clang_cl"

    configs: list[str] = []
    targets_by_name: dict[str, dict] = {}
    id_to_name: dict[str, str] = {}
    for config in (codemodel.get("configurations") or ())[:32]:
        if not isinstance(config, dict):
            continue
        config_name = clip(config.get("name") or "", 64)
        if config_name not in configs:
            configs.append(config_name)
        for ref in (config.get("targets") or ())[:MAX_TARGETS]:
            if not isinstance(ref, dict):
                continue
            json_file = ref.get("jsonFile")
            name = ref.get("name")
            if not isinstance(json_file, str) or not isinstance(name, str):
                continue
            raw = objects.get(json_file)
            if raw is None:
                truncated = True
                notes.append("target reply file is missing: %s" % clip(json_file, 120))
                continue
            parsed = _parse_target(loads_bounded_json(raw, max_bytes=MAX_OBJECT_BYTES,
                                                      what="target reply"))
            if parsed is None or not TARGET_NAME_RE.fullmatch(parsed.name):
                truncated = True
                notes.append("target reply was malformed: %s" % clip(json_file, 120))
                continue
            id_to_name[parsed.target_id] = parsed.name
            entry = targets_by_name.setdefault(parsed.name, {"target": parsed, "configs": []})
            if config_name and config_name not in entry["configs"]:
                entry["configs"].append(config_name)
    if len(targets_by_name) > MAX_TARGETS:
        truncated = True
        notes.append("target list truncated at %d" % MAX_TARGETS)

    multi_config = index.multi_config or len(configs) > 1
    targets: list[BuildTarget] = []
    units: list[CompileUnit] = []
    generated: list[str] = []
    for name, entry in list(targets_by_name.items())[:MAX_TARGETS]:
        target: _Target = entry["target"]
        blobs = [
            _absolute(source.path, cm_source) for source in target.sources
            if _UNITY_BLOB_RE.search("/" + norm_path(source.path))
        ]
        members = [
            _absolute(source.path, cm_source) for source in target.sources
            if source.group < 0 and not source.generated
            and posixpath.splitext(source.path)[1].lower() in SOURCE_SUFFIXES
        ] if blobs else []
        unity_map = _unity_mapping(blobs, members, unity_blobs or {}, build_dir)
        blob_groups = {
            _absolute(source.path, cm_source): target.groups[source.group]
            for source in target.sources
            if source.group >= 0 and _UNITY_BLOB_RE.search("/" + norm_path(source.path))
        }
        pch_headers: list[str] = []
        compiled = 0
        for source in target.sources:
            absolute = _absolute(source.path, cm_source)
            label, rel = path_label(absolute, source_root=source_root, build_dir=build_dir)
            is_generated = source.generated or (label.startswith("<build>"))
            if is_generated and rel and rel not in generated:
                generated.append(rel)
            if source.group < 0:
                if absolute in members:
                    blob = unity_map.get(absolute, "")
                    blob_rel = (rel_under(blob, build_dir) or "") if blob and build_dir else ""
                    blob_group = blob_groups.get(blob) if blob else None
                    language = blob_group.language if blob_group else (
                        "C" if posixpath.splitext(rel or label)[1].lower() == ".c" else "CXX")
                    pch_label = ""
                    if blob_group is not None and blob_group.pch_headers:
                        pch_label, _ = path_label(_absolute(blob_group.pch_headers[0], cm_source),
                                                  source_root=source_root, build_dir=build_dir)
                    if len(units) < MAX_UNITS:
                        units.append(CompileUnit(
                            file_label=label or "<unknown>", file_rel=rel, target=name,
                            config="" if multi_config else (entry["configs"][0] if entry["configs"] else ""),
                            language=language or "CXX",
                            family=family_by_language.get(language or "CXX", "other"),
                            std=blob_group.std if blob_group else "",
                            define_count=blob_group.define_count if blob_group else 0,
                            include_dir_count=blob_group.include_count if blob_group else 0,
                            pch=PchMode.USE if pch_label else PchMode.NONE, pch_header=pch_label,
                            unity_blob_rel=clip(blob_rel, 1024),
                            flags_digest=_flags_digest(blob_group.fragments) if blob_group else "",
                        ))
                    else:
                        truncated = True
                continue
            group = target.groups[source.group]
            if _UNITY_BLOB_RE.search("/" + norm_path(source.path)):
                continue
            compiled += 1
            for header in group.pch_headers:
                header_label, _ = path_label(_absolute(header, cm_source),
                                             source_root=source_root, build_dir=build_dir)
                if header_label and header_label not in pch_headers:
                    pch_headers.append(header_label)
            if posixpath.basename(norm_path(source.path)).startswith("cmake_pch"):
                continue  # the PCH creation TU is generated; its header is recorded above
            if label.startswith("<build>"):
                continue
            pch_mode = PchMode.USE if group.pch_headers else PchMode.NONE
            forced = []
            for value in _forced_includes(group.fragments):
                if posixpath.basename(norm_path(value)).startswith("cmake_pch"):
                    pch_mode = PchMode.USE
                    continue
                forced_label, _ = path_label(_absolute(value, cm_source), source_root=source_root,
                                             build_dir=build_dir)
                forced.append(forced_label)
            if forced and pch_mode is PchMode.NONE:
                pch_mode = PchMode.FORCED_INCLUDE
            pch_label = ""
            if group.pch_headers:
                pch_label, _ = path_label(_absolute(group.pch_headers[0], cm_source),
                                          source_root=source_root, build_dir=build_dir)
            if len(units) >= MAX_UNITS:
                truncated = True
                continue
            units.append(CompileUnit(
                file_label=label or "<unknown>", file_rel=rel, target=name,
                config="" if multi_config else (entry["configs"][0] if entry["configs"] else ""),
                language=group.language or "CXX",
                family=family_by_language.get(group.language, "other"),
                std=group.std, define_count=group.define_count,
                include_dir_count=group.include_count, pch=pch_mode, pch_header=pch_label,
                forced_includes=tuple(forced[:16]), flags_digest=_flags_digest(group.fragments),
            ))
        depends = tuple(
            id_to_name[item] for item in target.depends_ids if item in id_to_name
        )[:64]
        targets.append(BuildTarget(
            name=name, type=target_type_from_name(target.type),
            configs=tuple(entry["configs"][:32]), source_count=compiled + len(members),
            artifacts=target.artifacts, folder=target.folder, depends=depends,
            unity=bool(blobs), pch_headers=tuple(pch_headers[:8]),
        ))
        if blobs and len(blobs) > 1 and len(unity_map) < len(members):
            notes.append("unity blob membership unknown for part of %s; compile_one falls back"
                         " to a target build" % name)

    model = BuildModel(
        project_label=clip(project_label, 1024) or "project",
        source_root=norm_path(source_root), build_dir=norm_path(build_dir),
        system=BuildSystem.CMAKE, generator=generator_from_name(index.generator or
                                                                 cache.get("CMAKE_GENERATOR", "")),
        multi_config=multi_config, configs=tuple(item for item in configs if item)[:32],
        targets=tuple(targets), toolchains=toolchains, units=tuple(units),
        presets=tuple(presets), source=ModelSource.FILE_API,
        reply_client=(codemodel_ref.client or "stateless"),
        compile_db_available=compile_db_available, file_api_available=True,
        cmake_version=index.cmake_version, truncated=truncated,
        notes=tuple(notes), created_at=float(created_at),
        generated_rel=tuple(generated[:20_000]), build_inputs=tuple(inputs[:20_000]),
    )
    safety = classify_targets(model)
    model = replace(model, targets=apply_safety(model, safety),
                    notes=bounded_notes(list(model.notes) + list(safety.notes)))
    return finalize_model(model)


def cache_launchers(cache: Mapping[str, str]) -> tuple[str, ...]:
    """Compiler launcher names configured in the cache (sccache, ccache, ...)."""
    out: list[str] = []
    for key in ("CMAKE_C_COMPILER_LAUNCHER", "CMAKE_CXX_COMPILER_LAUNCHER",
                "CMAKE_CUDA_COMPILER_LAUNCHER"):
        value = cache.get(key, "")
        for item in value.split(";"):
            base = posixpath.basename(norm_path(item.strip())).lower()
            if base.endswith(".exe"):
                base = base[:-4]
            if base and base not in out:
                out.append(base)
    return tuple(out)


__all__ = [
    "CACHE_ALLOWLIST", "CMakeFilesInfo", "CodemodelInfo", "INDEX_NAME_RE", "MAX_INDEX_BYTES",
    "MAX_OBJECT_BYTES", "MAX_REPLY_FILES", "MAX_TOTAL_BYTES", "REPLY_NAME_RE",
    "ReplyIndex", "ReplyObject", "SONDER_CLIENT", "cache_launchers", "codemodel_target_files",
    "compiler_family_from_id", "model_from_file_api", "newest_index_name", "parse_codemodel_v2",
    "parse_cache_v2", "parse_cmake_cache_text", "parse_cmakefiles_v1", "parse_reply_index",
    "parse_toolchains_v1", "query_document",
]
