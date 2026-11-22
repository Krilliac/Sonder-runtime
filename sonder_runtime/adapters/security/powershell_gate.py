"""Select executable PowerShell inputs for the existing permission decider.

Only execution tools participate. Mentioning PowerShell in chat, a file write,
or a tool description must never launch a parser or change its catalog grade.
This is a syntax inspection, not a sandbox for programs called by literal name.
"""
from __future__ import annotations

import json
import ntpath
import re
from collections.abc import Mapping

from ...domain.security.delete_calls import HOST_PROGRAM_TOOLS
from .powershell_ast import PowerShellInspection, inspect_powershell

_LANGUAGES = frozenset({"powershell", "pwsh", "ps1"})
_CODE_TOOLS = frozenset({"run_code", "runwindow"})
_ARGV_TOOLS = frozenset({"workspace_run", "run_program", "isolated_run"})
_SCRIPT_TOOLS = frozenset({"script_run", "run_script"})
_TOOLS = _CODE_TOOLS | _ARGV_TOOLS | _SCRIPT_TOOLS | {
    "parallel_run_code", "build_run", "run_project",
}
# An optionally quoted, optionally path-qualified PowerShell name at the very
# start of a command.  Unquoted paths start at a token boundary so matching
# stays linear in the input.
_PS_LEADING = re.compile(r'''(?ix)\A\s*
    (?:"[^"\r\n]*[\\/]
      |'[^'\r\n]*[\\/]
      |["']?(?<![^\s"'|&()<>@;,=])[^\s"'|&()<>@;,=]*[\\/]
      |["']?(?<![\w.$\\/-])
    )(?:powershell|pwsh|powers~\d+)(?:\.exe)?(?![\w.-])''')
# A word-bounded literal PowerShell name anywhere in a cmd.exe command line,
# on any line, in any position (after redirections, groups, if/for/start/cmd,
# quotes).  ``/c`` style switches end at the name (``cmd/cpwsh``).  Trailing
# dots and ``\.`` components count because Win32 path normalization (which
# ``start`` applies) drops them (``powershell.exe.``, ``powershell.exe\.``),
# and an 8.3 short name (``POWERS~1``) names the same file.  A directory
# component followed by a backslash and a name (``\PowerShell\7\pwsh.exe``)
# cannot be run as a command.  Names produced by ``%``/``!`` expansion are
# covered by ``_cmd_may_expand``; renamed copies and links are not recognised.
_PS_WORD = re.compile(
    r'''(?i)(?:(?<![\w.$-])|(?<=/[a-z]))(?:powershell|pwsh|powers~\d+)(?:\.exe)?'''
    r'''(?![\w-]|\.\w|\\(?!\.+(?:[\s"'&|<>()\\/]|\Z)))''')
# Carets and quotes can split a command word for cmd.exe (p^wsh, "pw"sh);
# names that appear once they are removed are counted too (fail closed).
_CMD_NOISE = re.compile(r'[\^"]')


def _cmd_may_expand(command):
    """True when cmd.exe expansion could rewrite any word of *command*.

    A pair of ``%`` signs can expand a variable, a substring of one or a
    ``for`` variable anywhere in the line, including inside a command word
    (``pw%PATHEXT:~0,0%sh``, ``set X=pw&call %X%sh``, ``for %a in (pw) do
    %ash``); a pair of ``!`` signs does the same under delayed expansion
    (``cmd /v:on``).  The result can be any text, so no name check rules
    PowerShell out.  A lone ``%`` or ``!`` is left as typed.
    """
    return command.count("%") >= 2 or command.count("!") >= 2


def _launch_ends(command):
    """End offsets of PowerShell names in a cmd.exe command line.

    Returns one entry per occurrence in the raw text, plus a sentinel ``-1``
    for each extra occurrence that only appears once cmd.exe strips carets and
    quotes, so callers can count every possible launcher.
    """
    ends = [match.end() for match in _PS_WORD.finditer(command)]
    hidden = len(_PS_WORD.findall(_CMD_NOISE.sub("", command)))
    return ends + [-1] * max(0, hidden - len(ends))


