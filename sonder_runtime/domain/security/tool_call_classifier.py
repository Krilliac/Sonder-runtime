"""Static safety rules for model-originated tool calls.

Three verdicts:

* ``ALLOW``: no objection.  It never loosens anything: the operator's
  permission mode and rules still decide the call at dispatch.  For a host
  program it is an allowlist: a bare program name from ``_SAFE_PROGRAMS``
  whose every flag is on that program's read-only list.
* ``ASK``: a person must confirm the call first: file deletes, elevated
  commands (``sudo``, ``runas``, ``-Verb RunAs``...), every ``git`` call (the
  repository's ``.git/config`` can name an fsmonitor, a pager or a diff driver,
  so even ``git status`` can run code), a program named by a path instead of a
  bare name, a flag outside a safe program's read-only list, host programs and
  code the classifier cannot show to be read-only, and every tool or argument
  shape it does not know.  ASK is the default for anything not recognised.
* ``BLOCK``: refused in every permission mode, bypass included: recognised
  destructive commands (disk formatting and partitioning, boot and shadow-copy
  deletion, shutdown, download-and-execute, fork bombs) and deletes aimed at a
  drive, filesystem, home or system root.

BLOCK is a denylist, and a denylist is not complete: it refuses the
destructive shapes it recognises, and the same effect spelled some other way
is not blocked by it.  It is a last net (in bypass, the only one), never a
finding that a call is harmless; that is why ALLOW is an allowlist and
everything unrecognised asks.

``git_commit`` and the permission catalog's read-only (``safe``/``ask``) and
file-edit (``mutation``) grades are ``ALLOW``.  The caller passes the grade
(``risk``) it read from the permission catalog; an unknown or missing grade
is ``ASK``.

It fails closed: any fault while classifying answers ``ASK``, never ``ALLOW``.
It is purely static (no model call, no I/O), so there is no timeout path.
Arguments are inspected in memory and never logged or kept.

Everything here is a pure function over immutable tables: safe to call from
any thread, no state, no ownership.
"""
from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

ALLOW, ASK, BLOCK = "allow", "ask", "block"
VERDICTS = frozenset({ALLOW, ASK, BLOCK})

_ALWAYS_ALLOWED = frozenset({"git_commit"})
_ALWAYS_ASK = frozenset({"file_delete"})
_DELETE_TOOLS = frozenset({"file_delete"})
_RISK_VERDICTS: Mapping[str, str] = {"safe": ALLOW, "ask": ALLOW, "mutation": ALLOW}
_ARGV_TOOLS = frozenset({"workspace_run"})
# Argument keys whose values are a program, a command line or code to run.
_COMMAND_KEYS = ("program", "args_json", "args", "argv", "code", "command", "cmd", "script")

_ELEVATION_TOKENS = frozenset({"sudo", "su", "doas", "runas", "gsudo", "pkexec"})
_ELEVATION_TEXT = ("-verb runas", "-verb:runas", "-verb 'runas'", '-verb "runas"')
_BLOCK_TOKENS = frozenset({
    "diskpart", "format-volume", "clear-disk", "initialize-disk", "remove-partition",
    "bcdedit", "stop-computer", "restart-computer", "set-mppreference",
})
_BLOCK_TEXT = (
    "--no-preserve-root", "vssadmin delete", "wbadmin delete", "cipher /w",
    "reg delete hklm", "reg delete hkey_local_machine", "of=/dev/sd", "of=/dev/nvme",
    "of=/dev/hd", "> /dev/sd", ":(){", "downloadstring", "allprofiles state off",
    "shutdown /s", "shutdown /r", "shutdown -h", "shutdown -r", "shutdown now",
)
# What a download is piped into: shells, interpreters and PowerShell's evaluator.
_PIPE_SHELLS = frozenset({
    "sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "iex", "invoke-expression",
    "python", "py", "pwsh", "powershell", "cmd", "perl", "ruby", "node", "php", "lua",
})
_EVAL_TOKENS = frozenset({"iex", "invoke-expression"})
# Words that may stand before the pipe target (``| sudo -E bash``).
_PIPE_PREFIXES = _ELEVATION_TOKENS | {"env", "exec", "command", "nohup", "time"}
_DOWNLOADERS = frozenset({
    "curl", "wget", "iwr", "invoke-webrequest", "irm", "invoke-restmethod",
    "start-bitstransfer", "bitsadmin", "certutil",
})
_DELETE_VERBS = frozenset({"rm", "del", "erase", "rd", "rmdir", "remove-item", "ri", "rimraf"})

