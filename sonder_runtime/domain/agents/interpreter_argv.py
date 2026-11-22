"""Interpreter option grammar for the project-execution argv guard.

The host guard (``server._agent_project_execution_argument_error``) refuses
inline interpreter code and interpreter code arriving on stdin. Comparing
whole argv tokens against ``-c`` / ``-e`` is trivially walked around, because
the interpreters accept combined short options (``python -Ic``,
``bash -lc``), attached values (``python -cCODE``), ``=``-joined long options
(``node --eval=CODE``) and, for PowerShell, parameter prefixes and aliases
(``-Comm``, ``-e``, ``-ec``), Unicode dashes, padded tokens and positional
code (``powershell.exe "iex ..."``). Counting every non-dash token as a script
operand is walked around the same way (``python -W ignore`` with a stdin
program). These functions read each interpreter's own option grammar instead.

Pure functions over ``str``: no I/O, no state, safe from any thread. They are
a host argument policy, not an OS sandbox, and they err towards refusing:
where an interpreter's handling of a spelling is not pinned down, the
spelling is treated as code.
"""
from __future__ import annotations

import posixpath
import re
from collections.abc import Sequence
from typing import NamedTuple


class _Grammar(NamedTuple):
    """Short- and long-option grammar of one getopt-style interpreter family.

    code:    letters that run their value as inline code (-c, -e, ...)
    stop:    letters whose value is the rest of the token, so no later
             letter of that token is an option (python -Wonce, perl
             -MData::Dumper). Only letters whose parser in the interpreter's
             own source was checked belong here: perl -d, ruby -K / -W keep
             parsing switches after their value (perl -de CODE). Empty where
             that is not pinned down, so the whole cluster is searched
             (over-blocks rather than under-blocks)
    space_stop: letters whose value ends at the first whitespace of the
             token, after which the interpreter parses switches again (perl
             -i / -F: "-i.bak -e CODE" as one token runs CODE); everything
             after that whitespace is searched
    colon_stop: letters that are a stop letter only when ':' follows them
             (ruby -W:no-deprecated, perl -V:name); bare they keep parsing
    value:   letters taking a value -- the rest of the cluster, or the next
             token when the letter ends the cluster -- for operand counting.
             It may over-list for the next-token form: the operand scan
             follows both readings of such a token (a value, and an argument
             in its own right), so a letter that does not really take one
             only refuses more. An over-listed letter still ends the cluster
             (its attached text is not searched for ``stdin`` letters)
    next_value: letters that take the next unconsumed token wherever they
             stand in the cluster, after which the cluster goes on (bash
             -o / -O: "-os posix" sets posix and reads stdin)
    stdin:   letters that make stdin the program even with a script operand
             (python -i, bash -s / -i, node -i)
    long_code / long_stdin: the same for ``--name`` options
    long_value: ``--name`` options consuming the next token when written
             without ``=``; None means every such option is assumed to
             (node has many value options)
    option_prefixes: token prefixes that start an option cluster
    """

    code: str
    stop: str
    space_stop: str
    colon_stop: str
    value: str
    next_value: str
    stdin: str
    long_code: frozenset[str]
    long_stdin: frozenset[str]
    long_value: frozenset[str] | None
    option_prefixes: tuple[str, ...]


