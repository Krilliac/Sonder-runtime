"""Guarded, bounded reads of a C/C++ project and its build tree.

The reader returns bytes; the pure parsers in ``domain.build`` interpret
them. It never executes anything and never writes.

Containment (security section 3):

* every file is opened no-follow (``O_NOFOLLOW`` on POSIX; on Windows a
  reparse-point check before the open and a handle-identity comparison
  after it), and every directory between the root and the file is checked
  for symlinks and junctions;
* File API replies are read only from ``build_dir/.cmake/api/v1/reply/`` and
  only names listed in the newest ``index-*.json`` -- any client's codemodel,
  so trees configured by Visual Studio, CLion or VS Code are readable;
* preset includes and ``.props`` imports are followed only inside the
  project root, bounded in depth and count; nothing is evaluated;
* ``build_dir`` may not be the project root or one of its ancestors.

Bounds (security section 8): index 1 MiB; each reply object 8 MiB; at most
4096 reply files and 64 MiB in total; compile database 64 MiB; ``.sln``
4 MiB with at most 512 projects; each ``.vcxproj``/``.props`` 4 MiB, imports
at most 64 files at depth 4; each preset file 1 MiB, at most 32 files;
``CMakeCache.txt`` first 4 MiB, allowlisted keys only. Oversized input is
skipped with a note and marks the tree ``truncated``.
"""
from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

from ...application.build.ports import (
    BUILD_TREE_REJECTED,
    RawBuildTree,
    build_error,
)

MAX_INDEX_BYTES = 1024 * 1024
MAX_REPLY_OBJECT_BYTES = 8 * 1024 * 1024
MAX_REPLY_FILES = 4096
MAX_REPLY_TOTAL_BYTES = 64 * 1024 * 1024
MAX_COMPILE_DB_BYTES = 64 * 1024 * 1024
MAX_SOLUTION_BYTES = 4 * 1024 * 1024
MAX_SOLUTION_PROJECTS = 512
MAX_PROJECT_BYTES = 4 * 1024 * 1024
MAX_IMPORT_FILES = 64
MAX_IMPORT_DEPTH = 4
MAX_PRESET_BYTES = 1024 * 1024
MAX_PRESET_FILES = 32
MAX_PRESET_DEPTH = 4
MAX_CACHE_BYTES = 4 * 1024 * 1024
MAX_DIR_ENTRIES = 4096
MAX_JSON_DEPTH = 64
MAX_UNITY_BLOBS = 256
MAX_UNITY_BLOB_BYTES = 1024 * 1024
MAX_NOTES = 32

REPLY_RELATIVE = (".cmake", "api", "v1", "reply")
BUILD_LABEL = "<build>"
CACHE_KEYS = frozenset({
    "CMAKE_GENERATOR", "CMAKE_GENERATOR_PLATFORM", "CMAKE_GENERATOR_TOOLSET",
    "CMAKE_GENERATOR_INSTANCE", "CMAKE_BUILD_TYPE", "CMAKE_CONFIGURATION_TYPES",
    "CMAKE_C_COMPILER", "CMAKE_CXX_COMPILER", "CMAKE_C_COMPILER_LAUNCHER",
    "CMAKE_CXX_COMPILER_LAUNCHER", "CMAKE_CUDA_COMPILER_LAUNCHER", "CMAKE_EXPORT_COMPILE_COMMANDS",
    "CMAKE_HOME_DIRECTORY", "CMAKE_PROJECT_NAME", "CMAKE_UNITY_BUILD", "CMAKE_MAKE_PROGRAM",
    "CMAKE_CACHEFILE_DIR", "CMAKE_TOOLCHAIN_FILE", "VCPKG_MANIFEST_INSTALL",
    "FETCHCONTENT_FULLY_DISCONNECTED",
})
_REPLY_NAME_RE = re.compile(r"^[A-Za-z0-9_.+-]{1,200}\.json$")
_INDEX_NAME_RE = re.compile(r"^index-[A-Za-z0-9_.+-]{1,160}\.json$")
_CACHE_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]{0,127})(?::[A-Z_]{1,16})?=(.{0,4096})$")
_SLN_PROJECT_RE = re.compile(
    rb'^Project\("\{[0-9A-Fa-f-]{36}\}"\)\s*=\s*"([^"\r\n]{1,256})"\s*,\s*"([^"\r\n]{1,1024})"',
    re.MULTILINE,
)
_IMPORT_RE = re.compile(rb'<Import\s[^>]{0,2048}?Project\s*=\s*"([^"\r\n]{1,1024})"', re.IGNORECASE)
_UNITY_PATH_RE = re.compile(
    rb'"path"\s*:\s*"([^"\r\n]{1,1024}/Unity/unity_[0-9]{1,6}_(?:c|cxx|cu|objc|objcxx)\.(?:c|cxx|cu|m|mm))"')