# Protected roots, compared after ``_normalize_target``.  The home directory
# is the sentinel ``/~`` so that ``~/..`` normalizes to ``/``.
_HOME = "/~"
_HOME_SPELLINGS = (
    "~", "$home", "$env:userprofile", "$env:home", "%userprofile%",
    "%homedrive%%homepath%", "%homepath%",
)
_SYSTEM_SPELLINGS = (
    ("%systemroot%", "c:/windows"), ("%windir%", "c:/windows"),
    ("$env:systemroot", "c:/windows"), ("$env:windir", "c:/windows"),
    ("%systemdrive%", "c:"), ("$env:systemdrive", "c:"),
    ("%programfiles%", "c:/program files"), ("$env:programfiles", "c:/program files"),
)
# ``${HOME}`` / ``${env:USERPROFILE}`` are the braced spellings of the same variables.
_BRACED_VARIABLE = re.compile(r"\$\{([a-z_][a-z0-9_]*(?::[a-z_][a-z0-9_]*)?)\}")
# The root itself (a drive root is ``/`` after its drive letter).
_EXACT_ROOTS = frozenset({
    "/", _HOME, "/home", "/users", "/root", "/var", "/opt", "/srv", "/mnt", "/media",
})
# A home directory: one level below these.
_HOME_PARENTS = frozenset({"/home", "/users"})
# System trees: the root and anything below it.
_ROOT_TREES = (
    "/etc", "/usr", "/boot", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/sys", "/proc",
    "/dev", "/system", "/windows", "/program files", "/program files (x86)", "/programdata",
)

# Per safe program, the flags that only read: (bundlable short letters, long
# flags matched up to their ``=``, Windows ``/x`` switches matched up to their
# ``:``).  Any other flag asks.  Long flags match exactly, so a GNU-style
# abbreviation (``tail --fo`` for ``--follow``) asks too.  Left out on purpose:
# ``rg --pre/--pre-glob/-z/--search-zip/--hostname-bin`` (run programs),
# ``rg -u/--hidden/--no-ignore/-L``, ``tree -o`` (writes), ``tree -R/-H``,
# ``tail -f/-F/--follow`` (never exits).  ``git`` is not here: every git call asks.
_LS_SHORT = "1aAdFhilnrRStp"
_LS_LONG = frozenset({
    "--all", "--almost-all", "--directory", "--classify", "--human-readable", "--inode",
    "--numeric-uid-gid", "--reverse", "--recursive", "--size", "--color", "--sort",
})
_HEAD_TAIL_LONG = frozenset({"--lines", "--bytes", "--quiet", "--silent", "--verbose"})
_GREP_LONG = frozenset({
    "--ignore-case", "--line-number", "--recursive", "--files-with-matches",
    "--files-without-match", "--count", "--invert-match", "--word-regexp", "--line-regexp",
    "--extended-regexp", "--fixed-strings", "--basic-regexp", "--only-matching",
    "--no-filename", "--with-filename", "--no-messages", "--quiet", "--silent", "--regexp",
    "--after-context", "--before-context", "--context", "--max-count", "--include",
    "--exclude", "--exclude-dir", "--color", "--colour",
})
_RG_LONG = frozenset({
    "--ignore-case", "--smart-case", "--case-sensitive", "--word-regexp", "--line-regexp",
    "--files-with-matches", "--files-without-match", "--count", "--count-matches",
    "--invert-match", "--fixed-strings", "--regexp", "--glob", "--iglob", "--type",
    "--type-not", "--after-context", "--before-context", "--context", "--max-count",
    "--line-number", "--no-line-number", "--with-filename", "--no-filename",
    "--only-matching", "--quiet", "--heading", "--no-heading", "--color", "--json",
    "--files", "--type-list", "--max-columns", "--max-depth", "--multiline", "--vimgrep",
    "--stats", "--sort", "--column",
})
_NO_FLAGS: frozenset[str] = frozenset()
_SAFE_PROGRAMS: Mapping[str, tuple[str, frozenset[str], frozenset[str]]] = MappingProxyType({
    "ls": (_LS_SHORT, _LS_LONG, _NO_FLAGS),
    "dir": (_LS_SHORT, _LS_LONG, frozenset({"/a", "/b", "/o", "/s", "/w", "/q", "/l", "/n", "/x"})),
    "pwd": ("LP", _NO_FLAGS, _NO_FLAGS),
    "echo": ("neE", _NO_FLAGS, _NO_FLAGS),
    "cat": ("AbeEnstTuv", frozenset({"--number", "--number-nonblank", "--show-all", "--squeeze-blank"}),
            _NO_FLAGS),
    "type": ("", _NO_FLAGS, _NO_FLAGS),
    "head": ("cnqv", _HEAD_TAIL_LONG, _NO_FLAGS),
    "tail": ("cnqv", _HEAD_TAIL_LONG, _NO_FLAGS),
    "wc": ("clmwL", frozenset({"--lines", "--words", "--bytes", "--chars", "--max-line-length"}),
           _NO_FLAGS),
    "which": ("a", _NO_FLAGS, _NO_FLAGS),
    "where": ("", _NO_FLAGS, frozenset({"/r", "/q", "/f", "/t"})),
    "whoami": ("", _NO_FLAGS, frozenset({"/user", "/groups", "/priv", "/all", "/fo", "/nh"})),
    "findstr": ("", _NO_FLAGS, frozenset({
        "/s", "/i", "/n", "/m", "/c", "/r", "/l", "/v", "/x", "/b", "/e", "/o", "/p"})),
    "grep": ("ABCcEeFGHhiLlmnoqrsvwx", _GREP_LONG, _NO_FLAGS),
    "rg": ("0ABCcFeHgIijlMmNnoqSsTtvwxU", _RG_LONG, _NO_FLAGS),
    "tree": ("aCdDfFghiILnPpsu", frozenset({"--noreport", "--dirsfirst"}), frozenset({"/f", "/a"})),
})
_PUNCTUATION = str.maketrans({c: " " for c in "()[]{},;'\"`"})