# Matched case-sensitively against the raw argv token: python -x is a flag
# and -X takes a value; ruby -e is inline code and -E an encoding.
_GETOPT_GRAMMAR: dict[str, _Grammar] = {
    "python": _Grammar(
        code="c", stop="cmWXQ", space_stop="", colon_stop="",
        value="mWXQ", next_value="", stdin="i",
        long_code=frozenset(), long_stdin=frozenset(),
        long_value=frozenset({"--check-hash-based-pycs"}),
        option_prefixes=("-",),
    ),
    "node": _Grammar(
        code="ep", stop="", space_stop="", colon_stop="",
        value="rC", next_value="", stdin="i",
        long_code=frozenset({"--eval", "--print"}),
        long_stdin=frozenset({"--interactive"}),
        long_value=None,
        option_prefixes=("-",),
    ),
    "shell": _Grammar(
        code="c", stop="", space_stop="", colon_stop="",
        value="", next_value="oO", stdin="is",
        long_code=frozenset(), long_stdin=frozenset(),
        long_value=frozenset({"--rcfile", "--init-file"}),
        option_prefixes=("-", "+"),
    ),
    # perl.c: -I -M -m -x take the whole rest of the token; -i and -F stop
    # at whitespace and switch parsing resumes; -d (bar -d:Mod), -V (bar
    # -V:name), -C, -l, -0 and -D keep parsing after their value.
    "perl": _Grammar(
        code="eE", stop="IMmx", space_stop="iF", colon_stop="V",
        value="IMmixdFV", next_value="", stdin="",
        long_code=frozenset(), long_stdin=frozenset(), long_value=frozenset(),
        option_prefixes=("-",),
    ),
    # ruby.c proc_options: -r -i -x -C -X -F -E -I end the token ("break");
    # -K, -W (bar -W:category) and -0 "goto reswitch" and keep parsing; -T
    # is no longer an option. proc_long_options: the long options below take
    # the next argv token when written without '=' (--enable / --disable
    # also carry an attached value after '-', as in --disable-gems).
    "ruby": _Grammar(
        code="e", stop="ICXrxEFi", space_stop="", colon_stop="W",
        value="ICXrxEFKTWi", next_value="", stdin="",
        long_code=frozenset(), long_stdin=frozenset(),
        long_value=frozenset({
            "--enable", "--disable", "--encoding", "--internal-encoding",
            "--external-encoding", "--parser", "--source-encoding", "--dump",
            "--backtrace-limit", "--crash-report",
        }),
        option_prefixes=("-",),
    ),
}
# `python -m code` (and the asyncio / _pyrepl REPLs) execute stdin even though
# -m names a module, and `python -m pdb script.py` reads debugger commands
# from stdin, which run Python statements (`!import os`); any other module
# named by -m is the program, so stdin is data to it (`python -m json.tool`
# stays allowed).
_PYTHON_STDIN_REPL_MODULES = frozenset({"code", "asyncio", "_pyrepl", "pdb"})
_WIN_EXT = r"(?:\.(?:exe|cmd|bat|com))?"
_VERSION = r"(?:\d+(?:\.\d+)*)?%s" % _WIN_EXT


def program_name(token: object) -> str:
    """Casefolded basename of an argv token, normalised like the executor.

    Strips outer whitespace (matching ``workbench._resolve_program`` and
    ``run_program``), collapses ``.``, ``..`` and trailing separators across
    both ``/`` and ``\\``, strips a Windows drive prefix (``C:python``), and
    trims trailing dots and spaces (Windows filename aliases such as
    ``python.exe.`` or ``python.exe .``).
    """
    text = str(token).strip()
    if not text:
        return ""
    normalised = posixpath.normpath(text.replace("\\", "/"))
    base = normalised.rsplit("/", 1)[-1]
    if len(base) >= 2 and base[1] == ":" and base[0].isascii() and base[0].isalpha():
        base = base[2:]
    return base.rstrip(". ").casefold()


def interpreter_family(name: str) -> str:
    """The inline-capable interpreter family a program basename names, or ''."""
    if re.fullmatch(r"(?:(?:pythonw?|pypyw?)%s|pyw?%s)" % (_VERSION, _WIN_EXT), name):
        return "python"
    if re.fullmatch(r"node(?:js)?%s" % _VERSION, name):
        return "node"
    if re.fullmatch(r"(?:ba|z|da)?sh%s" % _WIN_EXT, name):
        return "shell"
    if re.fullmatch(r"perl%s" % _VERSION, name):
        return "perl"
    if re.fullmatch(r"ruby%s" % _VERSION, name):
        return "ruby"
    if re.fullmatch(r"powershell%s" % _WIN_EXT, name):
        return "powershell"
    if re.fullmatch(r"pwsh%s" % _WIN_EXT, name):
        return "pwsh"
    if re.fullmatch(r"cmd%s" % _WIN_EXT, name):
        return "cmd"
    return ""