def _opaque(reason):
    return PowerShellInspection(False, reason)


class ApprovalRequired(str):
    """A skipped generated candidate, distinct from executed program output."""


def run_generated_code(run, code, *, language="python", **kwargs):
    """Inspect generated PowerShell immediately before entering its runner.

    Generation workers have no interactive approval channel. Skip opaque
    candidates rather than transferring approval of a generator to unseen code.
    The caller may submit the resulting source to the ordinary source-aware
    run_code approval surface. Other languages keep the existing runner path.
    """
    if str(language).strip().lower() in _LANGUAGES and kwargs.get("execute", True):
        extra = kwargs.get("extra", "")
        source = code + (("\n\n" + extra) if extra else "")
        inspection = inspect_powershell(source)
        if not inspection.inspectable:
            return False, ApprovalRequired("requires approval: " + inspection.reason)
    return run(code, language=language, **kwargs)


def loop_arguments(arguments):
    """Mirror the loop's aliases without altering the approval's call digest."""
    if not isinstance(arguments, Mapping):
        return arguments
    result = dict(arguments)
    if "args" in arguments:
        result["args_json"] = arguments["args"]
    for key in ("files", "commands"):
        if arguments.get(key):
            result[key + "_json"] = arguments[key]
    return result


def runnable_arguments(sources, extract):
    """Bind exactly the first runnable block selected by a console surface."""
    for source in sources:
        block = extract(source)
        if block is not None:
            return {"code": block["code"], "language": block["language"]}
    return None


def _leaf_name(value):
    """Return the final path component after Win32 trailing dot/space normalization."""
    if not isinstance(value, str):
        return ""
    parts = re.split(r"[\\/]", value.strip())
    while len(parts) > 1 and re.fullmatch(r"[. ]*", parts[-1]):
        parts.pop()
    return parts[-1].rstrip(". ")


def _is_powershell(value):
    """True when the program path *value* names a PowerShell executable.

    CreateProcess normalizes the path before running it, which drops
    trailing ``.`` components and trailing dots and spaces
    (``powershell.exe\\.``, ``pwsh.exe.``); an 8.3 short name
    (``POWERS~1.EXE``) names the same file.  Renamed copies and links are not
    recognised.
    """
    return bool(re.fullmatch(r"(?i)(?:powershell|pwsh|powers~\d+)(?:\.exe)?", _leaf_name(value)))


def _is_ps1_path(value):
    """True when *value* names a ``.ps1`` script after Win32 path normalization."""
    return _leaf_name(value).lower().endswith(".ps1")


def _array(value):
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        raise ValueError("expected argv list")
    return value


def _quote(value):
    return "'" + value.replace("'", "''") + "'"


def _script(path):
    # Use the same workspace resolver as the runner; caller-supplied approval
    # knobs never authorize a new read while deciding whether to ask.
    from ..filesystem import workbench

    try:
        target = workbench._resolve(path)
        with target.open("rb") as stream:
            data = stream.read(256 * 1024 + 1)
        if len(data) > 256 * 1024:
            return _opaque("script exceeds inspection limit")
        encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        return inspect_powershell(data.decode(encoding))
    except (OSError, ValueError, UnicodeError):
        return _opaque("script source unavailable for inspection")


def _argv(argv, stdin="", *, local=True, script_reader=None):
    if not argv or not _is_powershell(argv[0]):
        return None
    if not all(isinstance(item, str) for item in argv):
        return _opaque("invalid PowerShell argv")
    # Each native argv element is data, not a fragment to interpolate as code.
    result = inspect_powershell("& " + " ".join(_quote(item) for item in argv))
    if not result.inspectable and result.reason not in {
        "external PowerShell script needs inspection", "PowerShell command reads unseen stdin",
    }:
        return result
    for index, argument in enumerate(argv[1:], 1):
        flag = argument.lower().translate(str.maketrans({"\u2013": "-", "\u2014": "-", "\u2015": "-"}))
        if flag.startswith("/"):
            flag = "-" + flag[1:]
        if flag.startswith("-") and "-file".startswith(flag) and len(flag) > 1:
            if index + 1 >= len(argv):
                return _opaque("missing PowerShell file operand")
            path = argv[index + 1]
            if path == "-":
                return inspect_powershell(stdin)
            if script_reader is not None:
                return script_reader(path)
            return _script(path) if local else _opaque("container script source unavailable")
        if flag.startswith("-") and "-command".startswith(flag) and len(flag) > 1:
            if index + 1 < len(argv) and argv[index + 1] == "-":
                return inspect_powershell(stdin)
            return result
    # No command switch: PowerShell reads stdin or a positional script path.
    positional = next((item for item in argv[1:] if _is_ps1_path(item)), "")
    if positional:
        if script_reader is not None:
            return script_reader(positional)
        return _script(positional) if local else _opaque("container script source unavailable")
    return inspect_powershell(stdin) if stdin else result


