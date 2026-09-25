"""``compile_commands.json`` parsing and the include-trace argv sanitizer.

Pure: callers pass bytes and (for response files) already-read text.

``sanitize_for_trace`` is an ALLOWLIST rebuild (F6). A compile-db argv can
execute code through flags such as ``-wrapper``, ``-fplugin=``,
``-Xclang -load``, ``-B``, ``-specs=``, ``--gcc-toolchain``, cl ``/B1``
``/B2`` ``/Bx`` ``/d1`` ``/d2`` or a compiler launcher, so nothing is copied
through unless it is on the short list of include/define/language flags that
only affect preprocessing. Every other token -- including every flag value --
is dropped and counted.
"""
from __future__ import annotations

import hashlib
import posixpath
import re
import shlex
from dataclasses import dataclass
from typing import Mapping

from .model import (
    BuildModel,
    BuildSystem,
    BuildTarget,
    CompileUnit,
    Generator,
    ModelSource,
    PchMode,
    TargetType,
    bounded_notes,
    finalize_model,
    clip,
    is_absolute,
    loads_bounded_json,
    norm_path,
    path_label,
    reject,
    rel_under,
)


MAX_DB_BYTES = 64 * 1024 * 1024
MAX_ENTRIES = 50_000
MAX_ENTRY_BYTES = 32 * 1024
MAX_ENTRY_TOKENS = 2000
MAX_RSP_BYTES = 256 * 1024
KNOWN_LAUNCHER_NAMES = frozenset({"ccache", "sccache", "buildcache", "clcache", "distcc", "icecc"})
_COMPILER_RE = re.compile(
    r"^(?P<name>gcc|g\+\+|cc|c\+\+|clang|clang\+\+|clang-cl|cl)(?:-\d+(?:\.\d+){0,2})?$"
)
SOURCE_SUFFIXES = frozenset({".c", ".cc", ".cpp", ".cxx", ".c++", ".cp", ".ixx", ".cppm", ".m", ".mm"})
HEADER_SUFFIXES = frozenset({".h", ".hh", ".hpp", ".hxx", ".h++", ".inl", ".ipp", ".tpp", ".inc"})


@dataclass(frozen=True, slots=True)
class CompileEntry:
    file: str
    file_label: str
    file_rel: str
    directory: str
    argv: tuple[str, ...]
    output: str = ""
    rsp_files: tuple[str, ...] = ()
    family: str = "other"
    windows: bool = False


@dataclass(frozen=True, slots=True)
class CompileDb:
    entries: tuple[CompileEntry, ...]
    truncated: bool
    notes: tuple[str, ...] = ()

    def entry_for(self, file_rel: str) -> CompileEntry | None:
        for entry in self.entries:
            if entry.file_rel == file_rel:
                return entry
        return None


@dataclass(frozen=True, slots=True)
class FlagSummary:
    compiler: str
    family: str
    style: str
    std: str
    define_count: int
    include_dir_count: int
    forced_includes: tuple[str, ...]
    pch: PchMode
    pch_header: str
    output: str
    rsp_files: tuple[str, ...]
    launcher: str
    flags_digest: str


@dataclass(frozen=True, slots=True)
class SanitizedArgv:
    argv: tuple[str, ...]
    family: str
    source: str
    dropped: int
    dangerous: int
    notes: tuple[str, ...] = ()
    # Headers the trace argv force-includes (PCH first); GCC/Clang ``-H``
    # does not list them, so the trace parser adds them as depth-1 edges.
    forced_includes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TraceRefused:
    code: str
    reason: str


# --- command splitting ----------------------------------------------------------