_POWERSHELL_FAMILIES = frozenset({"powershell", "pwsh"})
# Both PowerShells trim each argv token and take '-', '/' and the Unicode
# dashes U+2013, U+2014 and U+2015 as a parameter prefix (`pwsh <en dash>c`).
# pwsh also takes a second dash (`--nop`); Windows PowerShell does not and
# runs such a token as the start of a -Command instead.
_POWERSHELL_DASHES = "-\u2013\u2014\u2015"
# Command-line parameters other than -Command / -EncodedCommand /
# -CommandWithArgs, as (full name, shortest accepted prefix, aliases, kind).
# A name is accepted when it is a prefix of the full name at least as long as
# the shortest prefix, or one of the aliases. Windows PowerShell runs any
# token it does not take as a parameter as the start of a -Command (`-mt`,
# `-confi`, `--nop`, `-ep:x`), so its table lists only spellings checked
# against powershell.exe 5.1; anything else is refused. Version and
# PSConsoleFile are only parameters in first position there and are left out.
_POWERSHELL_PARAMETERS: dict[str, tuple[tuple[str, str, frozenset[str], str], ...]] = {
    "powershell": (
        ("file", "f", frozenset(), "file"),
        ("noprofile", "nop", frozenset(), "switch"),
        ("nologo", "nol", frozenset(), "switch"),
        ("noninteractive", "noni", frozenset(), "switch"),
        ("noexit", "noe", frozenset(), "switch"),
        ("sta", "st", frozenset(), "switch"),
        ("mta", "mta", frozenset(), "switch"),
        ("executionpolicy", "ex", frozenset({"ep"}), "value"),
        ("windowstyle", "w", frozenset(), "value"),
        ("inputformat", "inp", frozenset({"if"}), "value"),
        ("outputformat", "o", frozenset({"of"}), "value"),
        ("workingdirectory", "work", frozenset({"wd"}), "value"),
        ("configurationname", "config", frozenset(), "value"),
    ),
    # pwsh reports a token it does not take as a parameter as an unknown
    # script file, so a spelling listed here that it rejects fails closed.
    "pwsh": (
        ("file", "f", frozenset(), "file"),
        ("noprofile", "nop", frozenset(), "switch"),
        ("noprofileloadtime", "noprofileloadtime", frozenset(), "switch"),
        ("nologo", "nol", frozenset(), "switch"),
        ("noninteractive", "noni", frozenset(), "switch"),
        ("noexit", "noe", frozenset(), "switch"),
        ("interactive", "i", frozenset(), "switch"),
        ("login", "l", frozenset(), "switch"),
        ("sta", "sta", frozenset(), "switch"),
        ("mta", "mta", frozenset(), "switch"),
        ("sshservermode", "sshs", frozenset(), "switch"),
        ("version", "v", frozenset(), "switch"),
        ("executionpolicy", "ex", frozenset({"ep"}), "value"),
        ("windowstyle", "w", frozenset(), "value"),
        ("inputformat", "inp", frozenset({"if"}), "value"),
        ("outputformat", "o", frozenset({"of"}), "value"),
        ("workingdirectory", "workingdirectory", frozenset({"wd"}), "value"),
        ("configurationname", "config", frozenset(), "value"),
        ("configurationfile", "configurationfile", frozenset(), "value"),
        ("custompipename", "custompipename", frozenset(), "value"),
        ("settingsfile", "settings", frozenset(), "value"),
    ),
}


def _powershell_parameter_name(token: str, family: str = "pwsh") -> str:
    """Lowercased text of a PowerShell argv token after its prefix, or ''.

    The token is trimmed first, as PowerShell does. ``family`` "pwsh" also
    strips a second dash; that lenient reading is the one used for
    inline-code detection, where accepting more spellings only refuses more.
    """
    text = token.strip()
    if len(text) < 2 or text[0] not in _POWERSHELL_DASHES + "/":
        return ""
    text = text[1:]
    if family == "pwsh" and len(text) > 1 and text[0] in _POWERSHELL_DASHES:
        text = text[1:]
    return text.lower()


def _powershell_parameter_kind(family: str, name: str) -> str:
    """'file', 'switch' or 'value' for an accepted parameter name, else ''."""
    if not (name.isascii() and name.isalpha()):
        return ""
    kinds = {
        kind
        for full, shortest, aliases, kind in _POWERSHELL_PARAMETERS[family]
        if name in aliases or (full.startswith(name) and len(name) >= len(shortest))
    }
    # A name matching two kinds is not pinned down: treat it as unknown.
    return kinds.pop() if len(kinds) == 1 else ""


def _powershell_scan(family: str, tokens: Sequence[str]) -> tuple[bool, str | None]:
    """(runs inline code, -File operand or None) for a PowerShell argv.

    Refuses unless every token up to an explicit ``-File <script>`` is a
    parameter the table pins down (with its value). A positional token is a
    -Command to Windows PowerShell (``powershell.exe "iex ..."``); pwsh's
    positional -File is refused alike, so only the explicit form passes.
    """
    index = 0
    while index < len(tokens):
        token = str(tokens[index])
        if token_runs_inline_code(family, token):
            return True, None
        kind = _powershell_parameter_kind(family, _powershell_parameter_name(token, family))
        following = str(tokens[index + 1]) if index + 1 < len(tokens) else ""
        if kind == "file":
            return False, following
        if kind == "switch":
            index += 1
            continue
        if kind == "value":
            if following and token_runs_inline_code(family, following):
                return True, None
            index += 2
            continue
        return True, None
    return False, None


