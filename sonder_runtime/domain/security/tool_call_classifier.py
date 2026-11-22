"""Static safety rules for model-originated tool calls.

Three verdicts:

* ``ALLOW``: no objection.  It never loosens anything: the operator's
  permission mode and rules still decide the call at dispatch.  For a host
  program it is an allowlist: a bare program name from ``_SAFE_PROGRAMS``
  whose every flag is on that program's read-only list.
* ``ASK``: a person must confirm the call first: file deletes, elevated
  commands (``sudo``, ``runas``, ``-Verb RunAs``...), every ``git`` call and
  every ``git_*`` tool (the repository's hooks and ``.git/config`` can name an
  fsmonitor, a pager or a diff driver, so even ``git status`` can run code),
  a program named by a path instead of a bare name, a flag outside a safe
  program's read-only list, host programs and code the classifier cannot
  show to be read-only, and every tool or argument shape it does not know.
  ASK is the default for anything not recognised.
* ``BLOCK``: refused in every permission mode, bypass included: recognised
  destructive commands (disk formatting and partitioning, boot and shadow-copy
  deletion, shutdown, download-and-execute, fork bombs), deletes aimed at a
  drive, filesystem, home or system root, and command text above
  ``_MAX_TEXT`` characters (it is not read, and bypass would skip an ASK).

``classify`` answers ``BLOCK`` when the rules in this module (``_classify``)
or the stricter rules of ``tool_call_classifier_floor`` (``blocks``) block,
and this module's verdict otherwise.  The floor only adds BLOCKs: parameter
expansion WORDs (``${X:=/}``), the shell strings other code hands to an exec
call (``os.system("rm -rf /")``), argv elements read with no shell, the text
of ``run_program``'s ``command`` read as the one argv the client runs
(``rg -e x / ; echo --pre=rm``), a delete verb in any word or assembled by
quotes (``r"m" -rf /``), a flag's value (``-Path:'C:\\Program Files'``) and
the size cap.  So no verdict is ever lower than this module's own.

BLOCK is a denylist, and a denylist is not complete: it refuses the
destructive shapes it recognises, and the same effect spelled some other way
is not blocked by it.  It is a last net (in bypass, the only one), never a
finding that a call is harmless; that is why ALLOW is an allowlist and
everything unrecognised asks.

Where each rule reads:

* Shell text is the argv of a program (``program``/``args_json``/``args``/
  ``argv``, and ``checks[].argv``), ``command``/``cmd``/``script``/``stdin``,
  and ``run_code`` in a shell language (``_SHELL_LANGUAGES``; ``stdin`` of a
  ``run_code`` call follows its language).  Only shell text gets the delete
  scan: a delete verb (``rm``, ``del``, ``Remove-Item``...) with a root among
  its operands, a relative operand resolved against ``cwd`` (and any ``cd``
  before it), and a relative path that climbs past its anchor counted as a root.
  A parameter expansion (``"${OUT:?}/"``) is one word, a brace expansion
  (``./{a,b}/c``) is the words it expands to, and the operands end at a
  ``#`` comment (except for cmd's ``del``/``rd``: cmd has no ``#`` comment).
* Every command-bearing text, other code included (Python, JavaScript...),
  gets the token and text lists (``_BLOCK_TOKENS``, ``_BLOCK_TEXT``) and the
  download-and-execute checks.  Shell strings handed to ``os.system`` or
  ``subprocess`` inside other code are not parsed as shell.
* An unescaped newline ends a command in every scan; a newline after ``\\``,
  ``^``, a PowerShell backtick, a pipe or ``&&`` continues it, and so does a
  newline (or ``;``, ``|``, ``&``) inside a quote or a ``(``/``@(`` that a
  delete's command has left open.  Two checks follow data rather than one
  command, so they span lines: a download whose file a later command runs
  (or a substitution reads into an interpreter, ``sh -c "$(cat F)"``), and an
  evaluator (``iex``, ``[scriptblock]::Create``) in the same text as a download.
* A ``cd`` (``pushd``, ``Set-Location``) is read as moving every later
  command in the text, even one in a ``$(...)`` or ``( )`` (bash scopes
  those; PowerShell does not).

Every scan is one left-to-right pass over the text, linear in its length.
A tracked directory is kept bounded (``_Place``), so ``cd`` floods and a long
``cwd`` stay linear too; ``cwd`` counts toward ``_MAX_TEXT``.

``git_*`` tools ask whatever their catalog grade.  Otherwise the permission
catalog's read-only (``safe``/``ask``) and file-edit (``mutation``) grades are
``ALLOW``.  The caller passes the grade (``risk``) it read from the permission
catalog; an unknown or missing grade is ``ASK``.

It fails closed: a fault while classifying answers ``ASK`` for arguments of
a shape it does not read, and ``BLOCK`` for any other fault in a call that
carries command text (bypass skips an ASK), never ``ALLOW``.
It is purely static (no model call, no I/O), so there is no timeout path.
Arguments are inspected in memory and never logged or kept.

The tables are immutable and every function is pure; the scan state
(``_Scan``) is created per call and never shared, so ``classify`` is safe to
call from any thread.
"""
from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, NamedTuple

from sonder_runtime.domain.security import tool_call_classifier_floor as _floor

ALLOW, ASK, BLOCK = "allow", "ask", "block"
VERDICTS = frozenset({ALLOW, ASK, BLOCK})

