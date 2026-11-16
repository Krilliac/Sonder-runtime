"""The small files git reads to find a repository, read and parsed without git.

:mod:`live_checkout` asks "would git, run here, act on the live checkout?"
from the files git itself consults: a ``.git`` file's ``gitdir:`` line, a git
directory's ``commondir``, ``HEAD`` and loose refs, and ``core.worktree`` in
its config files.  A reader that treats a file it cannot fully read as
missing fails open (a ``.git`` file padded past a size cap was once invisible
while git, which strips trailing newlines, still used it), so every read here
has three outcomes:

* ``OK``: the bytes were read and parsed exactly as git parses them;
* ``ABSENT``: the file is not there (``ENOENT`` or ``ENOTDIR`` only);
* ``UNKNOWN``: anything else, with an :class:`Unknown` reason: larger than
  :data:`MAX_READ_BYTES`, any other ``OSError``, a path Python cannot name, a
  NUL byte, bytes that are not UTF-8, or text this module cannot parse
  exactly as git does (a BOM or a continuation line in a config that mentions
  ``worktree``, whitespace git does not trim, a drive-relative path).

Callers treat ``UNKNOWN`` as "could be the live repository".  Nothing here
runs git, writes, or caches; every function is stateless and safe from any
thread.  Each read holds at most :data:`MAX_READ_BYTES` in memory (a large
config is scanned in chunks instead).
"""
from __future__ import annotations

import errno
import os
import re
import stat
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PureWindowsPath

# Git refuses a .git file above 1 MiB; the same cap bounds every read here.
MAX_READ_BYTES = 1024 * 1024
_SCAN_CHUNK_BYTES = 64 * 1024
GITFILE_PREFIX = b"gitdir: "
_UTF8_BOM = b"\xef\xbb\xbf"
_WORKTREE_KEY = b"worktree"
_SYMREF_PREFIX = "ref:"
# Git's isspace(): space, tab, newline and carriage return; not \v or \f.
_GIT_SPACE = " \t\n\r"
# Reads of HEAD and the loose refs it names; git gives up after five as well.
MAX_REF_READS = 5
# Refs every worktree keeps for itself, under its own git directory.
_PER_WORKTREE_REF_PREFIXES = ("refs/worktree/", "refs/bisect/", "refs/rewritten/")
_ABSENT_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR})