def token_runs_inline_code(family: str, token: str) -> bool:
    """Whether one argv token selects inline code for ``family``.

    Combined short options (``python -Ic``, ``bash -lc``), attached values
    (``python -cCODE``), ``=``-joined long options (``node --eval=CODE``) and
    PowerShell's parameter prefixes and aliases (``-Comm``, ``-e``, ``-ec``,
    a Unicode-dash ``-c``, a whitespace-padded ``-c``) all count, not only
    the bare flag. For PowerShell use :func:`runs_inline_code`, which also
    refuses positional code.
    """
    text = str(token)
    if family in _POWERSHELL_FAMILIES:
        name = _powershell_parameter_name(text).split(":", 1)[0]
        return bool(name) and (
            name in {"c", "e", "ec", "cwa"}
            or (len(name) >= 2 and (
                "commandwithargs".startswith(name)
                or "encodedcommand".startswith(name)
            ))
        )
    if family == "cmd":
        # cmd.exe takes /C, /K and /R (= /C), the command glued on or not,
        # and finds them anywhere in its command line, not only at the start
        # of a token: `cmd /d/c dir` and `cmd x/c dir` both run `dir`.
        return bool(re.search(r"/[ckr]", text, re.IGNORECASE))
    grammar = _GETOPT_GRAMMAR.get(family)
    if grammar is None:
        return False
    if text.startswith("--"):
        return text.split("=", 1)[0] in grammar.long_code
    # The shells take `+` as an option prefix too, and bash still reads `c`
    # in `+c` / `+lc` as "run the next argument".
    if not text.startswith(grammar.option_prefixes) or len(text) < 2:
        return False
    cluster = text[1:]
    for position, letter in enumerate(cluster):
        if letter in grammar.code:
            return True
        rest = cluster[position + 1:]
        if letter in grammar.stop or (letter in grammar.colon_stop and rest.startswith(":")):
            return False
        if letter in grammar.space_stop:
            space = re.search(r"\s", rest)
            remainder = rest[space.end():] if space else ""
            return any(item in grammar.code for item in remainder)
    return False


def runs_inline_code(family: str, tokens: Sequence[str]) -> bool:
    """Whether ``family`` invoked with ``tokens`` runs code given in argv.

    For PowerShell this reads the whole argv (positional code, parameter
    values); for the other families any token selecting inline code counts.
    """
    if family in _POWERSHELL_FAMILIES:
        return _powershell_scan(family, tokens)[0]
    return any(token_runs_inline_code(family, str(token)) for token in tokens)


def _powershell_stdin_runs_as_code(family: str, tokens: Sequence[str]) -> bool:
    # Only an explicit -File <script> makes stdin data. A positional value is
    # a -Command to Windows PowerShell, and -File - reads stdin.
    inline, operand = _powershell_scan(family, tokens)
    return inline or operand is None or operand.strip() in {"", "-"}


def stdin_runs_as_code(family: str, tokens: Sequence[str]) -> bool:
    """Whether ``family`` invoked with ``tokens`` executes its stdin as code.

    An interpreter runs stdin as its program when argv names no script
    operand or names ``-`` or an empty name as the operand, also after
    ``--`` (``python -- -``, ``node -- ""``). node, perl and ruby read an
    empty script name as stdin; a whitespace-only name is treated the same
    way, which only refuses more.
    A value consumed by an option (``python -W ignore``, ``bash -o posix``) is
    not an operand, and a flag such as ``python -i`` or ``bash -s`` makes
    stdin the program even when an operand is present. Where an option may
    take the next token as its value, both readings are checked and stdin
    counts as code if either one runs it (``node --no-warnings - x.js``).
    """
    if family == "cmd":
        # cmd.exe without /C or /K reads its commands from stdin; it has no
        # script-operand form at all.
        return True
    if family in _POWERSHELL_FAMILIES:
        return _powershell_stdin_runs_as_code(family, tokens)
    grammar = _GETOPT_GRAMMAR.get(family)
    if grammar is None:
        return False
    texts = [str(token) for token in tokens]
    # A reading depends only on where it resumes and whether a stdin flag
    # was seen, so each (index, forced) state is walked once: at most
    # 2 * (len(texts) + 1) walks, however many options branch.
    pending: list[tuple[int, bool]] = [(0, False)]
    seen: set[tuple[int, bool]] = set()
    while pending:
        state = pending.pop()
        if state in seen:
            continue
        seen.add(state)
        if _reading_runs_stdin(family, grammar, texts, state[0], state[1], pending):
            return True
    return False


