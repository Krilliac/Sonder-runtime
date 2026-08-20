"""Bounded, read-only source symbol discovery for repository agents.

The index intentionally favors conservative declarations over parser-like
guessing. Python uses the standard-library AST; other supported languages use
anchored declaration patterns that never execute source or invoke toolchains.
"""
from __future__ import annotations

import ast
import contextlib
import fnmatch
import os
import re
import stat
from pathlib import Path

import sonder_runtime.adapters.filesystem.file_ops as file_ops


HARD_MAX_FILES = 2_000
HARD_MAX_TOTAL_BYTES = 8_000_000
HARD_MAX_FILE_BYTES = 512_000
HARD_MAX_SYMBOLS = 10_000
HARD_MAX_DISCOVERY_ENTRIES = 50_000
HARD_MAX_ERRORS = 200

DEFAULT_MAX_FILES = 200
DEFAULT_MAX_TOTAL_BYTES = 2_000_000
DEFAULT_MAX_FILE_BYTES = 256_000
DEFAULT_MAX_SYMBOLS = 2_000

EXTENSION_LANGUAGES = {
    ".py": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".c": "c", ".h": "cpp", ".cc": "cpp", ".cpp": "cpp",
    ".cxx": "cpp", ".hh": "cpp", ".hpp": "cpp", ".hxx": "cpp",
    ".cs": "csharp", ".rs": "rust", ".go": "go",
}
LANGUAGE_ALIASES = {
    "py": "python", "python": "python",
    "js": "javascript", "javascript": "javascript", "jsx": "javascript",
    "ts": "typescript", "typescript": "typescript", "tsx": "typescript",
    "c": "c", "cpp": "cpp", "c++": "cpp", "cc": "cpp",
    "csharp": "csharp", "c#": "csharp", "cs": "csharp",
    "rust": "rust", "rs": "rust", "go": "go", "golang": "go",
}

_IDENT = r"[A-Za-z_$][A-Za-z0-9_$]*"
_JS_DECL = re.compile(
    rf"^\s*(?:(?:export\s+)?(?:default\s+)?(?:declare\s+)?)"
    rf"(async\s+function|function|class|interface|enum|namespace|type)\s+({_IDENT})\b"
)
_JS_ARROW = re.compile(
    rf"^\s*(?:(?:export\s+)?(?:declare\s+)?)"
    rf"(?:const|let|var)\s+({_IDENT})\s*(?::[^=]+)?=\s*(?:async\s*)?"
    rf"(?:\([^)]*\)|{_IDENT})\s*=>"
)
_C_TYPE = re.compile(
    r"^\s*(?:typedef\s+)?(class|struct|union|enum|namespace)\s+"
    r"(?:class\s+)?([A-Za-z_]\w*)\b"
)
_C_MACRO = re.compile(r"^\s*#\s*define\s+([A-Za-z_]\w*)\b")
_C_FUNCTION = re.compile(
    r"^\s*(?:template\s*<[^;{}]*>\s*)?"
    r"(?:[A-Za-z_]\w*(?:::\w+)*(?:\s*<[^;{}()]*>)?[\s*&]+)+"
    r"([~A-Za-z_]\w*(?:::\w+)*)\s*\([^;{}]*\)\s*"
    r"(?:const\s*)?(?:noexcept(?:\([^)]*\))?\s*)?(?:->\s*[^;{]+\s*)?[;{]\s*$"
)
_CS_TYPE = re.compile(
    r"^\s*(?:(?:public|private|protected|internal|static|abstract|sealed|partial)\s+)*"
    r"(class|interface|struct|enum|record|namespace)\s+([A-Za-z_]\w*)\b"
)
_CS_METHOD = re.compile(
    r"^\s*(?:(?:public|private|protected|internal|static|virtual|override|abstract|"
    r"async|sealed|extern|unsafe|partial|new)\s+)+"
    r"(?:[A-Za-z_]\w*(?:[.<>,?\[\]]|::)*)\s+([A-Za-z_]\w*)\s*\([^;{}]*\)"
)
_RUST_DECL = re.compile(
    r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:unsafe\s+|async\s+|const\s+|extern\s+)*"
    r"(fn|struct|enum|trait|type|mod|union|static|const)\s+([A-Za-z_]\w*)\b"
)
_RUST_MACRO = re.compile(r"^\s*macro_rules!\s*([A-Za-z_]\w*)\b")
_GO_DECL = re.compile(
    r"^\s*(func|type|var|const)\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\b"
)