_ALWAYS_ASK = frozenset({"file_delete"})
# Tool names that ask whatever their catalog grade.
_ASK_PREFIXES = ("git_",)
_DELETE_TOOLS = frozenset({"file_delete"})
_RISK_VERDICTS: Mapping[str, str] = {"safe": ALLOW, "ask": ALLOW, "mutation": ALLOW}
_ARGV_TOOLS = frozenset({"workspace_run"})
# Keys whose values together are one program and its arguments.
_ARGV_KEYS = ("program", "args_json", "args", "argv")
# Keys whose values are shell command lines, a shell script or a shell's input.
_SHELL_KEYS = ("command", "cmd", "script", "stdin")
# ``run_code`` languages whose code is shell text.
_SHELL_LANGUAGES = frozenset({
    "sh", "bash", "zsh", "dash", "ksh", "fish", "shell", "pwsh", "powershell", "ps1", "cmd", "bat",
})
# Languages whose backtick is an escape character, not a command substitution.
_BACKTICK_ESCAPES = frozenset({"pwsh", "powershell", "ps1"})
# Above this many characters of command text ``_classify`` does not read it and asks (the floor blocks it).
_MAX_TEXT = 256 * 1024
# Substitutions nested deeper than this are read as part of the deepest one.
_MAX_DEPTH = 64
# A brace expansion of more words than this is read as its pieces instead.
_MAX_BRACE_WORDS = 16
# A tracked directory keeps its first _KEPT components and its depth: no root
# is decided past them (``/mnt/c/users/alice`` is the deepest, with one more
# to tell it from a folder inside it).  A longer component than _LONG_NAME
# names no root, so it is kept as a placeholder.
_KEPT = 6
_LONG_NAME = 64

_ELEVATION_TOKENS = frozenset({"sudo", "su", "doas", "runas", "gsudo", "pkexec"})
_ELEVATION_TEXT = ("-verb runas", "-verb:runas", "-verb 'runas'", '-verb "runas"')
_BLOCK_TOKENS = frozenset({
    "diskpart", "format-volume", "clear-disk", "initialize-disk", "remove-partition",
    "bcdedit", "stop-computer", "restart-computer", "set-mppreference",
})
_MKFS = re.compile(r"mkfs(?:\.[a-z0-9]+)?")
_BLOCK_TEXT = (
    "--no-preserve-root", "vssadmin delete", "wbadmin delete", "cipher /w",
    "reg delete hklm", "reg delete hkey_local_machine", "of=/dev/sd", "of=/dev/nvme",
    "of=/dev/hd", "> /dev/sd", ":(){", "downloadstring", "allprofiles state off",
    "shutdown /s", "shutdown /r", "shutdown -h", "shutdown -r", "shutdown now",
)
# What a download is piped or substituted into, or a downloaded file is run
# by: shells, interpreters, ``eval``, ``source``/``.``, and PowerShell's evaluator.
_PIPE_SHELLS = frozenset({
    "sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "iex", "invoke-expression",
    "python", "py", "pwsh", "powershell", "cmd", "perl", "ruby", "node", "php", "lua",
    "eval", "source", ".", "ash",
})
_EVAL_TOKENS = frozenset({"iex", "invoke-expression"})
# ``[scriptblock]::Create`` (and ``[System.Management.Automation.ScriptBlock]::Create``).
_SCRIPTBLOCK = "scriptblock]::create"
# Words that may stand before the program they run (``| sudo -E bash``,
# ``| timeout 5 bash``); past one, every later word may be that program.
_PIPE_PREFIXES = _ELEVATION_TOKENS | {
    "env", "exec", "command", "nohup", "time", "xargs", "timeout", "nice", "stdbuf", "busybox",
    "ionice",
}
_DOWNLOADERS = frozenset({
    "curl", "wget", "iwr", "invoke-webrequest", "irm", "invoke-restmethod",
    "start-bitstransfer", "bitsadmin", "certutil",
})
# Downloaders that write the file named by the URL's last path segment.
_URL_SAVERS = frozenset({"wget", "bitsadmin", "certutil", "start-bitstransfer"})
# Downloaders whose last word is the file they write.
_LAST_WORD_SAVERS = frozenset({"bitsadmin", "certutil"})
# Flags whose value is the downloaded file (``-O``/``-o`` are the same after lowercasing).
_OUTPUT_FLAGS = frozenset({"-o", "--output", "--output-document", "-outfile", "-destination"})
_OUTPUT_ASSIGNMENTS = ("--output=", "--output-document=")
_REDIRECTS = frozenset({">", ">>", "1>", "1>>"})
# Commands that write their input to the files they name.
_WRITERS = frozenset({"tee", "out-file", "set-content", "add-content"})
_NOT_A_FILE = frozenset({"", "-", ".", "..", "null", "nul", "$null"})
_DELETE_VERBS = frozenset({"rm", "del", "erase", "rd", "rmdir", "remove-item", "ri", "rimraf"})
# cmd's delete verbs, whose two-character ``/x`` words are switches, not paths.
_SWITCH_VERBS = frozenset({"del", "erase", "rd", "rmdir"})
_CD_VERBS = frozenset({"cd", "chdir", "pushd", "set-location", "sl"})

# Protected roots, compared after ``_normalize_targets``.  The home directory
# is the sentinel ``/~`` so that ``~/..`` normalizes to ``/``.
_HOME = "/~"
_HOME_SPELLINGS = (
    "~", "$home", "$env:userprofile", "$env:home", "%userprofile%",
    "%homedrive%%homepath%", "%homepath%", "$env:homedrive$env:homepath", "$env:homepath",
    "%homedrive%/%homepath%", "$env:homedrive/$env:homepath",
)
_SYSTEM_SPELLINGS = (
    ("%systemroot%", "c:/windows"), ("%windir%", "c:/windows"),
    ("$env:systemroot", "c:/windows"), ("$env:windir", "c:/windows"),
    ("%systemdrive%", "c:"), ("$env:systemdrive", "c:"),
    ("%homedrive%", "c:"), ("$env:homedrive", "c:"),
    ("%programfiles%", "c:/program files"), ("$env:programfiles", "c:/program files"),
)
# ``${HOME}`` / ``${env:USERPROFILE}`` are the braced spellings of the same variables.
_BRACED_VARIABLE = re.compile(r"\$\{([a-z_][a-z0-9_]*(?::[a-z_][a-z0-9_]*)?)\}")
# A shell parameter expansion with an operator (``${OUT:?}``, ``${1%/}``,
# ``${arr[0]}``, ``${#x}``) is its variable, one word.  A ``$(`` or backtick
# inside it is not folded away, and PowerShell's ``${C:\x}`` is not one.
_PARAMETER_EXPANSION = re.compile(
    r"\$\{[#!]?([a-z_][a-z0-9_]*|[0-9]+|[@*#?$!-])"
    r"(?:\}|(?::[-=?+0-9 ]|[-=?+#%/^,@\[])[^{}()`<\n]{0,256}\})")