def _names_stdin(operand: str) -> bool:
    """Whether a script operand names stdin: ``-``, empty or whitespace-only."""
    return operand.strip() in {"", "-"}


def _reading_runs_stdin(
    family: str,
    grammar: _Grammar,
    texts: list[str],
    index: int,
    forced: bool,
    pending: list[tuple[int, bool]],
) -> bool:
    """Whether one reading of ``texts[index:]`` runs stdin as the program.

    The walk takes each next-token value as consumed and appends to
    ``pending`` the readings in which it is not (``node --no-warnings -r
    pre.js -``: --no-warnings takes no value, so -r takes pre.js and ``-``
    is the stdin program).
    """
    while index < len(texts):
        text = texts[index]
        if _names_stdin(text):
            return True
        if text == "--":
            # `--` ends option parsing; the next token is the operand. A `-`
            # or empty name there (`python -- -`, `node -- ""`) is still the
            # stdin program, and nothing after `--` means no operand at all.
            return forced or index + 1 >= len(texts) or _names_stdin(texts[index + 1])
        consumed = 0
        if text.startswith("--"):
            name, equals, _ = text.partition("=")
            forced = forced or name in grammar.long_stdin
            if not equals and (grammar.long_value is None or name in grammar.long_value):
                consumed = 1
        elif text.startswith(grammar.option_prefixes) and len(text) > 1:
            cluster = text[1:]
            for position, letter in enumerate(cluster):
                if letter in grammar.stdin:
                    forced = True
                if letter in grammar.next_value:
                    consumed += 1
                    continue
                if letter in grammar.value:
                    attached = cluster[position + 1:]
                    if family == "python" and letter == "m":
                        # -m takes the module and leaves the rest of argv to
                        # it; a missing or `-` module is refused.
                        module = attached or (
                            texts[index + 1 + consumed]
                            if index + 1 + consumed < len(texts) else "-"
                        )
                        return forced or module == "-" or module in _PYTHON_STDIN_REPL_MODULES
                    if not attached:
                        consumed += 1
                    break
        else:
            return forced
        for skipped in range(consumed):
            pending.append((index + 1 + skipped, forced))
        index += 1 + consumed
    return True


_WINDOWS_SLASH_FAMILIES = frozenset({"cmd", "powershell", "pwsh"})
_DYNAMIC_EXPANSION_RE = re.compile(r"%[^%\r\n]+%|![^!\r\n]+!")
# cmd.exe skips these before a command name, in any order and any number
# (`=python`, `@=python`, `=@python`, `, ; = python`), at the start of every
# command and of the command line that `call`, `start`, `if` and `cmd /c`
# re-read.
_CMD_NAME_LEADERS = "@=,; \t"
# Characters that end a command name for cmd.exe and begin the child's own
# argument text: `python,x` runs python with the argument `,x`, `python/x`
# with `/x`, `python(x` with `(x`. Probed with cmd /d /s /c on Windows 11:
# `+`, `[`, `]` and `:` do not end a name (`python+`, `python[`, `python]`
# and `python:` are "not recognized"), and a leading `:` makes the line a
# label that runs nothing.
_CMD_NAME_TERMINATORS = ",;=/("
# Programs that take a command string of their own and decode or run it
# (`forfiles /c cmd0x20/c...`, `wmic process call create`, `schtasks /tr`,
# `mshta vbscript:...`, `rundll32`, `certutil -decode`, ...). They are not
# build tools, and their argument grammars hide an interpreter from this
# guard, so they are refused wherever they appear.
_COMMAND_STRING_LAUNCHERS = (
    "forfiles", "wmic", "schtasks", "at", "runas", "mshta", "rundll32",
    "wscript", "cscript", "regsvr32", "msiexec", "certutil", "bitsadmin",
)
_COMMAND_STRING_LAUNCHER_RE = re.compile(
    r"(?:%s)%s" % ("|".join(_COMMAND_STRING_LAUNCHERS), _WIN_EXT)
)

OPAQUE_QUOTES = (
    "double quotes are not supported in build_run commands (the program "
    "receives them as literal characters, not as argument grouping)"
)
OPAQUE_CARET = "caret (^) escapes are not supported in build_run commands"
OPAQUE_EXPANSION = (
    "variable expansion (%VAR%, !VAR!) is not supported in build_run commands"
)
OPAQUE_CONTROL = "control characters are not supported in build_run commands"
OPAQUE_NON_ASCII_WHITESPACE = (
    "non-ASCII whitespace is not supported in build_run commands"
)
OPAQUE_FOR_LOOP = "cmd.exe for loops are not supported in build_run commands"