_OBJECT_ID = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})(?:[ \t\n\r]|$)")
_REFNAME_FORBIDDEN = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]|\.\.|@\{|//|/\.|\.lock(?:/|$)")
_CONFIG_HEADER = re.compile(r'\[([A-Za-z0-9.-]+)(?:[ \t]+"((?:[^"\\]|\\.)*)")?\]')
_CONFIG_KEY = re.compile(r"([A-Za-z][A-Za-z0-9-]*)[ \t]*(?:=(.*))?")
_CONFIG_ESCAPES = {"t": "\t", "b": "\b", "n": "\n", "\\": "\\", '"': '"'}


class ReadState(Enum):
    """The three outcomes of every read and parse here."""

    OK = "ok"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class Unknown(str, Enum):
    """Why a file or a value could not be known."""

    TOO_LARGE = "larger than the read cap"
    OS_ERROR = "the operating system refused to report it"
    UNNAMEABLE = "the path cannot be named on this system"
    NOT_REGULAR = "not a regular file"
    NUL_BYTE = "holds a NUL byte"
    UNDECODABLE = "not valid UTF-8"
    UNPARSED = "not parsed exactly as git parses it"
    DRIVE_RELATIVE = "a drive-relative path or a rooted path without a drive"
    TOO_DEEP = "symbolic refs nested deeper than git follows"


@dataclass(frozen=True)
class Status:
    """``os.stat`` (or ``lstat``) of one path, tri-state."""

    state: ReadState
    result: os.stat_result | None = None
    unknown: Unknown | None = None


@dataclass(frozen=True)
class Read:
    """The bytes of one small regular file, tri-state."""

    state: ReadState
    data: bytes = b""
    unknown: Unknown | None = None


@dataclass(frozen=True)
class Pointer:
    """A path a pointer file names (``gitdir:``, ``commondir``, a worktree's ``gitdir``).

    ``ABSENT`` means the file names nothing: git rejects it and dies rather
    than use another repository (a ``.git`` file without ``gitdir: ``).
    """

    state: ReadState
    text: str = ""
    unknown: Unknown | None = None


@dataclass(frozen=True)
class ConfigValues:
    """Every ``core.worktree`` value in one config file, tri-state (``OK`` may hold none)."""

    state: ReadState
    values: tuple[str, ...] = ()
    unknown: Unknown | None = None


class RefKind(Enum):
    SYMBOLIC = "symbolic"
    DIRECT = "direct"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RefContent:
    """What one ``HEAD`` or loose ref file holds: a symbolic ref's target, an object id, or unknown."""

    kind: RefKind
    target: str = ""
    unknown: Unknown | None = None


@dataclass(frozen=True)
class FollowedRef:
    """The ref a commit through ``HEAD`` moves (``ref`` None when detached), tri-state."""

    state: ReadState
    ref: str | None = None
    unknown: Unknown | None = None


# ---- reads ----------------------------------------------------------------------

def status_of(path: Path, *, follow_links: bool = True) -> Status:
    """``os.stat`` of ``path``: absent only for ``ENOENT``/``ENOTDIR``, unknown for any other error."""
    try:
        result = os.stat(path) if follow_links else os.lstat(path)
    except OSError as exc:
        if exc.errno in _ABSENT_ERRNOS:
            return Status(ReadState.ABSENT)
        return Status(ReadState.UNKNOWN, unknown=Unknown.OS_ERROR)
    except ValueError:
        return Status(ReadState.UNKNOWN, unknown=Unknown.UNNAMEABLE)
    return Status(ReadState.OK, result)


def read_small(path: Path) -> Read:
    """The bytes of the regular file at ``path``; unknown above :data:`MAX_READ_BYTES`, never absent for it."""
    status = status_of(path)
    if status.state is not ReadState.OK:
        return Read(status.state, unknown=status.unknown)
    if status.result is None:  # an OK status always carries its stat; fail closed if it ever does not
        return Read(ReadState.UNKNOWN, unknown=Unknown.OS_ERROR)
    if not stat.S_ISREG(status.result.st_mode):
        return Read(ReadState.UNKNOWN, unknown=Unknown.NOT_REGULAR)
    if status.result.st_size > MAX_READ_BYTES:
        return Read(ReadState.UNKNOWN, unknown=Unknown.TOO_LARGE)
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_READ_BYTES + 1)
    except OSError as exc:
        if exc.errno in _ABSENT_ERRNOS:
            return Read(ReadState.ABSENT)
        return Read(ReadState.UNKNOWN, unknown=Unknown.OS_ERROR)
    except ValueError:
        return Read(ReadState.UNKNOWN, unknown=Unknown.UNNAMEABLE)
    if len(data) > MAX_READ_BYTES:
        return Read(ReadState.UNKNOWN, unknown=Unknown.TOO_LARGE)
    return Read(ReadState.OK, data)


def _mentions_worktree(path: Path) -> Read:
    """Whether the file at ``path`` holds ``worktree`` in any ASCII case, read in bounded chunks.

    ``OK`` with ``data`` ``b"1"`` when it does and ``b""`` when it does not.
    """
    overlap = len(_WORKTREE_KEY) - 1
    tail = b""
    try:
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(_SCAN_CHUNK_BYTES)
                if not chunk:
                    return Read(ReadState.OK, b"")
                window = (tail + chunk).lower()
                if _WORKTREE_KEY in window:
                    return Read(ReadState.OK, b"1")
                tail = window[-overlap:]
    except OSError as exc:
        if exc.errno in _ABSENT_ERRNOS:
            return Read(ReadState.ABSENT)
        return Read(ReadState.UNKNOWN, unknown=Unknown.OS_ERROR)
    except ValueError:
        return Read(ReadState.UNKNOWN, unknown=Unknown.UNNAMEABLE)


def core_worktree_values(path: Path) -> ConfigValues:
    """Every ``core.worktree`` in the config file at ``path`` (``include`` is not followed; git does not here either).

    A file over the read cap is scanned for ``worktree`` instead: none is
    no value, any is unknown.
    """
    status = status_of(path)
    if status.state is not ReadState.OK:
        return ConfigValues(status.state, unknown=status.unknown)
    if status.result is None:  # an OK status always carries its stat; fail closed if it ever does not
        return ConfigValues(ReadState.UNKNOWN, unknown=Unknown.OS_ERROR)
    if not stat.S_ISREG(status.result.st_mode):
        return ConfigValues(ReadState.UNKNOWN, unknown=Unknown.NOT_REGULAR)
    if status.result.st_size > MAX_READ_BYTES:
        scanned = _mentions_worktree(path)
        if scanned.state is not ReadState.OK:
            return ConfigValues(scanned.state, unknown=scanned.unknown)
        if scanned.data:
            return ConfigValues(ReadState.UNKNOWN, unknown=Unknown.TOO_LARGE)
        return ConfigValues(ReadState.OK)
    read = read_small(path)
    if read.state is not ReadState.OK:
        return ConfigValues(read.state, unknown=read.unknown)
    return parse_core_worktree(read.data)