# ``~alice`` is alice's home directory.
_USER_HOME = re.compile(r"~([^/]+)")
# Win32 device paths ``\\?\`` and ``\\.\`` (after ``\`` -> ``/``); ``\\?\UNC\`` names a share.
_DEVICE_PREFIX = re.compile(r"//[?.]/(unc/)?")
# A UNC share ``//host/share/rest``; ``c$`` is the administrative share of drive C.
_UNC = re.compile(r"//([^/]+)/([^/]+)(/.*)?")
_SLASHES = re.compile(r"/+")
_ADMIN_SHARE = re.compile(r"[a-z]\$")
# ``\\?\GLOBALROOT\Device\Harddisk0\Partition1``: the partition is the device.
_HARDDISK = re.compile(r"harddisk[0-9]+")
_PARTITION = re.compile(r"partition[0-9]+")
# A drive mounted as a directory: WSL ``/mnt/c``, Cygwin ``/cygdrive/c``, Git Bash
# ``/c``, matched on the resolved POSIX path (``/mnt//c`` and ``/tmp/../c`` too).
_MOUNTED_DRIVE = re.compile(r"/(?:mnt/|cygdrive/)?([a-z])(/.*)?")
# An 8.3 short name (``PROGRA~1``): its stem, ``~`` and a number.
_SHORT_NAME = re.compile(r"([^/~. ]{1,6})~[0-9]{1,6}(?:\.[^/.]{0,3})?")
# The root itself (a drive root is ``/`` after its drive letter).
_EXACT_ROOTS = frozenset({
    "/", _HOME, "/home", "/users", "/root", "/var", "/opt", "/srv", "/mnt", "/media",
})
# A home directory: exactly one level below these (not the folders inside it).
_HOME_PARENTS = frozenset({"/home", "/users"})
# System trees: the root and anything below it.
_ROOT_TREES = (
    "/etc", "/usr", "/boot", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/sys", "/proc",
    "/dev", "/system", "/windows", "/program files", "/program files (x86)", "/programdata",
)
# The 8.3 stems of the drive-root system trees that have a short name (``progra``).
_SHORT_TREE_STEMS = frozenset(
    tree[1:].replace(" ", "").replace(".", "")[:6] for tree in _ROOT_TREES
    if "/" not in tree[1:] and (len(tree) > 9 or " " in tree))

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
_SPACES = re.compile(r"[^\S\n]+")

# One token of command text.  ``(``, ``)``, ``[``, ``]``, ``{``, ``}``, ``,``
# and whitespace separate words, except inside a brace expansion
# (``./{a,b}/c``, no spaces or quotes), which is part of its word; quotes are
# their own tokens, so a quoted root with a space (``"C:\\Program Files"``) is
# read whole as well.
_BRACE_TEXT = r"[^\s{}()\[\]'\"`;|&<>,]*"
_BRACE_GROUP = re.compile(r"\{([^{}]*)\}")
_BRACE_MARKS = re.compile(r"[{},]")
_TOKEN = re.compile(
    r"(?P<nl>`?\r?\n)"
    r"|(?P<sub>\$\(|<\()"
    r"|(?P<tick>`)"
    r"|(?P<open>\()"
    r"|(?P<close>\))"
    r"|(?P<pipe>\|[|&]?)"
    r"|(?P<end>&&?|;)"
    r"|(?P<quote>['\"])"
    r"|(?P<word>(?:[^\s()\[\]{},'\"`;|&$<]|\$(?!\()|<(?!\()"
    r"|\{" + _BRACE_TEXT + r"(?:," + _BRACE_TEXT + r")+\})+)"
)


class _Unreadable(ValueError):
    """The arguments do not have a shape this classifier can inspect."""


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, str)]
    return []


def _parts(name: str, arguments: Mapping[str, Any]) -> list[tuple[str, bool, bool]]:
    """Every command-bearing argument of tool ``name`` as ``(text, is_shell, backtick_substitutes)``."""
    language = arguments.get("language")
    language = language.strip().casefold() if isinstance(language, str) else ""
    code_is_shell = language in _SHELL_LANGUAGES
    code_ticks = language not in _BACKTICK_ESCAPES
    parts: list[tuple[str, bool, bool]] = []
    argv: list[str] = []
    for key in _ARGV_KEYS:
        value = arguments.get(key)
        if key == "args_json" and isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                pass  # inspect the raw text instead
        argv.extend(_strings(value))
    if argv:
        parts.append((" ".join(argv), True, True))
    is_code = name == "run_code"
    for key in _SHELL_KEYS:
        # A run_code call's stdin is input to its language, not to a shell;
        # any other tool's stdin is shell text, whatever keys come with it.
        follows_code = key == "stdin" and is_code
        for text in _strings(arguments.get(key)):
            parts.append((text, code_is_shell if follows_code else True,
                          code_ticks if follows_code else True))
    for text in _strings(arguments.get("code")):
        parts.append((text, code_is_shell, code_ticks))
    # A standalone verification payload carries its commands as ``checks[].argv``.
    checks = arguments.get("checks")
    for check in checks if isinstance(checks, (list, tuple)) else ():
        if isinstance(check, Mapping) and (check_argv := _strings(check.get("argv"))):
            parts.append((" ".join(check_argv), True, True))
    return parts


def _is_drive_root(token: str) -> bool:
    return (2 <= len(token) <= 4 and token[0].isascii() and token[0].isalpha()
            and token[1] == ":" and token[2:] in ("", "\\", "/", "\\*", "/*"))


def _is_drive_letter(target: str) -> bool:
    return len(target) >= 2 and target[1] == ":" and target[0].isascii() and target[0].isalpha()


def _strip_component(part: str) -> str:
    part = part.split(":", 1)[0]  # an NTFS stream suffix (``::$INDEX_ALLOCATION``)
    return part if part in (".", "..") else part.rstrip(" .")


