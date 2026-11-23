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

from sonder_runtime.domain import network_path_spelling as _network_spelling


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
    "php": _Grammar(
        code="r", stop="cdzBFR", space_stop="", colon_stop="",
        value="cdzBFRf", next_value="", stdin="a",
        long_code=frozenset({"--run"}),
        long_stdin=frozenset({"--interactive"}),
        long_value=frozenset({"--file", "--define", "--php-ini"}),
        option_prefixes=("-",),
    ),
    "lua": _Grammar(
        code="e", stop="l", space_stop="", colon_stop="",
        value="l", next_value="", stdin="i",
        long_code=frozenset(), long_stdin=frozenset(), long_value=frozenset(),
        option_prefixes=("-",),
    ),
}
# `python -m code` (and the asyncio / _pyrepl REPLs) execute stdin even though
# -m names a module, and `python -m pdb script.py` reads debugger commands
# from stdin, which run Python statements (`!import os`); any other module
# named by -m is the program, so stdin is data to it (`python -m json.tool`
# stays allowed). Submodule spellings such as `code.__main__` run the same
# entry point, so callers match on the top-level module name before `.`.
_PYTHON_STDIN_REPL_MODULES = frozenset({"code", "asyncio", "_pyrepl", "pdb"})
# `python -m timeit` executes arbitrary Python statements given in argv
# without `-c`, and pass-through/launcher modules (`runpy`, `cProfile`,
# `profile`, `trace`, `idlelib`, `ipython`) can invoke another module or
# inline code (synced with `workbench._PYTHON_CODE_MODULES`).
_PYTHON_INLINE_MODULES = frozenset({
    "timeit", "runpy", "profile", "cprofile", "trace", "idlelib", "ipython",
})
_PERL_MODULE_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_:]*(?:'[A-Za-z0-9_:]+)*")
_RUBY_STDIN_REPL_SCRIPTS = frozenset({"irb", "pry"})
_WIN_EXT = r"(?:\.(?:exe|cmd|bat|com))?"
_VERSION = r"(?:\d+(?:\.\d+)*)?%s" % _WIN_EXT


def program_name(token: object) -> str:
    """Casefolded basename of an argv token, normalised like the executor.

    Strips outer whitespace (matching ``workbench._resolve_program`` and
    ``run_program``), collapses ``.``, ``..`` and trailing separators across
    both ``/`` and ``\\``, strips a Windows drive prefix (``C:python``) and
    NTFS alternate data stream suffix (``python.exe::$DATA``), and trims
    trailing dots and spaces (Windows filename aliases such as
    ``python.exe.`` or ``python.exe .``).
    """
    text = str(token).strip()
    if not text:
        return ""
    normalised = posixpath.normpath(text.replace("\\", "/"))
    base = normalised.rsplit("/", 1)[-1]
    if len(base) >= 2 and base[1] == ":" and base[0].isascii() and base[0].isalpha():
        base = base[2:]
    if ":" in base:
        base = base.split(":", 1)[0]
    return base.rstrip(". ").casefold()


def interpreter_family(name: str) -> str:
    """The inline-capable interpreter family a program basename names, or ''."""
    if re.fullmatch(r"(?:(?:pythonw?|pypyw?)%s|pyw?%s)" % (_VERSION, _WIN_EXT), name):
        return "python"
    if re.fullmatch(r"(?:node(?:js)?|bun)%s" % _VERSION, name):
        return "node"
    if re.fullmatch(r"deno%s" % _VERSION, name):
        return "deno"
    if re.fullmatch(r"(?:(?:ba|z|da|a|k|mk|c|tc|fi)?sh|git-bash|busybox)%s" % _WIN_EXT, name):
        return "shell"
    if re.fullmatch(r"perl%s" % _VERSION, name):
        return "perl"
    if re.fullmatch(r"ruby%s" % _VERSION, name):
        return "ruby"
    if re.fullmatch(r"php%s" % _VERSION, name):
        return "php"
    if re.fullmatch(r"lua(?:jit)?%s" % _VERSION, name):
        return "lua"
    if re.fullmatch(r"[gmn]?awk%s" % _VERSION, name):
        return "awk"
    if re.fullmatch(r"env%s" % _WIN_EXT, name):
        return "env"
    if re.fullmatch(r"(?:powershell(?:_ise)?|powers~\d+)%s" % _WIN_EXT, name):
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
        ("noexit", "noe", frozenset(), "stdin_switch"),
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
        ("noexit", "noe", frozenset(), "stdin_switch"),
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


def _powershell_scan(family: str, tokens: Sequence[str]) -> tuple[bool, str | None, bool]:
    """(runs inline code, -File operand or None, forces stdin) for PowerShell argv.

    Refuses unless every token up to an explicit ``-File <script>`` is a
    parameter the table pins down (with its value). A positional token is a
    -Command to Windows PowerShell (``powershell.exe "iex ..."``); pwsh's
    positional -File is refused alike, so only the explicit form passes.
    ``-NoExit`` drops into the stdin command loop after running ``-File``, so
    it sets ``forces_stdin=True`` like ``python -i``.
    """
    index = 0
    forces_stdin = False
    while index < len(tokens):
        token = str(tokens[index])
        if token_runs_inline_code(family, token):
            return True, None, forces_stdin
        kind = _powershell_parameter_kind(family, _powershell_parameter_name(token, family))
        following = str(tokens[index + 1]) if index + 1 < len(tokens) else ""
        if kind == "file":
            return False, following, forces_stdin
        if kind == "stdin_switch":
            forces_stdin = True
            index += 1
            continue
        if kind == "switch":
            index += 1
            continue
        if kind == "value":
            if following and token_runs_inline_code(family, following):
                return True, None, forces_stdin
            index += 2
            continue
        return True, None, forces_stdin
    return False, None, forces_stdin


def _perl_module_token_is_code(raw: str) -> bool:
    """Whether a Perl ``-M``/``-m`` or ``-d:`` module text injects code."""
    mod_part = raw.split("=", 1)[0].lstrip("-")
    return not mod_part or not bool(_PERL_MODULE_NAME_RE.fullmatch(mod_part))


def _node_data_import_value(value: str) -> bool:
    """Whether a Node ``--import`` / ``--loader`` value is an inline ``data:`` URL."""
    return value.strip().casefold().startswith("data:")


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
    if family == "env":
        if text.startswith("--"):
            return text.split("=", 1)[0] == "--split-string"
        return text.startswith("-") and len(text) >= 2 and "S" in text[1:]
    if family == "deno":
        return text.strip().casefold() == "eval"
    grammar = _GETOPT_GRAMMAR.get(family)
    if grammar is None:
        return False
    if text.startswith("--"):
        opt_name, has_eq, opt_val = text.partition("=")
        if family == "node":
            opt_name = opt_name.replace("_", "-")
            if (
                has_eq
                and opt_name in {"--import", "--loader", "--experimental-loader"}
                and _node_data_import_value(opt_val)
            ):
                return True
        return opt_name in grammar.long_code
    # The shells take `+` as an option prefix too, and bash still reads `c`
    # in `+c` / `+lc` as "run the next argument".
    if not text.startswith(grammar.option_prefixes) or len(text) < 2:
        return False
    cluster = text[1:]
    for position, letter in enumerate(cluster):
        if letter in grammar.code:
            return True
        rest = cluster[position + 1:]
        if family == "perl":
            if letter in ("M", "m") and rest:
                return _perl_module_token_is_code(rest)
            if letter == "d" and rest.startswith(":"):
                return _perl_module_token_is_code(rest[1:])
        if letter in grammar.stop or (letter in grammar.colon_stop and rest.startswith(":")):
            return False
        if letter in grammar.space_stop:
            space = re.search(r"\s", rest)
            remainder = rest[space.end():] if space else ""
            return any(item in grammar.code for item in remainder)
    return False


def _python_runs_inline_module(tokens: Sequence[str]) -> bool:
    """Whether Python argv runs an inline-eval ``-m`` module (e.g. ``timeit``)."""
    grammar = _GETOPT_GRAMMAR["python"]
    texts = [str(token) for token in tokens]
    for index, text in enumerate(texts):
        if text == "--":
            break
        if not text.startswith("-") or text.startswith("--") or len(text) < 2:
            if not text.startswith("-"):
                break
            continue
        cluster = text[1:]
        for position, letter in enumerate(cluster):
            rest = cluster[position + 1:]
            if letter == "m":
                module = rest or (texts[index + 1] if index + 1 < len(texts) else "")
                return module.strip().split(".", 1)[0].casefold() in _PYTHON_INLINE_MODULES
            if letter in grammar.stop:
                break
    return False


def _awk_runs_inline_code(tokens: Sequence[str]) -> bool:
    """Whether ``awk`` runs an inline program (no ``-f``/``--file`` or uses ``-e``)."""
    has_file = False
    for token in tokens:
        text = str(token)
        if text == "--":
            break
        if text in {"-e", "--source"} or text.startswith(("-e", "--source=")):
            return True
        if text in {"-f", "--file"} or text.startswith(("-f", "--file=")):
            has_file = True
    return not has_file


def runs_inline_code(family: str, tokens: Sequence[str]) -> bool:
    """Whether ``family`` invoked with ``tokens`` runs code given in argv.

    For PowerShell this reads the whole argv (positional code, parameter
    values); for the other families any token selecting inline code counts.
    """
    if family in _POWERSHELL_FAMILIES:
        return _powershell_scan(family, tokens)[0]
    if family == "awk":
        return _awk_runs_inline_code(tokens)
    if any(token_runs_inline_code(family, str(token)) for token in tokens):
        return True
    if family == "python":
        return _python_runs_inline_module(tokens)
    if family == "node":
        texts = [str(token) for token in tokens]
        for index, text in enumerate(texts[:-1]):
            if text.startswith("--") and "=" not in text:
                opt_name = text.replace("_", "-")
                if opt_name in {"--import", "--loader", "--experimental-loader"} and _node_data_import_value(texts[index + 1]):
                    return True
    if family == "perl":
        texts = [str(token) for token in tokens]
        for index, text in enumerate(texts[:-1]):
            if text in {"-M", "-m"} and _perl_module_token_is_code(texts[index + 1]):
                return True
    return False


def _powershell_stdin_runs_as_code(family: str, tokens: Sequence[str]) -> bool:
    # Only an explicit -File <script> (without -NoExit) makes stdin data.
    # A positional value is a -Command to Windows PowerShell, and -File -
    # reads stdin.
    inline, operand, forces_stdin = _powershell_scan(family, tokens)
    return inline or forces_stdin or operand is None or operand.strip() in {"", "-"}


def _deno_stdin_runs_as_code(tokens: Sequence[str]) -> bool:
    """Whether ``deno`` invoked with ``tokens`` executes stdin as code."""
    texts = [str(token) for token in tokens]
    if not texts:
        return True
    non_flags = [tok for tok in texts if tok and not tok.startswith("-")]
    if not non_flags:
        return True
    sub = non_flags[0].casefold()
    if sub == "repl":
        return True
    return any(_names_stdin(tok) for tok in texts)


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
    if family == "deno":
        return _deno_stdin_runs_as_code(tokens)
    grammar = _GETOPT_GRAMMAR.get(family)
    if grammar is None:
        return False
    texts = [str(token) for token in tokens]
    # A reading depends only on where it resumes and whether a stdin flag
    # (or ruby -S PATH search) was seen, so each state is walked once.
    pending: list[tuple[int, bool, bool]] = [(0, False, False)]
    seen: set[tuple[int, bool, bool]] = set()
    while pending:
        state = pending.pop()
        if state in seen:
            continue
        seen.add(state)
        if _reading_runs_stdin(
            family, grammar, texts, state[0], state[1], state[2], pending,
        ):
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
    ruby_path_search: bool,
    pending: list[tuple[int, bool, bool]],
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
            if forced or index + 1 >= len(texts) or _names_stdin(texts[index + 1]):
                return True
            return ruby_path_search and program_name(texts[index + 1]) in _RUBY_STDIN_REPL_SCRIPTS
        consumed = 0
        if text.startswith("--"):
            name, equals, _ = text.partition("=")
            if family == "node":
                name = name.replace("_", "-")
            forced = forced or name in grammar.long_stdin
            if not equals and (grammar.long_value is None or name in grammar.long_value):
                consumed = 1
        elif text.startswith(grammar.option_prefixes) and len(text) > 1:
            cluster = text[1:]
            for position, letter in enumerate(cluster):
                if letter in grammar.stdin:
                    forced = True
                if family == "ruby" and letter == "S":
                    ruby_path_search = True
                if letter in grammar.next_value:
                    consumed += 1
                    continue
                if letter in grammar.value:
                    attached = cluster[position + 1:]
                    if family == "python" and letter == "m":
                        # -m takes the module and leaves the rest of argv to
                        # it; a missing or `-` module is refused, as is any
                        # submodule of the REPL modules (`code.__main__`).
                        module = attached or (
                            texts[index + 1 + consumed]
                            if index + 1 + consumed < len(texts) else "-"
                        )
                        top_module = module.strip().split(".", 1)[0].casefold()
                        return forced or module == "-" or top_module in _PYTHON_STDIN_REPL_MODULES
                    if not attached:
                        consumed += 1
                    break
        else:
            if ruby_path_search and program_name(text) in _RUBY_STDIN_REPL_SCRIPTS:
                return True
            return forced
        for skipped in range(consumed):
            pending.append((index + 1 + skipped, forced, ruby_path_search))
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
    "forfiles", "wmic", "schtasks", "runas", "mshta", "rundll32",
    "wscript", "cscript", "regsvr32", "msiexec", "certutil", "bitsadmin",
    "wsl", "wslg", "pcalua", "conhost", "syncappvpublishingserver",
    "cmstp", "infdefaultinstall", "odbcconf", "scriptrunner", "mavinject",
    "regasm", "regsvcs", "installutil", "ssh", "ftp", "diskshadow",
)
_COMMAND_STRING_LAUNCHER_RE = re.compile(
    r"(?:%s)%s" % ("|".join(_COMMAND_STRING_LAUNCHERS), _WIN_EXT)
)
# Short two-letter Windows launchers that also collide with common English
# words or test-filter tokens (`echo look at this`, `pytest -k at`). Refused
# in command position (index 0, after a command-launching wrapper, or with an
# explicit path/extension) rather than as plain data arguments.
_COMMAND_POS_ONLY_LAUNCHERS = ("at", "sc", "hh")
_COMMAND_POS_ONLY_LAUNCHER_RE = re.compile(
    r"(?:%s)%s" % ("|".join(_COMMAND_POS_ONLY_LAUNCHERS), _WIN_EXT)
)
_CMD_WRAPPER_VERBS = frozenset({"call", "start", "if", "else", "do"})
_LAUNCHER_WRAPPER_VERBS = _CMD_WRAPPER_VERBS | frozenset({
    "cmd", "uv", "npx", "pnpx", "bunx", "env", "sudo", "xargs", "run",
    "exec", "nice", "nohup", "timeout", "time", "stdbuf", "setsid",
})
_WIN_EXEC_EXTS = (".exe", ".cmd", ".bat", ".com")

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


