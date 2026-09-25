"""REPL presentation for the C++ build tools: ``/build``, ``/fix-build``, ``/fix-build-restore``.

Root-free and adapter-free. Every command becomes one typed tool call through
``execute_tool(tool_name, arguments) -> Mapping`` (the REPL lane supplies it,
routed through the typed gateway after the console's own permission prompt);
nothing here reaches the build services directly, so the console, the native
MCP surface and HTTP share one schema, one permission decision and one
receipt per call.

Wiring (owned by the REPL lane, docs/architecture/CPP-BUILD-FIX.md):

* ``repl.py`` calls ``register_build_commands(register, facade_getter=...)``
  where ``register(name, handler, spec)`` installs ``handler(arg) -> str``;
* ``command_catalog.py`` lists ``BUILD_COMMAND_SPECS`` (name, usage, summary,
  and the typed tools each command fronts, which grade its risk).
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
    "       /fix-build status|cancel <build-fix-id> [--wait S]"
)
RESTORE_USAGE = "usage: /fix-build-restore <build-fix-id> [file...]"


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
        "[--revert-after] [--verify-dependents]",
        "Repair a failing C/C++ build target with a bounded, verified patch loop",
        ("build_fix", "build_fix_result"),
    ),
    BuildCommandSpec(
        "/fix-build-restore", "/fix-build-restore <job_id> [file...]",
        "Write a build fix's stored original files back",
        ("build_fix_restore",),
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
    words = split_words(arg)
    if not words or not _FIX_ID.fullmatch(words[0]) or len(words) > 7:
        raise UsageError("/fix-build-restore <build-fix-id> [file...]")
    if any(word.startswith("--") for word in words[1:]):
        raise UsageError("/fix-build-restore takes file names only")
    arguments: dict[str, Any] = {"job_id": words[0]}
    if words[1:]:
        arguments["files"] = list(words[1:])
    return "build_fix_restore", arguments


# --- rendering ---------------------------------------------------------------------------


def _scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool)) and not isinstance(value, bytes)


def render_result(tool: str, payload: Mapping[str, Any]) -> str:
    """Operator text for one typed result (the payload is already label-only)."""
    if not isinstance(payload, Mapping):
        return str(payload)[:_RENDER_MAX_CHARS]
    if payload.get("ok") is False:
        code = payload.get("error_code") or "FAILED"
        message = payload.get("message") or ""
        return "%s refused: %s%s" % (tool, code, (" -- " + str(message)) if message else "")
    lines: list[str] = []
    head = [str(payload.get(key)) for key in ("object", "job_id", "status", "stop_reason")
            if payload.get(key)]
    if head:
        lines.append("%s: %s" % (tool, " ".join(head)))
    for key in ("action", "system", "target", "config", "platform", "world", "network",
                "isolation_truth", "verification_scope", "exit_code", "duration_seconds",
                "command_digest"):
        value = payload.get(key)
        if _scalar(value) and value != "":
            lines.append("  %s: %s" % (key, value))
    command = payload.get("display_command") or payload.get("display_argv")
    if isinstance(command, list) and command:
        lines.append("  command: %s" % " ".join(str(part) for part in command))
    for key in ("first_errors", "attempts", "files", "targets", "notes"):
        items = payload.get(key)
        if isinstance(items, list) and items:
            lines.append("  %s (%d):" % (key, len(items)))
            for item in items[:24]:
                if isinstance(item, Mapping):
                    text = ", ".join("%s=%s" % (k, v) for k, v in item.items() if _scalar(v))
                else:
                    text = str(item)
                lines.append("    " + text[:240])
    if payload.get("next"):
        lines.append("  next: %s" % payload["next"])
    if not lines:
        lines.append("%s: %s" % (tool, ", ".join(
            "%s=%s" % (k, v) for k, v in payload.items() if _scalar(v))))
    text = "\n".join(lines)
    if len(text) > _RENDER_MAX_CHARS:
        text = text[:_RENDER_MAX_CHARS].rstrip() + "\n... (cut)"
    return text


class BuildReplFacade:
    """Run REPL build commands only through ``execute_tool``."""

    def __init__(self, execute_tool: Callable[[str, dict], Mapping[str, Any]] | None) -> None:
        self._execute_tool = execute_tool

    def _run(self, parser: Callable[[str], tuple[str, dict]], usage: str, arg: str) -> str:
        if self._execute_tool is None:
            return NOT_COMPOSED
        try:
            tool, arguments = parser(arg)
        except UsageError as exc:
            return "%s\n%s" % (exc, usage)
        payload = self._execute_tool(tool, arguments)
        return render_result(tool, payload)

    def build(self, arg: str) -> str:
        return self._run(parse_build, BUILD_USAGE, arg)

    def fix_build(self, arg: str) -> str:
        return self._run(parse_fix, FIX_USAGE, arg)

    def fix_build_restore(self, arg: str) -> str:
        return self._run(parse_restore, RESTORE_USAGE, arg)


def register_build_commands(register: Callable[[str, Callable[[str], str], BuildCommandSpec], Any],
                            *, facade_getter: Callable[[], BuildReplFacade | None]) -> None:
    """Install the three commands; the facade is resolved per call."""
    def handler(method: str) -> Callable[[str], str]:
        def run(arg: str = "") -> str:
            facade = facade_getter()
            if facade is None:
                return NOT_COMPOSED
            return getattr(facade, method)(arg)
        return run

    methods = {"/build": "build", "/fix-build": "fix_build", "/fix-build-restore": "fix_build_restore"}
    for spec in BUILD_COMMAND_SPECS:
        register(spec.name, handler(methods[spec.name]), spec)


__all__ = [
    "BUILD_COMMAND_SPECS", "BUILD_USAGE", "BuildCommandSpec", "BuildReplFacade", "FIX_USAGE",
    "NOT_COMPOSED", "RESTORE_USAGE", "UsageError", "parse_build", "parse_fix", "parse_restore",
    "register_build_commands", "render_result", "split_words",
]