# ---- pointer files ------------------------------------------------------------------

def path_text_problem(text: str) -> Unknown | None:
    """Why a path git read from a file cannot be compared with what git uses, or None.

    Empty text, a control character (an embedded newline: git keeps it,
    a line-based reader would not) and, on Windows, a drive-relative path
    (``C:x``, resolved against the process's directory on that drive) or a
    rooted one without a drive.
    """
    if not text or any(ord(char) < 0x20 or char == "\x7f" for char in text):
        return Unknown.UNPARSED
    if os.name == "nt":
        windows = PureWindowsPath(text)
        if bool(windows.drive) != bool(windows.root):
            return Unknown.DRIVE_RELATIVE
    return None


def parse_pointer(data: bytes, *, prefix: bytes = b"") -> Pointer:
    """A pointer file's path as git reads it: after ``prefix``, trailing CR and LF stripped, the rest literal.

    ``ABSENT`` when ``prefix`` is missing (git dies on such a ``.git`` file).
    """
    if not data.startswith(prefix):
        return Pointer(ReadState.ABSENT)
    value = data[len(prefix):].rstrip(b"\r\n")
    if b"\0" in value:
        return Pointer(ReadState.UNKNOWN, unknown=Unknown.NUL_BYTE)
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        return Pointer(ReadState.UNKNOWN, unknown=Unknown.UNDECODABLE)
    problem = path_text_problem(text)
    if problem is not None:
        return Pointer(ReadState.UNKNOWN, unknown=problem)
    return Pointer(ReadState.OK, text)


def named_path(text: str, base: Path) -> Path:
    """The path a pointer's text names: as written when absolute, else relative to ``base``."""
    candidate = Path(text)
    return candidate if candidate.is_absolute() else base / candidate


# ---- HEAD and loose refs ---------------------------------------------------------------

def valid_refname(name: str) -> bool:
    """Whether ``name`` is a ``refs/`` name this module can follow as git does."""
    if not name.startswith("refs/") or name.endswith(("/", ".")):
        return False
    return _REFNAME_FORBIDDEN.search(name) is None


def parse_ref(data: bytes) -> RefContent:
    """One ``HEAD`` or loose ref file as git reads it.

    Git trims trailing whitespace, and after ``ref:`` skips every whitespace
    character, newlines included (``ref:\\nrefs/heads/master`` is a symbolic
    ref).  An object id may be followed by whitespace and anything else.
    """
    if b"\0" in data:
        return RefContent(RefKind.UNKNOWN, unknown=Unknown.NUL_BYTE)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return RefContent(RefKind.UNKNOWN, unknown=Unknown.UNDECODABLE)
    if any((ord(char) < 0x20 and char not in _GIT_SPACE) or char == "\x7f" for char in text):
        return RefContent(RefKind.UNKNOWN, unknown=Unknown.UNPARSED)
    text = text.rstrip(_GIT_SPACE)
    if text.startswith(_SYMREF_PREFIX):
        name = text[len(_SYMREF_PREFIX):].lstrip(_GIT_SPACE)
        if not valid_refname(name):
            return RefContent(RefKind.UNKNOWN, unknown=Unknown.UNPARSED)
        return RefContent(RefKind.SYMBOLIC, name)
    if _OBJECT_ID.match(text):
        return RefContent(RefKind.DIRECT)
    return RefContent(RefKind.UNKNOWN, unknown=Unknown.UNPARSED)