_MACRO_PREFIXES = ("$(msbuildthisfiledirectory)", "$(projectdir)", "$(solutiondir)")


def _is_reparse_stat(info: os.stat_result) -> bool:
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except OSError:
        return None


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _inside(child: str, parent: str) -> bool:
    child_n, parent_n = _norm(child), _norm(parent)
    if child_n == parent_n:
        return True
    return child_n.startswith(parent_n.rstrip(os.sep) + os.sep)


class _Budget:
    __slots__ = ("files", "bytes")

    def __init__(self) -> None:
        self.files = 0
        self.bytes = 0


@dataclass
class _Notes:
    items: list[str] = field(default_factory=list)
    truncated: bool = False

    def add(self, text: str, *, truncated: bool = False) -> None:
        if text not in self.items and len(self.items) < MAX_NOTES:
            self.items.append(text)
        if truncated:
            self.truncated = True


class GuardedBuildTreeReader:
    """``BuildTreeReader``: bounded, no-follow reads inside a project and build tree."""

    def __init__(self, *, user_presets: bool = True) -> None:
        self._user_presets = bool(user_presets)

    # -- primitives ------------------------------------------------------------

    @staticmethod
    def _components_ok(root: Path, parts: tuple[str, ...]) -> bool:
        """No symlink or junction in any directory below ``root`` up to the file."""
        current = root
        info = _lstat(current)
        if info is None or _is_reparse_stat(info) or not stat.S_ISDIR(info.st_mode):
            return False
        for part in parts[:-1]:
            if part in ("", ".", "..") or "/" in part or "\\" in part:
                return False
            current = current / part
            info = _lstat(current)
            if info is None or _is_reparse_stat(info) or not stat.S_ISDIR(info.st_mode):
                return False
        return True

    def read_bytes(self, root: str, parts: tuple[str, ...], max_bytes: int,
                   notes: _Notes, label: str) -> bytes | None:
        """The file's bytes, or None (missing, refused, or too large -- noted)."""
        base = Path(root)
        if not parts or not self._components_ok(base, parts):
            if parts and os.path.lexists(base.joinpath(*parts)):
                notes.add("%s is behind a symlink or junction; refused" % label)
            return None
        path = base.joinpath(*parts)
        before = _lstat(path)
        if before is None:
            return None
        if _is_reparse_stat(before):
            notes.add("%s is a symlink or junction; refused" % label)
            return None
        if not stat.S_ISREG(before.st_mode):
            notes.add("%s is not a regular file; refused" % label)
            return None
        if before.st_size > max_bytes:
            notes.add("%s exceeds %d bytes; skipped" % (label, max_bytes), truncated=True)
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(path, flags)
        except OSError:
            notes.add("%s could not be opened" % label)
            return None
        try:
            after = os.fstat(fd)
            if (not stat.S_ISREG(after.st_mode) or after.st_ino != before.st_ino
                    or after.st_dev != before.st_dev):
                notes.add("%s changed while it was opened; refused" % label)
                return None
            chunks: list[bytes] = []
            size = 0
            while size <= max_bytes:
                chunk = os.read(fd, min(1 << 20, max_bytes + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
        finally:
            os.close(fd)
        if size > max_bytes:
            notes.add("%s exceeds %d bytes; skipped" % (label, max_bytes), truncated=True)
            return None
        return b"".join(chunks)

    @staticmethod
    def _list(directory: Path) -> list[str]:
        info = _lstat(directory)
        if info is None or _is_reparse_stat(info) or not stat.S_ISDIR(info.st_mode):
            return []
        try:
            with os.scandir(directory) as entries:
                return [entry.name for index, entry in enumerate(entries) if index < MAX_DIR_ENTRIES]
        except OSError:
            return []

    @staticmethod
    def _json(data: bytes | None) -> object:
        if data is None:
            return None
        text = data.decode("utf-8-sig", errors="replace")
        depth = deepest = 0
        in_string = escaped = False
        for char in text:
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char in "[{":
                depth += 1
                deepest = max(deepest, depth)
                if deepest > MAX_JSON_DEPTH:
                    return None
            elif char in "]}":
                depth -= 1
        try:
            return json.loads(text)
        except (ValueError, RecursionError):
            return None

    # -- validation ------------------------------------------------------------

    @staticmethod
    def check_roots(project_root: str, build_dir: str) -> None:
        if not project_root or not os.path.isabs(project_root):
            raise build_error(BUILD_TREE_REJECTED, "project root must be an absolute path")
        if build_dir:
            if not os.path.isabs(build_dir):
                raise build_error(BUILD_TREE_REJECTED, "build directory must be an absolute path")
            if _inside(project_root, build_dir):
                raise build_error(BUILD_TREE_REJECTED,
                                  "the build directory may not be the project root or its ancestor")

    @staticmethod
    def build_label(project_root: str, build_dir: str) -> str:
        if build_dir and _inside(build_dir, project_root):
            rel = os.path.relpath(build_dir, project_root).replace(os.sep, "/")
            return rel
        return BUILD_LABEL

    # -- public ----------------------------------------------------------------

    def fingerprint(self, project_root: str, build_dir: str) -> str:
        """sha256 over cheap stats: newest reply index, cache, compile db, sln, presets."""
        self.check_roots(project_root, build_dir)
        material: list[str] = []
        root = Path(project_root)

        def stat_of(path: Path) -> str:
            info = _lstat(path)
            if info is None:
                return "-"
            return "%d:%d:%d" % (info.st_mode, info.st_size, getattr(info, "st_mtime_ns", 0))

        if build_dir:
            build = Path(build_dir)
            reply = build.joinpath(*REPLY_RELATIVE)
            indexes = sorted(name for name in self._list(reply) if _INDEX_NAME_RE.match(name))
            newest = indexes[-1] if indexes else ""
            material.append("index=" + newest + "@" + (stat_of(reply / newest) if newest else "-"))
            for name in ("CMakeCache.txt", "compile_commands.json", "build.ninja", "Makefile"):
                material.append(name + "=" + stat_of(build / name))
            for name in sorted(self._list(build)):
                if name.endswith(".sln") or re.match(r"^build-[A-Za-z0-9_.-]{1,64}\.ninja$", name):
                    material.append(name + "=" + stat_of(build / name))
        for name in sorted(self._list(root))[:MAX_DIR_ENTRIES]:
            if name.endswith(".sln") or name in ("CMakePresets.json", "CMakeUserPresets.json",
                                                  "CMakeLists.txt"):
                material.append("src:" + name + "=" + stat_of(root / name))
        return hashlib.sha256("\n".join(material).encode("utf-8", "replace")).hexdigest()

    def read_presets(self, project_root: str) -> RawBuildTree:
        self.check_roots(project_root, "")
        notes = _Notes()
        presets, includes = self._presets(project_root, notes)
        return RawBuildTree(
            project_root=project_root, build_dir="", presets=presets, preset_includes=includes,
            cmake_lists_present=os.path.isfile(os.path.join(project_root, "CMakeLists.txt")),
            truncated=notes.truncated, notes=tuple(notes.items),
        )

    def read(self, project_root: str, build_dir: str) -> RawBuildTree:
        self.check_roots(project_root, build_dir)
        notes = _Notes()
        root = Path(project_root)
        presets, includes = self._presets(project_root, notes)
        reply_index: tuple[tuple[str, bytes], ...] = ()
        reply_objects: tuple[tuple[str, bytes], ...] = ()
        unity_blobs: tuple[tuple[str, bytes], ...] = ()
        compile_db = None
        cache_values: tuple[tuple[str, str], ...] = ()
        ninja_files: list[str] = []
        ninja_present = makefile_present = False
        solution = None
        vcxproj: tuple[tuple[str, bytes], ...] = ()
        props: tuple[tuple[str, bytes], ...] = ()
        build_label = self.build_label(project_root, build_dir)
        build_info = _lstat(Path(build_dir)) if build_dir else None
        if build_info is not None and _is_reparse_stat(build_info):
            raise build_error(BUILD_TREE_REJECTED, "the build directory is a symlink or junction")
        if build_info is not None and stat.S_ISDIR(build_info.st_mode):
            reply_index, reply_objects = self._reply(build_dir, notes)
            unity_blobs = self._unity_blobs(project_root, build_dir, reply_objects, notes)
            compile_db = self.read_bytes(build_dir, ("compile_commands.json",), MAX_COMPILE_DB_BYTES,
                                         notes, "compile_commands.json")
            cache_values = self._cache(build_dir, notes)
            names = self._list(Path(build_dir))
            for name in sorted(names):
                if name == "build.ninja" or re.match(r"^build-[A-Za-z0-9_.-]{1,64}\.ninja$", name):
                    if self._regular(Path(build_dir) / name):
                        ninja_files.append(name)
            ninja_present = bool(ninja_files)
            makefile_present = self._regular(Path(build_dir) / "Makefile")
            build_solution = sorted(name for name in names if name.endswith(".sln"))
            if build_solution:
                # A CMake Visual Studio generator tree: its generated projects
                # are the ClCompile targets for compile_one.
                solution, vcxproj, props = self._solution(build_dir, build_solution[0], notes,
                                                          label_prefix=BUILD_LABEL + "/",
                                                          import_root=project_root)
        elif build_dir:
            notes.add("build directory %s does not exist" % build_label)
        if solution is None:
            source_solution = sorted(name for name in self._list(root) if name.endswith(".sln"))
            if source_solution:
                solution, vcxproj, props = self._solution(project_root, source_solution[0], notes,
                                                          label_prefix="", import_root=project_root)
        return RawBuildTree(
            project_root=project_root,
            build_dir=build_dir,
            reply_index=reply_index,
            reply_objects=reply_objects,
            compile_db=compile_db,
            solution=solution,
            vcxproj=vcxproj,
            props_imports=props,
            presets=presets,
            preset_includes=includes,
            cache_values=cache_values,
            ninja_present=ninja_present,
            makefile_present=makefile_present,
            ninja_files=tuple(ninja_files),
            unity_blobs=unity_blobs,
            cmake_lists_present=self._regular(root / "CMakeLists.txt"),
            truncated=notes.truncated,
            notes=tuple(notes.items),
            fingerprint=self.fingerprint(project_root, build_dir),
        )

    # -- narrow reads for planning -----------------------------------------------

    def read_cache(self, build_dir: str) -> tuple[tuple[str, str], ...]:
        """Allowlisted ``CMakeCache.txt`` values (compiler launchers, generator)."""
        if not build_dir or not os.path.isabs(build_dir):
            return ()
        return self._cache(build_dir, _Notes())

    def read_compile_db(self, build_dir: str) -> tuple[bytes | None, tuple[str, ...]]:
        notes = _Notes()
        if not build_dir or not os.path.isabs(build_dir):
            return None, ()
        data = self.read_bytes(build_dir, ("compile_commands.json",), MAX_COMPILE_DB_BYTES, notes,
                               "compile_commands.json")
        return data, tuple(notes.items)

    def read_response_file(self, build_dir: str, path: str) -> str | None:
        """A compiler response file inside ``build_dir`` (256 KiB), or None."""
        if not build_dir or not path:
            return None
        absolute = path if os.path.isabs(path) else os.path.join(build_dir, path)
        if not _inside(absolute, build_dir):
            return None
        rel = os.path.relpath(os.path.normpath(absolute), build_dir)
        if rel.startswith(".."):
            return None
        data = self.read_bytes(build_dir, tuple(rel.replace(os.sep, "/").split("/")), 256 * 1024,
                               _Notes(), "response file")
        return None if data is None else data.decode("utf-8", errors="replace")

    def build_projects(self, build_dir: str) -> tuple[str, ...]:
        """Labels (``<build>/...``) of the ``.vcxproj`` files a generated solution lists."""
        if not build_dir or not os.path.isabs(build_dir):
            return ()
        names = sorted(name for name in self._list(Path(build_dir)) if name.endswith(".sln"))
        if not names:
            return ()
        _, projects, _ = self._solution(build_dir, names[0], _Notes(),
                                        label_prefix=BUILD_LABEL + "/", import_root="")
        return tuple(label for label, _ in projects)

    @staticmethod
    def _regular(path: Path) -> bool:
        info = _lstat(path)
        return info is not None and stat.S_ISREG(info.st_mode) and not _is_reparse_stat(info)

    # -- File API --------------------------------------------------------------

    def _reply(self, build_dir: str, notes: _Notes):
        reply_dir = Path(build_dir).joinpath(*REPLY_RELATIVE)
        if not self._components_ok(Path(build_dir), (*REPLY_RELATIVE, "x")):
            if os.path.lexists(reply_dir):
                notes.add("the File API reply directory is behind a symlink; refused")
            return (), ()
        names = self._list(reply_dir)
        indexes = sorted(name for name in names if _INDEX_NAME_RE.match(name))
        if not indexes:
            return (), ()
        newest = indexes[-1]
        data = self.read_bytes(build_dir, (*REPLY_RELATIVE, newest), MAX_INDEX_BYTES, notes,
                               "File API index")
        if data is None:
            return (), ()
        index = self._json(data)
        if not isinstance(index, dict):
            notes.add("the File API index is not valid JSON", truncated=True)
            return ((newest, data),), ()
        listed = self._listed_objects(index)
        available = set(names)
        budget = _Budget()
        objects: list[tuple[str, bytes]] = []
        seen: set[str] = set()
        queue = list(listed)
        while queue:
            name = queue.pop(0)
            if name in seen:
                continue
            seen.add(name)
            if name not in available:
                notes.add("the File API index names a missing reply file", truncated=True)
                continue
            if budget.files >= MAX_REPLY_FILES or budget.bytes >= MAX_REPLY_TOTAL_BYTES:
                notes.add("File API reply exceeds its file or byte budget", truncated=True)
                break
            body = self.read_bytes(build_dir, (*REPLY_RELATIVE, name), MAX_REPLY_OBJECT_BYTES, notes,
                                   "File API reply object")
            if body is None:
                continue
            if budget.bytes + len(body) > MAX_REPLY_TOTAL_BYTES:
                notes.add("File API reply exceeds its byte budget", truncated=True)
                break
            budget.files += 1
            budget.bytes += len(body)
            objects.append((name, body))
            if name.startswith("codemodel-v2"):
                queue.extend(self._codemodel_children(self._json(body)))
        return ((newest, data),), tuple(objects)

    def _unity_blobs(self, project_root: str, build_dir: str,
                     objects: tuple[tuple[str, bytes], ...], notes: _Notes):
        """Unity blobs named by target replies, read from inside ``build_dir`` only."""
        found: list[str] = []
        for name, body in objects:
            if not name.startswith("target-"):
                continue
            for raw in _UNITY_PATH_RE.findall(body):
                text = raw.decode("utf-8", errors="replace").replace("\\\\", "/")
                absolute = text if (text.startswith("/") or re.match(r"^[A-Za-z]:/", text)) \
                    else os.path.join(project_root, *text.split("/"))
                if not _inside(absolute, build_dir):
                    continue
                rel = os.path.relpath(os.path.normpath(absolute), build_dir).replace(os.sep, "/")
                if rel not in found:
                    found.append(rel)
        blobs: list[tuple[str, bytes]] = []
        for rel in found[:MAX_UNITY_BLOBS]:
            data = self.read_bytes(build_dir, tuple(rel.split("/")), MAX_UNITY_BLOB_BYTES, notes,
                                   "unity blob")
            if data is not None:
                blobs.append((rel, data))
        if len(found) > MAX_UNITY_BLOBS:
            notes.add("more than %d unity blobs; the rest are not mapped" % MAX_UNITY_BLOBS,
                      truncated=True)
        return tuple(blobs)

    @staticmethod
    def _listed_objects(index: dict) -> list[str]:
        found: list[str] = []

        def add(entry: object) -> None:
            if isinstance(entry, dict):
                name = entry.get("jsonFile")
                if isinstance(name, str) and _REPLY_NAME_RE.match(name) and name not in found:
                    found.append(name)

        for entry in index.get("objects") or ():
            add(entry)
        reply = index.get("reply")
        if isinstance(reply, dict):
            for client in list(reply.values())[:256]:
                if not isinstance(client, dict):
                    add(client)
                    continue
                for item in list(client.values())[:256]:
                    if isinstance(item, dict) and "responses" in item:
                        for response in (item.get("responses") or ())[:256]:
                            add(response)
                    else:
                        add(item)
        return found

    @staticmethod
    def _codemodel_children(codemodel: object) -> list[str]:
        names: list[str] = []
        if not isinstance(codemodel, dict):
            return names
        for config in (codemodel.get("configurations") or ())[:64]:
            if not isinstance(config, dict):
                continue
            for key in ("targets", "directories"):
                for item in (config.get(key) or ())[:MAX_REPLY_FILES]:
                    if isinstance(item, dict):
                        name = item.get("jsonFile")
                        if isinstance(name, str) and _REPLY_NAME_RE.match(name) and name not in names:
                            names.append(name)
        return names

    # -- cache -----------------------------------------------------------------

    def _cache(self, build_dir: str, notes: _Notes) -> tuple[tuple[str, str], ...]:
        path = Path(build_dir) / "CMakeCache.txt"
        info = _lstat(path)
        if info is None:
            return ()
        if _is_reparse_stat(info) or not stat.S_ISREG(info.st_mode):
            notes.add("CMakeCache.txt is not a regular file; refused")
            return ()
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(path, flags)
        except OSError:
            return ()
        try:
            data = os.read(fd, MAX_CACHE_BYTES)
        finally:
            os.close(fd)
        if info.st_size > MAX_CACHE_BYTES:
            notes.add("CMakeCache.txt read to its first 4 MiB", truncated=True)
        values: dict[str, str] = {}
        for raw in data.decode("utf-8", errors="replace").splitlines():
            if not raw or raw.startswith(("#", "//")):
                continue
            match = _CACHE_LINE_RE.match(raw)
            if match and match.group(1) in CACHE_KEYS and match.group(1) not in values:
                values[match.group(1)] = match.group(2).strip()
        return tuple(sorted(values.items()))

    # -- presets ---------------------------------------------------------------

    def _presets(self, project_root: str, notes: _Notes):
        files: list[tuple[str, bytes]] = []
        includes: list[tuple[str, bytes]] = []
        names = ["CMakePresets.json"] + (["CMakeUserPresets.json"] if self._user_presets else [])
        seen: set[str] = set()
        queue: list[tuple[str, int]] = []
        for name in names:
            data = self.read_bytes(project_root, (name,), MAX_PRESET_BYTES, notes, name)
            if data is not None:
                files.append((name, data))
                seen.add(name)
                queue.extend((item, 1) for item in self._preset_includes(data, ""))
        while queue:
            label, depth = queue.pop(0)
            if label in seen:
                continue
            if depth > MAX_PRESET_DEPTH or len(files) + len(includes) >= MAX_PRESET_FILES:
                notes.add("preset includes exceed their depth or file budget", truncated=True)
                break
            seen.add(label)
            parts = tuple(label.split("/"))
            data = self.read_bytes(project_root, parts, MAX_PRESET_BYTES, notes, "preset include")
            if data is None:
                notes.add("a preset include is missing or refused: %s" % label[:120])
                continue
            includes.append((label, data))
            queue.extend((item, depth + 1) for item in self._preset_includes(data, posixpath.dirname(label)))
        return tuple(files), tuple(includes)

    def _preset_includes(self, data: bytes, base: str) -> list[str]:
        document = self._json(data)
        if not isinstance(document, dict):
            return []
        found: list[str] = []
        for item in (document.get("include") or ())[:MAX_PRESET_FILES]:
            if not isinstance(item, str) or "$" in item or "\x00" in item:
                continue  # macro-expanded include paths are not followed
            label = _in_root_label(base, item)
            if label is not None and label not in found:
                found.append(label)
        return found

    # -- MSBuild -----------------------------------------------------------------

    def _solution(self, directory: str, name: str, notes: _Notes, *, label_prefix: str,
                  import_root: str):
        data = self.read_bytes(directory, (name,), MAX_SOLUTION_BYTES, notes, "solution")
        if data is None:
            return None, (), ()
        solution = (label_prefix + name, data)
        projects: list[tuple[str, bytes]] = []
        props: list[tuple[str, bytes]] = []
        seen_props: set[str] = set()
        matches = _SLN_PROJECT_RE.findall(data)
        if len(matches) > MAX_SOLUTION_PROJECTS:
            notes.add("the solution lists more than %d projects" % MAX_SOLUTION_PROJECTS, truncated=True)
        for _, raw_path in matches[:MAX_SOLUTION_PROJECTS]:
            text = raw_path.decode("utf-8", errors="replace")
            if not text.lower().endswith(".vcxproj"):
                continue
            rel = _in_root_label("", text.replace("\\", "/"))
            if rel is None:
                notes.add("a solution project outside the tree was skipped")
                continue
            body = self.read_bytes(directory, tuple(rel.split("/")), MAX_PROJECT_BYTES, notes, "project")
            if body is None:
                continue
            projects.append((label_prefix + rel, body))
            if directory == import_root:
                self._imports(import_root, rel, body, props, seen_props, notes, depth=1)
        return solution, tuple(projects), tuple(props)

    def _imports(self, root: str, owner_rel: str, body: bytes, out: list[tuple[str, bytes]],
                 seen: set[str], notes: _Notes, *, depth: int) -> None:
        if depth > MAX_IMPORT_DEPTH:
            notes.add(".props imports exceed depth %d" % MAX_IMPORT_DEPTH, truncated=True)
            return
        base = posixpath.dirname(owner_rel)
        for raw in _IMPORT_RE.findall(body)[:256]:
            text = raw.decode("utf-8", errors="replace").replace("\\", "/")
            lowered = text.lower()
            for prefix in _MACRO_PREFIXES:
                if lowered.startswith(prefix):
                    text = text[len(prefix):].lstrip("/")
                    break
            if "$(" in text or "%" in text or not text.lower().endswith((".props", ".targets")):
                continue  # evaluation-dependent or SDK imports are not followed
            label = _in_root_label(base, text)
            if label is None or label in seen:
                continue
            if len(out) >= MAX_IMPORT_FILES:
                notes.add(".props imports exceed %d files" % MAX_IMPORT_FILES, truncated=True)
                return
            seen.add(label)
            data = self.read_bytes(root, tuple(label.split("/")), MAX_PROJECT_BYTES, notes, "props import")
            if data is None:
                continue
            out.append((label, data))
            self._imports(root, label, data, out, seen, notes, depth=depth + 1)


def _in_root_label(base: str, relative: str) -> str | None:
    """``base``-relative ``relative`` as a root-relative label, or None when it escapes."""
    text = relative.replace("\\", "/")
    if not text or text.startswith("/") or re.match(r"^[A-Za-z]:", text) or "\x00" in text:
        return None
    joined = posixpath.normpath(posixpath.join(base, text)) if base else posixpath.normpath(text)
    if joined in ("", ".", "..") or joined.startswith("../"):
        return None
    return joined


def label_path(project_root: str, build_dir: str, label: str) -> str:
    """The absolute path behind a reader label (``<build>/...`` or root-relative)."""
    if label.startswith(BUILD_LABEL + "/"):
        rel = label[len(BUILD_LABEL) + 1:]
        base = build_dir
    else:
        rel, base = label, project_root
    safe = _in_root_label("", rel)
    if safe is None or not base:
        raise build_error(BUILD_TREE_REJECTED, "a tree label escapes its root")
    return os.path.join(base, *safe.split("/"))


__all__ = [
    "BUILD_LABEL", "CACHE_KEYS", "GuardedBuildTreeReader", "MAX_COMPILE_DB_BYTES",
    "MAX_INDEX_BYTES", "MAX_REPLY_OBJECT_BYTES", "REPLY_RELATIVE", "label_path",
]
