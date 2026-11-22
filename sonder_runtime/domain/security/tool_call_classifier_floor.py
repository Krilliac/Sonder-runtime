"""Stricter BLOCK rules under ``tool_call_classifier``: a floor that only raises.

``tool_call_classifier.classify`` answers ``BLOCK`` when ``blocks`` is True
and its own rules' verdict otherwise, so nothing here can lower a verdict.
This module answers one question, whether a call blocks; it has no ALLOW or
ASK rules of its own.  Every rule below reads a call as more dangerous than
the base rules do, never as less.

Where each rule reads:

* Shell text is the argv of a program (``program``/``args_json``/``args``/
  ``argv``, and ``checks[].argv``) joined with spaces,
  ``command``/``cmd``/``script``/``stdin``, and ``run_code`` in a shell
  language (``_SHELL_LANGUAGES``; ``stdin`` of a ``run_code`` call follows
  its language).  Only shell text gets the delete scan: a delete verb
  (``rm``, ``del``, ``Remove-Item``...) as any word of a command with a
  root among its operands, a relative operand resolved against ``cwd`` (and
  any ``cd`` before it), and a relative path that climbs past its anchor
  counted as a root (with no ``cwd``, ``dist`` is not a root and ``../dist``
  is).  Every word may be the command word (``valgrind rm``, ``script -c``,
  ``call rd``, ``grep rm -rf /``): no program is trusted to only print its
  words.  A word names a delete verb as itself (``rm``, ``/bin/rm``), as
  any piece between ``=``, ``:``, ``+`` and ``!`` (``X=rm``,
  ``alias x=rm``, ``--pager=rm``, ``--pretty=format:rm``, ``+rm``), and as
  the shell passes it once quotes and escapes are removed (``r"m"``,
  ``$'rm'``, ``--pre=r\\m``).  A flag word glued to a quote
  (``-O'rm -rf /'``) and a short-flag bundle holding a run flag of a
  program named before it in the command (``_RUN_FLAGS``: ``git grep
  -nOrm``, ``man -aPrm``) also name the verb any of their short flags may
  take as a value, and the word after such a run flag is its value
  (``rg --pre rm``, ``git grep -O rm``).  A delete a flag runs
  (``rg --pre=rm``, ``rg --pre rm``, ``--format=rm``) reads every word of
  its command after the program as an operand: the program applies it to
  the paths it was given anywhere (``rg -e x / --pre=rm``).  ``>|`` is a
  redirection (bash's noclobber) read fail-closed: past its target the next
  word may be a command word again.  A quoted operand with spaces is read
  whole (``rm -rf "C:\\Program Files"``), and so is a run of words between
  flags inside a quote that holds the delete
  (``"Remove-Item -Recurse -Path:'C:\\Program Files'"``), a flag's value
  starting it.  A word is also read in pieces where shells split it
  differently: ``C:\\)`` as ``C:\\`` (PowerShell, cmd), ``/>x`` as ``/``.
  A parameter expansion (``"${OUT:?}/"``, ``${A[0]:-x}``) is one word, read
  both as its variable and, for ``-``/``=``/``+`` (with or without ``:``),
  as its WORD, nested expansions to any depth up to ``_MAX_FOLDS``; one
  whose WORD holds ``;``, ``|``, ``&`` or a newline is read both unfolded
  (cmd's and PowerShell's separators) and as bash's one word, with any
  ``$(...)`` in it as empty.  One left unfolded is also read as ``/``.  A
  brace expansion (``./{a,b}/c``) is the words it expands to; one too large
  to expand blocks in a delete.  The operands end at a ``#`` comment (except
  for cmd's ``del``/``rd``: cmd has no ``#`` comment); ``#`` starts one only
  at the start of a word that is not a brace expansion, at the start of the
  text or after a blank or one of ``; & | ( ) < >``, and a newline always
  ends one (after a pipe or ``&&`` the command goes on past it).  In a text
  with a PowerShell block comment (``<#``) a word starting ``#>`` starts none.
* An argv is also read with no shell, each element one word, and so is the
  text of every shell key but ``run_code``'s ``stdin``: the client runs
  ``run_program``'s ``command`` as one argv split by ``shlex.split`` (POSIX
  rules on POSIX, Windows rules on Windows) with no shell, so ``;``, ``|``,
  ``&&`` and newlines are arguments of the first program, not separators.
  Each such text is read as given and ``str.strip()``-ed (the client strips
  ``command`` first), each split as ``shlex.split`` splits it with
  ``posix=True`` and with ``posix=False`` (``_shlex_words``), its quotes
  then stripped as the client strips them.  The desktop client
  (``app/lib/local_manager_native.dart``) runs ``command`` itself: each
  text is also read as it splits it (``_dart_words``: Dart's ``trim`` and
  ``_splitCommand``, words cut at a NUL), and ``cwd``, ``run_code``'s
  ``language`` and ``file_delete``'s ``path`` are also read Dart-trimmed.
  ``program`` is read stripped
  too, with ``args`` and with ``args_json`` each alone, as the workbench
  runs it (``_program_argvs``).  In each argv a delete verb
  element (named as in the shell scan) and a root among the elements after
  it blocks, whatever ``#``, ``;`` or ``|`` they hold; a delete a flag runs
  reads every element after ``argv[0]`` (``rg -e x / ; echo --pre=rm``: rg
  deletes every file it searches under ``/``).
* A finder (``find``, ``fd``: ``_FINDERS``) that deletes what it finds
  (``-delete``, or a delete verb after an exec flag: ``-exec rm``,
  ``-x rm``, ``-Hxrm``) blocks when any word after it is a root, or the
  working directory it searches with no path is (``.`` against ``cwd``
  and a shell's ``cd``) (``_finder_at``, ``_finder_targets``), in every
  shell command and argv: it deletes under every path it searches,
  wherever the path stands.
* Every command-bearing text gets the token and text lists
  (``_BLOCK_TOKENS``, ``_BLOCK_TEXT``) and the download-and-execute checks.
  In other code (Python, JavaScript...), the string literals inside an exec
  call's brackets (``os.system``, ``os.popen``, ``subprocess.*``,
  ``child_process.exec``/``execSync``/``spawn``...: ``_EXEC_CALL``) get the
  whole shell scan, each alone (in every folded reading, as shell text) and
  those of the outermost call joined as an argv, unfolded
  (``subprocess.run(["rm", "-rf", "/"])``), and a literal root handed
  directly to a delete call (``shutil.rmtree("/")``, ``os.unlink``,
  ``fs.rmSync``: ``_DELETE_CALLS``) blocks.  A command or path built at run
  time (``os.system(cmd)``) is not seen.
* An unescaped newline ends a command in every scan; a newline after ``\\``,
  ``^``, a PowerShell backtick, a pipe or ``&&`` continues it (in a comment,
  one after ``\\``, ``^`` or a backtick ends it, and a delete's next line is
  also read as its operands, as cmd would).  In a delete,
  a newline (or ``;``, ``|``, ``&``) inside a quote, an unescaped unquoted
  ``(``/``@(`` it left open, an open ``(`` in its comment, or after a
  trailing ``,`` is held: the text after it is read both as commands of
  their own (with the pipe, download and delete checks) and as more
  operands of the delete, so a held separator never hides a command.  An
  escaped (``\\(``, ``^(``, PowerShell's backtick) or quoted parenthesis is
  not counted.  Two checks follow data rather than one
  command, so they span lines: a download whose file a later command runs
  (or a substitution reads into an interpreter, ``sh -c "$(cat F)"``), and an
  evaluator (``iex``, ``[scriptblock]::Create``) in the same text as a download.
* A ``cd`` (``pushd``, ``Set-Location``) is read as moving every later
  command in the text, even one in a ``$(...)`` or ``( )`` (bash scopes
  those; PowerShell does not).
* Command text above ``_MAX_TEXT`` characters (``cwd`` included) is not
  read, and blocks: bypass would skip an ASK.
* ``file_delete`` of a root blocks, as in the base rules.

Every scan is one left-to-right pass over the text, linear in its length
(up to seven passes when it has parameter expansions, one per reading,
after at most ``6 * _MAX_FOLDS`` linear folding passes, and up to four
linear ``shlex`` readings and one ``_dart_words`` reading of each shell key).  A tracked directory is kept
bounded (``_Place``), so ``cd`` floods and a long ``cwd`` stay linear too.

It fails closed: ``blocks`` is True on a fault while reading a call that
carries command text (bypass skips an ASK), and False on arguments of a
shape it does not read (the base rules ask for those).  It is purely static
(no model call, no I/O).  Arguments are inspected in memory and never
logged or kept.

The tables are immutable and every function is pure; the scan state
(``_Scan``) is created per call and never shared, so ``blocks`` is safe to
call from any thread.
"""
from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any, NamedTuple