def followed_ref(git_dir: Path, common_dir: Path) -> FollowedRef:
    """The ref a commit through ``git_dir``'s ``HEAD`` moves, following loose symbolic refs.

    Shared refs (``refs/heads/...``) are read from ``common_dir`` only, as
    git reads them; the per-worktree ones from ``git_dir``.  A ref with no
    loose file (packed, or not yet created) is the one a commit writes.
    ``ref`` is None when ``HEAD`` is detached.  The caller rules out a
    reftable store, whose ``HEAD`` file is a stub.
    """
    head = read_small(git_dir / "HEAD")
    if head.state is not ReadState.OK:
        return FollowedRef(ReadState.UNKNOWN, unknown=head.unknown or Unknown.UNPARSED)
    content = parse_ref(head.data)
    name: str | None = None
    for _ in range(MAX_REF_READS):
        if content.kind is RefKind.UNKNOWN:
            return FollowedRef(ReadState.UNKNOWN, unknown=content.unknown)
        if content.kind is RefKind.DIRECT:
            return FollowedRef(ReadState.OK, name)
        name = content.target
        base = git_dir if name.startswith(_PER_WORKTREE_REF_PREFIXES) else common_dir
        loose = read_small(base.joinpath(*name.split("/")))
        if loose.state is ReadState.ABSENT:
            return FollowedRef(ReadState.OK, name)
        if loose.state is ReadState.UNKNOWN:
            return FollowedRef(ReadState.UNKNOWN, unknown=loose.unknown)
        content = parse_ref(loose.data)
    return FollowedRef(ReadState.UNKNOWN, unknown=Unknown.TOO_DEEP)


# ---- config ------------------------------------------------------------------------------

def _ambiguous_character(char: str) -> bool:
    """A character git and a line-based reader may split or trim differently (lone CR, FF, NBSP, U+2028...)."""
    if char == "\t":
        return False
    return ord(char) < 0x20 or char == "\x7f" or (char.isspace() and char != " ")


def _config_value(raw: str) -> str | None:
    """One config value as git unquotes it; None where git reports an error."""
    value = ""
    spaces = 0
    quoted = False
    index = 0
    while index < len(raw):
        char = raw[index]
        index += 1
        if char in " \t" and not quoted:
            if value:
                spaces += 1
            continue
        if not quoted and char in "#;":
            break
        value += " " * spaces
        spaces = 0
        if char == "\\":
            if index >= len(raw) or raw[index] not in _CONFIG_ESCAPES:
                return None
            value += _CONFIG_ESCAPES[raw[index]]
            index += 1
        elif char == '"':
            quoted = not quoted
        else:
            value += char
    return None if quoted else value


def parse_core_worktree(data: bytes) -> ConfigValues:
    """Every ``core.worktree`` value in one config file's bytes.

    A file that never mentions ``worktree`` (any ASCII case) holds none.  One
    that does is parsed strictly: a BOM, a NUL, bytes that are not UTF-8, a
    line break other than ``\\n``/``\\r\\n``, whitespace git does not trim, a
    backslash continuation, a second section header on one line, or any line
    git's grammar would not accept make it unknown.
    """
    if _WORKTREE_KEY not in data.lower():
        return ConfigValues(ReadState.OK)
    if b"\0" in data:
        return ConfigValues(ReadState.UNKNOWN, unknown=Unknown.NUL_BYTE)
    if data.startswith(_UTF8_BOM):
        return ConfigValues(ReadState.UNKNOWN, unknown=Unknown.UNPARSED)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ConfigValues(ReadState.UNKNOWN, unknown=Unknown.UNDECODABLE)
    unparsed = ConfigValues(ReadState.UNKNOWN, unknown=Unknown.UNPARSED)
    values: list[str] = []
    in_core: bool | None = None
    for raw in text.split("\n"):
        line = raw[:-1] if raw.endswith("\r") else raw
        if any(_ambiguous_character(char) for char in line) or line.endswith("\\"):
            return unparsed
        rest = line.lstrip(" \t")
        if rest.startswith("["):
            header = _CONFIG_HEADER.match(rest)
            if header is None:
                return unparsed
            in_core = header.group(1).casefold() == "core" and header.group(2) is None
            rest = rest[header.end():].lstrip(" \t")
            if rest.startswith("["):
                return unparsed
        if not rest or rest[0] in "#;":
            continue
        entry = _CONFIG_KEY.fullmatch(rest)
        if entry is None or in_core is None:
            return unparsed
        if in_core and entry.group(1).casefold() == "worktree":
            value = None if entry.group(2) is None else _config_value(entry.group(2))
            if value is None:
                return unparsed
            values.append(value)
    return ConfigValues(ReadState.OK, tuple(values))


__all__ = [
    "GITFILE_PREFIX", "MAX_READ_BYTES", "MAX_REF_READS", "ConfigValues", "FollowedRef", "Pointer",
    "Read", "ReadState", "RefContent", "RefKind", "Status", "Unknown", "core_worktree_values",
    "followed_ref", "named_path", "parse_core_worktree", "parse_pointer", "parse_ref",
    "path_text_problem", "read_small", "status_of", "valid_refname",
]