class _Unreadable(ValueError):
    """The arguments do not have a shape this classifier can inspect."""


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, str)]
    return []


def _command_text(arguments: Mapping[str, Any]) -> str:
    """Every command-bearing argument, lowercased and whitespace-collapsed."""
    parts: list[str] = []
    for key in _COMMAND_KEYS:
        value = arguments.get(key)
        if key == "args_json" and isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                pass  # inspect the raw text instead
        parts.extend(_strings(value))
    return " ".join(" ".join(parts).lower().split())


def _is_drive_root(token: str) -> bool:
    return (2 <= len(token) <= 4 and token[0].isascii() and token[0].isalpha()
            and token[1] == ":" and token[2:] in ("", "\\", "/", "\\*", "/*"))


def _normalize_target(token: str) -> str:
    """One spelling per path: case-folded, ``/`` separators, variables mapped, ``.``/``..``/``/*`` resolved.

    The home directory becomes ``/~``.  A drive letter stays in front
    (``c:/windows``).  A relative path stays relative.
    """
    target = _BRACED_VARIABLE.sub(r"$\1", token.strip().strip("'\"").casefold()).replace("\\", "/")
    for spelling in _HOME_SPELLINGS:
        if target == spelling or target.startswith(spelling + "/"):
            target = _HOME + target[len(spelling):]
            break
    else:
        for spelling, root in _SYSTEM_SPELLINGS:
            if target == spelling or target.startswith(spelling + "/"):
                target = root + target[len(spelling):]
                break
    drive = ""
    if len(target) >= 2 and target[1] == ":" and target[0].isascii() and target[0].isalpha():
        drive, target = target[:2], "/" + target[2:]
    while target.endswith("/*"):
        target = target[:-2] or "/"
    if target.startswith("/"):
        target = posixpath.normpath("/" + target.lstrip("/"))
    return drive + target


def _is_root_target(token: str) -> bool:
    """Whether ``token`` names a drive, filesystem or home root, or a path in a system tree."""
    target = _normalize_target(token)
    path = target[2:] if target[1:2] == ":" else target
    if not path.startswith("/"):
        return False
    return (path in _EXACT_ROOTS or posixpath.dirname(path) in _HOME_PARENTS
            or any(path == tree or path.startswith(tree + "/") for tree in _ROOT_TREES))


def _is_delete_verb(token: str) -> bool:
    return token in _DELETE_VERBS or token == "rmtree" or token.endswith(".rmtree")


def _downloads(words: list[str]) -> bool:
    return any(_program_name(word) in _DOWNLOADERS for word in words)


def _interpreter_name(word: str) -> str:
    """``python3.12`` -> ``python``; ``/bin/sh`` -> ``sh``; any other word its program name."""
    name = _program_name(word)
    unversioned = name.rstrip("0123456789.")
    return unversioned if unversioned in _PIPE_SHELLS else name


def _pipe_target(words: list[str]) -> str:
    """The program a pipe feeds, past ``sudo``/``env``-style prefixes and their flags."""
    for word in words:
        name = _interpreter_name(word)
        if name in _PIPE_PREFIXES or _is_flag(word):
            continue
        return name
    return ""


