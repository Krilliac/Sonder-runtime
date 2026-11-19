"""Whether a tool call deletes files, for the always-ask rule on deletes.

``file_delete`` deletes by name. A host-program or code tool (``workspace_run``,
``run_program``, ``script_run``, ``run_code``, ``parallel_run_code``,
``build_run``) deletes when its arguments hold a delete command: ``git rm``,
``rm``, ``del``, ``Remove-Item``, ``unlink``, ``shutil.rmtree``,
``os.remove``, ``find ... -delete``, ``git clean`` or a git alias.

Every string leaf of the arguments is scanned, at any key (``stdin`` and
``code`` included) and through JSON-encoded strings, and split into words on
everything but ``[0-9a-z_.-]``, so a shell operator (``true&&rm``,
``echo|rm``) cannot glue a verb to its neighbour. A program suffix (the
default PATHEXT, ``.exe`` ``.com`` ``.bat`` ``.cmd`` ``.vbs`` ``.js`` and the
rest, plus ``.ps1``, any case) is taken off a word, and each text is also scanned with its ``^`` removed (cmd's
escape: ``de^l``). A call the scan cannot read counts as a delete: one that
names PowerShell and passes ``-EncodedCommand`` (``-e``, ``-ec``, ``-enc`` or
any prefix, with any dash or ``/``), and one holding a ``^`` that reaches cmd
(it names ``cmd`` or ``%COMSPEC%``, or is ``build_run``, which runs its
command through cmd on Windows). The scan is deliberately broad: a false
positive costs one approval, a false negative is a silent delete.

Residual gaps (best effort, by design of a word scan): a delete made through a
language API the verb list does not name (``__import__('os').remove``,
``fs.unlinkSync``, ``[IO.File]::Delete``, ``File.Delete``, ``std::remove``,
or a bare name such as ``from os import remove; remove(p)``), a verb split by
shell quoting or expansion (``r''m``, ``r${x}m``), a command that destroys
files without deleting them by name (``git reset --hard``, ``truncate``, ``mv``
out of the project), or one inside a script file the call only names, is not
seen here.

Pure and stateless over immutable tables: safe from any thread, no ownership.
It never raises: arguments that cannot be read answer ``True`` for a host tool
(fail closed).
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

#: Tools that delete files by name.
DELETE_TOOLS = frozenset({"file_delete"})

#: Tools that start a host program or run code, and so can delete a file
#: without ``file_delete``.
HOST_PROGRAM_TOOLS = frozenset({
    "workspace_run", "run_program", "script_run", "run_code", "parallel_run_code", "build_run",
})

#: Nesting the leaf walk follows (containers and JSON-encoded strings alike).
MAX_DEPTH = 8

_DELETE_VERBS = frozenset({
    "rm", "del", "erase", "rd", "rmdir", "remove-item", "ri", "rimraf", "unlink", "shred",
    "rmtree", "os.remove", "os.unlink", "os.rmdir", "os.removedirs",
})
_WORD_SPLIT = re.compile(r"[^0-9a-z_.\-]+")
#: Windows program suffixes a verb may carry (``rimraf.cmd``, ``rm.ps1``): the
#: default PATHEXT, plus ``.ps1``.
_PROGRAM_SUFFIXES = (
    ".exe", ".com", ".bat", ".cmd", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".msc", ".ps1",
)
#: Words that name PowerShell (``WindowsPowerShell``, ``pwsh-preview`` included).
_POWERSHELL_NAMES = ("powershell", "pwsh")
#: ``-EncodedCommand``, its ``-ec`` alias and every prefix of it, with the
#: dashes taken off (``/enc`` and an en-dash leave no dash after the split).
_ENCODED_COMMAND_FLAGS = frozenset(
    {"ec"} | {"encodedcommand"[:n] for n in range(1, len("encodedcommand") + 1)})
#: Words that name cmd.exe, where ``^`` escapes the next character (``de^l``).
_CMD_NAMES = frozenset({"cmd", "comspec"})
#: Host tools that hand their command string to cmd.exe on Windows.
_CMD_STRING_TOOLS = frozenset({"build_run"})


def _leaves(value: Any, depth: int = 0) -> list[str]:
    """Every string in a JSON-shaped value, JSON-decoding ``[``/``{`` strings."""
    if depth > MAX_DEPTH:
        # Too deep to scan: a delete verb is assumed, so the call asks.
        return ["rm"]
    if isinstance(value, str):
        out = [value]
        if value.strip()[:1] in ("[", "{"):
            try:
                decoded = json.loads(value)
            except ValueError:
                return out  # the raw text is scanned as it is
            out.extend(_leaves(decoded, depth + 1))
        return out
    if isinstance(value, Mapping):
        return [text for item in value.values() for text in _leaves(item, depth + 1)]
    if isinstance(value, (list, tuple)):
        return [text for item in value for text in _leaves(item, depth + 1)]
    return []


def _program_name(word: str) -> str:
    """``word`` without its program suffixes (``rimraf.cmd.exe`` is ``rimraf``)."""
    while word.endswith(_PROGRAM_SUFFIXES):
        word = word[:word.rfind(".")]
    return word


def _is_delete_verb(word: str) -> bool:
    return word in _DELETE_VERBS or word.endswith((".rmtree", ".unlink"))


def _scan(tool_name: str, arguments: Mapping[str, Any]) -> bool:
    texts = [text.lower() for text in _leaves(arguments)]
    # cmd drops a ``^`` and keeps the character after it, so ``de^l`` runs
    # ``del``: each text is also scanned with its carets taken out.
    texts += [text.replace("^", "") for text in texts if "^" in text]
    words = [
        _program_name(word)
        for text in texts
        for word in _WORD_SPLIT.split(text)
        if word
    ]
    if any(_is_delete_verb(word) for word in words):
        return True
    # A command the word scan cannot read is assumed to delete: an encoded
    # PowerShell command, or any ``^`` in a call that reaches cmd.exe.
    if any(name in word for word in words for name in _POWERSHELL_NAMES) and any(
            word.lstrip("-") in _ENCODED_COMMAND_FLAGS for word in words):
        return True
    if any("^" in text for text in texts) and (
            tool_name in _CMD_STRING_TOOLS or not _CMD_NAMES.isdisjoint(words)):
        return True
    if "find" in words and "-delete" in words:
        return True
    if "git" in words:
        after = words[words.index("git") + 1:]
        # A git alias (``-c alias.x=rm``) is a delete the verb scan cannot see.
        return "clean" in after or "--config-env" in after or any(
            "alias." in word for word in after)
    return False


def deletes_files(tool_name: str, arguments: Any) -> bool:
    """Whether this call deletes files: ``file_delete``, or a host tool doing it.

    ``arguments`` of ``None`` (no arguments) answers ``False`` for a host tool;
    any other non-mapping, or a fault while scanning, answers ``True``.
    """
    name = str(tool_name or "").strip().lstrip("/")
    if name in DELETE_TOOLS:
        return True
    if name not in HOST_PROGRAM_TOOLS:
        return False
    if arguments is None:
        return False
    if not isinstance(arguments, Mapping):
        return True
    try:
        return _scan(name, arguments)
    except Exception:  # noqa: BLE001 - fail closed: an unreadable call asks
        return True


__all__ = ["DELETE_TOOLS", "HOST_PROGRAM_TOOLS", "MAX_DEPTH", "deletes_files"]