def program_name_candidates(
    token: object, *, include_slash_prefixes: bool = False,
) -> tuple[str, ...]:
    """Every normalised program name ``token`` can name once cmd.exe reads it.

    The whole token, and each piece between cmd.exe's name delimiters ``=``,
    ``,``, ``;`` and ``(`` with leading ``@`` stripped: ``call =python``,
    ``if exist x=python``, ``start @python`` and ``call cmd(/c ...)`` all
    reach the command name. When ``include_slash_prefixes`` is True, prefixes
    before ``/`` (``forfiles/m``, ``C:/Windows/System32/forfiles.exe/c``) are
    also yielded. Each candidate goes through :func:`program_name`.
    """
    text = str(token)
    names: list[str] = []
    for raw in (text, *re.split(r"[=,;(]", text)):
        cleaned = raw.strip().lstrip(_CMD_NAME_LEADERS)
        name = program_name(cleaned)
        if name and name not in names:
            names.append(name)
        if include_slash_prefixes and "/" in cleaned:
            pos = 0
            while True:
                slash = cleaned.find("/", pos)
                if slash < 0:
                    break
                prefix_name = program_name(cleaned[:slash])
                if prefix_name and prefix_name not in names:
                    names.append(prefix_name)
                pos = slash + 1
    return tuple(names)