def _strip_components(path: str) -> str:
    """Win32 drops a stream suffix and trailing dots and spaces from every component but ``.`` and ``..``.

    ``C:\\Windows.`` and ``C:\\Windows::$INDEX_ALLOCATION`` are
    ``C:\\Windows`` and ``C:\\ .`` is ``C:\\``; a component that is only dots
    and spaces (``...``, ``.. ``) vanishes.
    """
    return "/".join(_strip_component(part) for part in path.split("/"))


def _resolve(drive: str, target: str) -> tuple[str, str]:
    """``(drive, path)`` with stream suffixes, trailing dots, ``/*``, ``.`` and ``..`` resolved."""
    target = _strip_components(target)
    while target.endswith("/*"):
        target = target[:-2] or "/"
    if target.startswith("/"):
        target = posixpath.normpath("/" + target.lstrip("/"))
    elif target:
        target = posixpath.normpath(target)
    return drive, target


def _device_targets(target: str) -> tuple[list[tuple[str, str]], str]:
    """A Win32 device path's remainder: its readings, or ``[]`` and the drive-letter or share path it names."""
    if target.startswith("globalroot/"):
        target = target[len("globalroot/"):]
        if target.startswith("device/"):
            name, _, rest = target[len("device/"):].partition("/")
            part, _, after = rest.partition("/")
            if _HARDDISK.fullmatch(name) and _PARTITION.fullmatch(part):
                name, rest = name + "/" + part, after
            return [_resolve("//device/" + name, "/" + rest)], ""
        target = target.removeprefix("??/")
    if _is_drive_letter(target):
        return [], target
    if target.startswith("unc/"):
        return [], "//" + target[4:]
    # ``Volume{guid}``, ``PhysicalDrive0``, ``HarddiskVolume3``: the device is the drive.
    name, _, rest = target.partition("/")
    return [_resolve("//./" + name, "/" + rest)], ""


def _normalize_targets(token: str) -> list[tuple[str, str]]:
    """Every reading of a path as ``(drive, path)``: case-folded, ``/`` separators, variables mapped, resolved.

    The home directory becomes ``/~`` and ``~alice`` becomes ``/home/alice``.
    A drive letter is the drive (``("c:", "/windows")``), and a device path
    (``\\\\?\\C:\\``, ``\\??\\C:\\``, ``\\\\?\\GLOBALROOT\\Device\\X``) is read
    as the path it names.  A UNC path (``\\\\host\\c$\\``) is read both as a
    share, whose root is ``/`` (the administrative share ``c$`` is drive
    ``c:``), and as a POSIX path; ``/mnt/c``, ``/cygdrive/c`` and ``/c`` are
    read both as drive ``c:`` and as POSIX paths.  A relative path stays relative.
    """
    target = _BRACED_VARIABLE.sub(r"$\1", token.strip().strip("'\"").casefold()).replace("\\", "/")
    device = target.startswith("/??/")
    if device:
        target = target[4:]
    elif match := _DEVICE_PREFIX.match(target):
        device = not match.group(1)
        target = ("" if device else "//") + target[match.end():]
    if device:
        device_readings, target = _device_targets(target)
        if device_readings:
            return device_readings
    for spelling in _HOME_SPELLINGS:
        if target == spelling or target.startswith(spelling + "/"):
            target = _HOME + target[len(spelling):]
            break
    else:
        for spelling, root in _SYSTEM_SPELLINGS:
            if target == spelling or target.startswith(spelling + "/"):
                target = root + target[len(spelling):]
                break
        else:
            if user := _USER_HOME.fullmatch(target.split("/", 1)[0]):
                target = "/home/" + user.group(1) + target[len(user.group(0)):]
    if _is_drive_letter(target):
        return [_resolve(target[:2], "/" + target[2:])]
    readings = []
    if target.startswith("//") and not target.startswith("///"):
        share = _UNC.fullmatch("//" + _SLASHES.sub("/", target[2:]))
        if share:
            host, name, rest = share.group(1), share.group(2), share.group(3) or "/"
            drive = name[0] + ":" if _ADMIN_SHARE.fullmatch(name) else f"//{host}/{name}"
            readings.append(_resolve(drive, rest))
    posix = _resolve("", target)
    if mounted := _MOUNTED_DRIVE.fullmatch(posix[1]):
        readings.append(_resolve(mounted.group(1) + ":", mounted.group(2) or "/"))
    readings.append(posix)
    return readings


class _Place(NamedTuple):
    """One reading of a directory, bounded in size whatever its depth.

    Whether a path is a root depends only on its drive, its first few
    components and its depth (``_is_root_reading``), so a directory keeps
    just those: ``head`` is its first ``_KEPT`` components (a longer one than
    ``_LONG_NAME`` as a placeholder) and ``depth`` how many it has.  A
    relative one also counts the ``..`` it starts with (``ups``).  Immutable.
    """

    drive: str
    absolute: bool
    ups: int
    head: tuple[str, ...]
    depth: int


def _place(drive: str, path: str) -> _Place:
    """The ``_Place`` of one ``_normalize_targets`` reading."""
    parts = [part for part in path.split("/") if part and part != "."]
    absolute = path.startswith("/")
    ups = 0
    if not absolute:
        while ups < len(parts) and parts[ups] == "..":
            ups += 1
    head = tuple(part if len(part) <= _LONG_NAME else "\x01" for part in parts[ups:ups + _KEPT])
    return _Place(drive, absolute, ups, head, len(parts) - ups)


def _places(cwd: str) -> tuple[_Place, ...]:
    return tuple(_place(drive, path) for drive, path in _normalize_targets(cwd)) if cwd else ()


def _descend(place: _Place, relative: _Place) -> _Place:
    """``place`` joined with a relative path: its ``..`` stop at a root and climb past a relative anchor."""
    depth, ups = place.depth - relative.ups, place.ups
    if depth < 0:
        ups += 0 if place.absolute else -depth
        depth = 0
    # Past len(head), head is all _KEPT components and the joined head starts with them.
    head = place.head[:depth] + relative.head if depth <= len(place.head) else place.head
    return _Place(place.drive, place.absolute, ups, head[:_KEPT], depth + relative.depth)


