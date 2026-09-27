"""REPL presentation for the C++ build tools: ``/build`` and ``/fix-build``.

Root-free and adapter-free. Every command becomes one typed tool call through
``execute_tool(tool_name, arguments) -> Mapping`` (the REPL lane supplies it,
routed through the typed gateway after the console's own permission prompt);
nothing here reaches the build services directly, so the console, the native
MCP surface and HTTP share one schema, one permission decision and one
receipt per call.

Wiring (owned by the REPL lane, docs/architecture/CPP-BUILD-FIX.md):

* ``repl.py`` has one branch per command in its slash chain (so the catalog
  and the permission-gate map can read them); each branch runs
  ``BuildReplFacade.dispatch`` after the console's gate answered, and renders
  the returned ``BuildOutcome`` (``summary_rows``/``list_sections``) with the
  console's notice/table/footer components. ``register_build_commands``
  installs the same handlers for a host that registers commands instead;
* ``command_catalog.py`` maps each command to the typed tools it fronts
  (``BUILD_COMMAND_SPECS.tools``), which grade its risk, and narrows the read
  forms (``/build model``, ``/build status``, ``/fix-build status``) to the
  safe member they reach.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Any, Callable, Mapping

NOT_COMPOSED = "build tools are not composed in this runtime"
BUILD_USAGE = (
    "usage: /build model [--dir D] [--preset P] [--detail summary|targets|compile_units|"
    "toolchain|presets] [--target T] [--refresh]\n"
    "       /build run [configure | compile <file> | <target>] [--config C] [--platform P] "
    "[--preset P] [--build-preset P] [--dir D] [--generator G] [--jobs N] [--timeout S] "
    "[--wait S] [--allow-network]\n"
    "       /build trace <file> [--config C] [--dir D]\n"
    "       /build status|cancel <build-job-id> [--wait S]"
)
FIX_USAGE = (
    "usage: /fix-build <target> [--config C] [--platform P] [--file F] [--attempts N] "
    "[--revert-after] [--verify-dependents] [--dir D] [--wait S]\n"
    "       /fix-build status|result|cancel <build-fix-id> [--wait S]\n"
    "       /fix-build restore <build-fix-id> [file...]"
)
RESTORE_USAGE = "usage: /fix-build restore <build-fix-id> [file...]"


@dataclass(frozen=True)
class BuildCommandSpec:
    name: str
    usage: str
    summary: str
    tools: tuple[str, ...]


BUILD_COMMAND_SPECS = (
    BuildCommandSpec(
        "/build", "/build [model|run|status|cancel|trace] ...",
        "Describe, configure, build, compile one file or trace includes of a C/C++ project",
        ("build_model", "build_job", "build_job_result"),
    ),
    BuildCommandSpec(
        "/fix-build",
        "/fix-build <target> [--config C] [--platform P] [--file F] [--attempts N] "
        "[--revert-after] [--verify-dependents] | status|result|cancel|restore <job_id>",
        "Repair a failing C/C++ build target with a bounded, verified patch loop; "
        "restore writes a fix's stored original files back",
        ("build_fix", "build_fix_result", "build_fix_restore"),
    ),
)

_JOB_ID = re.compile(r"^build-job-[0-9a-f]{16,32}$")
_FIX_ID = re.compile(r"^build-fix-[0-9a-f]{16,32}$")
_FLAG_FIELDS = {
    "--dir": "build_dir", "--project": "project", "--preset": "preset",
    "--build-preset": "build_preset", "--config": "config", "--platform": "platform",
    "--generator": "generator", "--profile": "profile", "--target": "target",
    "--detail": "detail", "--file": "focus_file",
}
_INT_FLAGS = {"--jobs": "jobs", "--timeout": "timeout_seconds", "--wait": "wait_seconds",
              "--attempts": "attempts", "--max-items": "max_items"}
_BOOL_FLAGS = {"--refresh": "refresh", "--allow-network": "allow_network",
               "--revert-after": "revert_after", "--verify-dependents": "verify_dependents"}
_RENDER_MAX_CHARS = 12_000


class UsageError(ValueError):
    pass


def split_words(arg: str) -> list[str]:
    """Split like a shell, but keep backslashes (Windows paths)."""
    lexer = shlex.shlex(str(arg or ""), posix=True)
    lexer.whitespace_split = True
    lexer.escape = ""
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError as exc:
        raise UsageError(str(exc)) from None


def _options(words: list[str], allowed: frozenset[str]) -> tuple[list[str], dict]:
    positional: list[str] = []
    options: dict[str, Any] = {}
    index = 0
    while index < len(words):
        word = words[index]
        if word.startswith("--") and word not in allowed:
            raise UsageError("unknown option %s" % word[:40])
        if word in _BOOL_FLAGS:
            options[_BOOL_FLAGS[word]] = True
            index += 1
            continue
        if word in _FLAG_FIELDS or word in _INT_FLAGS:
            if index + 1 >= len(words):
                raise UsageError("%s needs a value" % word)
            value = words[index + 1]
            if word in _INT_FLAGS:
                if not re.fullmatch(r"\d{1,6}", value):
                    raise UsageError("%s needs a number" % word)
                options[_INT_FLAGS[word]] = int(value)
            else:
                options[_FLAG_FIELDS[word]] = value
            index += 2
            continue
        positional.append(word)
        index += 1
    return positional, options


_MODEL_FLAGS = frozenset({"--dir", "--project", "--preset", "--detail", "--target", "--refresh",
                          "--max-items"})
_RUN_FLAGS = frozenset({"--dir", "--project", "--preset", "--build-preset", "--config",
                        "--platform", "--generator", "--profile", "--jobs", "--timeout", "--wait",
                        "--allow-network"})
_TRACE_FLAGS = frozenset({"--dir", "--project", "--config", "--platform", "--wait"})
_JOB_FLAGS = frozenset({"--wait"})
_FIX_FLAGS = frozenset({"--dir", "--project", "--config", "--platform", "--file", "--attempts",
                        "--revert-after", "--verify-dependents", "--wait", "--timeout",
                        "--allow-network"})


def parse_build(arg: str) -> tuple[str, dict]:
    """``/build ...`` -> ``(tool_name, arguments)``; raises ``UsageError``."""
    words = split_words(arg)
    if not words:
        return "build_model", {}
    verb, rest = words[0].lower(), words[1:]
    if verb == "model":
        positional, options = _options(rest, _MODEL_FLAGS)
        if positional:
            raise UsageError("/build model takes options only")
        return "build_model", options
    if verb == "run":
        positional, options = _options(rest, _RUN_FLAGS)
        if positional[:1] == ["configure"]:
            if len(positional) != 1:
                raise UsageError("/build run configure takes options only")
            return "build_job", {**options, "action": "configure"}
        if positional[:1] == ["compile"]:
            if len(positional) != 2:
                raise UsageError("/build run compile <file>")
            return "build_job", {**options, "action": "compile_one", "file": positional[1]}
        if len(positional) > 1:
            raise UsageError("/build run takes one target")
        arguments = {**options, "action": "build"}
        if positional:
            arguments["target"] = positional[0]
        return "build_job", arguments
    if verb == "trace":
        positional, options = _options(rest, _TRACE_FLAGS)
        if len(positional) != 1:
            raise UsageError("/build trace <file>")
        return "build_job", {**options, "action": "include_trace", "file": positional[0]}
    if verb in ("status", "result", "cancel"):
        positional, options = _options(rest, _JOB_FLAGS)
        if len(positional) != 1 or not _JOB_ID.fullmatch(positional[0]):
            raise UsageError("/build %s <build-job-id>" % verb)
        arguments = {"job_id": positional[0], **options}
        if verb == "cancel":
            arguments = {"job_id": positional[0], "cancel": True}
        return "build_job_result", arguments
    raise UsageError("unknown /build action %s" % verb[:40])


def parse_fix(arg: str) -> tuple[str, dict]:
    """``/fix-build ...`` -> ``(tool_name, arguments)``; raises ``UsageError``."""
    words = split_words(arg)
    if not words:
        raise UsageError("/fix-build needs a target")
    if words[0].lower() == "restore" and len(words) >= 2 and _FIX_ID.fullmatch(words[1]):
        return _restore_call(words[1:])
    if words[0].lower() in ("status", "result", "cancel") and len(words) >= 2 \
            and _FIX_ID.fullmatch(words[1]):
        verb = words[0].lower()
        positional, options = _options(words[2:], _JOB_FLAGS)
        if positional:
            raise UsageError("/fix-build %s <build-fix-id>" % verb)
        if verb == "cancel":
            return "build_fix_result", {"job_id": words[1], "cancel": True}
        return "build_fix_result", {"job_id": words[1], **options}
    positional, options = _options(words, _FIX_FLAGS)
    if len(positional) != 1:
        raise UsageError("/fix-build takes exactly one target")
    return "build_fix", {**options, "target": positional[0]}


def parse_restore(arg: str) -> tuple[str, dict]:
    """``<build-fix-id> [file...]`` -> ``("build_fix_restore", arguments)``."""
    return _restore_call(split_words(arg))


def _restore_call(words: list[str]) -> tuple[str, dict]:
    if not words or not _FIX_ID.fullmatch(words[0]) or len(words) > 7:
        raise UsageError("/fix-build restore <build-fix-id> [file...]")
    if any(word.startswith("--") for word in words[1:]):
        raise UsageError("/fix-build restore takes file names only")
    arguments: dict[str, Any] = {"job_id": words[0]}
    if words[1:]:
        arguments["files"] = list(words[1:])
    return "build_fix_restore", arguments


# --- rendering ---------------------------------------------------------------------------


def _scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool)) and not isinstance(value, bytes)


_HEAD_KEYS = ("object", "job_id", "status", "stop_reason")
_ROW_KEYS = ("action", "system", "target", "config", "platform", "world", "network",
             "isolation_truth", "verification_scope", "exit_code", "duration_seconds",
             "command_digest")
_LIST_KEYS = ("first_errors", "attempts", "files", "targets", "notes")
_LIST_MAX_ITEMS = 24
_ITEM_MAX_CHARS = 240


def refusal(payload: Mapping[str, Any]) -> tuple[str, str] | None:
    """``(error_code, message)`` for a refused call, else None."""
    if not isinstance(payload, Mapping) or payload.get("ok") is not False:
        return None
    return str(payload.get("error_code") or "FAILED"), str(payload.get("message") or "")


def result_head(payload: Mapping[str, Any]) -> str:
    """``object job_id status stop_reason`` -- whichever are present."""
    return " ".join(str(payload.get(key)) for key in _HEAD_KEYS if payload.get(key))


def summary_rows(payload: Mapping[str, Any]) -> list[tuple[str, str]]:
    """The scalar facts of one result, in a fixed order, as ``(key, value)``."""
    rows: list[tuple[str, str]] = []
    for key in _ROW_KEYS:
        value = payload.get(key)
        if _scalar(value) and value != "":
            rows.append((key, str(value)))
    command = payload.get("display_command") or payload.get("display_argv")
    if isinstance(command, list) and command:
        rows.append(("command", " ".join(str(part) for part in command)))
    return rows


def list_sections(payload: Mapping[str, Any]) -> list[tuple[str, int, list[str]]]:
    """``(key, total, first items as one-line text)`` for each list the result carries."""
    sections: list[tuple[str, int, list[str]]] = []
    for key in _LIST_KEYS:
        items = payload.get(key)
        if not isinstance(items, list) or not items:
            continue
        lines = []
        for item in items[:_LIST_MAX_ITEMS]:
            if isinstance(item, Mapping):
                text = ", ".join("%s=%s" % (k, v) for k, v in item.items() if _scalar(v))
            else:
                text = str(item)
            lines.append(text[:_ITEM_MAX_CHARS])
        sections.append((key, len(items), lines))
    return sections


def render_result(tool: str, payload: Mapping[str, Any]) -> str:
    """Operator text for one typed result (the payload is already label-only)."""
    if not isinstance(payload, Mapping):
        return str(payload)[:_RENDER_MAX_CHARS]
    refused = refusal(payload)
    if refused is not None:
        code, message = refused
        return "%s refused: %s%s" % (tool, code, (" -- " + message) if message else "")
    lines: list[str] = []
    head = result_head(payload)
    if head:
        lines.append("%s: %s" % (tool, head))
    lines.extend("  %s: %s" % row for row in summary_rows(payload))
    for key, total, items in list_sections(payload):
        lines.append("  %s (%d):" % (key, total))
        lines.extend("    " + item for item in items)
    if payload.get("next"):
        lines.append("  next: %s" % payload["next"])
    if not lines:
        lines.append("%s: %s" % (tool, ", ".join(
            "%s=%s" % (k, v) for k, v in payload.items() if _scalar(v))))
    text = "\n".join(lines)
    if len(text) > _RENDER_MAX_CHARS:
        text = text[:_RENDER_MAX_CHARS].rstrip() + "\n... (cut)"
    return text


@dataclass(frozen=True)
class BuildOutcome:
    """One console command's result, for a surface that lays it out itself.

    ``kind`` is ``"result"`` (the tool answered), ``"refused"`` (the tool or
    the gateway refused: ``payload`` carries ``error_code``/``message``),
    ``"usage"`` (the line did not parse; nothing ran) or ``"unavailable"``
    (the build tools are not composed; nothing ran). ``text`` is always the
    plain rendering, for piped output and logs.
    """

    kind: str
    tool: str
    payload: Mapping[str, Any] | None
    text: str


_COMMANDS = {
    "/build": (lambda arg: parse_build(arg), BUILD_USAGE),
    "/fix-build": (lambda arg: parse_fix(arg), FIX_USAGE),
}


class BuildReplFacade:
    """Run REPL build commands only through ``execute_tool``."""

    def __init__(self, execute_tool: Callable[[str, dict], Mapping[str, Any]] | None) -> None:
        self._execute_tool = execute_tool

    def _run(self, parser: Callable[[str], tuple[str, dict]], usage: str, arg: str) -> str:
        return self._outcome(parser, usage, arg).text

    def _outcome(self, parser: Callable[[str], tuple[str, dict]], usage: str,
                 arg: str) -> BuildOutcome:
        if self._execute_tool is None:
            return BuildOutcome("unavailable", "", None, NOT_COMPOSED)
        try:
            tool, arguments = parser(arg)
        except UsageError as exc:
            return BuildOutcome("usage", "", None, "%s\n%s" % (exc, usage))
        payload = self._execute_tool(tool, arguments)
        kind = "refused" if isinstance(payload, Mapping) and refusal(payload) else "result"
        return BuildOutcome(kind, tool, payload if isinstance(payload, Mapping) else None,
                            render_result(tool, payload))

    def dispatch(self, command: str, arg: str) -> BuildOutcome:
        """Run ``/build`` or ``/fix-build`` and return the structured outcome."""
        entry = _COMMANDS.get(str(command or "").lower())
        if entry is None:
            raise ValueError("not a build command: %s" % str(command)[:40])
        parser, usage = entry
        return self._outcome(parser, usage, arg)

    def build(self, arg: str) -> str:
        return self._run(parse_build, BUILD_USAGE, arg)

    def fix_build(self, arg: str) -> str:
        return self._run(parse_fix, FIX_USAGE, arg)

    def fix_build_restore(self, arg: str) -> str:
        return self._run(parse_restore, RESTORE_USAGE, arg)


def usage_error(command: str, arg: str) -> str:
    """The usage text ``command`` answers for ``arg`` without any tool call, or "".

    Pure: the console asks this before its permission gate, so a malformed
    line is told how to type the command instead of being asked to approve
    (or refused) a build that would only have printed usage.
    """
    entry = _COMMANDS.get(str(command or "").lower())
    if entry is None:
        return ""
    parser, usage = entry
    try:
        parser(arg)
    except UsageError as exc:
        return "%s\n%s" % (exc, usage)
    return ""


def register_build_commands(register: Callable[[str, Callable[[str], str], BuildCommandSpec], Any],
                            *, facade_getter: Callable[[], BuildReplFacade | None]) -> None:
    """Install the commands; the facade is resolved per call."""
    def handler(method: str) -> Callable[[str], str]:
        def run(arg: str = "") -> str:
            facade = facade_getter()
            if facade is None:
                return NOT_COMPOSED
            return getattr(facade, method)(arg)
        return run

    methods = {"/build": "build", "/fix-build": "fix_build"}
    for spec in BUILD_COMMAND_SPECS:
        register(spec.name, handler(methods[spec.name]), spec)


__all__ = [
    "BUILD_COMMAND_SPECS", "BUILD_USAGE", "BuildCommandSpec", "BuildOutcome", "BuildReplFacade",
    "FIX_USAGE", "NOT_COMPOSED", "RESTORE_USAGE", "UsageError", "list_sections", "parse_build",
    "parse_fix", "parse_restore", "refusal", "register_build_commands", "render_result",
    "result_head", "split_words", "summary_rows", "usage_error",
]