def _token_has_path_or_exec_ext(token: str) -> bool:
    """Whether ``token`` carries a path separator or Windows executable suffix."""
    lowered = token.strip().rstrip(". ").casefold()
    return (
        "/" in lowered
        or "\\" in lowered
        or lowered.endswith(_WIN_EXEC_EXTS)
    )


def command_string_launcher(tokens: Sequence[object]) -> str:
    """The first command-string launcher any of ``tokens`` names, or ''.

    Names are matched after :func:`program_name_candidates` normalisation
    (case, path, ``.exe``/``.cmd``/``.bat``/``.com``, cmd.exe delimiters and
    glued ``/`` switches), the same way the interpreter families are. Short
    two-letter Windows launchers (``at``, ``sc``, ``hh``) are matched only in
    command position (index 0, after a command-launching wrapper, or with an
    explicit path/extension) so ordinary text arguments such as ``echo look at
    this`` or ``pytest -k at`` are not falsely rejected.
    """
    wrapper_seen = False
    for index, token in enumerate(tokens):
        raw_text = str(token)
        candidates = program_name_candidates(raw_text, include_slash_prefixes=True)
        for name in candidates:
            if _COMMAND_STRING_LAUNCHER_RE.fullmatch(name):
                return name
        in_cmd_pos = (
            index == 0
            or wrapper_seen
            or _token_has_path_or_exec_ext(raw_text)
        )
        if in_cmd_pos:
            for name in candidates:
                if _COMMAND_POS_ONLY_LAUNCHER_RE.fullmatch(name):
                    return name
        if any(
            name in _LAUNCHER_WRAPPER_VERBS
            or any(name.endswith(ext) and name[:-len(ext)] in _LAUNCHER_WRAPPER_VERBS for ext in _WIN_EXEC_EXTS)
            for name in candidates
        ):
            wrapper_seen = True
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
    """Split ``cmd/c``, ``cmd.exe/d/c``, ``powershell/c``, ``powershell/nop``."""
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
                is_verb
                or fam == "cmd"
                or token_runs_inline_code(fam, suffix)
                or (
                    fam in _POWERSHELL_FAMILIES
                    and bool(_powershell_parameter_kind(fam, _powershell_parameter_name(suffix, fam)))
                )
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
    for idx, tok in enumerate(tokens):
        if "/" in tok:
            head, tail = tok.split("/", 1)
            if program_name(head.lstrip(_CMD_NAME_LEADERS)) == "for" and tail[:1].casefold() in {"f", "d", "l", "r"}:
                return True
        if idx + 1 >= len(tokens):
            continue
        if program_name(tok.split("/", 1)[0].lstrip(_CMD_NAME_LEADERS)) != "for":
            continue
        nxt = tokens[idx + 1].casefold()
        if nxt in {"/f", "/d", "/l", "/r"} or (
            len(nxt) >= 2 and nxt[0] == "%" and nxt[1].isalpha()
        ):
            return True
    return False