def _place_readings(place: _Place) -> list[tuple[str, str]]:
    """``place`` as ``(drive, path)`` readings with the same answer from ``_is_root_reading``."""
    if not place.absolute:
        return [("", "../" if place.ups else ".")]
    parts = place.head
    if place.depth > len(parts):
        parts += ("\x00",)  # deeper than the kept components
    else:
        while parts and parts[-1] == "*":  # ``/*`` is the directory itself, as in _resolve
            parts = parts[:-1]
    drive, path = place.drive, "/" + "/".join(parts)
    readings = [(drive, path)]
    if not drive and (mounted := _MOUNTED_DRIVE.fullmatch(path)):
        readings.append((mounted.group(1) + ":", mounted.group(2) or "/"))
    return readings


def _against(readings: list[tuple[str, str]], cwd: tuple[_Place, ...],
             relatives: dict[str, _Place] | None = None) -> list[tuple[str, str]]:
    """``readings``, with a relative one resolved against each reading of ``cwd`` when there is one.

    ``relatives`` caches the ``_Place`` of a relative reading across calls.
    """
    if not cwd:
        return readings
    resolved: list[tuple[str, str]] = []
    for drive, path in readings:
        if drive or path.startswith("/"):
            resolved.append((drive, path))
        else:
            relative = relatives.get(path) if relatives is not None else None
            if relative is None:
                relative = _place("", path)
                if relatives is not None:
                    relatives[path] = relative
            for place in cwd:
                resolved.extend(_place_readings(_descend(place, relative)))
    return resolved


def _is_root_path(path: str) -> bool:
    return path.startswith("/") and (
        path in _EXACT_ROOTS or posixpath.dirname(path) in _HOME_PARENTS
        or any(path == tree or path.startswith(tree + "/") for tree in _ROOT_TREES))


def _is_root_reading(drive: str, path: str) -> bool:
    """A root, a system tree, an 8.3 name directly under a drive root, or a relative path above its anchor."""
    if not path.startswith("/"):
        return path == ".." or path.startswith("../")
    if _is_root_path(path):
        return True
    if not (len(drive) == 2 and _is_drive_letter(drive)):
        return False
    first, _, rest = path[1:].partition("/")
    short = _SHORT_NAME.fullmatch(first)
    return short is not None and (not rest or short.group(1) in _SHORT_TREE_STEMS)


_EMPTY_PLACE = _Place("", False, 0, (), 0)


def _any_root_against(readings: list[tuple[str, str]], cwd: tuple[_Place, ...],
                      relatives: dict[str, _Place]) -> bool:
    """Whether any reading resolves to a root against ``cwd`` (fast equivalent of ``_against``)."""
    if not cwd:
        for drive, path in readings:
            if _is_root_reading(drive, path):
                return True
        return False
    for drive, path in readings:
        if drive or path.startswith("/"):
            if _is_root_reading(drive, path):
                return True
        else:
            relative = relatives.get(path)
            if relative is None:
                relative = relatives[path] = _place("", path)
            for place in cwd:
                if not place.absolute:
                    if place.ups or relative.ups > place.depth:
                        return True
                else:
                    for rdrive, rpath in _place_readings(_descend(place, relative)):
                        if _is_root_reading(rdrive, rpath):
                            return True
    return False


def _is_root_target(token: str, cwd: tuple[_Place, ...] = ()) -> bool:
    """Whether any reading of ``token`` names a drive, share, filesystem or home root, or a path in a system tree."""
    return any(_is_root_reading(drive, path) for drive, path in _against(_normalize_targets(token), cwd))


def _is_delete_verb(name: str) -> bool:
    return name in _DELETE_VERBS or name == "rmtree" or name.endswith(".rmtree")


def _is_flag(token: str) -> bool:
    """``-rf``, ``--force``, ``-Recurse`` or a Windows switch such as ``/s``; not ``/``, ``/.`` or ``//``."""
    return (len(token) > 1 and token[0] == "-") or (
        len(token) == 2 and token[0] == "/" and token[1].isalpha())


def _is_operand_flag(token: str, switches: bool) -> bool:
    """A flag of a delete verb; ``/x`` is a switch only for cmd's verbs (``rm /c`` names a path)."""
    return (len(token) > 1 and token[0] == "-") or (
        switches and len(token) == 2 and token[0] == "/" and token[1].isalpha())


def _program_name(program: str) -> str:
    name = program.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def _file_name(path: str) -> str:
    """The last component of a path or URL, without a query or fragment."""
    path = path.split("?", 1)[0].split("#", 1)[0].replace("\\", "/").rstrip("/")
    return path.rsplit("/", 1)[-1]


def _ends_in_output_flag(word: str) -> bool:
    """A bundle of short flags whose last one is ``-o`` (curl's ``-fsSLo FILE``)."""
    return len(word) > 2 and word[0] == "-" and word[-1] == "o" and word[1:].isalpha()


def _fold_variables(text: str) -> str:
    """``${HOME}`` is ``$home``, and ``${OUT:?}``, ``${1%/}`` and ``${arr[0]}`` are ``$out``, ``$1`` and ``$arr``."""
    text = _BRACED_VARIABLE.sub(r"$\1", text)
    for _nested in range(2):  # ``${a:-${b:-c}}``
        text = _PARAMETER_EXPANSION.sub(r"$\1", text)
    return text


def _brace_words(word: str) -> tuple[list[str], bool]:
    """Bash's brace expansion of ``word`` (``./{a,b}/c`` is ``./a/c ./b/c``) and True.

    Past ``_MAX_BRACE_WORDS`` words, the pieces between its braces and commas
    and False.  ``_TOKEN`` lets a ``{`` into a word only as a group with a
    comma, so every group found here expands.  Empty words are dropped, as
    bash drops them (``{,}`` is no word).
    """
    done: list[str] = []
    pending = [word]
    while pending:
        current = pending.pop()
        group = _BRACE_GROUP.search(current)
        if group is None:
            if current:
                done.append(current)
            continue
        head, tail = current[:group.start()], current[group.end():]
        pending.extend(head + choice + tail for choice in reversed(group.group(1).split(",")))
        if len(done) + len(pending) > _MAX_BRACE_WORDS:
            return [piece for piece in _BRACE_MARKS.split(word) if piece], False
    return done, True


