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

from .powershell_ast import PowerShellInspection, inspect_powershell

_LANGUAGES = frozenset({"powershell", "pwsh", "ps1"})
_CODE_TOOLS = frozenset({"run_code", "runwindow"})
_ARGV_TOOLS = frozenset({"workspace_run", "run_program", "isolated_run"})
_SCRIPT_TOOLS = frozenset({"script_run", "run_script"})
_TOOLS = _CODE_TOOLS | _ARGV_TOOLS | _SCRIPT_TOOLS | {
    "parallel_run_code", "build_run", "run_project",
}
_PS_LAUNCH = re.compile(
    r'''(?ix)(?:^|[&|]|\b(?:call|start|cmd\s+(?:/\w+\s+)*))\s*["']?
    (?:[^\r\n"'|&]*[\\/])?(?:powershell|pwsh)(?:\.exe)?(?=[\s"']|$)'''
)


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


def run_intent(run, gated, sources, extract, **kwargs):
    """Gate legacy natural PowerShell runs without changing other languages."""
    block = runnable_arguments(sources, extract)
    if block and str(block["language"]).lower() in _LANGUAGES:
        return gated("/run", **kwargs)
    return run(**kwargs)


def _is_powershell(value):
    return isinstance(value, str) and ntpath.basename(value).lower() in {
        "powershell", "powershell.exe", "pwsh", "pwsh.exe",
    }


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
    positional = next((item for item in argv[1:] if item.lower().endswith(".ps1")), "")
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
        path = arguments.get("path", "")
        return isinstance(path, str) and path.lower().endswith(".ps1")
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
        return isinstance(command, str) and bool(_PS_LAUNCH.search(command))
    return False


def powershell_arguments(tool_name, arguments):
    """Return ``arguments`` only when the call would launch PowerShell.

    Surfaces that historically decided without arguments (console catalogue,
    loop actions, ``/run`` aliases) bind arguments for PowerShell calls only, so
    every other call keeps its original argument-free digest and decision.
    Selection never launches the parser; malformed PowerShell-looking inputs
    are forwarded so the decider fails closed on them.
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
            if isinstance(path, str) and path.lower().endswith(".ps1"):
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
            # equivalent to PowerShell AST tokenization, so they are opaque.
            if isinstance(command, str) and _PS_LAUNCH.search(command):
                if any(char in command for char in ("%", "!", "^")):
                    return _opaque("shell expansion around PowerShell")
                if re.match(r"\s*(?:cmd|call|start)\b", command, re.I):
                    return _opaque("nested shell around PowerShell")
                return inspect_powershell(command)
    except (TypeError, ValueError, RecursionError):
        return _opaque("invalid PowerShell execution arguments")
    return None