def program_name_candidates(token: object) -> tuple[str, ...]:
    """Every normalised program name ``token`` can name once cmd.exe reads it.

    The whole token, and each piece between cmd.exe's name delimiters ``=``,
    ``,`` and ``;`` with leading ``@`` stripped: ``call =python``,
    ``if exist x=python`` and ``start @python`` all reach ``python``. Each
    candidate goes through :func:`program_name`. A piece that is not really a
    program only makes the guard refuse more.
    """
    text = str(token)
    names: list[str] = []
    for raw in (text, *re.split(r"[=,;]", text)):
        name = program_name(raw.strip().lstrip(_CMD_NAME_LEADERS))
        if name and name not in names:
            names.append(name)
    return tuple(names)


def command_string_launcher(tokens: Sequence[object]) -> str:
    """The first command-string launcher any of ``tokens`` names, or ''.

    Names are matched after :func:`program_name_candidates` normalisation
    (case, path, ``.exe``/``.cmd``/``.bat``/``.com``, cmd.exe delimiters), the
    same way the interpreter families are.
    """
    for token in tokens:
        for name in program_name_candidates(token):
            if _COMMAND_STRING_LAUNCHER_RE.fullmatch(name):
                return name
    return ""


class BuildCommand(NamedTuple):
    """Parsed structure of a ``build_run`` command string.

    segments:      each chained/piped command as ``(tokens, receives_stdin)``,
                   with shell redirections removed from ``tokens`` and
                   ``cmd.exe`` verb normalisation applied.
    redir_targets: target tokens from ``<`` / ``>`` / ``>>`` redirections, so
                   the caller's path guard still confines them.
    opaque:        True when the command cannot be read safely: a double
                   quote, a ``^`` caret escape, dynamic shell expansion
                   (``%...%``, ``!...!``), a non-tab ASCII control character
                   (``< 0x20`` or ``0x7f``), non-ASCII whitespace, or a
                   ``for`` command.
    reason:        the ``OPAQUE_*`` text naming why, or '' when not opaque.
    """

    segments: tuple[tuple[tuple[str, ...], bool], ...]
    redir_targets: tuple[str, ...]
    opaque: bool
    reason: str = ""


def _opaque_reason(command: str) -> str:
    """Why ``command`` cannot be read safely before tokenising, or ''.

    A double quote is a grouping toggle to cmd.exe, but harness_tools hands
    the command to ``cmd /d /s /c`` through ``subprocess.list2cmdline``, which
    escapes it as ``\\"``; the child's CRT reads that as a literal quote, so
    cmd.exe and the child disagree on argv (``python -X "x -c CODE#"``). On
    POSIX the command is ``str.split()`` and quotes are literal too. Non-ASCII
    whitespace splits words for ``str.split()`` but not for the child CRT,
    which splits on space and tab only.
    """
    if '"' in command:
        return OPAQUE_QUOTES
    if "^" in command:
        # Multi-pass cmd.exe parsing (e.g. a pipe child re-parsing `^&`/`^|`)
        # cannot be safely modelled in one pass.
        return OPAQUE_CARET
    if _DYNAMIC_EXPANSION_RE.search(command):
        return OPAQUE_EXPANSION
    if any((ord(ch) < 0x20 and ch != "\t") or ord(ch) == 0x7F for ch in command):
        return OPAQUE_CONTROL
    if any(ch.isspace() and not ch.isascii() for ch in command):
        return OPAQUE_NON_ASCII_WHITESPACE
    return ""


def _split_glued_windows_switch(tok: str, *, is_verb: bool) -> tuple[str, str] | None:
    """Split ``cmd/c``, ``cmd.exe/d/c``, ``powershell/c`` into (program, /switch)."""
    start = 0
    while True:
        pos = tok.find("/", start)
        if pos < 0:
            return None
        prefix = tok[:pos]
        suffix = tok[pos:]
        if prefix and len(suffix) > 1:
            fam = interpreter_family(program_name(prefix))
            if fam in _WINDOWS_SLASH_FAMILIES and (
                is_verb or fam == "cmd" or token_runs_inline_code(fam, suffix)
            ):
                return prefix, suffix
        start = pos + 1