class _Command:
    """One command's words, and what the scan has learned about them so far."""

    __slots__ = ("after_download", "comment_at", "delete_at", "downloads", "interpreter_at", "parens",
                 "piped", "prefixed", "program_at", "quote", "quote_from", "saves_urls", "settled",
                 "switches", "words")

    def __init__(self, piped: bool = False, after_download: bool = False) -> None:
        self.words: list[str] = []
        self.piped = piped  # it reads a pipe
        self.after_download = after_download  # a download ran earlier on its line
        self.settled = False  # its program is known
        self.prefixed = False  # a ``sudo``/``env``-style prefix stands before its program
        self.program_at = -1
        self.interpreter_at = -1
        self.delete_at = -1
        self.switches = False
        self.downloads = False
        self.saves_urls = False
        self.quote = ""  # the open quote character, if any
        self.quote_from = 0
        self.comment_at = -1  # where an unquoted ``#`` word starts a shell comment
        self.parens = 0  # ``(`` / ``@(`` opened in it and not yet closed


class _Context:
    """The top level, or one ``$(...)``, ``<(...)`` or backtick substitution."""

    __slots__ = ("at_start", "command", "downloaded", "feeds", "kind", "operands", "parens")

    def __init__(self, kind: str, *, feeds: bool = False, at_start: bool = False,
                 operands: bool = False) -> None:
        self.kind = kind  # "top", "sub" or "tick"
        self.command = _Command()
        self.parens = 0  # plain parentheses open inside this substitution
        self.feeds = feeds  # its output is the text or script of an interpreter
        self.at_start = at_start  # its output is run as a command
        self.downloaded = False  # a downloader ran inside it
        self.operands = operands  # its words are operands of an enclosing delete