_DELETE_TOOLS = frozenset({"file_delete"})
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
# Above this many characters of command text the classifier does not read it: BLOCK.
_MAX_TEXT = 256 * 1024
# Substitutions nested deeper than this are read as part of the deepest one.
_MAX_DEPTH = 64
# A brace expansion of more words than this is not expanded: in a delete it
# blocks, anywhere else it is read as its pieces.
_MAX_BRACE_WORDS = 64
# A tracked directory keeps its first _KEPT components and its depth: no root
# is decided past them (``/mnt/c/users/alice`` is the deepest, with one more
# to tell it from a folder inside it).  A longer component than _LONG_NAME
# names no root, so it is kept as a placeholder.
_KEPT = 6
_LONG_NAME = 64

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
_PIPE_PREFIXES = frozenset({"sudo", "su", "doas", "runas", "gsudo", "pkexec"}) | {
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
_REDIRECTS = frozenset({">", ">>", "1>", "1>>", ">|", "1>|"})
# Commands that write their input to the files they name.
_WRITERS = frozenset({"tee", "out-file", "set-content", "add-content"})
_NOT_A_FILE = frozenset({"", "-", ".", "..", "null", "nul", "$null"})
_DELETE_VERBS = frozenset({"rm", "del", "erase", "rd", "rmdir", "remove-item", "ri", "rimraf"})
# cmd's delete verbs, whose two-character ``/x`` words are switches, not paths.
_SWITCH_VERBS = frozenset({"del", "erase", "rd", "rmdir"})
# The names ``_is_delete_verb`` takes whole, longest first (a bundle's value, ``_runs_delete``).
_SUFFIX_VERBS = tuple(sorted(_DELETE_VERBS | {"rmtree"}, key=len, reverse=True))
_CD_VERBS = frozenset({"cd", "chdir", "pushd", "set-location", "sl"})
# Reserved words that stand before a command (``then rm``, ``do rm``, ``! rm``).
_KEYWORDS = frozenset({"if", "then", "else", "elif", "do", "while", "until", "!", "coproc"})
# Programs that run a command one of their flags names, and those flags as
# (long flags, short letters).  The text is lowercased, so ``-p`` is also
# ``man -P`` and ``-o`` also ``git grep -O``.  git, getopt_long, argp and
# Getopt::Long take a unique prefix of a long flag (``--op``, ``--pag``;
# Getopt::Long also ``+pager``), and a short flag with a value takes the rest
# of its bundle (``-nOrm``) or the next word, so a prefix of a long one and a
# bundle holding a short one's letter are run flags too.  ``git grep`` is
# ``grep`` after ``git``.
_RUN_FLAGS: Mapping[str, tuple[tuple[str, ...], str]] = MappingProxyType({
    "rg": (("--pre", "--hostname-bin", "--search-zip"), "z"),
    "man": (("--pager", "--html"), "ph"),
    "git grep": (("--open-files-in-pager",), "o"),
    "ag": (("--pager",), ""),
    "ack": (("--pager", "--ackrc"), ""),
    "printf": ((), "v"),
})
# What a flag word starts with (``-x``, ``--x``, Getopt::Long's ``+x``), and the
# characters a shell removes from a word: quotes, bash's ``\``, PowerShell's
# backtick and cmd's ``^``.  A flag word the shell assembles with one of them
# (``--o\p``, ``-""O``, ``--"pager"``, ``--o`p``) may be any flag after removal,
# so it is read as a run flag: every later word glued to it may be a command
# word (``_escaped_flag``, ``_Command.runs``).
_FLAG_STARTS = ("-", "+")
_ESCAPE_MARKS = "\\^`'\""
_FLAG_OR_ESCAPE = "-+\\^`'\""
_UNESCAPED = str.maketrans("", "", _ESCAPE_MARKS)
# The pieces of a word that may name a command: ``X=rm``, ``--pager=rm``,
# ``--pretty=format:rm``, less's ``+!rm``, ack's ``+rm``.
_COMMAND_PIECES = re.compile(r"[=:+!]")
# The characters ``shlex`` splits words at.
_SHLEX_BLANKS = " \t\r\n"
# Dart's ``String.trim`` whitespace: Unicode White_Space and U+FEFF (every code unit
# whose one-character string Dart 3.13.1 trims to empty).  The desktop client trims
# ``command`` with it and splits at it (``_splitCommand``, ``ch.trim().isEmpty``).
_DART_BLANKS = ("\t\n\x0b\x0c\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
                "\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff")
_DART_CLASS = "".join(re.escape(char) for char in _DART_BLANKS)
# One ``_splitCommand`` word: unquoted text and quoted runs glued together; a quote
# left open runs to the end of the text.
_DART_WORD = re.compile(rf"(?:[^{_DART_CLASS}'\"]+|\"[^\"]*(?:\"|\Z)|'[^']*(?:'|\Z))+")
# The pieces of one such word, its quotes removed.
_DART_PIECE = re.compile(r"\"([^\"]*)(?:\"|\Z)|'([^']*)(?:'|\Z)|([^\"']+)")
# A piece is a delete verb by its last characters only (``remove-item.exe``,
# ``x.rmtree``): ``_runs_delete`` reads this many of a word.
_VERB_TAIL = 32
# A quoted parenthesis is glued to a word shorter than this (``"C:\\Program
# Files (x86)"``); a longer word decides its root by its start, and keeping
# such a word growing would copy it once per parenthesis.
_PAREN_GLUE = 256
# A redirection word (``>``, ``2>``, ``&>``, ``2>&1``, ``<<<``, ``>x``): it and,
# when its target is not glued to it, the next word stand outside command position.
_REDIRECT = re.compile(r"[0-9]*(?:&>>?|[<>]{1,3}[&|]?)")
# ``>|`` (``2>|``) is bash's noclobber-override redirection; cmd reads a ``>`` and a pipe.
_NOCLOBBER = re.compile(r"[0-9]*>\|")
_REDIRECT_MARK = re.compile(r"[<>]")
# ``C:\)``: in bash an escaped ``)``, in PowerShell and cmd ``C:\`` and a ``)``.
_ESCAPED_PAREN = re.compile(r"(?<=\\)[()]")
# Words after which any later word may be a command: ``find -exec``,
# ``git rebase --exec``, ``uv run``, ``docker exec``, ``git submodule foreach``.
_EXEC_WORDS = frozenset({"-exec", "-execdir", "-ok", "-okdir", "--exec", "run", "exec", "foreach"})
# Programs that delete or run a command on everything under the paths they search, as
# ``find`` or ``fd`` (Debian's ``fdfind``, Homebrew's GNU ``gfind``).  GNU find takes its
# predicates whole (no prefix, no bundle, one dash), ``-delete`` among them; fd (clap,
# no ``infer_long_args``) takes ``--exec``/``--exec-batch`` whole or with ``=``, and
# ``-x``/``-X`` in a short-flag bundle, its value glued (``-Hxrm``) or the next words.
_FINDERS: Mapping[str, str] = MappingProxyType({"find": "find", "gfind": "find", "fd": "fd", "fdfind": "fd"})
_FIND_EXECS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
_FD_EXECS = frozenset({"--exec", "--exec-batch"})
# The characters after which a ``#`` starts a shell comment (and the start of the text).
_COMMENT_AFTER = frozenset(" \t\r\n\f\v;&|()<>")
# Parameter-expansion operators whose WORD may stand in place of the expansion.
_WORD_OPERATORS = frozenset({"-", ":-", "=", ":=", "+", ":+"})

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
# ``${arr[0]}``, ``${arr[@]:-x}``, ``${#x}``) is its variable, one word, and
# PowerShell's ``${C:\x}`` is not one.  ``_PARAMETER_EXPANSION`` leaves alone
# one whose WORD holds a ``;``, ``|``, ``&``, newline, ``$(`` or backtick (cmd
# and PowerShell read the separators as separators, so this reading keeps the
# command after them); ``_BASH_EXPANSION`` folds those too, as bash reads them:
# one word whose substitutions are read as empty.  Neither WORD holds a ``{``,
# so the spans two matches scan never overlap and every pass is linear.
_EXPANSION_HEAD = (r"\$\{[#!]?(?P<name>[a-z_][a-z0-9_]*|[0-9]+|[@*#?$!-])(?:\[[^\[\]{}\n]*\])?"
                   r"(?:\}|(?P<op>:[-=?+0-9 ]|[-=?+#%/^,@\[])(?P<word>")
_PARAMETER_EXPANSION = re.compile(_EXPANSION_HEAD + r"[^{}()`<\n;|&]{0,256})\})")
_BASH_EXPANSION = re.compile(
    _EXPANSION_HEAD + r"(?:[^{}()`<]|<(?!\()|\$\([^{}()`]*\)|`[^{}`]*`){0,256})\})")
_SUBSTITUTION = re.compile(r"\$\([^{}()`]*\)|`[^{}`]*`")
# In bash a WORD's ``;``, ``|`` and ``&`` are literal and its newline splits words.
_LITERAL_SEPARATORS = str.maketrans({";": "_", "|": "_", "&": "_", "\n": " "})
# An expansion with a WORD operator left unfolded (nested deeper than
# _MAX_FOLDS, or a WORD neither regex reads): its value is unknown, so a
# reading puts a root in its place and a delete of it blocks.
_UNFOLDED = re.compile(r"\$\{[#!]?(?:[a-z_][a-z0-9_]*|[0-9]+|[@*#?$!-])(?:\[[^\[\]{}\n]*\])?:?[-=+]")
_MAX_FOLDS = 16
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

_SPACES = re.compile(r"[^\S\n]+")

# One token of command text.  ``(``, ``)``, ``[``, ``]``, ``{``, ``}``, ``,``
# and whitespace separate words, except inside a brace expansion
# (``./{a,b}/c``, no spaces or quotes), which is part of its word; quotes are
# their own tokens, so a quoted root with a space (``"C:\\Program Files"``) is
# read whole as well, and bash's ``$'...'`` and ``$"..."`` quote like ``'``
# and ``"``.  An escaped parenthesis (``\(``) is part of its word (``_root``
# also reads ``C:\)`` as ``C:\``), the ``&`` of ``>&`` and ``<&`` (``2>&1``)
# is part of its word (``&>x`` is ``&`` and ``>x``, as cmd reads it), so is the ``|`` of
# ``>|`` (bash's noclobber redirection, not a pipe), and a ``{`` or ``}`` outside a brace
# expansion is its own token (``block``).
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
    r"|(?P<end>&&|(?<![<>])&|;)"
    r"|(?P<quote>\$?['\"])"
    r"|(?P<word>(?:\\[()]|[^\s()\[\]{},'\"`;|&$<]|\$(?![('\"])|<(?!\()|(?<=[<>])&|(?<=>)\|"
    r"|\{" + _BRACE_TEXT + r"(?:," + _BRACE_TEXT + r")+\})+)"
    r"|(?P<block>[{}])"
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
    # The desktop client trims it with Dart's ``String.trim`` (``\ufeffbash`` runs bash).
    language = language.strip().strip(_DART_BLANKS).casefold() if isinstance(language, str) else ""
    code_is_shell = language in _SHELL_LANGUAGES
    code_ticks = language not in _BACKTICK_ESCAPES
    parts: list[tuple[str, bool, bool]] = []
    argv = _main_argv(arguments)
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
    parts.extend((" ".join(check_argv), True, True) for check_argv in _check_argvs(arguments))
    return parts


def _argv_values(arguments: Mapping[str, Any], key: str) -> list[str]:
    """The strings of argv key ``key``; ``args_json`` text is decoded first (its raw text if it is not JSON)."""
    value = arguments.get(key)
    if key == "args_json" and isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            pass  # inspect the raw text instead
    return _strings(value)


def _main_argv(arguments: Mapping[str, Any]) -> list[str]:
    """The program and its arguments, from ``program``/``args_json``/``args``/``argv`` together."""
    return [word for key in _ARGV_KEYS for word in _argv_values(arguments, key)]


def _program_argvs(arguments: Mapping[str, Any]) -> list[list[str]]:
    """``program`` with ``args_json`` and with ``args``, each on its own, as the workbench runs them.

    ``workbench.run_program`` runs ``str(program).strip()`` (``_resolve_program``)
    with the elements of one list: the agent loop's ``workspace_run`` takes
    ``args`` before ``args_json``, tool dispatch ``args_json`` before ``args``
    (``server.py``).  ``_main_argv`` reads every key together with the program
    as given; these are the program stripped (a glued ``\\xa0`` or newline is
    gone when it runs) with each list alone.
    """
    program = arguments.get("program")
    if not isinstance(program, str) or not (name := program.strip()):
        return []
    return [[name, *_argv_values(arguments, key)] for key in ("args_json", "args")]


def _client_texts(text: str) -> tuple[str, ...]:
    """``text`` as given and ``str.strip()``-ed, as the client strips ``command`` before ``shlex.split``.

    ``str.strip`` drops every ``str.isspace`` character at either end
    (``\\x0b``, ``\\x1c``, ``\\x85``, ``\\xa0``, ``\\u3000``...), and ``shlex``
    splits on ``_SHLEX_BLANKS`` only, so the stripped text can split into
    different words (``\\xa0rg`` is ``rg``).  Where the strip drops nothing
    but ``_SHLEX_BLANKS`` the text is read once: ``shlex`` skips blanks before
    the first word and ends the last word at one, so both split alike
    wherever the stripped text splits at all (where it does not, the client
    runs nothing).
    """
    stripped = text.strip()
    return (text,) if stripped == text.strip(_SHLEX_BLANKS) else (text, stripped)


def _check_argvs(arguments: Mapping[str, Any]) -> list[list[str]]:
    """A standalone verification payload's commands, ``checks[].argv``."""
    checks = arguments.get("checks")
    return [check_argv for check in (checks if isinstance(checks, (list, tuple)) else ())
            if isinstance(check, Mapping) and (check_argv := _strings(check.get("argv")))]


def _shlex_words(text: str, posix: bool) -> list[str]:
    """``shlex.split(text, posix=posix)``, read in one linear pass, as the client splits ``command``.

    ``shlex`` grows each token one character at a time, which is quadratic in a
    long token; this is the same state machine (``whitespace_split`` on, no
    comment characters) building each token from a list.  Where ``shlex``
    raises (an open quote or a last escape), the words read so far are
    returned with the open token as the last one: the client then runs
    nothing, and reading them anyway can only add a BLOCK.
    """
    words: list[str] = []
    token: list[str] = []
    state = " "  # " " between tokens, "a" in a word, a quote character, or "\\" after an escape
    escaped_from = "a"  # the state an escape returns to
    quoted = False  # the token held a quote (posix: an empty ``""`` is a token)
    for char in text:
        if state == " ":
            if char in _SHLEX_BLANKS:
                continue
            if posix and char == "\\":
                escaped_from, state = "a", char
            elif char in "'\"":
                if not posix:
                    token.append(char)
                state = char
            else:
                token.append(char)
                state = "a"
        elif state in "'\"":
            quoted = True
            if char == state:
                if not posix:
                    token.append(char)
                    words.append("".join(token))
                    token, quoted, state = [], False, " "
                else:
                    state = "a"
            elif posix and char == "\\" and state == '"':
                escaped_from, state = state, char
            else:
                token.append(char)
        elif state == "\\":
            # In a double quote only the quote and the escape character may be escaped.
            if escaped_from in "'\"" and char != "\\" and char != escaped_from:
                token.append("\\")
            token.append(char)
            state = escaped_from
        elif char in _SHLEX_BLANKS:
            if token or (posix and quoted):
                words.append("".join(token))
            token, quoted, state = [], False, " "
        elif posix and char in "'\"":
            state = char
        elif posix and char == "\\":
            escaped_from, state = "a", char
        else:
            token.append(char)
    if token or (posix and quoted):
        words.append("".join(token))
    return words


def _client_argv(words: list[str]) -> list[str]:
    """The client's argv from ``shlex`` words: a word wholly in matching quotes loses them."""
    return [word[1:-1] if len(word) >= 2 and word[0] == word[-1] and word[0] in "'\"" else word for word in words]


def _dart_words(text: str) -> list[str]:
    """``text`` as the desktop client runs ``run_program``'s ``command``, in one linear pass.

    ``app/lib/local_manager_native.dart`` ``executeClientTool`` trims it with
    Dart's ``String.trim`` (``_DART_BLANKS``) and splits it with
    ``_splitCommand``: words end at ``_DART_BLANKS``, a ``'`` or ``"`` quote
    anywhere in a word is removed and holds blanks, there is no escape
    character, a quote left open runs to the end, and a word with no text
    (``""``) is no word.  Dart then starts the argv with no shell and passes
    each word up to its first NUL (measured on Windows), so each word is cut there.
    """
    words: list[str] = []
    for match in _DART_WORD.finditer(text.strip(_DART_BLANKS)):
        word = match.group()
        if "'" in word or '"' in word:
            word = "".join(piece for found in _DART_PIECE.finditer(word) for piece in found.groups() if piece)
        if word:
            words.append(word.split("\x00", 1)[0])
    return words


def _argv_readings(name: str, arguments: Mapping[str, Any]) -> list[list[str]]:
    """Every argv the call may run with no shell.

    The argv keys together and ``checks[].argv`` as given, the program
    stripped with each of its argument lists (``_program_argvs``), and the
    text of every shell key (but ``run_code``'s ``stdin``, its language's
    input) split as the client splits ``run_program``'s ``command``: as given
    and stripped (``_client_texts``), each both ways (once where both ways
    split alike), and as the desktop client splits it (``_dart_words``).
    """
    main = _main_argv(arguments)
    # A program reading equal to the main one (one list, no padding) is not read twice.
    readings = [argv for argv in (main, *(argv for argv in _program_argvs(arguments) if argv != main),
                                  *_check_argvs(arguments)) if argv]
    for key in _SHELL_KEYS:
        if key == "stdin" and name == "run_code":
            continue
        for text in _strings(arguments.get(key)):
            for reading in _client_texts(text):
                # With no quote or escape character both ways split alike: read it once.
                modes = (True, False) if any(mark in reading for mark in "'\"\\") else (True,)
                readings.extend(_client_argv(_shlex_words(reading, posix)) for posix in modes)
            dart = _dart_words(text)
            if dart != readings[-1]:
                readings.append(dart)
    return readings


def _runner(name: str, after_git: bool) -> str:
    """The ``_RUN_FLAGS`` program a word named ``name`` is (``grep`` after ``git`` is ``git grep``), or ``""``."""
    runner = "git grep" if name == "grep" and after_git else name
    return runner if runner in _RUN_FLAGS else ""


def _is_run_flag(word: str, runners: frozenset[str]) -> bool:
    """Whether ``word`` is a run flag of one of ``runners``: a listed long flag or a prefix of one, or a bundle.

    ``--pre``, ``--pre=x``, ``--pr``, ack's ``+pag``; a short-flag bundle
    holding a listed letter anywhere in its first or last ``_VERB_TAIL``
    characters (``-nO``, ``-nOrm``).  Each check is bounded, whatever the word.
    """
    for runner in runners:
        longs, letters = _RUN_FLAGS[runner]
        if word.startswith(("--", "+")):
            name = ("--" + word[1:] if word[0] == "+" else word)[:_VERB_TAIL].split("=", 1)[0]
            if len(name) > 2 and any(flag.startswith(name) for flag in longs):
                return True
        elif len(word) > 1 and word[0] == "-" and any(
                letter in word[1:_VERB_TAIL] or letter in word[-_VERB_TAIL:] for letter in letters):
            return True
    return False


def _bundle_runs(word: str, runners: frozenset[str]) -> bool:
    """Whether ``word`` is a short-flag bundle holding a run flag (``-nOrm`` of ``git grep``)."""
    return len(word) > 2 and word[0] == "-" and word[1] != "-" and _is_run_flag(word, runners)


def _fd_exec(word: str) -> bool:
    """Whether ``word`` (unescaped, lowercased) is fd's ``--exec``/``--exec-batch`` or a bundle holding ``-x``/``-X``."""
    if word.startswith("--"):
        return word.split("=", 1)[0] in _FD_EXECS
    return len(word) > 1 and word[0] == "-" and word.find("x", 1) > 0


def _finder_at(words: list[str]) -> int:
    """Where ``words`` (lowercased) name a finder that deletes what it finds, or -1.

    Any word may be the finder (``sudo find``, ``xargs fd``), as any word may
    be a delete verb.  It deletes with find's ``-delete``, or with a delete
    verb (``_runs_delete``, also inside an argv element: ``sh -c "rm -rf {}"``)
    at or after an exec flag of a finder named before it (find's ``-exec``,
    ``-execdir``, ``-ok``, ``-okdir``; fd's ``-x``, ``-X``, ``--exec``,
    ``--exec-batch``, ``--exec=rm``, ``-Hxrm``).  Quotes and escape
    characters a shell removes are removed first (``-del"ete"``).  The answer
    is the first finder's index.  One pass: linear.
    """
    first, finds, fds, running = -1, False, False, False
    for index, word in enumerate(words):
        plain = _unescaped(word)
        kind = _FINDERS.get(_program_name(plain[-_VERB_TAIL:]))
        if kind:
            first = index if first < 0 else first
            finds, fds = finds or kind == "find", fds or kind == "fd"
            continue
        if first < 0:
            continue
        if finds and plain == "-delete":
            return first
        runs = (finds and plain in _FIND_EXECS) or (fds and _fd_exec(plain))
        if (runs or running) and (_runs_delete(plain, runs and not plain.startswith("--"))
                                  or any(_runs_delete(piece) for piece in plain.split())):
            return first
        running = running or runs
    return -1


def _finder_targets(words: list[str]) -> list[str]:
    """The paths a finder whose arguments are ``words`` may search (any position, fail closed).

    ``.`` first: with no path given, find and fd search the working directory
    (``find -delete`` with ``cwd`` ``/``), and it is read even when one is
    given.  Then each word that is not a flag, a flag's value
    (``--search-path=/``, ``--base-directory=/``) and, in a short-flag
    bundle, what follows a ``c`` (fd's ``-C/``, base directory ``/``).
    """
    targets: list[str] = ["."]
    for word in words:
        if not _is_operand_flag(word, False):
            targets.append(word)
            continue
        if value := _flag_value(word):
            targets.append(value)
        if word[1] != "-" and (at := word.find("c", 1)) > 0 and (value := word[at + 1:].removeprefix("=")):
            targets.append(value)
    return targets


def _argv_blocked(
    argv: list[str],
    cwd: tuple[_Place, ...],
    root_cache: dict[str, bool] | None = None,
    relatives: dict[str, _Place] | None = None,
) -> bool:
    """Whether an argv (no shell: no comment, separator or quote) deletes a root.

    Every element is one word and may be the program (``sudo rm``,
    ``xargs rm``, ``echo x ; rm``): a delete verb element (``_runs_delete``,
    a run flag's bundle, ``_bundle_runs``) and a root among the elements
    after it.  A delete a flag runs (``--pre=rm``, ``-Orm``, the value after
    a run flag of a program named before it: ``rg ... --pre rm``) applies to
    the program's operands wherever they stand, so every element after
    ``argv[0]`` is read as one of its operands.  One pass to find the delete
    and one over its operands: linear.
    """
    words = [word.casefold() for word in argv]
    cache = root_cache if root_cache is not None else {}

    def _is_root(target: str) -> bool:
        cached = cache.get(target)
        if cached is None:
            cached = cache[target] = any(
                _is_root_reading(drive, path)
                for drive, path in _against(_normalize_targets(target), cwd, relatives)
            )
        return cached

    # Every finder's name has an ``f``: an argv without one names none (a cheap first check).
    if any("f" in word for word in words) and (finder_at := _finder_at(words)) >= 0 and any(
            _is_root(target) for target in _finder_targets(words[finder_at + 1:])):
        return True
    delete_at, switches, everywhere = -1, False, False
    runners: frozenset[str] = frozenset()
    after_git, after_run_flag = False, False
    for index, word in enumerate(words):
        verb = _runs_delete(word, _bundle_runs(word, runners))
        if verb:
            if delete_at < 0:
                delete_at, switches = index, verb in _SWITCH_VERBS
            if _flag_like(word) or after_run_flag:
                everywhere = True
                break
        after_run_flag = "=" not in word and _is_run_flag(word, runners)
        name = _program_name(word[-_VERB_TAIL:])
        if runner := _runner(name, after_git):
            runners = runners | {runner}
        after_git = after_git or name == "git"
    if delete_at < 0:
        return False
    operands = words[1:] if everywhere else words[delete_at + 1:]
    return any(_is_root(_flag_value(operand) if _is_operand_flag(operand, switches) else operand)
               for operand in operands)


# One token of non-shell code (Python, JavaScript, Ruby...): a string literal (with
# a prefix such as ``r``/``b``/``f``), a dotted name, or a bracket.
_CODE_TOKEN = re.compile(
    r"(?P<str>(?P<prefix>[rbuf]{0,2})(?P<body>\"\"\"[\s\S]*?\"\"\"|'''[\s\S]*?'''"
    r"|\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*'|`(?:[^`\\]|\\.)*`))"
    r"|(?P<name>[a-z_$][\w$]*(?:[ \t]*\.[ \t]*[a-z_$][\w$]*)*)"
    r"|(?P<open>[(\[])|(?P<close>[)\]])"
)
# Calls whose string arguments are a command line or an argv: ``os.system``,
# ``subprocess.run``/``Popen``/``check_output``, ``child_process.exec``/``execSync``/``spawn``...
_EXEC_CALL = re.compile(
    r"system|popen[0-9]?|run|call|check_call|check_output|getoutput|getstatusoutput|exec[a-z_]*|spawn[a-z_]*"
    r"|shell_exec|passthru|proc_open|startfile|create_subprocess_(?:exec|shell)|execa[a-z_]*|eval")
# Calls that delete the path a string argument names (``shutil.rmtree``, ``fs.rmSync``...).
_DELETE_CALLS = frozenset({
    "rmtree", "rmdir", "rmdirsync", "unlink", "unlinksync", "removedirs", "rm", "rmsync", "rimraf",
    "rimrafsync", "remove_tree", "rm_rf", "rm_r", "rm_f", "remove_dir", "remove_entry", "remove_entry_secure",
    "emptydir", "emptydirsync", "deletefile",
})
_CODE_ESCAPES: Mapping[str, str] = MappingProxyType({"n": "\n", "t": "\t", "r": "\r"})
_ESCAPE = re.compile(r"\\(.)", re.DOTALL)
# Deeper brackets than this are read as part of the deepest one.
_MAX_CODE_DEPTH = 64


def _literal(match: re.Match[str]) -> str:
    """The text of a string literal: quotes dropped, escapes (but a raw string's) resolved."""
    body = match.group("body")
    quote = 3 if body[:3] in ('"""', "'''") else 1
    body = body[quote:-quote]
    if "r" in match.group("prefix"):
        return body
    return _ESCAPE.sub(lambda escape: _CODE_ESCAPES.get(escape.group(1), escape.group(1)), body)


def _code_commands(code: str) -> tuple[list[str], list[str], list[str]]:
    """In non-shell code, the strings handed to an exec call and the paths handed to a delete call.

    ``(commands, argvs, targets)``.  Each string literal inside an exec call's
    brackets (``os.system("rm -rf /")``) is a command line, and the literals of
    the outermost exec call joined with spaces are an argv
    (``subprocess.run(["rm", "-rf", "/"])``, ``spawn('rm', ['-rf', '/'])``).  A
    literal directly inside a delete call's brackets (``shutil.rmtree("/")``) is
    a path it deletes.  One linear pass: every literal is kept at most twice.
    """
    commands: list[str] = []
    argvs: list[str] = []
    targets: list[str] = []
    frames: list[str] = []  # per open bracket: "exec", "delete" or ""
    overflow = 0  # brackets open past _MAX_CODE_DEPTH
    exec_starts: list[int] = []  # where each open exec call's literals start in ``collected``
    collected: list[str] = []
    name, name_end = "", -1
    for match in _CODE_TOKEN.finditer(code):
        kind = match.lastgroup
        if kind == "name":
            name, name_end = match.group(), match.end()
            continue
        if kind == "open":
            frame = ""
            if match.group() == "(" and name and not code[name_end:match.start()].strip():
                dotted = "".join(name.split())
                callee = dotted.rsplit(".", 1)[-1]
                frame = ("exec" if _EXEC_CALL.fullmatch(callee)
                         else "delete" if callee in _DELETE_CALLS or dotted.endswith("os.remove") else "")
            if len(frames) < _MAX_CODE_DEPTH:
                frames.append(frame)
                if frame == "exec":
                    exec_starts.append(len(collected))
            else:
                overflow += 1
        elif kind == "close":
            if overflow:
                overflow -= 1
            elif frames and frames.pop() == "exec":
                start = exec_starts.pop()
                if not exec_starts:
                    argvs.append(" ".join(collected[start:]))
                    collected.clear()
        else:
            text = _literal(match)
            if exec_starts:
                collected.append(text)
                commands.append(text)
            if frames and frames[-1] == "delete":
                targets.append(text)
        name = ""
    if exec_starts:
        argvs.append(" ".join(collected))  # an exec call left open
    return commands, argvs, targets


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


def _runs_delete(word: str, glued: bool = False) -> str:
    """The delete verb ``word`` may run as a command, or ``""``.

    ``rm``, and any piece of the word between ``=``, ``:``, ``+`` and ``!``
    (``X=rm``, ``alias x=rm``, ``$r=Remove-Item``, ``--pager=rm``,
    ``--pretty=format:rm``, ``+rm``).  ``glued`` (a quote glued to the word,
    ``-O'rm -rf /'``, or a run flag's bundle, ``-nOrm``) also reads the value
    of any short flag of a bundle (``-orm``, ``-norm``).  Only the last
    ``_VERB_TAIL`` characters are read: they decide every piece (past them a
    component is longer than any verb, and only an ``.rmtree`` suffix can make
    it one), so a word glued from many quotes is read in linear time.
    """
    tail = word[-_VERB_TAIL:]
    name = _program_name(tail)
    if tail and _is_delete_verb(name):
        return name
    if "=" in tail or ":" in tail or "+" in tail or "!" in tail:
        for piece in _COMMAND_PIECES.split(tail):
            if piece:
                pname = _program_name(piece)
                if _is_delete_verb(pname):
                    return pname
    if glued and len(word) > 2 and word[0] == "-" and word[1] != "-":
        # Any flag of a bundle may take the rest as its value (``-nOrm``: ``-O rm``; ``-w5Orm``):
        # every suffix of the tail past the flag's ``-x``.  A suffix's name is a suffix of the
        # last one's name (``_program_name``), so the longest delete verb it ends in is the one.
        bname = name if len(word) > _VERB_TAIL else _program_name(tail[2:])
        if bname.endswith(".rmtree"):
            return bname
        if bname.endswith(_SUFFIX_VERBS):
            return next(verb for verb in _SUFFIX_VERBS if bname.endswith(verb))
    return ""


def _unescaped(word: str) -> str:
    """``word`` without the quotes and escape characters a shell removes (``--o\\p`` is ``--op``)."""
    return word.translate(_UNESCAPED)


def _flag_like(word: str) -> bool:
    """Whether ``word``, past any leading quote or escape character, starts as a flag (``-``, ``+``).

    Only its first ``_VERB_TAIL`` characters are read (a word glued many times is
    read once per piece): a longer word that starts with that many escape
    characters is a flag, fail closed.
    """
    if not word or word[0] not in _FLAG_OR_ESCAPE:
        return False
    head = word[:_VERB_TAIL].lstrip(_ESCAPE_MARKS)
    return head[:1] in _FLAG_STARTS or (not head and len(word) > _VERB_TAIL)


def _escaped_flag(word: str) -> bool:
    """Whether ``word`` is a flag word holding a quote or an escape character (``--o\\p``, ``\\--pre``).

    The shell removes them, so it may be any flag (``--op``, ``--pre``): it is
    read as a run flag, fail closed (``_Command.runs``).
    """
    return _flag_like(word) and any(mark in word for mark in _ESCAPE_MARKS)


def _is_flag(token: str) -> bool:
    """``-rf``, ``--force``, ``-Recurse`` or a Windows switch such as ``/s``; not ``/``, ``/.`` or ``//``."""
    return (len(token) > 1 and token[0] == "-") or (
        len(token) == 2 and token[0] == "/" and token[1].isalpha())


def _is_operand_flag(token: str, switches: bool) -> bool:
    """A flag of a delete verb; ``/x`` is a switch only for cmd's verbs (``rm /c`` names a path)."""
    return (len(token) > 1 and token[0] == "-") or (
        switches and len(token) == 2 and token[0] == "/" and token[1].isalpha())


def _flag_value(flag: str) -> str:
    """The value a flag carries after its first ``=`` or ``:`` (``-Path:C:\\``), or ``""``."""
    for mark in ("=", ":"):
        if mark in flag:
            return flag.split(mark, 1)[1]
    return ""


def _program_name(program: str) -> str:
    if "\\" in program:
        program = program.replace("\\", "/")
    if "/" in program:
        program = program.rsplit("/", 1)[-1]
    name = program.lower()
    return name[:-4] if name.endswith(".exe") else name


def _file_name(path: str) -> str:
    """The last component of a path or URL, without a query or fragment."""
    path = path.split("?", 1)[0].split("#", 1)[0].replace("\\", "/").rstrip("/")
    return path.rsplit("/", 1)[-1]


def _ends_in_output_flag(word: str) -> bool:
    """A bundle of short flags whose last one is ``-o`` (curl's ``-fsSLo FILE``)."""
    return len(word) > 2 and word[0] == "-" and word[-1] == "o" and word[1:].isalpha()


def _fold(match: re.Match[str]) -> str:
    return "$" + match.group("name")


def _fold_to_word(match: re.Match[str]) -> str:
    """The WORD of ``${X:-WORD}``, ``${X:=WORD}``, ``${X:+WORD}`` (and without ``:``); else the variable."""
    if match.group("op") in _WORD_OPERATORS:
        return match.group("word")
    return _fold(match)


def _fold_to_bash_word(match: re.Match[str]) -> str:
    """``_fold_to_word`` as bash reads WORD: substitutions empty, ``;|&`` literal, a newline a blank."""
    if match.group("op") in _WORD_OPERATORS:
        return _SUBSTITUTION.sub("", match.group("word")).translate(_LITERAL_SEPARATORS)
    return _fold(match)


_Fold = Callable[[re.Match[str]], str]
# Which expansions read as their WORD: every one as its variable, every one
# as its WORD, and the innermost as its variable with every enclosing one as
# its WORD (``${X:-${HOME:-a}}`` is ``$home`` when X is unset).  Each pass
# folds the innermost expansions; passes repeat until nothing changes, at
# most _MAX_FOLDS.
_FOLDINGS: tuple[tuple[re.Pattern[str], _Fold, _Fold], ...] = (
    (_PARAMETER_EXPANSION, _fold, _fold), (_PARAMETER_EXPANSION, _fold_to_word, _fold_to_word),
    (_PARAMETER_EXPANSION, _fold, _fold_to_word),
    (_BASH_EXPANSION, _fold, _fold), (_BASH_EXPANSION, _fold_to_bash_word, _fold_to_bash_word),
    (_BASH_EXPANSION, _fold, _fold_to_bash_word),
)


def _fold_passes(text: str, pattern: re.Pattern[str], first: _Fold, later: _Fold) -> str:
    fold = first
    for _ in range(_MAX_FOLDS):
        folded = pattern.sub(fold, text)
        if folded == text:
            break
        text, fold = folded, later
    return text


def _fold_variables(text: str) -> list[str]:
    """Every reading of ``text`` with its parameter expansions folded, without repeats.

    ``${HOME}`` is ``$home``, and ``${OUT:?}``, ``${1%/}`` and ``${arr[0]}``
    are ``$out``, ``$1`` and ``$arr``.  ``${X:-/}`` (and ``:=``, ``:+``,
    ``-``, ``=``, ``+``) is read both as ``$x`` and as ``/``; the WORD of
    ``:?``, ``%``, ``#``, ``/``, ``^`` and ``,`` is dropped.  An expansion
    whose WORD holds a separator is read both unfolded and as bash reads it
    (``_BASH_EXPANSION``).  One that is still unfolded after _MAX_FOLDS
    passes (or whose WORD neither reads) is also read as ``/``.  At most
    seven readings, each a bounded number of linear substitutions.
    """
    text = _BRACED_VARIABLE.sub(r"$\1", text)
    if "${" not in text:
        return [text]
    readings: list[str] = []
    for pattern, first, later in _FOLDINGS:
        reading = _fold_passes(text, pattern, first, later)
        if reading not in readings:
            readings.append(reading)
        if later is _fold_to_bash_word and first is later and _UNFOLDED.search(reading):
            readings.append(_UNFOLDED.sub(" / ", reading))  # bash's WORD reading, still unfolded
    return readings


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

    __slots__ = ("after_download", "after_git", "candidate_at", "carry", "comment_at", "comment_parens",
                 "delete_at", "downloads", "flag_delete", "glue_head", "glue_len", "glue_tail", "gluing",
                 "has_f", "interpreter_at", "parens", "piped", "prefixed",
                 "program_at", "quote", "quote_from", "redirect_target", "runners", "runs", "saves_urls",
                 "settled", "switches", "unsettled", "words")

    def __init__(self, piped: bool = False, after_download: bool = False) -> None:
        self.words: list[str] = []
        self.gluing: list[str] | None = None  # buffered pieces for words[-1] while gluing (O(N) join)
        self.glue_head = ""
        self.glue_tail = ""
        self.glue_len = 0
        self.has_f = False  # whether any word in this command contains ``f`` (every finder name does)
        self.piped = piped  # it reads a pipe
        self.after_download = after_download  # a download ran earlier on its line
        self.settled = False  # its program is known
        self.prefixed = False  # a ``sudo``/``env``-style prefix stands before its program
        self.program_at = -1
        self.interpreter_at = -1
        self.delete_at = -1
        self.switches = False
        self.flag_delete = False  # its delete is a flag's value (``--pre=rm``): every word is an operand
        self.downloads = False
        self.saves_urls = False
        self.quote = ""  # the open quote character, if any
        self.quote_from = 0
        self.comment_at = -1  # where an unquoted ``#`` word starts a shell comment
        self.parens = 0  # unescaped, unquoted ``(`` / ``@(`` opened after its delete verb, not yet closed
        self.comment_parens = 0  # ``(`` opened inside its comment, not yet closed there
        self.carry = False  # its words are also operands of a delete before a held separator
        self.runs = False  # it runs a command named by its later words (``bash -c``, ``find -exec``)
        self.candidate_at = -1  # the last word read where a command word may stand
        self.redirect_target = False  # the next word is the target of a redirection
        self.runners: frozenset[str] = frozenset()  # the ``_RUN_FLAGS`` programs named so far
        self.after_git = False  # ``git`` is among its words (``git grep``)
        self.unsettled = False  # its last word, an operand of a delete, grew by gluing: read it when it ends

    def reopen(self) -> None:
        """A ``(``, ``{`` or a new command: the next word is not a redirection's target."""
        self.redirect_target = False

    def flush_glued(self) -> None:
        """Materialise buffered glued pieces into ``words[-1]`` once the word ends."""
        if self.gluing is not None:
            self.words[-1] = "".join(self.gluing)
            self.gluing = None


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

    Owned by a single ``blocks`` call and dropped with it.  Each method
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
        self.word_readings: dict[str, list[tuple[str, str]]] = {}  # all piece readings per word
        self.relatives: dict[str, _Place] = {}  # _place per relative reading
        self.roots: dict[str, bool] = {}  # per word, for the current cwd: cleared when it changes
        self.shell = True
        self.line_download = False
        self.previous = ""
        self.block_comments = False  # the text has a PowerShell ``<#`` block comment
        self.finders = False  # the text may name a finder: every word is built from its characters

    def run(self, text: str, *, shell: bool, ticks: bool) -> bool:
        self.shell, self.names = shell, set()
        self.block_comments = "<#" in text
        self.finders = "f" in text  # every ``_FINDERS`` name has an ``f``
        self._move(self.cwd0)
        self.line_download, self.previous = False, ""
        stack = [_Context("top")]
        joins = False  # the last token was a pipe or ``&&``: a newline continues the command
        glue = -1  # where a closing quote or substitution ended: a word there continues it
        word_end = -1  # where the last word token ended
        quote_glued = False  # the open quote continues the word before it
        for match in _TOKEN.finditer(text):
            kind, token, context = match.lastgroup, match.group(), stack[-1]
            start = match.start()
            before = text[start - 1] if start else ""
            if kind == "word":
                comment_ok = not before or before in _COMMENT_AFTER
                if "{" in token:
                    if self._words(context, token, start == glue, comment_ok):
                        return True
                elif self._word(context, token, start == glue, comment_ok):
                    return True
                word_end = match.end()
            elif kind == "nl":
                continued = joins or before in ("\\", "^")
                if token[0] == "`":
                    if ticks and context.kind == "tick":
                        if self._close(stack):  # the backtick closes a substitution
                            return True
                        context = stack[-1]
                    else:
                        continued = True  # PowerShell's line continuation
                command = context.command
                if continued and command.comment_at >= 0:
                    # A newline ends a comment whatever stands before it: after a
                    # pipe or ``&&`` the command goes on without the comment...
                    if joins:
                        command.flush_glued()
                        del command.words[command.comment_at:]
                        command.comment_at, command.comment_parens = -1, 0
                        continue
                    # ...and a ``\``, ``^`` or backtick in it continues nothing in bash or
                    # PowerShell but may in cmd (no ``#`` comment): read both ways.
                    if self._end(context, carry=self.shell and (command.delete_at >= 0 or command.carry)):
                        return True
                    continue
                if continued:
                    continue
                if self._held(context.command, _trailing_comma(text, start)):
                    # Read both ways: the next line is a command of its own, and
                    # its words are operands of the delete (``_end`` with carry).
                    if self._end(context, carry=True):
                        return True
                    continue
                if self._end(context):
                    return True
                self.line_download = False
            elif kind == "pipe" or kind == "end":
                command = context.command
                if self._end(context, piped=kind == "pipe", carry=self._held(command)):
                    return True
            elif kind == "quote":
                command = context.command
                if command.comment_at >= 0 and not command.switches:
                    continue  # a quote inside a comment
                opening = not command.quote
                quoted = bool(command.quote) and len(command.words) > command.quote_from
                if self._quote(command, token[-1]):  # ``$'`` and ``$"`` quote as ``'`` and ``"``
                    return True
                if opening:
                    # ``r"m"``: a quote right after a word continues that word.
                    quote_glued = bool(command.quote and command.words) and start in (glue, word_end)
                    if quote_glued:
                        glue = match.end()
                        command.quote_from -= 1  # the quoted words start with the word it continues
                elif (quoted or quote_glued) and not command.quote:  # a closing quote after a quoted word
                    glue = match.end()
            elif kind == "tick":
                if not ticks:
                    # An escape character.  Next to a flag word (``--o`p=x``, ``-`-pre``,
                    # ``` `--pre ```) it may assemble any flag: a run flag (``_escaped_flag``).
                    command = context.command
                    if ((start in (word_end, glue) and command.words and _flag_like(
                            command.glue_head if command.gluing is not None else command.words[-1]))
                            or _flag_like(text[match.end():match.end() + _VERB_TAIL])):
                        command.runs = True
                elif context.kind == "tick":
                    if self._close(stack):
                        return True
                    glue = match.end()
                elif len(stack) <= _MAX_DEPTH:
                    stack.append(self._open("tick", context))
            elif kind == "sub" and len(stack) <= _MAX_DEPTH:
                stack.append(self._open("sub", context))
            elif kind == "block":
                command = context.command
                if token == "{" and not command.quote and command.comment_at < 0:
                    command.reopen()  # ``{ rm -rf /; }``, ``ForEach-Object { Remove-Item ... }``
            elif not self._paren(context.command, kind == "close",
                                 before == "^" or (before == "`" and not ticks)):
                # Escaped, quoted or commented out: not a parenthesis of the text.  A
                # quoted one is text of its word, so a quoted operand joined at its
                # closing quote (``_quote``) keeps it: ``"C:\\Program Files (x86)"``.
                command = context.command
                if self.shell and command.quote and command.comment_at < 0:
                    command.flush_glued()
                    if command.words and start == word_end and len(command.words[-1]) < _PAREN_GLUE:
                        command.words[-1] += token
                        command.unsettled = command.unsettled or context.operands
                    else:
                        if self._settle(command):
                            return True
                        command.words.append(token)
                    glue = word_end = match.end()
            elif context.kind == "sub":
                # A plain parenthesis in a substitution, or past _MAX_DEPTH a
                # substitution read as part of the deepest one: only its closing
                # parenthesis is tracked.
                if kind != "close":
                    context.parens += 1
                elif context.parens:
                    context.parens -= 1
                elif self._close(stack):
                    return True
                else:
                    glue = match.end()
            if kind == "nl" or stack[-1].command.comment_at < 0:
                joins = kind == "pipe" or token == "&&"  # a comment after a pipe keeps it open
        while len(stack) > 1:
            if self._close(stack):
                return True
        return self._end(stack[0])

    def _held(self, command: _Command, comma: bool = False) -> bool:
        """Whether a separator or newline here may not end a delete's operands.

        It may not inside a quote, an unescaped ``(`` / ``@(`` the delete left
        open, an open ``(`` in its comment (the comment may not be one), or
        after a trailing ``,`` (PowerShell continues the list on the next line;
        ``comma`` is passed at a newline only).  A held separator still ends the
        command: the text after it is read both as a command of its own and as
        more operands of the delete (``_Command.carry``), so a wrong guess at
        quoting can add a BLOCK but never hide one.
        """
        if not self.shell or (command.delete_at < 0 and not command.carry):
            return False
        return bool(command.quote or command.parens or command.comment_parens
                    or (comma and command.comment_at < 0))

    @staticmethod
    def _paren(command: _Command, close: bool, escaped: bool) -> bool:
        """Count one ``(`` or ``)`` of ``command``; False when it is escaped, quoted or in a comment."""
        if escaped or command.quote:
            return False
        if command.comment_at >= 0:
            if not close:
                command.comment_parens += 1
            elif command.comment_parens:
                command.comment_parens -= 1
            return False
        command.reopen()  # ``(rm -rf /)``, PowerShell ``(Remove-Item ...)``, ``case a) rm ...``
        if not close:
            if command.delete_at >= 0 or command.carry:
                command.parens += 1
        elif command.parens:
            command.parens -= 1
        return True

    def _move(self, cwd: tuple[_Place, ...]) -> None:
        self.cwd = cwd
        self.roots.clear()

    def _root(self, word: str) -> bool:
        """Whether ``word``, or a piece of it another shell reads (``_operand_pieces``), is a root."""
        cached = self.roots.get(word)
        if cached is None:
            all_readings = self.word_readings.get(word)
            if all_readings is None:
                pieces = _operand_pieces(word)
                if len(pieces) == 1:
                    all_readings = self._readings(pieces[0])
                else:
                    all_readings = []
                    for piece in pieces:
                        all_readings.extend(self._readings(piece))
                self.word_readings[word] = all_readings
            cached = self.roots[word] = _any_root_against(all_readings, self.cwd, self.relatives)
        return cached

    def _readings(self, word: str) -> list[tuple[str, str]]:
        readings = self.readings.get(word)
        if readings is None:
            readings = self.readings[word] = _normalize_targets(word)
        return readings

    def _words(self, context: _Context, token: str, glued: bool, comment_ok: bool) -> bool:
        """One word token, brace-expanded; every expanded word keeps what it is glued to.

        A brace expansion too large to expand is a BLOCK where it is a delete's
        operand (its words may build a root its pieces do not show).
        """
        if "{" not in token:
            return self._word(context, token, glued, comment_ok)
        words, expanded = _brace_words(token)
        command = context.command
        if (not expanded and self.shell and command.comment_at < 0
                and (command.delete_at >= 0 or command.carry or context.operands)):
            return True
        if expanded and glued and command.words:
            command.flush_glued()
            prefix = command.words[-1]
        else:
            prefix = ""
        # A ``#`` that brace expansion yields is an argument (``{#,}`` is ``#``), not a comment.
        comment_ok = comment_ok and token[0] == "#"
        for index, word in enumerate(words):
            if self._word(context, word if index == 0 else prefix + word, glued and index == 0,
                          comment_ok and index == 0):
                return True
        return False

    def _word(self, context: _Context, word: str, glued: bool, comment_ok: bool = False) -> bool:
        name = _program_name(word) if ("/" in word or "\\" in word or word.endswith(".exe")) else word
        if (word in _BLOCK_TOKENS or name in _BLOCK_TOKENS or word.startswith("mkfs")
                or (name.startswith("mkfs") and _MKFS.fullmatch(name))):
            return True
        if self.previous == "format" and _is_drive_root(word):
            return True
        self.previous = word
        command = context.command
        if self.finders and not command.has_f and "f" in word:
            command.has_f = True
        if name in _DOWNLOADERS:
            command.downloads = context.downloaded = self.downloaded = self.line_download = True
            command.saves_urls = command.saves_urls or name in _URL_SAVERS
        if name in _EVAL_TOKENS:
            self.evaluated = True
        if word == "--remote-name" or word == "--remote-name-all":
            command.saves_urls = True
        if glued and command.words:
            # ``"$HOME"/``, ``$(pwd)/c``: one shell word with what it follows.
            piece = word
            if command.gluing is None:
                base = command.words[-1]
                command.gluing = [base, piece]
                command.glue_len = len(base) + len(piece)
                command.glue_head = (base + piece)[:_VERB_TAIL + 1] if len(base) <= _VERB_TAIL else base[:_VERB_TAIL + 1]
                command.glue_tail = (base[-_VERB_TAIL:] + piece)[-_VERB_TAIL:]
            else:
                command.gluing.append(piece)
                command.glue_len += len(piece)
                if len(command.glue_head) <= _VERB_TAIL:
                    command.glue_head = (command.glue_head + piece)[:_VERB_TAIL + 1]
                command.glue_tail = (command.glue_tail + piece)[-_VERB_TAIL:]
            at = len(command.words) - 1
            tail = command.glue_tail
            # ``"g"it grep -nOrm``; a name is short, so its tail is enough (and stays linear).
            tail_name = _program_name(tail) if ("/" in tail or "\\" in tail or tail.endswith(".exe")) else tail
            self._note_program(command, tail_name)
            is_flag = _flag_like(command.glue_head)
            if is_flag:
                # A flag word the shell assembles from quoted pieces (``-""O'x'``, ``-n"O"'x'``,
                # ``--"pager"=x``) may be any flag: a run flag, whose value may be a command.
                command.runs = True
            if (self.shell and command.comment_at < 0 and command.delete_at < 0
                    and (command.candidate_at == at or command.runs)):
                verb = ""
                if tail and _is_delete_verb(tail_name):
                    verb = tail_name
                elif "=" in tail or ":" in tail or "+" in tail or "!" in tail:
                    verb = _runs_delete(tail, False)
                if not verb and command.glue_len > 2 and command.glue_head[0] == "-" and command.glue_head[1] != "-":
                    bname = tail_name if command.glue_len > _VERB_TAIL else _program_name(tail[2:])
                    if bname.endswith(".rmtree"):
                        verb = bname
                    elif bname.endswith(_SUFFIX_VERBS):
                        verb = next(v for v in _SUFFIX_VERBS if bname.endswith(v))
                if not verb:
                    if _is_delete_verb(name):
                        verb = name
                    elif "=" in piece or ":" in piece or "+" in piece or "!" in piece:
                        verb = _runs_delete(piece)
                if not verb and ("\\" in tail or "^" in tail):
                    plain = _unescaped(tail)
                    if plain != tail:
                        verb = _runs_delete(plain, True)
                if verb:
                    # ``"r"m -rf /``, ``r"m" -rf /``, ``X='rm -rf /'``, ``--pager='rm -rf /'``, ``-O'rm -rf /'``;
                    # the word as the shell passes it (``--pre=r\m``), and the piece alone, as the
                    # pre-lane scan read it (PowerShell's ``-`O'rd ...'`` is ``-Ord ...``, read here
                    # as ``-``, then ``o`` with ``rd`` glued to it).
                    command.delete_at = at
                    command.switches = verb in _SWITCH_VERBS
                    command.flag_delete = is_flag
                    return False
            if self.shell and context.operands and command.comment_at < 0:
                # An operand of a delete (``rm -rf $(echo a"/")``): read whole once it ends
                # (``_settle``), not at every quote glued to it, which would be quadratic.
                command.unsettled = True
            return False
        if self._settle(command):
            return True
        # ``#`` starts a comment only as a word of its own: ``a#b`` and ``"x"#c`` are one word.
        # A carried command has none: its quote may be read either way, and a comment only hides.
        # PowerShell's block comment ends at ``#>`` and its code goes on: ``#>`` starts none.
        if (self.shell and command.comment_at < 0 and not command.quote and not command.carry
                and word[0] == "#" and comment_ok and not (self.block_comments and word.startswith("#>"))):
            command.comment_at = len(command.words)
        if command.comment_at >= 0:
            # A comment: not a delete, not an operand (but cmd's ``del``/``rd`` read
            # it: cmd has no ``#`` comment), and not a program or command word.
            command.words.append(word)
            return False
        if command.delete_at < 0:
            # Every word may be the command word: ``valgrind rm``, ``grep rm -rf /``.
            command.candidate_at = len(command.words)
            if not self.shell:
                verb = ""
            elif (len(word) <= _VERB_TAIL and not (command.runners and _bundle_runs(word, command.runners))
                  and "=" not in word and ":" not in word and "+" not in word and "!" not in word):
                verb = name if _is_delete_verb(name) else ""
            else:
                verb = _runs_delete(word, bool(command.runners) and _bundle_runs(word, command.runners))
            if verb:
                # ``rm``, an assignment's or a flag's value (``X=rm``, ``alias x=rm``, ``--pre=rm``),
                # PowerShell's ``$r=Remove-Item``, a run flag's bundle (``git grep -nOrm``), the
                # value after a run flag (``rg --pre rm``).
                command.delete_at = len(command.words)
                command.switches = verb in _SWITCH_VERBS
                previous = command.words[-1] if command.words else ""
                command.flag_delete = _flag_like(word) or (
                    bool(command.runners) and "=" not in previous and _is_run_flag(previous, command.runners))
            elif self.shell and context.operands and not _is_flag(word) and self._root(word):
                return True
        elif self.shell and context.operands and not _is_flag(word) and self._root(word):
            return True
        self._add(command, word, name)
        self._advance(command, word, name)
        return False

    def _advance(self, command: _Command, word: str, name: str) -> None:
        """What ``word`` tells about the words after it.

        An interpreter (``bash -c``, ``eval``), an exec word (``-exec``, ``run``)
        or a flag word holding a quote or an escape character (``_escaped_flag``)
        makes every later word glued to a quote a possible command
        (``_Command.runs``).  A program with a run flag (``_RUN_FLAGS``; ``grep``
        after ``git``) makes a later bundle holding one (``_bundle_runs``) and
        the word after one possible commands.  A redirection and its target are
        tracked for ``_add``; ``>|`` (bash's noclobber redirection, cmd's ``>``
        and a pipe) is read fail-closed: past its target the next word may be
        the program again.
        """
        if name in _EXEC_WORDS or (word[0] in _FLAG_OR_ESCAPE and _escaped_flag(word)):
            command.runs = True
        if name in _RUN_FLAGS or name == "git" or (command.after_git and name == "grep"):
            self._note_program(command, name)
        if ">|" in word and _NOCLOBBER.match(word):
            command.redirect_target = _NOCLOBBER.fullmatch(word) is not None
            return
        if command.redirect_target:
            command.redirect_target = False
            return
        if (">" in word or "<" in word) and (redirect := _REDIRECT.match(word)):
            command.redirect_target = redirect.end() == len(word)  # ``> out``, not ``>out``
            return
        if name in _PIPE_SHELLS or (name and name[-1] in "0123456789." and name.rstrip("0123456789.") in _PIPE_SHELLS):
            command.runs = True

    @staticmethod
    def _note_program(command: _Command, name: str) -> None:
        """Note the ``_RUN_FLAGS`` program a word named ``name`` is (``_runner``)."""
        if (runner := _runner(name, command.after_git)) and runner not in command.runners:
            command.runners = command.runners | {runner}
        command.after_git = command.after_git or name == "git"

    @staticmethod
    def _add(command: _Command, word: str, name: str) -> None:
        """Append ``word``, tracking the program it may be (``_command_programs`` as a stream)."""
        index = len(command.words)
        command.words.append(word)
        if (command.settled or _is_flag(word) or "=" in word or command.redirect_target
                or ((">" in word or "<" in word) and _REDIRECT.match(word))):
            return  # ``2>/dev/null sh``, ``> out sh``: the redirection is not the program
        if name and name[-1] in "0123456789.":
            unversioned = name.rstrip("0123456789.")
            interpreter = unversioned if unversioned in _PIPE_SHELLS else name
        else:
            interpreter = name
        if interpreter in _KEYWORDS and command.program_at < 0:
            return  # ``then cd /``: the command word comes after it
        if interpreter in _PIPE_PREFIXES:
            command.prefixed = True
            return
        if command.program_at < 0:
            command.program_at = index
        if interpreter in _PIPE_SHELLS and command.interpreter_at < 0:
            command.interpreter_at = index
        command.settled = not command.prefixed

    def _quote(self, command: _Command, mark: str) -> bool:
        """At a closing quote, a delete's quoted operand with spaces is checked whole.

        A quote that holds the delete itself (``"Remove-Item -Recurse
        -Path:'C:\\Program Files'"``, ``sh -c "rm -rf 'C:\\Program Files'"``)
        has its words after the delete read as quoted operands too
        (``_spaced_root``): an inner quote is text of the outer one.
        """
        if not command.quote:
            command.quote, command.quote_from = mark, len(command.words)
            return False
        if mark != command.quote:
            return False
        command.quote = ""
        start = command.quote_from
        if not self.shell or len(command.words) - start < 2:
            return False
        command.flush_glued()
        if start <= command.delete_at and self._spaced_root(command.words[command.delete_at + 1:], command.switches):
            return True
        if 0 <= command.delete_at < start or command.carry:
            joined = " ".join(command.words[start:])
            if _is_operand_flag(joined, command.switches):  # ``-Path:"C:\\Program Files"``
                joined = _flag_value(joined)
            return bool(joined) and self._root(joined)
        return False

    def _spaced_root(self, words: list[str], switches: bool) -> bool:
        """Whether quoted ``words`` after a delete in their quote, read as one operand with spaces, name a root.

        The whole quoted text (``"C:\\Program Files"``, ``-Path:"C:\\Program Files"``),
        and each run of words between flags, a flag's value starting it
        (``"Remove-Item -Recurse -Path:'C:\\Program Files'"``: the inner quote is
        text of the outer one).  Every word is joined at most twice.
        """
        if len(words) < 2:
            return False
        joined = " ".join(words)
        if _is_operand_flag(joined, switches):
            joined = _flag_value(joined)
        if joined and self._root(joined):
            return True
        run: list[str] = []
        for word in (*words, "--"):  # ``--``: a flag that ends the last run
            if not _is_operand_flag(word, switches):
                run.append(word)
                continue
            if len(run) > 1 and self._root(" ".join(run)):
                return True
            value = _flag_value(word)
            run = [value] if value else []
        return False

    def _open(self, kind: str, parent: _Context) -> _Context:
        command = parent.command
        return _Context(kind, feeds=command.interpreter_at >= 0,
                        at_start=self.shell and not command.words,
                        operands=self.shell and command.comment_at < 0 and (
                            parent.operands or command.delete_at >= 0))

    def _settle(self, command: _Command) -> bool:
        """Whether the last word, an operand of a delete grown by gluing (``unsettled``), is a root."""
        command.flush_glued()
        if not command.unsettled:
            return False
        command.unsettled = False
        return self._root(command.words[-1])

    def _close(self, stack: list[_Context]) -> bool:
        child = stack.pop()
        if self._end(child):
            return True
        if child.downloaded and (child.feeds or child.at_start):
            return True
        parent = stack[-1]
        command = parent.command
        parent.downloaded = parent.downloaded or child.downloaded
        if self._settle(command):
            return True
        self._add(command, "$()", "$()")  # the substitution stands as one word
        return False

    def _end(self, context: _Context, *, piped: bool = False, carry: bool = False) -> bool:
        """The command in ``context`` ends; the next one reads a pipe when ``piped``.

        With ``carry`` (a held separator, ``_held``) the next command keeps the
        open quote and parentheses and its words are also operands of this
        delete, with its cmd switches.
        """
        command = context.command
        if (command.gluing is not None or command.unsettled) and self._settle(command):
            return True
        if command.words:
            if self._ended(command):
                return True
            # ``sh -c "$(cat F)"`` after F was downloaded: the substitution's text is the download.
            if context.kind != "top" and self.names and any(
                    _file_name(word) in self.names for word in command.words):
                context.downloaded = True
        quote, parens = (command.quote, command.parens) if carry else ("", 0)
        switches = carry and command.switches
        if command.words or piped != command.piped:
            context.command = _Command(piped, piped and self.line_download)
        else:
            # An empty command is reused: newline floods allocate nothing.
            command.reopen()
            command.comment_parens = 0
        following = context.command
        following.quote, following.quote_from, following.parens = quote, 0, parens
        following.carry, following.switches = carry, switches
        self.previous = ""
        return False

    def _ended(self, command: _Command) -> bool:
        words = command.words
        if command.piped and command.interpreter_at >= 0 and command.after_download:
            return True  # ``curl x | sh``
        if self.shell and (command.delete_at >= 0 or command.carry):
            # The operands end at a comment; cmd (``del``, ``rd``) has no ``#`` comment.
            # A carried command's words are all operands of the delete before it, and a
            # flag's delete (``rg -e x / --pre=rm``) reads every word after the program.
            end = command.comment_at if command.comment_at >= 0 and not command.switches else len(words)
            first = command.delete_at + 1
            if command.carry:
                first = 0
            elif command.flag_delete:
                first = command.program_at + 1 if 0 <= command.program_at < command.delete_at else 0
            operands = words if first == 0 and end == len(words) else words[first:end]
            if self._names_a_root(operands, command.switches):
                return True
        if self.shell and self.finders and command.has_f:
            # ``find / -delete``, ``find / -exec rm {} +``, ``fd . / -x rm``: a finder deletes
            # under every path it searches, wherever the path stands (``_finder_at``), and
            # under the cd-tracked working directory with none (``cd / && find -delete``).
            end = command.comment_at if command.comment_at >= 0 else len(words)
            finder_at = _finder_at(words[:end])
            if finder_at >= 0 and any(self._root(target) for target in _finder_targets(words[finder_at + 1:end])):
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

    def _names_a_root(self, operands: list[str], switches: bool) -> bool:
        """Whether a delete's ``operands`` (or a flag's value) name a root."""
        roots = self.roots
        for word in operands:
            if not ((len(word) > 1 and word[0] == "-")
                    or (switches and len(word) == 2 and word[0] == "/" and word[1].isalpha())):
                cached = roots.get(word)
                if cached is None:
                    cached = self._root(word)
                if cached:
                    return True
            elif (value := _flag_value(word)) and self._root(value):
                return True  # ``-Path:"C:\"``, ``-LiteralPath:C:\``, ``--dir=/``
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


def _operand_pieces(word: str) -> list[str]:
    """``word`` and the pieces of it other readings make: ``C:\\`` of ``C:\\)``, ``/`` of ``/>x``.

    PowerShell and cmd read ``C:\\)`` as ``C:\\`` and a ``)`` (bash as an escaped
    ``)``), and every shell ends a word at a redirection (``/>x``, ``/<x``).
    """
    pieces = [word]
    if "\\" in word and ("(" in word or ")" in word):
        pieces.extend(piece for piece in _ESCAPED_PAREN.split(word) if piece)
    if _REDIRECT_MARK.search(word, 1):
        piece = _REDIRECT_MARK.split(word, 1)[0]
        if piece:
            pieces.append(piece)
    if word.startswith("#>") and len(word) > 2:
        pieces.append(word[2:])  # PowerShell's code after a block comment's end: ``#>C:\``
    return pieces


def _trailing_comma(text: str, end: int) -> bool:
    """Whether the line ending at ``end`` ends in ``,`` (spaces and tabs after it allowed).

    Each call walks back over the blanks before one newline only, so the calls
    for all the newlines of a text are linear in it together.
    """
    index = end - 1
    while index >= 0 and text[index] in " \t\r":
        index -= 1
    return index >= 0 and text[index] == ","


def _blocked(parts: list[tuple[str, bool, bool]], cwd: str) -> bool:
    """Whether the command texts ``parts`` delete a root, download and run, or hold a BLOCK token or text."""
    scan = _Scan(cwd)
    texts = [(reading, shell, ticks) for text, shell, ticks in parts for reading in _fold_variables(text)]
    every = _SPACES.sub(" ", "\n".join(text for text, _shell, _ticks in texts))
    if any(marker in every for marker in _BLOCK_TEXT):
        return True
    if any(scan.run(text, shell=shell, ticks=ticks) for text, shell, ticks in texts):
        return True
    for text, shell, _ticks in parts:
        if not shell:
            # Other code: the command lines it hands to an exec call are shell text,
            # each read in every folded reading as shell text is; an argv joined
            # from several literals has no shell and is read unfolded.
            commands, argvs, targets = _code_commands(text)
            if any(_is_root_target(target, scan.cwd0) for target in targets):
                return True
            if any(scan.run(reading, shell=True, ticks=True)
                   for command in commands for reading in _fold_variables(command)):
                return True
            if any(scan.run(argv, shell=True, ticks=True) for argv in argvs):
                return True
    # ``iex (iwr ...)``, ``[scriptblock]::Create((iwr ...))``: a download evaluated with no pipe at all.
    return scan.downloaded and (scan.evaluated or _SCRIPTBLOCK in every)


def _delete_target_blocked(arguments: Mapping[str, Any], cwd: str) -> bool:
    path = arguments.get("path")
    if not isinstance(path, str):
        raise _Unreadable("file_delete needs a path")
    places = _places(cwd)
    # The desktop client trims the path with Dart's ``String.trim`` (``\ufeff/`` is ``/``).
    return any(_is_root_target(reading, places) for reading in {path, path.strip(_DART_BLANKS)}) or (
        path.strip().strip(_DART_BLANKS).rstrip(" .") == "")


def _blocks(tool_name: str, arguments: Any) -> bool:
    """Whether a stricter rule blocks the call; raises on a fault (``blocks`` answers for it)."""
    if not isinstance(arguments, Mapping):
        return False  # the base rules ask for a shape they do not read
    name = str(tool_name or "").strip().lstrip("/")
    parts = _parts(name, arguments)
    cwd = arguments.get("cwd")
    cwd = cwd.lower() if isinstance(cwd, str) else ""
    if sum(len(text) for text, _shell, _ticks in parts) + len(cwd) > _MAX_TEXT:
        return True  # too long to read: fail closed (bypass skips an ASK)
    parts = [(text.lower(), shell, ticks) for text, shell, ticks in parts]
    if parts and _blocked(parts, cwd):
        return True
    # An argv has no shell: each element is one word (``["-rf", "#", "/"]`` deletes ``/``),
    # and the client runs ``command`` as one (``rg -e x / ; echo --pre=rm``).
    places = _places(cwd)
    if (dart_cwd := cwd.strip(_DART_BLANKS)) != cwd:
        places += _places(dart_cwd)  # the desktop client trims ``cwd`` with Dart's ``String.trim``
    root_cache: dict[str, bool] = {}
    relatives: dict[str, _Place] = {}
    if any(_argv_blocked(argv, places, root_cache, relatives) for argv in _argv_readings(name, arguments)):
        return True
    return name in _DELETE_TOOLS and _delete_target_blocked(arguments, cwd)


def carries_commands(arguments: Any) -> bool:
    """Whether ``arguments`` hold command text (an argv, a shell key, code or checks); True if unreadable."""
    try:
        return isinstance(arguments, Mapping) and any(
            arguments.get(key) for key in (*_ARGV_KEYS, *_SHELL_KEYS, "code", "checks"))
    except Exception:  # noqa: BLE001 - fail closed
        return True


def blocks(tool_name: str, arguments: Any) -> bool:
    """Whether a stricter rule blocks the call.

    Arguments of a shape it does not read answer False (the base rules ask).
    Any other fault answers True for a call that carries command text: bypass
    skips an ASK, so a fault while reading a command must not let it run unread.
    """
    try:
        return _blocks(tool_name, arguments)
    except _Unreadable:
        return False
    except Exception:  # noqa: BLE001 - fail closed: a fault in a command is a BLOCK
        return carries_commands(arguments)


__all__ = ["blocks", "carries_commands"]
