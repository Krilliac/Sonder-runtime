"""Static safety rules for model-originated tool calls.

Three verdicts:

* ``ALLOW``: no objection.  It never loosens anything: the operator's
  permission mode and rules still decide the call at dispatch.
* ``ASK``: a person must confirm the call first: file deletes, elevated
  commands (``sudo``, ``runas``, ``-Verb RunAs``...), host programs and code
  the classifier cannot show to be read-only, and every tool or argument shape
  it does not know.
* ``BLOCK``: refused in every permission mode, bypass included: recognised
  destructive commands (disk formatting and partitioning, boot and shadow-copy
  deletion, shutdown, download-and-execute pipes, fork bombs) and deletes
  aimed at a drive, filesystem or home root.

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
from collections.abc import Mapping
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
_PIPE_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "iex", "invoke-expression"})
_DOWNLOADERS = frozenset({"curl", "wget", "iwr", "invoke-webrequest", "irm", "invoke-restmethod"})
_DELETE_VERBS = frozenset({"rm", "del", "erase", "rd", "rmdir", "remove-item", "ri", "rimraf"})
_ROOT_TARGETS = frozenset({
    "/", "/*", "~", "~/", "~/*", "\\", "$home", "%userprofile%", "%systemroot%",
    "/etc", "/usr", "/home", "/boot", "/var", "c:\\windows", "c:/windows",
})
_SAFE_PROGRAMS = frozenset({
    "ls", "dir", "pwd", "echo", "cat", "type", "head", "tail", "wc", "which", "where",
    "whoami", "findstr", "grep", "rg", "tree",
})
_SAFE_GIT = frozenset({
    "status", "diff", "log", "show", "rev-parse", "ls-files", "blame", "grep", "describe", "commit",
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


def _is_root_target(token: str) -> bool:
    return token in _ROOT_TARGETS or _is_drive_root(token)


def _is_delete_verb(token: str) -> bool:
    return token in _DELETE_VERBS or token == "rmtree" or token.endswith(".rmtree")


def _blocked(text: str) -> bool:
    if any(marker in text for marker in _BLOCK_TEXT):
        return True
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
        if words and words[0] in _PIPE_SHELLS and any(
                word in _DOWNLOADERS for earlier in segments[:index] for word in earlier):
            return True
    return False


def _is_flag(token: str) -> bool:
    """``-rf``, ``--force``, ``-Recurse`` or a Windows switch such as ``/s``; not a bare ``/``."""
    return (len(token) > 1 and token[0] == "-") or (len(token) == 2 and token[0] == "/")


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


def _safe_argv(argv: list[str]) -> bool:
    name = _program_name(argv[0])
    if name == "git":
        return len(argv) > 1 and argv[1].lower() in _SAFE_GIT and "--force" not in argv
    return name in _SAFE_PROGRAMS


def _delete_target_blocked(arguments: Mapping[str, Any]) -> bool:
    path = arguments.get("path")
    if not isinstance(path, str):
        raise _Unreadable("file_delete needs a path")
    target = path.strip().lower().rstrip(" .")
    return _is_root_target(target) or target in ("", ".", "..")


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