def _project(arguments):
    from ..execution_tools import code_runner

    commands = code_runner._commands_from_json(arguments.get("commands_json") or arguments.get("commands"))
    if commands is None:
        return None  # auto-detection has no PowerShell runner
    last = None
    for command in commands:
        if not _is_powershell(command["cmd"][0]):
            continue

        def read_project(path, cwd=command["cwd"]):
            wanted = ntpath.normpath(ntpath.join(cwd, path)).replace("\\", "/")
            files = code_runner._project_files_from_json(arguments.get("files_json") or arguments.get("files"))
            for item in files:
                if ntpath.normpath(item["path"]).replace("\\", "/") == wanted:
                    return inspect_powershell(item["content"])
            return _opaque("project PowerShell source unavailable")

        last = _argv(command["cmd"], arguments.get("stdin", ""), script_reader=read_project)
        if last is not None and not last.inspectable:
            return last
    return last


def _selects_powershell(name, arguments):
    if name in _CODE_TOOLS:
        return str(arguments.get("language") or "python").strip().lower() in _LANGUAGES
    if name == "run_project":
        from ..execution_tools import code_runner

        commands = code_runner._commands_from_json(arguments.get("commands_json") or arguments.get("commands"))
        return any(_is_powershell(command["cmd"][0]) for command in commands or ())
    if name in _SCRIPT_TOOLS:
        return _is_ps1_path(arguments.get("path", ""))
    if name == "isolated_run":
        argv = _array(arguments.get("argv_json", []))
        return bool(argv) and _is_powershell(argv[0])
    if name in _ARGV_TOOLS:
        return _is_powershell(arguments.get("program", ""))
    if name == "parallel_run_code":
        return any(
            isinstance(job, dict)
            and str(job.get("language") or job.get("lang") or "python").strip().lower() in _LANGUAGES
            for job in _array(arguments.get("jobs_json", []))
        )
    if name == "build_run":
        command = arguments.get("command", "")
        return isinstance(command, str) and (bool(_launch_ends(command)) or _cmd_may_expand(command))
    return False


def powershell_arguments(tool_name, arguments):
    """Return ``arguments`` only when the call would or could launch PowerShell.

    Surfaces that historically decided without arguments (console catalogue,
    loop actions, ``/run`` aliases) bind arguments for PowerShell calls only, so
    every other call keeps its original argument-free digest and decision.
    Selection never launches the parser; malformed PowerShell-looking inputs
    and build commands whose cmd.exe expansions could produce a launcher are
    forwarded so the decider fails closed on them.
    """
    name = str(tool_name or "").strip().lstrip("/")
    if name not in _TOOLS or not isinstance(arguments, Mapping):
        return None
    try:
        return arguments if _selects_powershell(name, arguments) else None
    except (TypeError, ValueError, KeyError, IndexError, RecursionError):
        return arguments


RUN_COMMANDS = ("/run", "/runwindow", "/runnew", "/runconsole")


def slash_run_arguments(cmd, sources, extract):
    """Arguments a ``/run`` alias binds at the gate: the PowerShell block, else None.

    ``sources`` is a zero-argument callable so other commands never walk the
    conversation history.
    """
    if cmd not in RUN_COMMANDS:
        return None
    return runnable_powershell(runnable_arguments(sources(), extract))