def _bounded(value, default: int, ceiling: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(ceiling, parsed))


def _language_filter(value: str) -> str:
    raw = str(value or "").strip().casefold()
    if not raw or raw in {"auto", "all", "*"}:
        return ""
    try:
        return LANGUAGE_ALIASES[raw]
    except KeyError as exc:
        raise ValueError(
            "unsupported language %r; use python, javascript, typescript, c, "
            "cpp, csharp, rust, go, or auto" % value
        ) from exc


def _is_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        attrs = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _opened_handle_path(fd: int) -> Path:
    """Return the path of the already-open handle, or fail closed."""
    if os.name == "nt":
        import ctypes
        import msvcrt

        handle = msvcrt.get_osfhandle(fd)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        function = kernel32.GetFinalPathNameByHandleW
        function.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
        function.restype = ctypes.c_uint32
        needed = function(handle, None, 0, 0)
        if not needed:
            raise OSError(ctypes.get_last_error(), "could not resolve opened file handle")
        buffer = ctypes.create_unicode_buffer(needed + 1)
        if not function(handle, buffer, len(buffer), 0):
            raise OSError(ctypes.get_last_error(), "could not resolve opened file handle")
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return Path(value)
    for link in ("/proc/self/fd/%d" % fd, "/dev/fd/%d" % fd):
        try:
            value = os.readlink(link)
        except OSError:
            continue
        if value.endswith(" (deleted)"):
            raise PermissionError("opened source was deleted during validation")
        return Path(value)
    raise PermissionError("platform cannot validate an opened file handle")