def _has_cmd_for_loop(tokens: Sequence[str]) -> bool:
    """Whether ``tokens`` invokes a ``cmd.exe`` ``for`` loop."""
    if not tokens:
        return False
    verb_head = tokens[0].split("=", 1)[0].split("/", 1)[0]
    if program_name(verb_head) == "for":
        return True
    for idx, tok in enumerate(tokens[:-1]):
        if program_name(tok.split("/", 1)[0]) != "for":
            continue
        nxt = tokens[idx + 1].casefold()
        if nxt in {"/f", "/d", "/l", "/r"} or (
            len(nxt) >= 2 and nxt[0] == "%" and nxt[1].isalpha()
        ):
            return True
    return False


def _expand_cmd_segment(
    tokens: list[str], *, child_tokens: bool = False,
) -> tuple[tuple[str, ...], bool]:
    """Normalise one ``cmd.exe`` command segment.

    Strips the leading ``@ = , ;`` cmd.exe skips before a command name,
    detects ``for`` loops, and -- for cmd.exe's own tokenisation only -- splits
    glued ``name=value`` and ``name/switch`` verbs. The child tokenisation
    keeps every later token whole, as the child's CRT does.
    """
    while tokens:
        head = tokens[0].lstrip(_CMD_NAME_LEADERS)
        if head:
            tokens = [head, *tokens[1:]]
            break
        tokens = tokens[1:]
    if not tokens:
        return (), False
    if _has_cmd_for_loop(tokens):
        return tuple(tokens), True
    if child_tokens:
        return tuple(tokens), False
    expanded: list[str] = []
    for idx, tok in enumerate(tokens):
        if "=" in tok and not tok.startswith(("-", "/")):
            head, tail = tok.split("=", 1)
            if head and interpreter_family(program_name(head)):
                expanded.append(head)
                if tail:
                    expanded.append(tail)
                continue
        if "/" in tok and not interpreter_family(program_name(tok)):
            split_result = _split_glued_windows_switch(tok, is_verb=(idx == 0))
            if split_result is not None:
                expanded.extend(split_result)
                continue
        expanded.append(tok)
    return tuple(expanded), False