class _Scan:
    """One left-to-right pass over the command texts of one call.

    Owned by a single ``classify`` call and dropped with it.  Each method
    answers True as soon as a BLOCK shape is seen.  Every word is visited a
    bounded number of times, so the pass is linear in the text.
    """

    def __init__(self, cwd: str) -> None:
        self.cwd0 = _places(cwd)
        self.cwd = self.cwd0  # the directory a relative operand resolves against
        self.downloaded = False
        self.evaluated = False
        self.names: set[str] = set()  # the file names downloads have written
        self.readings: dict[str, list[tuple[str, str]]] = {}  # _normalize_targets per word
        self.relatives: dict[str, _Place] = {}  # _place per relative reading
        self.roots: dict[str, bool] = {}  # per word, for the current cwd: cleared when it changes
        self.shell = True
        self.line_download = False
        self.previous = ""

    def run(self, text: str, *, shell: bool, ticks: bool) -> bool:
        self.shell, self.names = shell, set()
        self._move(self.cwd0)
        self.line_download, self.previous = False, ""
        stack = [_Context("top")]
        joins = False  # the last token was a pipe or ``&&``: a newline continues the command
        glue = -1  # where a closing quote or substitution ended: a word there continues it
        for match in _TOKEN.finditer(text):
            kind, token, context = match.lastgroup, match.group(), stack[-1]
            if kind == "word":
                if self._words(context, token, match.start() == glue):
                    return True
            elif kind == "nl":
                if token[0] == "`":
                    if not (ticks and context.kind == "tick"):
                        continue  # PowerShell's line continuation
                    if self._close(stack):  # the backtick closes a substitution
                        return True
                    context = stack[-1]
                start = match.start()
                if joins or text[start - 1:start] in ("\\", "^") or self._held(context.command):
                    continue
                if self._end(context):
                    return True
                self.line_download = False
            elif kind == "pipe" or kind == "end":
                if self._held(context.command):
                    continue
                if self._end(context, piped=kind == "pipe"):
                    return True
            elif kind == "quote":
                command = context.command
                if command.comment_at >= 0 and not command.switches:
                    continue  # a quote inside a comment
                quoted = bool(command.quote) and len(command.words) > command.quote_from
                if self._quote(command, token):
                    return True
                if quoted and not command.quote:  # a closing quote after a quoted word
                    glue = match.end()
            elif kind == "tick":
                if not ticks:
                    pass  # an escape character
                elif context.kind == "tick":
                    if self._close(stack):
                        return True
                    glue = match.end()
                elif len(stack) <= _MAX_DEPTH:
                    stack.append(self._open("tick", context))
            elif kind == "sub" and len(stack) <= _MAX_DEPTH:
                stack.append(self._open("sub", context))
            elif context.kind == "sub":
                # A plain parenthesis, or past _MAX_DEPTH a substitution read as
                # part of the deepest one: only its closing parenthesis is tracked.
                command = context.command
                if kind != "close":
                    context.parens += 1
                    command.parens += 1
                elif context.parens:
                    context.parens -= 1
                    command.parens -= 1 if command.parens else 0
                elif self._close(stack):
                    return True
                else:
                    glue = match.end()
            elif kind == "open":
                context.command.parens += 1
            elif kind == "close" and context.command.parens:
                context.command.parens -= 1
            joins = kind == "pipe" or token == "&&"
        while len(stack) > 1:
            if self._close(stack):
                return True
        return self._end(stack[0])

    def _held(self, command: _Command) -> bool:
        """Whether a separator or newline here is inside a quote or ``(`` a delete's command left open."""
        return self.shell and command.delete_at >= 0 and bool(command.quote or command.parens)

    def _move(self, cwd: tuple[_Place, ...]) -> None:
        self.cwd = cwd
        self.roots.clear()

    def _root(self, word: str) -> bool:
        cached = self.roots.get(word)
        if cached is None:
            cached = self.roots[word] = _any_root_against(self._readings(word), self.cwd, self.relatives)
        return cached

    def _readings(self, word: str) -> list[tuple[str, str]]:
        readings = self.readings.get(word)
        if readings is None:
            readings = self.readings[word] = _normalize_targets(word)
        return readings

    def _words(self, context: _Context, token: str, glued: bool) -> bool:
        """One word token, brace-expanded; every expanded word keeps what it is glued to."""
        if "{" not in token:
            return self._word(context, token, glued)
        words, expanded = _brace_words(token)
        command = context.command
        prefix = command.words[-1] if expanded and glued and command.words else ""
        for index, word in enumerate(words):
            if self._word(context, word if index == 0 else prefix + word, glued and index == 0):
                return True
        return False

    def _word(self, context: _Context, word: str, glued: bool) -> bool:
        name = _program_name(word) if ("/" in word or "\\" in word or word.endswith(".exe")) else word
        if (word in _BLOCK_TOKENS or name in _BLOCK_TOKENS or word.startswith("mkfs")
                or (name.startswith("mkfs") and _MKFS.fullmatch(name))):
            return True
        if self.previous == "format" and _is_drive_root(word):
            return True
        self.previous = word
        command = context.command
        if name in _DOWNLOADERS:
            command.downloads = context.downloaded = self.downloaded = self.line_download = True
            command.saves_urls = command.saves_urls or name in _URL_SAVERS
        if name in _EVAL_TOKENS:
            self.evaluated = True
        if word == "--remote-name" or word == "--remote-name-all":
            command.saves_urls = True
        if glued and command.words:
            # ``"$HOME"/``, ``$(pwd)/c``: one shell word with what it follows.
            word = command.words[-1] = command.words[-1] + word
            return self.shell and context.operands and command.comment_at < 0 and self._root(word)
        if self.shell and command.comment_at < 0 and not command.quote and word[0] == "#":
            command.comment_at = len(command.words)
        if command.comment_at >= 0:
            pass  # a comment: not a delete, and not an operand
        elif self.shell and command.delete_at < 0 and _is_delete_verb(name):
            command.delete_at = len(command.words)
            command.switches = name in _SWITCH_VERBS
        elif self.shell and context.operands and not _is_flag(word) and self._root(word):
            return True
        self._add(command, word, name)
        return False

    @staticmethod
    def _add(command: _Command, word: str, name: str) -> None:
        """Append ``word``, tracking the program it may be (``_command_programs`` as a stream)."""
        index = len(command.words)
        command.words.append(word)
        if command.settled or _is_flag(word) or "=" in word:
            return
        unversioned = name.rstrip("0123456789.")
        interpreter = unversioned if unversioned in _PIPE_SHELLS else name
        if interpreter in _PIPE_PREFIXES:
            command.prefixed = True
            return
        if command.program_at < 0:
            command.program_at = index
        if interpreter in _PIPE_SHELLS and command.interpreter_at < 0:
            command.interpreter_at = index
        command.settled = not command.prefixed

    def _quote(self, command: _Command, mark: str) -> bool:
        """At a closing quote, a delete's quoted operand with spaces is checked whole."""
        if not command.quote:
            command.quote, command.quote_from = mark, len(command.words)
            return False
        if mark != command.quote:
            return False
        command.quote = ""
        start = command.quote_from
        if (self.shell and 0 <= command.delete_at < start
                and len(command.words) - start > 1):
            return self._root(" ".join(command.words[start:]))
        return False

    def _open(self, kind: str, parent: _Context) -> _Context:
        command = parent.command
        return _Context(kind, feeds=command.interpreter_at >= 0,
                        at_start=self.shell and not command.words,
                        operands=self.shell and command.comment_at < 0 and (
                            parent.operands or command.delete_at >= 0))

    def _close(self, stack: list[_Context]) -> bool:
        child = stack.pop()
        if self._end(child):
            return True
        if child.downloaded and (child.feeds or child.at_start):
            return True
        parent = stack[-1]
        parent.downloaded = parent.downloaded or child.downloaded
        self._add(parent.command, "$()", "$()")  # the substitution stands as one word
        return False

    def _end(self, context: _Context, *, piped: bool = False) -> bool:
        """The command in ``context`` ends; the next one reads a pipe when ``piped``."""
        command = context.command
        if command.words:
            if self._ended(command):
                return True
            # ``sh -c "$(cat F)"`` after F was downloaded: the substitution's text is the download.
            if context.kind != "top" and self.names and any(
                    _file_name(word) in self.names for word in command.words):
                context.downloaded = True
        if command.words or piped != command.piped:
            context.command = _Command(piped, piped and self.line_download)
        else:
            # An empty command is reused: newline floods allocate nothing.
            command.quote, command.parens = "", 0
        self.previous = ""
        return False

    def _ended(self, command: _Command) -> bool:
        words = command.words
        if command.piped and command.after_download and command.interpreter_at >= 0:
            return True  # ``curl x | sh``
        if self.shell and command.delete_at >= 0:
            # The operands end at a comment; cmd (``del``, ``rd``) has no ``#`` comment.
            end = command.comment_at if command.comment_at >= 0 and not command.switches else len(words)
            for word in words[command.delete_at + 1:end]:
                if not _is_operand_flag(word, command.switches) and self._root(word):
                    return True
        if self.names and self._runs_a_download(command):
            return True
        if command.downloads or (command.piped and command.after_download):
            self._record_outputs(command)
        if self.shell and command.program_at >= 0:
            prog = words[command.program_at]
            if prog in _CD_VERBS or (("/" in prog or "\\" in prog or prog.endswith(".exe"))
                                     and _program_name(prog) in _CD_VERBS):
                self._change_directory(words[command.program_at + 1:])
        return False

    def _runs_a_download(self, command: _Command) -> bool:
        """Whether the command runs a file a download wrote: ``./tool``, ``bash a.sh``, ``pwsh -File a.ps1``."""
        words = command.words
        if command.program_at >= 0:
            program = words[command.program_at]
            name = _file_name(program)
            if name in self.names and ("/" in program or "\\" in program or "." in name):
                return True
        if command.interpreter_at < 0:
            return False
        previous, script = "", ""
        for word in words[command.interpreter_at + 1:]:
            if previous == "-file" or previous == "-f":
                if _file_name(word) in self.names:
                    return True
            elif not script and word != "<" and not _is_flag(word):
                script = word.removeprefix("<")
            previous = word
        return bool(script) and _file_name(script) in self.names

    def _record_outputs(self, command: _Command) -> None:
        """Note the files this download (or the writer it pipes into) names."""
        words = command.words
        writer = command.piped and command.program_at >= 0 and (
            _program_name(words[command.program_at]) in _WRITERS)
        last_word_saver = False
        previous = ""
        for index, word in enumerate(words):
            last_word_saver = last_word_saver or _program_name(word) in _LAST_WORD_SAVERS
            if previous in _OUTPUT_FLAGS or previous in _REDIRECTS or _ends_in_output_flag(previous):
                self._note(word)
            elif word.startswith(_OUTPUT_ASSIGNMENTS):
                self._note(word.split("=", 1)[1])
            elif word[:1] == ">" and word not in _REDIRECTS:
                self._note(word.lstrip(">"))
            elif word.startswith("-o") and len(word) > 2 and word[2] != "-" and word not in _OUTPUT_FLAGS:
                self._note(word[2:])  # curl's ``-oFILE``
            if "://" in word and command.saves_urls and "/" in word.split("://", 1)[1]:
                self._note(word)
            if writer and index > command.program_at and not _is_flag(word):
                self._note(word)
            previous = word
        if last_word_saver:
            self._note(words[-1])  # ``certutil -urlcache -f URL FILE``, ``bitsadmin /transfer j URL FILE``

    def _note(self, path: str) -> None:
        name = _file_name(path)
        if name not in _NOT_A_FILE:
            self.names.add(name)

    def _change_directory(self, operands: list[str]) -> None:
        target = "~"
        for word in operands:
            if not _is_flag(word):
                target = word
                break
        readings = self._readings(target)
        if len(readings) == 1 and not readings[0][0] and not readings[0][1].startswith("/"):
            path = readings[0][1]
            rel = self.relatives.get(path)
            if rel is None:
                rel = self.relatives[path] = _place("", path)
            cwd = self.cwd
            if not cwd:
                self._move((_descend(_EMPTY_PLACE, rel),))
            elif len(cwd) == 1:
                self._move((_descend(cwd[0], rel),))
            else:
                self._move(tuple(_descend(place, rel) for place in cwd))
            return
        if any(drive or path.startswith("/") for drive, path in readings):
            self._move(tuple(_place(drive, path) for drive, path in readings))
        else:
            # Each step is bounded (``_Place``), so a ``cd`` flood stays linear.
            relatives = []
            for _drive, path in readings:
                rel = self.relatives.get(path)
                if rel is None:
                    rel = self.relatives[path] = _place("", path)
                relatives.append(rel)
            self._move(tuple(_descend(place, relative) for place in self.cwd or (_EMPTY_PLACE,)
                             for relative in relatives))