def _blocked(text: str) -> bool:
    if any(marker in text for marker in _BLOCK_TEXT):
        return True
    text = _BRACED_VARIABLE.sub(r"$\1", text)
    tokens = text.translate(_PUNCTUATION).split()
    if any(token in _BLOCK_TOKENS or token.startswith("mkfs") for token in tokens):
        return True
    for index, token in enumerate(tokens[:-1]):
        if token == "format" and _is_drive_root(tokens[index + 1]):
            return True
    if any(_is_delete_verb(token) and _deletes_a_root(tokens[index + 1:index + 6])
           for index, token in enumerate(tokens)):
        return True
    segments = [segment.translate(_PUNCTUATION).split() for segment in text.split("|")]
    for index, words in enumerate(segments[1:], start=1):
        if _pipe_target(words) in _PIPE_SHELLS and any(
                _downloads(earlier) for earlier in segments[:index]):
            return True
    # ``iex (iwr ...)``: PowerShell evaluates a download with no pipe at all.
    return any(token in _EVAL_TOKENS for token in tokens) and _downloads(tokens)


def _is_flag(token: str) -> bool:
    """``-rf``, ``--force``, ``-Recurse`` or a Windows switch such as ``/s``; not ``/``, ``/.`` or ``//``."""
    return (len(token) > 1 and token[0] == "-") or (
        len(token) == 2 and token[0] == "/" and token[1].isalpha())


def _deletes_a_root(following: list[str]) -> bool:
    """Whether the first operand after a delete verb (past its flags) is a root target."""
    for token in following:
        if _is_flag(token):
            continue
        return _is_root_target(token)
    return False


def _elevated(text: str) -> bool:
    tokens = text.translate(_PUNCTUATION).split()
    return any(token in _ELEVATION_TOKENS for token in tokens) or any(
        marker in text for marker in _ELEVATION_TEXT)


def _argv(arguments: Mapping[str, Any]) -> list[str]:
    program = arguments.get("program")
    raw = arguments.get("args_json", "[]")
    try:
        args = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError as exc:
        raise _Unreadable("args_json is not JSON") from exc
    if not isinstance(program, str) or not isinstance(args, list) or not all(
            isinstance(item, str) for item in args):
        raise _Unreadable("program and args_json must be strings")
    return [program, *args]


def _program_name(program: str) -> str:
    name = program.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def _bare_program(program: str) -> str:
    """The program name when ``program`` is a bare name; ``""`` for a path, a drive or padding.

    A path (``./rg``, ``C:/evil/grep.exe``, ``C:grep.exe``) may name a binary
    the model planted, so it is never a safe program.
    """
    if not program or program != program.strip() or any(mark in program for mark in "/\\:"):
        return ""
    return _program_name(program)


def _arg_reads_only(arg: str, short: str, long_flags: frozenset[str], switches: frozenset[str]) -> bool:
    if switches and arg.startswith("/"):
        return arg.split(":", 1)[0].casefold() in switches
    if arg == "--":
        return True
    if arg.startswith("--"):
        return arg.split("=", 1)[0] in long_flags
    if len(arg) > 1 and arg[0] == "-":
        return all(letter in short for letter in arg[1:])
    return True  # an operand


def _safe_argv(argv: list[str]) -> bool:
    flags = _SAFE_PROGRAMS.get(_bare_program(argv[0]))
    return flags is not None and all(_arg_reads_only(arg, *flags) for arg in argv[1:])


def _delete_target_blocked(arguments: Mapping[str, Any]) -> bool:
    path = arguments.get("path")
    if not isinstance(path, str):
        raise _Unreadable("file_delete needs a path")
    return _is_root_target(path) or path.strip().rstrip(" .") == ""


def _classify(tool_name: str, arguments: Any, risk: str) -> str:
    if not isinstance(arguments, Mapping):
        if arguments is not None:
            return ASK
        arguments = {}
    name = str(tool_name or "").strip().lstrip("/")
    text = _command_text(arguments)
    if text and _blocked(text):
        return BLOCK
    if name in _DELETE_TOOLS and _delete_target_blocked(arguments):
        return BLOCK
    if name in _ALWAYS_ALLOWED:
        return ALLOW
    if name in _ALWAYS_ASK or (text and _elevated(text)):
        return ASK
    if name in _ARGV_TOOLS:
        return ALLOW if _safe_argv(_argv(arguments)) else ASK
    return _RISK_VERDICTS.get(str(risk or ""), ASK)


def classify(tool_name: str, arguments: Any, *, risk: str) -> str:
    """``ALLOW``, ``ASK`` or ``BLOCK`` for one tool call; ``ASK`` on any fault.

    ``risk`` is the permission catalog's grade for ``tool_name``.
    """
    try:
        verdict = _classify(tool_name, arguments, risk)
    except Exception:  # noqa: BLE001 - fail closed: a fault is never an allow
        return ASK
    return verdict if verdict in VERDICTS else ASK


__all__ = ["ALLOW", "ASK", "BLOCK", "VERDICTS", "classify"]