@contextlib.contextmanager
def _open_guarded_binary(path: Path, extra_roots: str):
    """Open without following a replacement link and validate the handle."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    if os.name == "nt":
        import ctypes
        import msvcrt

        class FileAttributeTagInfo(ctypes.Structure):
            _fields_ = [("attributes", ctypes.c_uint32), ("reparse_tag", ctypes.c_uint32)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel32.CreateFileW
        create.argtypes = [
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ]
        create.restype = ctypes.c_void_p
        raw_handle = create(
            str(path), 0x80000000, 0x00000001 | 0x00000002 | 0x00000004,
            None, 3, 0x00200000 | 0x08000000, None,
        )
        invalid = ctypes.c_void_p(-1).value
        if raw_handle == invalid:
            raise OSError(ctypes.get_last_error(), "could not open guarded source")
        info = FileAttributeTagInfo()
        get_info = kernel32.GetFileInformationByHandleEx
        get_info.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        get_info.restype = ctypes.c_int
        if not get_info(raw_handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(raw_handle)
            raise OSError(ctypes.get_last_error(), "could not inspect guarded source")
        if info.attributes & 0x00000400:
            kernel32.CloseHandle(raw_handle)
            raise PermissionError("replacement symlink or junction is not indexed")
        try:
            fd = msvcrt.open_osfhandle(raw_handle, flags)
        except Exception:
            kernel32.CloseHandle(raw_handle)
            raise
    else:
        fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise PermissionError("opened source is not a regular file")
        actual = file_ops.resolve_repository_read_path(
            str(_opened_handle_path(fd)), allow_workspace_root=False,
            reject_sensitive=True, extra_roots=extra_roots,
        )
        current = actual.stat(follow_symlinks=False)
        if not os.path.samestat(opened, current):
            raise PermissionError("source changed while validating its opened handle")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            yield handle
    finally:
        os.close(fd)


def _requested_path(path: str) -> Path:
    candidate = Path(str(path or ".")).expanduser()
    if not candidate.is_absolute():
        candidate = file_ops.workspace_root() / candidate
    return candidate.absolute()


def _reject_symlinked_root(path: str) -> None:
    requested = _requested_path(path)
    if _is_reparse(requested):
        raise PermissionError("symbol index root may not be a symlink or junction")
    lexical = os.path.normcase(os.path.normpath(os.path.abspath(str(requested))))
    physical = os.path.normcase(os.path.normpath(os.path.realpath(str(requested))))
    if lexical != physical:
        raise PermissionError("symbol index root traverses a symlink or junction")


def _matches_glob(relative: str, pattern: str) -> bool:
    relative = relative.replace("\\", "/")
    name = relative.rsplit("/", 1)[-1]
    patterns = [pattern]
    if pattern.startswith("**/"):
        patterns.append(pattern[3:])
    return any(
        fnmatch.fnmatchcase(relative, candidate)
        or fnmatch.fnmatchcase(name, candidate)
        for candidate in patterns
    )


def _iter_source_files(root: Path, pattern: str):
    """Yield deterministic candidates without following links or reparse points."""
    if root.is_file():
        if _matches_glob(root.name, pattern):
            yield root, root.name, 1
        return
    stack = [(root, "")]
    discovered = 0
    while stack:
        directory, prefix = stack.pop()
        try:
            entries = []
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    discovered += 1
                    if discovered > HARD_MAX_DISCOVERY_ENTRIES:
                        yield None, "", discovered, "__DISCOVERY_LIMIT__"
                        return
                    entries.append(entry)
            entries.sort(key=lambda entry: (entry.name.casefold(), entry.name))
        except OSError as exc:
            yield None, prefix or ".", discovered, "could not scan directory: %s" % exc
            continue
        child_dirs = []
        for entry in entries:
            relative = "%s/%s" % (prefix, entry.name) if prefix else entry.name
            child = Path(entry.path)
            try:
                if entry.is_symlink() or _is_reparse(child):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name.casefold() in file_ops.SENSITIVE_READ_DIRECTORIES:
                        continue
                    child_dirs.append((child, relative))
                elif entry.is_file(follow_symlinks=False) and _matches_glob(relative, pattern):
                    yield child, relative.replace("\\", "/"), discovered
            except OSError:
                yield None, relative.replace("\\", "/"), discovered, "could not inspect entry"
        for item in reversed(child_dirs):
            stack.append(item)


class _PythonSymbols(ast.NodeVisitor):
    def __init__(self, limit: int):
        self.scope = []
        self.scope_kinds = []
        self.symbols = []
        self.limit = limit
        self.overflow = False

    def _record(self, node, kind: str) -> None:
        if len(self.symbols) >= self.limit:
            self.overflow = True
            return
        name = str(getattr(node, "name", ""))
        qualified = ".".join(self.scope + [name])
        self.symbols.append({
            "line": int(getattr(node, "lineno", 1)),
            "column": int(getattr(node, "col_offset", 0)) + 1,
            "kind": kind,
            "name": qualified,
        })

    def visit_ClassDef(self, node):
        self._record(node, "class")
        self.scope.append(node.name)
        self.scope_kinds.append("class")
        self.generic_visit(node)
        self.scope_kinds.pop()
        self.scope.pop()

    def visit_FunctionDef(self, node):
        self._record(
            node,
            "method" if self.scope_kinds and self.scope_kinds[-1] == "class" else "function",
        )
        self.scope.append(node.name)
        self.scope_kinds.append("function")
        self.generic_visit(node)
        self.scope_kinds.pop()
        self.scope.pop()

    def visit_AsyncFunctionDef(self, node):
        self._record(
            node,
            "async_method"
            if self.scope_kinds and self.scope_kinds[-1] == "class"
            else "async_function",
        )
        self.scope.append(node.name)
        self.scope_kinds.append("function")
        self.generic_visit(node)
        self.scope_kinds.pop()
        self.scope.pop()


def _python_symbols(text: str, limit: int):
    tree = ast.parse(text)
    visitor = _PythonSymbols(limit)
    visitor.visit(tree)
    return visitor.symbols, visitor.overflow


def _regex_symbols(text: str, language: str, limit: int):
    symbols = []
    seen = set()
    for line_number, line in enumerate(text.splitlines(), 1):
        candidates = []
        if language in {"javascript", "typescript"}:
            match = _JS_DECL.match(line)
            if match:
                raw_kind = match.group(1).replace(" ", "_")
                candidates.append((raw_kind, match.group(2), match.start(2) + 1))
            match = _JS_ARROW.match(line)
            if match:
                candidates.append(("function", match.group(1), match.start(1) + 1))
        elif language in {"c", "cpp"}:
            match = _C_TYPE.match(line)
            if match:
                candidates.append((match.group(1), match.group(2), match.start(2) + 1))
            match = _C_MACRO.match(line)
            if match:
                candidates.append(("macro", match.group(1), match.start(1) + 1))
            match = _C_FUNCTION.match(line)
            if match:
                name = match.group(1)
                if name.rsplit("::", 1)[-1] not in {"if", "for", "while", "switch", "catch"}:
                    candidates.append(("function", name, match.start(1) + 1))
        elif language == "csharp":
            match = _CS_TYPE.match(line)
            if match:
                candidates.append((match.group(1), match.group(2), match.start(2) + 1))
            match = _CS_METHOD.match(line)
            if match:
                candidates.append(("method", match.group(1), match.start(1) + 1))
        elif language == "rust":
            match = _RUST_DECL.match(line)
            if match:
                candidates.append((match.group(1), match.group(2), match.start(2) + 1))
            match = _RUST_MACRO.match(line)
            if match:
                candidates.append(("macro", match.group(1), match.start(1) + 1))
        elif language == "go":
            match = _GO_DECL.match(line)
            if match:
                candidates.append((match.group(1), match.group(2), match.start(2) + 1))
        for kind, name, column in candidates:
            key = (line_number, column, kind, name)
            if key not in seen:
                if len(symbols) >= limit:
                    return symbols, True
                seen.add(key)
                symbols.append({
                    "line": line_number, "column": column,
                    "kind": kind, "name": name,
                })
    return symbols, False


def index_repository(
    path: str = ".",
    *,
    glob: str = "*",
    language: str = "",
    max_files: int = DEFAULT_MAX_FILES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_symbols: int = DEFAULT_MAX_SYMBOLS,
    extra_roots: str = "",
) -> dict:
    """Return a deterministic, bounded symbol index for one guarded path."""
    pattern = str(glob or "*").strip() or "*"
    selected_language = _language_filter(language)
    limits = {
        "max_files": _bounded(max_files, DEFAULT_MAX_FILES, HARD_MAX_FILES),
        "max_total_bytes": _bounded(
            max_total_bytes, DEFAULT_MAX_TOTAL_BYTES, HARD_MAX_TOTAL_BYTES
        ),
        "max_file_bytes": _bounded(
            max_file_bytes, DEFAULT_MAX_FILE_BYTES, HARD_MAX_FILE_BYTES
        ),
        "max_symbols": _bounded(max_symbols, DEFAULT_MAX_SYMBOLS, HARD_MAX_SYMBOLS),
    }
    _reject_symlinked_root(path)
    root = file_ops.resolve_repository_read_path(
        path, allow_workspace_root=True, reject_sensitive=True,
        extra_roots=extra_roots,
    )
    if not root.exists():
        raise FileNotFoundError("symbol index path not found: %s" % root)
    if not (root.is_dir() or root.is_file()):
        raise ValueError("symbol index path is not a file or directory: %s" % root)

    result = {
        "root": str(root), "glob": pattern,
        "language": selected_language or "auto", "limits": limits,
        "files": 0, "bytes": 0, "symbols": [], "errors": [],
        "truncated": False, "truncation_reasons": [],
    }

    def truncate(reason: str) -> None:
        result["truncated"] = True
        if reason not in result["truncation_reasons"]:
            result["truncation_reasons"].append(reason)

    def add_error(relative: str, error: str) -> bool:
        if len(result["errors"]) >= HARD_MAX_ERRORS:
            truncate("max_errors")
            return False
        result["errors"].append({"path": relative, "error": error})
        return True

    for item in _iter_source_files(root, pattern):
        if len(item) == 4:
            candidate, relative, _, error = item
            if error == "__DISCOVERY_LIMIT__":
                truncate("max_discovery_entries")
                break
            if not add_error(relative, error):
                break
            continue
        candidate, relative, _ = item
        detected = EXTENSION_LANGUAGES.get(candidate.suffix.casefold(), "")
        if not detected or (selected_language and detected != selected_language):
            continue
        if result["files"] >= limits["max_files"]:
            truncate("max_files")
            break
        result["files"] += 1
        try:
            guarded = file_ops.resolve_repository_read_path(
                str(candidate), allow_workspace_root=False, reject_sensitive=True,
                extra_roots=extra_roots,
            )
            if _is_reparse(candidate):
                raise PermissionError("symlink or junction is not indexed")
            size = guarded.stat().st_size
            if size > limits["max_file_bytes"]:
                if not add_error(
                    relative,
                    "file exceeds max_file_bytes (%d > %d)" % (
                        size, limits["max_file_bytes"]
                    ),
                ):
                    break
                continue
            if result["bytes"] + size > limits["max_total_bytes"]:
                truncate("max_total_bytes")
                break
            remaining_bytes = limits["max_total_bytes"] - result["bytes"]
            read_limit = min(limits["max_file_bytes"], remaining_bytes)
            with _open_guarded_binary(guarded, extra_roots) as handle:
                payload = handle.read(read_limit)
                observed_size = os.fstat(handle.fileno()).st_size
            if observed_size > limits["max_file_bytes"]:
                if not add_error(relative, "file grew beyond max_file_bytes while reading"):
                    break
                continue
            if observed_size > remaining_bytes:
                truncate("max_total_bytes")
                break
            result["bytes"] += len(payload)
            text = payload.decode("utf-8-sig")
            remaining_symbols = limits["max_symbols"] - len(result["symbols"])
            if remaining_symbols <= 0:
                truncate("max_symbols")
                break
            extracted, overflow = (
                _python_symbols(text, remaining_symbols) if detected == "python"
                else _regex_symbols(text, detected, remaining_symbols)
            )
        except SyntaxError as exc:
            if not add_error(
                relative,
                "syntax error at line %s: %s" % (
                    exc.lineno or "?", exc.msg or "invalid Python"
                ),
            ):
                break
            continue
        except UnicodeDecodeError as exc:
            if not add_error(relative, "invalid UTF-8 at byte %d" % exc.start):
                break
            continue
        except (OSError, PermissionError, ValueError) as exc:
            if not add_error(relative, str(exc)):
                break
            continue
        for symbol in extracted:
            result["symbols"].append({"path": relative, "language": detected, **symbol})
        if overflow:
            truncate("max_symbols")
            break
    return result


def format_index(data: dict) -> str:
    """Render stable line-oriented output suitable for tool observations."""
    limits = data["limits"]
    lines = [
        "repository symbol index",
        "  root: %s" % data["root"],
        "  filters: glob=%s language=%s" % (data["glob"], data["language"]),
        "  limits: files=%d total_bytes=%d file_bytes=%d symbols=%d" % (
            limits["max_files"], limits["max_total_bytes"],
            limits["max_file_bytes"], limits["max_symbols"],
        ),
        "  scanned: files=%d bytes=%d symbols=%d errors=%d" % (
            data["files"], data["bytes"], len(data["symbols"]), len(data["errors"]),
        ),
        "  truncated: %s%s" % (
            "yes" if data["truncated"] else "no",
            " (%s)" % ",".join(data["truncation_reasons"])
            if data["truncation_reasons"] else "",
        ),
        "symbols:",
    ]
    if data["symbols"]:
        lines.extend(
            "  {path}:{line}:{column} {kind} {name} [{language}]".format(**row)
            for row in data["symbols"]
        )
    else:
        lines.append("  (none)")
    if data["errors"]:
        lines.append("errors:")
        lines.extend("  %s: %s" % (row["path"], row["error"]) for row in data["errors"])
    return "\n".join(lines)