def _blocked(parts: list[tuple[str, bool, bool]], cwd: str) -> bool:
    texts = [(_fold_variables(text), shell, ticks) for text, shell, ticks in parts]
    every = _SPACES.sub(" ", "\n".join(text for text, _shell, _ticks in texts))
    if any(marker in every for marker in _BLOCK_TEXT):
        return True
    scan = _Scan(cwd)
    if any(scan.run(text, shell=shell, ticks=ticks) for text, shell, ticks in texts):
        return True
    # ``iex (iwr ...)``, ``[scriptblock]::Create((iwr ...))``: a download evaluated with no pipe at all.
    return scan.downloaded and (scan.evaluated or _SCRIPTBLOCK in every)


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


def _delete_target_blocked(arguments: Mapping[str, Any], cwd: str) -> bool:
    path = arguments.get("path")
    if not isinstance(path, str):
        raise _Unreadable("file_delete needs a path")
    return _is_root_target(path, _places(cwd)) or path.strip().rstrip(" .") == ""


def _classify(tool_name: str, arguments: Any, risk: str) -> str:
    if not isinstance(arguments, Mapping):
        if arguments is not None:
            return ASK
        arguments = {}
    name = str(tool_name or "").strip().lstrip("/")
    parts = _parts(name, arguments)
    cwd = arguments.get("cwd")
    cwd = cwd.lower() if isinstance(cwd, str) else ""
    if sum(len(text) for text, _shell, _ticks in parts) + len(cwd) > _MAX_TEXT:
        return ASK  # too long to read: fail closed
    parts = [(text.lower(), shell, ticks) for text, shell, ticks in parts]
    if parts and _blocked(parts, cwd):
        return BLOCK
    if name in _DELETE_TOOLS and _delete_target_blocked(arguments, cwd):
        return BLOCK
    if name in _ALWAYS_ASK or name.startswith(_ASK_PREFIXES):
        return ASK
    if any(_elevated(_SPACES.sub(" ", text)) for text, _shell, _ticks in parts):
        return ASK
    if name in _ARGV_TOOLS:
        return ALLOW if _safe_argv(_argv(arguments)) else ASK
    return _RISK_VERDICTS.get(str(risk or ""), ASK)


def classify(tool_name: str, arguments: Any, *, risk: str) -> str:
    """``ALLOW``, ``ASK`` or ``BLOCK`` for one tool call; never ``ALLOW`` on a fault.

    ``risk`` is the permission catalog's grade for ``tool_name``.  The
    verdict is ``BLOCK`` when this module's rules (``_classify``) or the
    floor's (``tool_call_classifier_floor.blocks``) block, and this module's
    verdict otherwise.  A fault in ``_classify`` asks for arguments of a shape
    it does not read and blocks any other call that carries command text: bypass
    skips an ASK, so a fault while reading a command must not let it run unread.
    """
    try:
        verdict = _classify(tool_name, arguments, risk)
    except _Unreadable:
        verdict = ASK
    except Exception:  # noqa: BLE001 - fail closed: a fault is never an allow
        verdict = BLOCK if _floor.carries_commands(arguments) else ASK
    if verdict not in VERDICTS:
        verdict = ASK
    if verdict == BLOCK or _floor.blocks(tool_name, arguments):
        return BLOCK
    return verdict


__all__ = ["ALLOW", "ASK", "BLOCK", "VERDICTS", "classify"]