def loop_gate_arguments(tool_name, action):
    """The loop action to bind at the gate: the action for PowerShell, else None."""
    if powershell_arguments(tool_name, loop_arguments(action)) is None:
        return None
    return action


def loop_decision_arguments(tool_name, action):
    """The arguments the loop surface's permission decision binds.

    PowerShell binds the action as sent (:func:`loop_gate_arguments`). A
    host-program tool can delete files, so it binds its aliased arguments and
    the delete ask sees what would run. Any other tool decides without them.
    """
    gate_arguments = loop_gate_arguments(tool_name, action)
    if gate_arguments is None and tool_name in HOST_PROGRAM_TOOLS:
        return loop_arguments(action)
    return gate_arguments


def runnable_powershell(block):
    """The ``{code, language}`` of a selected runnable block, if PowerShell."""
    if block and str(block.get("language") or "").strip().lower() in _LANGUAGES:
        return {"code": block["code"], "language": block["language"]}
    return None


def inspect_tool_call(tool_name, arguments):
    """Return an inspection for PowerShell, or None for an unaffected call.

    Argumentless catalog/preflight decisions retain their current grade. Live
    surfaces must pass executable inputs (the REPL explicitly binds its last
    runnable block). Failures cannot become a more permissive result.
    """
    name = str(tool_name or "").strip().lstrip("/")
    if name not in _TOOLS or not isinstance(arguments, Mapping):
        return None
    try:
        if name in _CODE_TOOLS:
            if str(arguments.get("language") or "python").strip().lower() in _LANGUAGES:
                return inspect_powershell(arguments.get("code", ""))
        elif name == "run_project":
            return _project(arguments)
        elif name in _SCRIPT_TOOLS:
            path = arguments.get("path", "")
            if _is_ps1_path(path):
                return _script(path)
        elif name in _ARGV_TOOLS:
            if name == "isolated_run":
                argv = _array(arguments.get("argv_json", []))
            else:
                program = arguments.get("program", "")
                if not _is_powershell(program):
                    return None
                argv = [program, *_array(arguments.get("args_json", arguments.get("args", [])))]
            return _argv(argv, arguments.get("stdin", ""), local=name != "isolated_run")
        elif name == "parallel_run_code":
            jobs = _array(arguments.get("jobs_json", []))
            last = None
            for job in jobs:
                if not isinstance(job, dict):
                    continue  # string jobs are Python; invalid jobs never launch
                language = str(job.get("language") or job.get("lang") or "python").lower()
                if language.strip() not in _LANGUAGES:
                    continue
                source = str(job.get("code", "")) + "\n" + str(job.get("extra") or job.get("check") or "")
                last = inspect_powershell(source)
                if not last.inspectable:
                    return last
            return last
        elif name == "build_run":
            command = arguments.get("command", "")
            # Custom Windows builds run under cmd.exe; only explicit PS
            # launchers need PS inspection. Cmd expansions cannot be proven
            # equivalent to PowerShell AST tokenization, so they are opaque,
            # and checked before any name match because they can build one.
            if isinstance(command, str) and _cmd_may_expand(command):
                return _opaque("cmd.exe expansion may produce a PowerShell launcher")
            ends = _launch_ends(command) if isinstance(command, str) else []
            if ends:
                # Only a single launcher at the start of the command is
                # tokenized as PowerShell; any other occurrence is opaque.
                leading = _PS_LEADING.match(command)
                rest = command[leading.end():] if leading is not None else ""
                if (len(ends) != 1 or leading is None or leading.end() != ends[0]
                        or not re.match(r'''["']?(?:\s|\Z)''', rest)
                        or _PS_WORD.search(_CMD_NOISE.sub("", rest))):
                    return _opaque("PowerShell launcher outside command start")
                if any(char in command for char in ("%", "!", "^", "`")):
                    return _opaque("shell expansion around PowerShell")
                if re.match(r"\s*(?:cmd|call|start)\b", command, re.I):
                    return _opaque("nested shell around PowerShell")
                return inspect_powershell(command)
    except (TypeError, ValueError, RecursionError):
        return _opaque("invalid PowerShell execution arguments")
    return None