def _split_windows(command: str) -> list[str]:
    """``CommandLineToArgvW`` rules (post-2008 CRT: ``""`` inside quotes is a quote)."""
    args: list[str] = []
    n = len(command)
    i = 0
    # argv[0]: quotes delimit, no backslash processing.
    while i < n and command[i] in " \t":
        i += 1
    if i < n:
        if command[i] == '"':
            end = command.find('"', i + 1)
            end = n if end < 0 else end
            args.append(command[i + 1:end])
            i = end + 1
        else:
            start = i
            while i < n and command[i] not in " \t":
                i += 1
            args.append(command[start:i])
    while True:
        while i < n and command[i] in " \t":
            i += 1
        if i >= n:
            break
        buf: list[str] = []
        quoted = False
        while i < n:
            ch = command[i]
            if ch in " \t" and not quoted:
                break
            if ch == "\\":
                j = i
                while j < n and command[j] == "\\":
                    j += 1
                count = j - i
                if j < n and command[j] == '"':
                    buf.append("\\" * (count // 2))
                    if count % 2:
                        buf.append('"')
                        i = j + 1
                    else:
                        i = j
                else:
                    buf.append("\\" * count)
                    i = j
                continue
            if ch == '"':
                if quoted and i + 1 < n and command[i + 1] == '"':
                    buf.append('"')
                    i += 2
                    continue
                quoted = not quoted
                i += 1
                continue
            buf.append(ch)
            i += 1
        args.append("".join(buf))
    return args


def split_command(command: str, *, windows: bool) -> tuple[str, ...]:
    """Split a compile-db ``command`` string (Windows or POSIX rules), bounded."""
    if not isinstance(command, str):
        raise reject("BUILD_TREE_REJECTED", "command must be a string")
    if len(command.encode("utf-8", errors="replace")) > MAX_ENTRY_BYTES or "\x00" in command:
        raise reject("BUILD_TREE_REJECTED", "compile command exceeds bounds")
    if windows:
        tokens = _split_windows(command)
    else:
        try:
            tokens = shlex.split(command, posix=True)
        except ValueError as exc:
            raise reject("BUILD_TREE_REJECTED", "unbalanced quoting in compile command") from exc
    if len(tokens) > MAX_ENTRY_TOKENS:
        raise reject("BUILD_TREE_REJECTED", "compile command has too many tokens")
    return tuple(tokens)


def _basename(token: str) -> str:
    base = token.replace("\\", "/").rsplit("/", 1)[-1]
    lower = base.lower()
    return base[:-4] if lower.endswith(".exe") else base


def compiler_name(token: str) -> str:
    """``g++``/``clang-cl``/``cl`` ... for a compiler token, or ''."""
    match = _COMPILER_RE.fullmatch(_basename(token).lower())
    return match.group("name") if match else ""


def family_for_compiler(token: str) -> str:
    name = compiler_name(token)
    if name in ("cl",):
        return "msvc"
    if name == "clang-cl":
        return "clang_cl"
    if name in ("clang", "clang++"):
        return "clang"
    if name in ("gcc", "g++", "cc", "c++"):
        return "gnu"
    return "other"


def _is_windows_command(command: str, directory: str) -> bool:
    first = command.lstrip().split(" ", 1)[0].strip('"').lower()
    return bool(re.match(r"^[A-Za-z]:[\\/]", directory or "")) or first.endswith(".exe") \
        or compiler_name(first) in ("cl", "clang-cl")


# --- flag classification ----------------------------------------------------------

def strip_launcher(argv: tuple[str, ...] | list[str],
                   known_launchers: frozenset[str]) -> tuple[tuple[str, ...], str]:
    """Drop a leading compiler launcher only when it is a known inventory record."""
    tokens = tuple(argv)
    if tokens and _basename(tokens[0]).lower() in KNOWN_LAUNCHER_NAMES:
        name = _basename(tokens[0]).lower()
        if name in {item.lower() for item in known_launchers}:
            return tokens[1:], name
    return tokens, ""


def _msvc_flag(token: str) -> str:
    """cl accepts ``-X`` and ``/X`` alike; canonicalize to ``/X``."""
    if token.startswith("-") and len(token) > 1:
        return "/" + token[1:]
    return token


_GNU_VALUE_FLAGS = frozenset({
    "-I", "-isystem", "-iquote", "-idirafter", "-D", "-U", "-include", "-imacros", "-o",
    "-x", "-MF", "-MT", "-MQ", "-Xclang", "-Xlinker", "-Xpreprocessor", "-Xassembler",
    "-target", "-B", "-L", "-l", "-arch", "-isysroot", "--sysroot", "-wrapper",
    "-specs", "-include-pch", "-iprefix", "-iwithprefix", "-mllvm",
})
_MSVC_VALUE_FLAGS = frozenset({"/I", "/D", "/U", "/FI", "/external:I", "/Fo", "/Fp", "/Fd"})


def classify_flags(argv: tuple[str, ...] | list[str], *,
                   known_launchers: frozenset[str] = frozenset()) -> FlagSummary:
    """Coarse facts about one compile argv (nothing is executed or trusted)."""
    tokens, launcher = strip_launcher(tuple(argv), known_launchers)
    if not launcher and tokens and _basename(tokens[0]).lower() in KNOWN_LAUNCHER_NAMES:
        launcher = _basename(tokens[0]).lower()
        tokens = tokens[1:]
    compiler = tokens[0] if tokens else ""
    family = family_for_compiler(compiler)
    style = "msvc" if family in ("msvc", "clang_cl") else "gnu"
    std = ""
    defines = includes = 0
    forced: list[str] = []
    pch = PchMode.NONE
    pch_header = ""
    output = ""
    rsp: list[str] = []
    index = 1
    while index < len(tokens):
        token = tokens[index]
        nxt = tokens[index + 1] if index + 1 < len(tokens) else ""
        if token.startswith("@"):
            rsp.append(token[1:])
            index += 1
            continue
        if style == "msvc":
            flag = _msvc_flag(token)
            upper = flag[:3].upper()
            if flag in _MSVC_VALUE_FLAGS:
                value, index = nxt, index + 2
                flag_name = flag
            else:
                value, index = "", index + 1
                flag_name = ""
            if flag_name == "/I" or flag.startswith("/I") and len(flag) > 2 \
                    or flag_name == "/external:I" or flag.startswith("/external:I") and len(flag) > 11:
                includes += 1
            elif flag_name == "/D" or flag.startswith("/D") and len(flag) > 2 and not flag.startswith("/DEBUG"):
                defines += 1
            elif flag_name == "/FI" or upper == "/FI":
                forced.append(value or flag[3:])
                if pch is PchMode.NONE:
                    pch = PchMode.FORCED_INCLUDE
            elif upper == "/YU":
                pch = PchMode.USE
                pch_header = flag[3:] or pch_header
            elif upper == "/YC":
                pch = PchMode.CREATE
                pch_header = flag[3:] or pch_header
            elif flag.startswith("/std:"):
                std = flag[5:]
            elif flag_name == "/Fo" or flag.startswith("/Fo"):
                output = value or flag[3:]
            continue
        if token in _GNU_VALUE_FLAGS:
            value = nxt
            index += 2
            if token in ("-I", "-isystem", "-iquote", "-idirafter"):
                includes += 1
            elif token == "-D":
                defines += 1
            elif token == "-include":
                forced.append(value)
            elif token == "-o":
                output = value
            continue
        index += 1
        if token.startswith("-I") or token.startswith("-isystem") or token.startswith("-iquote"):
            includes += 1
        elif token.startswith("-D"):
            defines += 1
        elif token.startswith("-std="):
            std = token[5:]
    real_forced = [item for item in forced if not _basename(item).startswith("cmake_pch")]
    if any(_basename(item).startswith("cmake_pch") for item in forced):
        pch = PchMode.USE
    elif real_forced and pch is PchMode.NONE:
        pch = PchMode.FORCED_INCLUDE
    digest_material = "\x1f".join(token for token in tokens[1:] if not _looks_path_like(token))
    return FlagSummary(
        compiler=compiler, family=family, style=style, std=clip(std, 32),
        define_count=defines, include_dir_count=includes,
        forced_includes=tuple(clip(item, 1024) for item in real_forced[:16]),
        pch=pch, pch_header=clip(pch_header, 1024), output=clip(output, 1024),
        rsp_files=tuple(clip(item, 1024) for item in rsp[:16]), launcher=launcher,
        flags_digest=hashlib.sha256(digest_material.encode("utf-8", "replace")).hexdigest()[:16],
    )


def _looks_path_like(token: str) -> bool:
    return is_absolute(token) or "/" in token.replace("\\", "/") and not token.startswith("-")


# --- compile_commands.json --------------------------------------------------------

def parse_compile_commands(data: bytes, *, source_root: str, build_dir: str,
                           max_entries: int = MAX_ENTRIES) -> CompileDb:
    """Parse and bound a compile database; response files are recorded, not expanded."""
    document = loads_bounded_json(data, max_bytes=MAX_DB_BYTES, what="compile_commands.json")
    if not isinstance(document, list):
        raise reject("BUILD_TREE_REJECTED", "compile_commands.json must be a list")
    limit = max(1, min(int(max_entries), MAX_ENTRIES))
    entries: list[CompileEntry] = []
    notes: list[str] = []
    truncated = len(document) > limit
    skipped = 0
    for raw in document[:limit]:
        entry = _entry(raw, source_root=source_root, build_dir=build_dir)
        if entry is None:
            skipped += 1
            continue
        entries.append(entry)
    if truncated:
        notes.append("compile database truncated at %d entries" % limit)
    if skipped:
        notes.append("%d compile database entries were malformed or oversized" % skipped)
    return CompileDb(entries=tuple(entries), truncated=truncated or bool(skipped),
                     notes=bounded_notes(notes))


def _entry(raw: object, *, source_root: str, build_dir: str) -> CompileEntry | None:
    if not isinstance(raw, dict):
        return None
    directory = raw.get("directory")
    file = raw.get("file")
    if not isinstance(directory, str) or not isinstance(file, str) or not file:
        return None
    if len(directory) > 4096 or len(file) > 4096 or "\x00" in directory + file:
        return None
    windows = False
    try:
        if isinstance(raw.get("arguments"), list):
            arguments = raw["arguments"]
            if len(arguments) > MAX_ENTRY_TOKENS or not all(isinstance(x, str) for x in arguments):
                return None
            if sum(len(x) for x in arguments) > MAX_ENTRY_BYTES or any("\x00" in x for x in arguments):
                return None
            argv = tuple(arguments)
            windows = bool(argv) and (_is_windows_command(argv[0], directory))
        elif isinstance(raw.get("command"), str):
            windows = _is_windows_command(raw["command"], directory)
            argv = split_command(raw["command"], windows=windows)
        else:
            return None
    except Exception:  # noqa: BLE001 - a malformed entry is skipped, never fatal
        return None
    if not argv:
        return None
    absolute = file if is_absolute(file) else norm_path(directory + "/" + file)
    label, rel = path_label(absolute, source_root=source_root, build_dir=build_dir)
    output = raw.get("output") if isinstance(raw.get("output"), str) else ""
    summary = classify_flags(argv)
    return CompileEntry(
        file=norm_path(absolute), file_label=clip(label, 1024), file_rel=clip(rel, 1024),
        directory=norm_path(directory), argv=argv, output=clip(output or summary.output, 1024),
        rsp_files=summary.rsp_files, family=summary.family, windows=windows,
    )


# --- include-trace sanitizer ------------------------------------------------------

_STD_GNU_RE = re.compile(r"^-std=[A-Za-z0-9+]{1,16}$")
_ARCH_RE = re.compile(
    r"^-m(?:32|64|x32|(?:arch|tune|cpu|fpu|float-abi)=[A-Za-z0-9_.+-]{1,32}"
    r"|(?:sse|avx|fma|bmi|popcnt|f16c|lzcnt|crc32|aes|pclmul)[A-Za-z0-9_.]{0,16})$"
)
_TARGET_RE = re.compile(r"^--target=[A-Za-z0-9_.-]{1,64}$")
_GNU_KEEP_BARE = frozenset({"-fexceptions", "-fno-exceptions", "-frtti", "-fno-rtti"})
_X_LANGS = frozenset({"c", "c++"})
_STD_MSVC_RE = re.compile(r"^/std:[A-Za-z0-9+]{1,16}$")
_EH_RE = re.compile(r"^/EH[a-z-]{0,4}$")
_ZC_RE = re.compile(r"^/Zc:[A-Za-z0-9_:+-]{1,40}$")
_EXTERNAL_W_RE = re.compile(r"^/external:W[0-4]$")
_MSVC_KEEP_BARE = frozenset({"/GR", "/GR-", "/permissive-", "/TP", "/TC"})
_DANGEROUS_PREFIXES = (
    "-wrapper", "-fplugin", "-Xclang", "-B", "-specs", "--gcc-toolchain", "-fuse-ld",
    "/B1", "/B2", "/Bx", "/d1", "/d2", "/analyze:plugin", "-load", "-add-plugin",
    "--sysroot", "-isysroot", "-mllvm",
)


def _value_ok(value: str) -> bool:
    return bool(value) and "\x00" not in value and "\n" not in value and "\r" not in value


def _inside(path: str, roots: tuple[str, ...], directory: str) -> str:
    """The normalized absolute path when inside one of ``roots``, else ''."""
    if not _value_ok(path):
        return ""
    absolute = path if is_absolute(path) else norm_path((directory or "") + "/" + path)
    for root in roots:
        if root and rel_under(absolute, root) not in (None, "."):
            return norm_path(absolute)
    return ""


def _same_file(a: str, b: str, directory: str) -> bool:
    left = a if is_absolute(a) else norm_path((directory or "") + "/" + a)
    right = b if is_absolute(b) else norm_path((directory or "") + "/" + b)
    left, right = norm_path(left), norm_path(right)
    if re.match(r"^[A-Za-z]:", left) or re.match(r"^[A-Za-z]:", right):
        return left.casefold() == right.casefold()
    return left == right


def _is_cmake_pch(path: str) -> bool:
    return _basename(path).startswith("cmake_pch")


def sanitize_for_trace(
    argv: tuple[str, ...] | list[str],
    family: str,
    *,
    source_file: str,
    roots: tuple[str, ...],
    directory: str = "",
    pch_header: str = "",
    known_launchers: frozenset[str] = frozenset(),
    rsp_contents: Mapping[str, str] | None = None,
    windows: bool = False,
) -> SanitizedArgv | TraceRefused:
    """Rebuild a compile argv from an allowlist for a preprocessing-only trace.

    ``roots`` bound which ``-include``/``/FI`` headers are kept (source root
    and build dir). ``pch_header`` is the real PCH header; ``/Yc``, ``/Yu``,
    ``/Fp`` and CMake's ``cmake_pch.hxx`` forced include are replaced by a
    forced include of it. The adapter must still verify that the compiler
    realpath-equals an inventory record before launching.
    """
    tokens = tuple(argv)
    if not tokens:
        return TraceRefused("RUNNER_UNAVAILABLE", "empty compile command")
    stripped, launcher = strip_launcher(tokens, known_launchers)
    if stripped and _basename(stripped[0]).lower() in KNOWN_LAUNCHER_NAMES:
        return TraceRefused("RUNNER_UNAVAILABLE",
                            "compiler launcher %s is not an inventory record"
                            % _basename(stripped[0]).lower())
    tokens = stripped
    if not tokens:
        return TraceRefused("RUNNER_UNAVAILABLE", "compile command has no compiler")
    compiler = tokens[0]
    derived = family_for_compiler(compiler)
    if derived == "other":
        return TraceRefused("RUNNER_UNAVAILABLE", "compiler is not gcc, clang, clang-cl or cl")
    style = "msvc" if derived in ("msvc", "clang_cl") else "gnu"
    wanted = "msvc" if family in ("msvc", "clang_cl") else "gnu"
    if family not in ("gnu", "clang", "msvc", "clang_cl") or style != wanted:
        return TraceRefused("RUNNER_UNAVAILABLE", "compiler family does not match the model")

    expanded: list[str] = []
    for token in tokens[1:]:
        if token.startswith("@"):
            if rsp_contents is None or token[1:] not in rsp_contents:
                return TraceRefused("RUNNER_UNAVAILABLE", "response file was not provided")
            text = rsp_contents[token[1:]]
            if not isinstance(text, str) or len(text.encode("utf-8", "replace")) > MAX_RSP_BYTES:
                return TraceRefused("RUNNER_UNAVAILABLE", "response file exceeds bounds")
            try:
                inner = split_command("x " + text.replace("\r", " ").replace("\n", " "),
                                      windows=windows or style == "msvc")[1:]
            except Exception:  # noqa: BLE001
                return TraceRefused("RUNNER_UNAVAILABLE", "response file is malformed")
            if any(item.startswith("@") for item in inner):
                return TraceRefused("RUNNER_UNAVAILABLE", "nested response files are refused")
            expanded.extend(inner)
        else:
            expanded.append(token)
    if len(expanded) > MAX_ENTRY_TOKENS:
        return TraceRefused("RUNNER_UNAVAILABLE", "compile command has too many tokens")

    kept: list[str] = []
    dropped = dangerous = 0
    source = ""
    forced: list[str] = []
    wants_pch = False
    index = 0

    def dangerous_token(tok: str) -> bool:
        probe = tok if style == "gnu" else _msvc_flag(tok)
        return any(probe.startswith(prefix) or tok.startswith(prefix) for prefix in _DANGEROUS_PREFIXES)

    while index < len(expanded):
        token = expanded[index]
        nxt = expanded[index + 1] if index + 1 < len(expanded) else ""
        if dangerous_token(token):
            dangerous += 1
        if not source and _same_file(token, source_file, directory):
            source = token
            index += 1
            continue
        if not (token.startswith("-") or (style == "msvc" and token.startswith("/"))):
            dropped += 1
            index += 1
            continue
        if style == "gnu":
            consumed, keep = _gnu_token(token, nxt, roots, directory, forced)
        else:
            consumed, keep, pch_seen = _msvc_token(token, nxt, roots, directory, forced,
                                                   source_file)
            if pch_seen:
                wants_pch = True
            if keep and keep[0] == "<source>":
                source = keep[1]
                keep = ()
                index += consumed
                continue
        if keep == ("<pch>",):
            wants_pch = True
            keep = ()
        if keep:
            kept.extend(keep)
        else:
            dropped += consumed
        index += consumed
    if not source:
        return TraceRefused("UNKNOWN_FILE", "the compile command does not name the requested file")
    notes: list[str] = []
    if wants_pch or pch_header:
        header = _inside(pch_header, roots, directory) if pch_header else ""
        if header:
            flag = ("-include", header) if style == "gnu" else ("/FI" + header,)
            if header not in forced:
                # The PCH is always the first thing a TU includes.
                kept[0:0] = list(flag)
                forced.insert(0, header)
            notes.append("precompiled header replaced by a forced include of its real header")
        elif wants_pch:
            notes.append("precompiled header use dropped; real header unknown")
    trace_flags = ("-fsyntax-only", "-H") if style == "gnu" else ("/Zs", "/showIncludes", "/nologo")
    if launcher:
        notes.append("compiler launcher %s stripped" % launcher)
    if dropped:
        notes.append("%d compile arguments dropped by the trace allowlist" % dropped)
    if dangerous:
        notes.append("%d code-executing compile arguments dropped" % dangerous)
    return SanitizedArgv(
        argv=(compiler, *kept, *trace_flags, source), family=derived, source=source,
        dropped=dropped, dangerous=dangerous, notes=bounded_notes(notes),
        forced_includes=tuple(forced[:16]),
    )


def _gnu_token(token: str, nxt: str, roots: tuple[str, ...], directory: str,
               forced: list[str]) -> tuple[int, tuple[str, ...]]:
    for flag in ("-isystem", "-iquote", "-I"):
        if token == flag:
            return (2, (flag, nxt)) if _value_ok(nxt) and not nxt.startswith("-") else (2, ())
        if token.startswith(flag) and len(token) > len(flag):
            value = token[len(flag):]
            return (1, (token,)) if _value_ok(value) and not value.startswith("-") else (1, ())
    if token in ("-D", "-U"):
        return (2, (token, nxt)) if _value_ok(nxt) and not nxt.startswith("-") else (2, ())
    if (token.startswith("-D") or token.startswith("-U")) and len(token) > 2:
        return 1, (token,)
    if _STD_GNU_RE.fullmatch(token) or token in _GNU_KEEP_BARE or _ARCH_RE.fullmatch(token) \
            or _TARGET_RE.fullmatch(token):
        return 1, (token,)
    if token == "-x":
        return (2, ("-x", nxt)) if nxt in _X_LANGS else (2, ())
    if token == "-include":
        if _is_cmake_pch(nxt):
            return 2, ("<pch>",)
        header = _inside(nxt, roots, directory)
        if header and posixpath.splitext(header)[1].lower() in HEADER_SUFFIXES:
            forced.append(header)
            return 2, ("-include", header)
        return 2, ()
    if token in _GNU_VALUE_FLAGS:
        return 2, ()
    return 1, ()


def _msvc_token(token: str, nxt: str, roots: tuple[str, ...], directory: str,
                forced: list[str], source_file: str) -> tuple[int, tuple[str, ...], bool]:
    flag = _msvc_flag(token)
    for name in ("/external:I", "/I"):
        if flag == name:
            ok = _value_ok(nxt) and not nxt.startswith("-")
            return 2, ((name, nxt) if ok else ()), False
        if flag.startswith(name) and len(flag) > len(name):
            return 1, (flag,), False
    if flag in ("/D", "/U"):
        return 2, ((flag, nxt) if _value_ok(nxt) else ()), False
    if (flag.startswith("/D") or flag.startswith("/U")) and len(flag) > 2 \
            and not flag.upper().startswith("/DEBUG"):
        return 1, (flag,), False
    if _STD_MSVC_RE.fullmatch(flag) or _EH_RE.fullmatch(flag) or _ZC_RE.fullmatch(flag) \
            or _EXTERNAL_W_RE.fullmatch(flag) or flag in _MSVC_KEEP_BARE:
        return 1, (flag,), False
    upper = flag[:3].upper()
    if upper in ("/YU", "/YC"):
        return 1, (), True
    if upper == "/FP":
        return 1, (), False
    if flag == "/FI" or upper == "/FI":
        value = nxt if flag == "/FI" else flag[3:]
        consumed = 2 if flag == "/FI" else 1
        if _is_cmake_pch(value):
            return consumed, (), True
        header = _inside(value, roots, directory)
        if header and posixpath.splitext(header)[1].lower() in HEADER_SUFFIXES:
            forced.append(header)
            return consumed, ("/FI" + header,), False
        return consumed, (), False
    if upper in ("/TP", "/TC") and len(flag) > 3 and flag[:3] in ("/Tp", "/Tc"):
        value = flag[3:]
        if _same_file(value, source_file, directory):
            return 1, ("<source>", value), False
        return 1, (), False
    return 1, (), False


# --- fallback model ---------------------------------------------------------------

_OBJECT_TARGET_RE = re.compile(r"(?:^|/)CMakeFiles/([A-Za-z0-9_][A-Za-z0-9_.+-]{0,127})\.dir/")


def model_from_compile_db(
    db: CompileDb,
    *,
    source_root: str,
    build_dir: str,
    project_label: str,
    system: BuildSystem = BuildSystem.NINJA,
    generator: Generator = Generator.OTHER,
    configs: tuple[str, ...] = (),
    created_at: float = 0.0,
    known_launchers: frozenset[str] = frozenset(),
) -> BuildModel:
    """A model from ``compile_commands.json`` alone (no File API reply).

    Targets are recovered from CMake object paths (``CMakeFiles/<t>.dir/``)
    and typed UNKNOWN; nothing about build-time tools or utility targets is
    known, so the notes say so and the fix loop stays conservative.
    """
    units: list[CompileUnit] = []
    targets: dict[str, int] = {}
    seen: set[tuple[str, str]] = set()
    for entry in db.entries:
        if not entry.file_rel:
            continue
        match = _OBJECT_TARGET_RE.search(norm_path(entry.output))
        target = match.group(1) if match else ""
        key = (target, entry.file_rel)
        if key in seen:
            continue
        seen.add(key)
        facts = classify_flags(entry.argv, known_launchers=known_launchers)
        forced = []
        pch_header = ""
        for header in facts.forced_includes:
            label, _rel = path_label(header if is_absolute(header) else
                                     norm_path(entry.directory + "/" + header),
                                     source_root=source_root, build_dir=build_dir)
            forced.append(label)
        if facts.pch_header:
            pch_header, _rel = path_label(facts.pch_header if is_absolute(facts.pch_header) else
                                          norm_path(entry.directory + "/" + facts.pch_header),
                                          source_root=source_root, build_dir=build_dir)
        if target:
            targets[target] = targets.get(target, 0) + 1
        units.append(CompileUnit(
            file_label=entry.file_label or entry.file_rel, file_rel=entry.file_rel, target=target,
            language="C" if entry.file_rel.lower().endswith(".c") else "CXX",
            family=entry.family if entry.family in ("gnu", "clang", "msvc", "clang_cl") else "other",
            std=facts.std, define_count=facts.define_count, include_dir_count=facts.include_dir_count,
            pch=facts.pch, pch_header=pch_header if not pch_header.startswith("<build>") else "",
            forced_includes=tuple(forced[:16]), flags_digest=facts.flags_digest,
        ))
    model = BuildModel(
        project_label=clip(project_label, 1024) or "project", source_root=norm_path(source_root),
        build_dir=norm_path(build_dir), system=system, generator=generator,
        configs=tuple(configs[:32]),
        targets=tuple(BuildTarget(name=name, type=TargetType.UNKNOWN, source_count=count)
                      for name, count in sorted(targets.items())[:2000]),
        units=tuple(units[:50_000]), source=ModelSource.COMPILE_DB, compile_db_available=True,
        truncated=db.truncated or len(units) > 50_000,
        notes=db.notes + ("model from compile_commands.json only: target types, build-time "
                          "tools and utility targets are unknown",),
        created_at=float(created_at),
    )
    return finalize_model(model)


__all__ = [
    "CompileDb", "CompileEntry", "FlagSummary", "HEADER_SUFFIXES", "KNOWN_LAUNCHER_NAMES",
    "MAX_ENTRIES", "SOURCE_SUFFIXES", "SanitizedArgv", "TraceRefused", "classify_flags",
    "compiler_name", "family_for_compiler", "model_from_compile_db", "parse_compile_commands",
    "sanitize_for_trace", "split_command", "strip_launcher",
]