def _part_names_cmd_target(part: str) -> bool:
    """Whether an ``=``-separated piece names an interpreter, launcher or ``for``."""
    cleaned = part.strip().lstrip(_CMD_NAME_LEADERS)
    if not cleaned:
        return False
    for name in program_name_candidates(cleaned, include_slash_prefixes=True):
        if (
            name == "for"
            or interpreter_family(name)
            or _COMMAND_STRING_LAUNCHER_RE.fullmatch(name)
            or _COMMAND_POS_ONLY_LAUNCHER_RE.fullmatch(name)
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
    first_verb = program_name(tokens[0].split("=", 1)[0].split("/", 1)[0])
    has_wrapper = first_verb in _CMD_WRAPPER_VERBS
    expanded: list[str] = []
    for idx, tok in enumerate(tokens):
        is_verb = idx == 0 or has_wrapper
        if "=" in tok and not tok.startswith(("-", "/")):
            parts = [part for part in tok.split("=") if part]
            if any(_part_names_cmd_target(part) for part in parts):
                for part_idx, part in enumerate(parts):
                    part_verb = is_verb or part_idx == 0
                    if "/" in part and not interpreter_family(program_name(part)):
                        split_part = _split_glued_windows_switch(part, is_verb=part_verb)
                        if split_part is not None:
                            expanded.extend(split_part)
                            continue
                    expanded.append(part)
                continue
        if "/" in tok and not interpreter_family(program_name(tok)):
            split_result = _split_glued_windows_switch(tok, is_verb=is_verb)
            if split_result is not None:
                expanded.extend(split_result)
                continue
        expanded.append(tok)
    if _has_cmd_for_loop(expanded):
        return tuple(expanded), True
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
        nonlocal cur_stdin, next_from_pipe
        if not cur_tokens and not cur_has_token:
            if next_from_pipe:
                cur_stdin = True
                next_from_pipe = False

    def _flush_word() -> None:
        nonlocal cur_has_token, cur_quoted, expect_redir_target, last_closed_group
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
            last_closed_group = None

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

        # cmd.exe delimits redirection filenames on space, tab, `,`, `;` and
        # `=` in both readings (`>nul=python -c ...`, `>nul,python -Wx,y`).
        if expect_redir_target and ch in " \t,=;":
            if cur_has_token:
                _flush_word()
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
            last_closed_group = None
            next_from_pipe = False
            i += 1
            continue

        if ch == "&":
            _flush_segment()
            last_closed_group = None
            next_from_pipe = False
            i += 2 if i + 1 < length and command[i + 1] == "&" else 1
            continue

        if ch == "|":
            _flush_segment()
            last_closed_group = None
            if i + 1 < length and command[i + 1] == "|":
                next_from_pipe = False
                i += 2
            else:
                next_from_pipe = True
                i += 1
            continue

        if ch == "(":
            _flush_segment()
            last_closed_group = None
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


class BuildGuardPlan(NamedTuple):
    """Guard verdict and path-confinement tokens extracted from ``build_run``."""

    opaque_reason: str
    launcher: str
    inline_violation: bool
    stdin_violation: bool
    path_tokens: tuple[str, ...]
    program_tokens: tuple[str, ...]


def _collect_segment_invocations(
    seg_list: Sequence[str], seg_stdin: bool,
) -> list[tuple[str, list[str], bool]]:
    """Collect every interpreter invocation named in one command segment."""
    found: list[tuple[str, list[str], bool]] = []
    for index, token in enumerate(seg_list):
        trailing = list(seg_list[index + 1:])
        if "(" in token:
            head, rest = token.split("(", 1)
            paren_fam = interpreter_family(program_name(head.strip().lstrip(_CMD_NAME_LEADERS)))
            if paren_fam:
                found.append((paren_fam, ["(" + rest, *trailing], seg_stdin))
        for name in program_name_candidates(token):
            family = interpreter_family(name)
            if family:
                found.append((family, trailing, seg_stdin))
    return found


def analyze_build_command(
    command_text: str, *, stdin_present: bool, is_windows: bool,
) -> BuildGuardPlan:
    """Evaluate a ``build_run`` command string under cmd, child-CRT and POSIX readings."""
    command_argv = command_text.split()
    readings = (
        parse_build_command(command_text),
        parse_build_command(command_text, child_tokens=True),
    )
    for parsed in readings:
        if parsed.opaque:
            return BuildGuardPlan(parsed.reason, "", False, False, (), ())

    path_tokens: list[str] = list(command_argv[1:])
    path_tokens.extend(readings[0].redir_targets)
    program_tokens: list[str] = list(command_argv[:1])
    invocations: list[tuple[str, list[str], bool]] = []
    launcher = ""

    for parsed in readings:
        for seg_tokens, seg_receives_stdin in parsed.segments:
            seg_stdin = stdin_present or seg_receives_stdin
            seg_list = list(seg_tokens)
            if not launcher:
                launcher = command_string_launcher(seg_list)
            if seg_list:
                program_tokens.append(seg_list[0])
            path_tokens.extend(seg_list[1:])
            invocations.extend(_collect_segment_invocations(seg_list, seg_stdin))

    if not is_windows and command_argv:
        # On POSIX harness_tools executes command.split() directly with no shell,
        # so `|` and `&` stay literal inside argv tokens (`python -W x|y -c ...`).
        if not launcher:
            launcher = command_string_launcher(command_argv)
        invocations.extend(_collect_segment_invocations(command_argv, stdin_present))

    inline_violation = any(
        runs_inline_code(family, tokens) for family, tokens, _ in invocations
    )
    stdin_violation = any(
        has_stdin and stdin_runs_as_code(family, tokens)
        for family, tokens, has_stdin in invocations
    )
    return BuildGuardPlan(
        opaque_reason="",
        launcher=launcher,
        inline_violation=inline_violation,
        stdin_violation=stdin_violation,
        path_tokens=tuple(path_tokens),
        program_tokens=tuple(program_tokens),
    )


def analyze_argv_command(
    program: str, argv: Sequence[str], *, stdin_present: bool,
) -> tuple[str, str]:
    """Evaluate an argv-style command (``workspace_run`` / ``run_program``).

    Returns ``("launcher", name)``, ``("inline", family)``,
    ``("stdin", family)``, or ``("", "")`` when allowed.
    """
    argv_list = [str(item) for item in argv]
    launcher_tokens = [program, *argv_list]
    launcher = command_string_launcher(launcher_tokens)
    if launcher:
        return "launcher", launcher
    invocations: list[tuple[str, list[str]]] = []
    direct_family = interpreter_family(program_name(program))
    if direct_family:
        invocations.append((direct_family, argv_list))
    for index, token in enumerate(argv_list):
        family = interpreter_family(program_name(token))
        if family:
            invocations.append((family, argv_list[index + 1:]))
    for family, tokens in invocations:
        if runs_inline_code(family, tokens):
            return "inline", family
    if stdin_present:
        for family, tokens in invocations:
            if stdin_runs_as_code(family, tokens):
                return "stdin", family
    return "", ""


def expand_argv_path_candidates(
    raw_item: object, *, is_windows: bool,
) -> tuple[bool, tuple[str, ...]]:
    """Expand one argv token into (network_violation, path_candidates).

    Checks both the whole token and every value after ``=`` (for both option
    spellings like ``--out=../x`` and non-option spellings like
    ``type=..\\outside.txt`` or ``dd of=../../outside.txt``), strips leading
    ``@``, ``=``, ``,`` and ``;``, rejects embedded UNC/device spellings, and
    expands glued option prefixes (``-C../..``, ``/Fe../x.exe``).
    """
    token = str(raw_item or "").strip().strip('"\'')
    if not token:
        return False, ()
    readings: list[str] = [token]
    if "=" in token:
        for part in token.split("=")[1:]:
            cleaned_part = part.strip().strip('"\'')
            if cleaned_part and cleaned_part not in readings:
                readings.append(cleaned_part)
    all_candidates: list[str] = []
    for reading in readings:
        raw = reading.lstrip("@=,;")
        if not raw:
            continue
        if _network_spelling.contains_network_spelling(raw):
            return True, ()
        candidates = [raw]
        switch = is_windows and raw.startswith("/")
        if raw.startswith("-") or switch:
            stop = re.search(r"[.~/\\=]|[A-Za-z]:[\\/]", raw[1:])
            if stop:
                glued = [raw[start:] for start in range(1, stop.start() + 2)]
                candidates = ([raw] if switch and "=" not in raw else []) + glued
        if any(_network_spelling.is_network_spelling(value) for value in candidates):
            return True, ()
        for candidate in candidates:
            if candidate not in all_candidates:
                all_candidates.append(candidate)
    return False, tuple(all_candidates)