def parse_build_command(command: str, *, child_tokens: bool = False) -> BuildCommand:
    """Parse a ``build_run`` command string the way ``cmd /d /s /c`` executes it.

    Splits chained/piped commands on ``&``, ``&&``, ``|``, ``||``, ``(``, ``)``
    and newlines, honours ``cmd.exe`` verb delimiters (leading ``@ = , ;``,
    glued ``=``/``/switch``), tracks stdin piped via ``|`` (including into
    parenthesised groups) or redirected via ``<``, strips redirection targets
    from segment argv, and fails closed (``opaque=True`` with a ``reason``) on
    double quotes, ``^`` caret escapes, dynamic expansions (``%...%``,
    ``!...!``), non-tab ASCII control characters (``< 0x20`` or ``0x7f``),
    non-ASCII whitespace and ``for`` loops.

    Words are split two ways, and the caller must judge both:

    * ``child_tokens=False`` -- cmd.exe's reading: words split on any
      non-newline whitespace plus ``,`` and ``;``, and every ``(`` / ``)``
      ends a segment.
    * ``child_tokens=True`` -- the child CRT's reading: after the command
      name, words split on space and tab only, so ``,``, ``;``, ``=`` and
      non-ASCII whitespace stay inside a token (``python -Wx,y``). The name
      itself starts after any leading ``@ = , ;``, space or tab and ends at
      space, tab or one of ``, ; = / (``, which begins the first argument.
      A ``(`` opens a group only at command position and a ``)`` closes one
      only inside a group; anywhere else both are literal argument text
      (``python -W ( -c CODE``).
    """
    reason = _opaque_reason(command)
    segments: list[tuple[list[str], bool]] = []
    redir_targets: list[str] = []
    cur_tokens: list[str] = []
    cur_buf: list[str] = []
    cur_has_token = False
    cur_quoted = False
    in_quote = False
    cur_stdin = False
    next_from_pipe = False
    group_stack: list[tuple[int, bool]] = []
    last_closed_group: tuple[int, int] | None = None
    expect_redir_target = False

    def _consume_pipe() -> None:
        nonlocal cur_stdin, next_from_pipe, last_closed_group
        if not cur_tokens and not cur_has_token:
            if next_from_pipe:
                cur_stdin = True
                next_from_pipe = False
            last_closed_group = None

    def _flush_word() -> None:
        nonlocal cur_has_token, cur_quoted, expect_redir_target
        if not cur_has_token:
            return
        word = "".join(cur_buf)
        cur_buf.clear()
        cur_has_token = False
        cur_quoted = False
        if expect_redir_target:
            redir_targets.append(word)
            expect_redir_target = False
        else:
            cur_tokens.append(word)

    def _flush_segment() -> None:
        nonlocal cur_stdin, last_closed_group, expect_redir_target
        _flush_word()
        expect_redir_target = False
        if cur_tokens:
            group_piped = any(piped for _, piped in group_stack)
            segments.append((list(cur_tokens), cur_stdin or group_piped))
            cur_tokens.clear()
            last_closed_group = None
        cur_stdin = False

    i = 0
    length = len(command)
    while i < length:
        ch = command[i]
        if in_quote:
            if ch == '"':
                in_quote = False
                cur_has_token = True
            else:
                cur_buf.append(ch)
                cur_has_token = True
            i += 1
            continue

        if ch == "^":
            # Already opaque (OPAQUE_CARET); drop it and keep scanning.
            i += 1
            continue

        if ch == '"':
            _consume_pipe()
            in_quote = True
            cur_has_token = True
            cur_quoted = True
            i += 1
            continue

        at_name = not cur_tokens and not expect_redir_target
        if child_tokens:
            if at_name and not cur_has_token and ch in _CMD_NAME_LEADERS:
                i += 1
                continue
            if ch in " \t":
                _flush_word()
                i += 1
                continue
            if at_name and cur_has_token and ch in _CMD_NAME_TERMINATORS:
                # The name ends here; this character starts the child's
                # first argument (`python,x` -> `,x`).
                _flush_word()
                cur_buf.append(ch)
                cur_has_token = True
                i += 1
                continue
            literal_paren = (
                (ch == "(" and not (at_name and not cur_has_token))
                or (ch == ")" and not group_stack)
            )
            if literal_paren:
                _consume_pipe()
                cur_buf.append(ch)
                cur_has_token = True
                i += 1
                continue
        elif ch in ",;" or (ch.isspace() and ch not in "\r\n"):
            _flush_word()
            i += 1
            continue

        if ch in "\r\n":
            _flush_segment()
            next_from_pipe = False
            i += 1
            continue

        if ch == "&":
            _flush_segment()
            next_from_pipe = False
            i += 2 if i + 1 < length and command[i + 1] == "&" else 1
            continue

        if ch == "|":
            _flush_segment()
            if i + 1 < length and command[i + 1] == "|":
                next_from_pipe = False
                i += 2
            else:
                next_from_pipe = True
                i += 1
            continue

        if ch == "(":
            _flush_segment()
            group_stack.append((len(segments), next_from_pipe))
            next_from_pipe = False
            i += 1
            continue

        if ch == ")":
            _flush_segment()
            next_from_pipe = False
            if group_stack:
                start_idx, _ = group_stack.pop()
                last_closed_group = (start_idx, len(segments))
            i += 1
            continue

        if ch in "<>":
            if (
                cur_has_token
                and not cur_quoted
                and len(cur_buf) == 1
                and cur_buf[0].isascii()
                and cur_buf[0].isdigit()
            ):
                cur_buf.clear()
                cur_has_token = False
            else:
                _flush_word()
            if ch == "<":
                if not cur_tokens and last_closed_group is not None:
                    g_start, g_end = last_closed_group
                    for seg_idx in range(g_start, g_end):
                        seg_toks, _ = segments[seg_idx]
                        segments[seg_idx] = (seg_toks, True)
                else:
                    cur_stdin = True
            i += 2 if ch == ">" and i + 1 < length and command[i + 1] == ">" else 1
            if i < length and command[i] == "&":
                i += 1
                while i < length and command[i].isascii() and command[i].isdigit():
                    i += 1
                expect_redir_target = False
            else:
                expect_redir_target = True
            continue

        _consume_pipe()
        cur_buf.append(ch)
        cur_has_token = True
        i += 1

    _flush_segment()

    final_segments: list[tuple[tuple[str, ...], bool]] = []
    for seg_tokens, seg_stdin in segments:
        expanded, seg_for_loop = _expand_cmd_segment(seg_tokens, child_tokens=child_tokens)
        if seg_for_loop and not reason:
            reason = OPAQUE_FOR_LOOP
        if expanded:
            final_segments.append((expanded, seg_stdin))
    return BuildCommand(
        segments=tuple(final_segments),
        redir_targets=tuple(redir_targets),
        opaque=bool(reason),
        reason=reason,
    )
