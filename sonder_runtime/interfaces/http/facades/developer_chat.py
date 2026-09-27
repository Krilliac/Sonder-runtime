"""App chat spellings of the developer commands, over the HTTP facades.

The Flutter chat sends a slash line to ``serve.py``'s ``_handle_slash``. For
``/test``, ``/digest``, ``/build``, ``/fix-build``, ``/crash`` and ``/profile``
that dispatcher parses the line here into exactly the call an HTTP route
would make, and nothing else:

* ``/test``, ``/digest``, ``/build`` and ``/fix-build`` become one typed tool
  call through the typed gateway -- the call ``/v1/tools/test-run``,
  ``/v1/tools/output-digest`` and ``/v1/build/*`` make -- as the
  authenticated principal with ``source="http"``. The line is graded
  unattended by the permission modes at ``_handle_slash``'s chain gate,
  before this module parses it and without the call's arguments, so a mode
  that refuses a run (``manual``, ``acceptEdits``, ``plan``) refuses the line
  there and the refusal names no call. Approving one run by its ``call_id``
  goes through the HTTP route (``POST /v1/tools/test-run`` or
  ``/v1/build/*``), whose refusal names it. ``render_refusal`` still shows a
  ``call_id`` when a gateway refusal carries one. ``/test`` and ``/digest``
  need admin authority, as their routes do; ``/build`` and ``/fix-build``
  need developer or admin authority, as ``/v1/build/*`` does;
* ``/crash`` and ``/profile`` become one request to the admin
  ``DebugToolsHttpFacade`` -- the ``/v1/tools/crash-*``, ``profile-*`` and
  ``debug-runs`` routes -- with the same admin guard, the same permission
  grading of host launches, the same 48 KB payload cap and the same
  ``SYMBOL_SERVER_NEEDS_CONSOLE`` refusal.

Console-only forms (``/test cancel``, ``/crash symbols``, ``/crash fix``,
``--repro``) answer with where to run them instead. This module parses and
renders, and ``reply`` runs one line through callables ``serve.py`` injects
(authority checks, principal, workspace roots, the application graph and the
debug facade); ``serve.py`` authenticates and sends.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from ...repl.facades import build_tools as repl_build
from ...repl.facades import debug_tools as repl_debug
from . import build_tools as http_build
from . import testing_tools as http_testing
from .typed_gateway import GatewayErrorCodes

# The authority each spelling's HTTP route requires: developer or admin for
# ``/v1/build/*``; admin for ``/v1/tools/test-run``, ``output-digest`` and the
# ``crash-*``/``profile-*`` routes.
DEVELOPER_COMMANDS = frozenset({"/build", "/fix-build"})
ADMIN_COMMANDS = frozenset({"/test", "/digest", "/crash", "/profile"})
CHAT_COMMANDS = DEVELOPER_COMMANDS | ADMIN_COMMANDS

TEST_USAGE = (
    "usage: /test [runner|auto] [selector]  |  /test status|result <test-run-id>"
)
DIGEST_USAGE = "usage: /digest <test-run-id | path>"
TEST_START_WAIT_SECONDS = 20
TEST_RESULT_WAIT_SECONDS = 30
DEBUG_RESULT_WAIT_SECONDS = 30
_TEST_RUNNERS = frozenset({
    "auto", "pytest", "unittest", "ctest", "cargo", "go", "dotnet", "npm",
    "pnpm", "yarn", "gradle", "maven", "make",
})
_TEST_JOB = re.compile(r"^test-run-[0-9a-f]{32}$")
_JOB_LIKE = re.compile(r"^[A-Za-z0-9_:-]{1,80}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_RENDER_MAX_CHARS = 12_000
_CANCEL_ELSEWHERE = (
    "the chat does not cancel test runs; an administrator cancels with "
    "POST /v1/jobs/%s/cancel, or /test cancel at the operator console"
)
_CONSOLE_ONLY = "%s needs the operator console (it is attended there); it is not served in chat"


class ChatUsage(ValueError):
    """The line does not parse; the message is the reply."""


@dataclass(frozen=True)
class TypedCall:
    """One typed gateway call; ``fallback`` runs when this one is ``JOB_NOT_FOUND``."""

    tool: str
    arguments: Mapping[str, Any]
    codes: GatewayErrorCodes
    fallback: "TypedCall | None" = None


@dataclass(frozen=True)
class DebugCall:
    """One ``DebugToolsHttpFacade`` request; ``fallback`` runs on ``on_code``."""

    method: str
    route: str
    payload: Mapping[str, Any] | None = None
    wait_seconds: int = 0
    fallback: "DebugCall | None" = None
    on_code: str = ""
    label: str = field(default="debug")


# --- parsing -------------------------------------------------------------------------------------


def parse_chat_command(cmd: str, arg: str) -> TypedCall | DebugCall:
    """The call a chat line makes; ``ChatUsage`` (the reply) when it makes none."""
    name = str(cmd or "").lower()
    text = str(arg or "").strip()
    if name == "/test":
        return _parse_test(text)
    if name == "/digest":
        return _parse_digest(text)
    if name in ("/build", "/fix-build"):
        return _parse_build(name, text)
    if name == "/crash":
        return _parse_crash(text)
    if name == "/profile":
        return _parse_profile(text)
    raise ChatUsage("not a developer chat command: %s" % name[:40])


def _parse_test(text: str) -> TypedCall:
    codes = http_testing.GATEWAY_CODES
    words = text.split()
    if words and words[0].lower() in ("status", "result", "cancel"):
        verb = words[0].lower()
        if len(words) != 2 or not _TEST_JOB.fullmatch(words[1]):
            raise ChatUsage(TEST_USAGE)
        if verb == "cancel":
            raise ChatUsage(_CANCEL_ELSEWHERE % words[1])
        wait = 0 if verb == "status" else TEST_RESULT_WAIT_SECONDS
        return TypedCall("test_run_result", {"job_id": words[1], "wait_seconds": wait}, codes)
    runner, selector = "auto", text
    if words and words[0].lower() in _TEST_RUNNERS:
        runner = words[0].lower()
        selector = text[len(words[0]):].strip()
    arguments: dict[str, Any] = {"runner": runner, "wait_seconds": TEST_START_WAIT_SECONDS}
    if selector:
        arguments["selector"] = selector
    return TypedCall("test_run", arguments, codes)


def _parse_digest(text: str) -> TypedCall:
    if not text or "\x00" in text or len(text) > 1024:
        raise ChatUsage(DIGEST_USAGE)
    codes = http_testing.GATEWAY_CODES
    by_path = TypedCall("output_digest", {"path": text}, codes)
    if _JOB_LIKE.fullmatch(text):
        # A job-shaped argument names the caller's own job first, then a file
        # of that name -- the REPL's rule, scoped to this principal's jobs.
        return TypedCall("output_digest", {"job_id": text}, codes, fallback=by_path)
    return by_path


def _parse_build(name: str, text: str) -> TypedCall:
    parser, usage = ((repl_build.parse_build, repl_build.BUILD_USAGE) if name == "/build"
                     else (repl_build.parse_fix, repl_build.FIX_USAGE))
    try:
        tool, arguments = parser(text)
    except repl_build.UsageError as exc:
        raise ChatUsage("%s\n%s" % (exc, usage)) from None
    return TypedCall(tool, arguments, http_build.GATEWAY_CODES)


def _words(text: str, usage: str) -> list[str]:
    try:
        words = repl_debug.split_line(text)
    except ValueError:
        raise ChatUsage(usage) from None
    if not words:
        raise ChatUsage(usage)
    return words


def _followup(words: list[str], usage: str) -> DebugCall | None:
    head = words[0].lower()
    if head not in repl_debug.RUN_ACTIONS or len(words) != 2:
        return None
    run_id = words[1]
    if not _RUN_ID.fullmatch(run_id):
        raise ChatUsage(usage)
    route = "/v1/tools/debug-runs/" + run_id
    if head == "cancel":
        return DebugCall("POST", route + "/cancel", None, label="cancel")
    wait = 0 if head == "status" else DEBUG_RESULT_WAIT_SECONDS
    return DebugCall("GET", route, None, wait_seconds=wait, label=head)


def _parse_crash(text: str) -> DebugCall:
    usage = repl_debug.CRASH_USAGE
    words = _words(text, usage)
    followup = _followup(words, usage)
    if followup is not None:
        return followup
    head = words[0].lower()
    if head in ("symbols", "fix"):
        raise ChatUsage(_CONSOLE_ONLY % ("/crash " + head))
    if head == "triage":
        if len(words) != 2:
            raise ChatUsage(usage)
        return DebugCall("POST", "/v1/tools/crash-triage", {"path": words[1]}, label="crash triage")
    try:
        options = repl_debug.parse_crash_words(words)
    except ValueError:
        raise ChatUsage(usage) from None
    if options.get("repro"):
        raise ChatUsage(_CONSOLE_ONLY % "--repro")
    payload: dict[str, Any] = {"path": options["path"], "engine": options["engine"],
                               "symbol_server": bool(options["online"])}
    if options["executable"]:
        payload["executable"] = options["executable"]
    if options["symbol_dirs"]:
        payload["symbol_dirs"] = list(options["symbol_dirs"])
    return DebugCall("POST", "/v1/tools/crash-digest", payload, label="crash digest")


def _parse_profile(text: str) -> DebugCall:
    usage = repl_debug.PROFILE_USAGE
    words = _words(text, usage)
    followup = _followup(words, usage)
    if followup is not None:
        return followup
    try:
        options = repl_debug.parse_profile_words(words)
    except ValueError:
        raise ChatUsage(usage) from None
    pure: dict[str, Any] = {"path": options["path"], "top_n": options["top_n"]}
    for key in ("frame_budget_ms", "thread", "frame_zone"):
        if options[key]:
            pure[key] = options[key]
    capture = dict(pure, engine=options["engine"])
    if options["executable"]:
        capture["executable"] = options["executable"]
    # The REPL's rule: a pure format is digested in-process; a binary capture
    # needs a host tool run, which is graded like the admin capture route.
    capture_call = DebugCall("POST", "/v1/tools/profile-capture-digest", capture,
                             label="profile capture")
    return DebugCall("POST", "/v1/tools/profile-digest", pure, fallback=capture_call,
                     on_code="CAPTURE_NEEDS_HOST_TOOL", label="profile digest")


# --- rendering -----------------------------------------------------------------------------------


def _error_parts(body: Mapping[str, Any]) -> tuple[str, str, Mapping[str, Any]]:
    error = body.get("error") if isinstance(body.get("error"), Mapping) else {}
    code = str(error.get("code") or body.get("error_code") or "FAILED")
    message = str(error.get("message") or body.get("message") or "")
    return code, message, error


def render_refusal(label: str, status: int, body: Mapping[str, Any]) -> str:
    """A refused or failed call as one reply (the ``call_id`` when it has one)."""
    code, message, error = _error_parts(body)
    decision = error.get("decision") if isinstance(error.get("decision"), Mapping) else {}
    call_id = str(decision.get("call_id") or body.get("call_id") or "")
    lines = ["%s refused: %s%s" % (label, code, (" -- " + message[:300]) if message else "")]
    if call_id:
        lines.append("  call_id: %s (an operator can approve this exact call once)" % call_id[:128])
    remedies = error.get("remedies")
    if isinstance(remedies, list):
        lines.extend("  - %s" % str(item)[:200] for item in remedies[:6])
    return "\n".join(lines)


def _test_report(body: Mapping[str, Any]) -> str:
    header = "test run %s: %s (%s)" % (body.get("job_id", ""), body.get("status", ""),
                                       body.get("runner", ""))
    if body.get("exit_code") is not None:
        header += " exit=%s" % body.get("exit_code")
    if isinstance(body.get("duration_seconds"), (int, float)):
        header += " in %.1fs" % body["duration_seconds"]
    lines = [header]
    command = body.get("display_command")
    if isinstance(command, list) and command:
        lines.append("  command: %s" % " ".join(str(part) for part in command))
    totals = body.get("totals")
    if isinstance(totals, Mapping):
        lines.append("  totals: %s%s" % (
            " ".join("%s=%s" % (key, totals.get(key, "?"))
                     for key in ("passed", "failed", "skipped", "errors", "total")),
            "" if body.get("totals_reliable", True) else " [unreliable]"))
    if body.get("summary_line"):
        lines.append("  summary: %s" % body["summary_line"])
    failures = body.get("failures")
    if isinstance(failures, list) and failures:
        lines.append("  failures:")
        for item in failures[:40]:
            if not isinstance(item, Mapping):
                continue
            where = str(item.get("file") or "")
            if where and item.get("line") is not None:
                where += ":%s" % item["line"]
            lines.append("    %s%s %s" % (item.get("id", ""), (" (%s)" % where) if where else "",
                                         str(item.get("message_excerpt") or "")[:240]))
    for note in (body.get("notes") or [])[:8]:
        lines.append("  note: %s" % note)
    return "\n".join(lines)


def _test_status(body: Mapping[str, Any]) -> str:
    line = "test run %s: %s (%s) %.0fs" % (
        body.get("job_id", ""), body.get("status", ""), body.get("runner", ""),
        float(body.get("elapsed_seconds") or 0))
    command = body.get("display_command")
    if isinstance(command, list) and command:
        line += "\n  command: %s" % " ".join(str(part) for part in command)
    return line + "\n  next: /test result %s" % body.get("job_id", "")


def render_typed(call: TypedCall, status: int, body: Mapping[str, Any]) -> str:
    """The chat reply for one typed gateway answer."""
    if status >= 400:
        return render_refusal(call.tool, status, body)
    shown = {key: value for key, value in body.items() if key != "receipt"}
    if shown.get("object") == "test_report":
        text = _test_report(shown)
    elif shown.get("object") == "test_run_status":
        text = _test_status(shown)
    elif call.tool == "output_digest":
        text = "output digest:\n" + json.dumps(shown, ensure_ascii=False, indent=1)
    else:
        text = repl_build.render_result(call.tool, shown)
    return _cap(text)


def render_debug(call: DebugCall, status: int, body: Mapping[str, Any]) -> str:
    """The chat reply for one debug facade answer (already capped at 48 KB)."""
    if status >= 400:
        return render_refusal(call.label, status, body)
    return _cap("%s:\n%s" % (call.label, json.dumps(dict(body), ensure_ascii=False, indent=1)))


def _cap(text: str) -> str:
    if len(text) > _RENDER_MAX_CHARS:
        return text[:_RENDER_MAX_CHARS].rstrip() + "\n... (cut; use the HTTP route for the full payload)"
    return text


def error_code(body: Mapping[str, Any]) -> str:
    return _error_parts(body)[0]


def reply(
    cmd: str,
    arg: str,
    context: Any,
    *,
    admin_authorized: Callable[[Any], bool],
    developer_authorized: Callable[[Any], bool],
    principal_of: Callable[[Any], str],
    workspace_roots_of: Callable[[Any], tuple],
    application: Callable[[], Any],
    debug_facade: Callable[[Any], Any],
    debug_context: Callable[[Any, str], Any],
) -> str:
    """Answer one chat line with exactly the call its HTTP route makes.

    A typed gateway call as the authenticated principal with
    ``source="http"``, or an admin ``DebugToolsHttpFacade`` request (same
    guard, grading and caps as ``/v1/tools/crash-*``). The authority check is
    the route's: developer or admin for ``/build`` and ``/fix-build``, admin
    for ``/test``, ``/digest``, ``/crash`` and ``/profile``.
    """
    admin_only = cmd in ADMIN_COMMANDS
    if not isinstance(context, dict):
        return "refused %s: an authenticated HTTP caller is required" % cmd
    allowed = admin_authorized(context) if admin_only else developer_authorized(context)
    if not allowed:
        return "refused %s: %s authority is required" % (
            cmd, "admin" if admin_only else "developer or admin")
    principal = principal_of(context)
    if not principal:
        return "refused %s: authenticated account identity is unavailable" % cmd
    try:
        call = parse_chat_command(cmd, arg)
    except ChatUsage as usage:
        return str(usage)
    app = application()
    if isinstance(call, TypedCall):
        from .typed_gateway import execute_typed_call

        roots = workspace_roots_of(context)
        auth_level = "admin" if admin_authorized(context) else "developer"
        while True:
            status, body = execute_typed_call(
                lambda: getattr(app, "tools", None), call.tool, call.arguments,
                call.codes, principal_id=principal, workspace_roots=roots,
                auth_level=auth_level,
            )
            if (call.fallback is not None and status == 404
                    and error_code(body) == "JOB_NOT_FOUND"):
                call = call.fallback
                continue
            return render_typed(call, status, body)
    facade = debug_facade(app)
    try:
        operation = debug_context(context, "chat-" + uuid.uuid4().hex)
    except PermissionError:
        return "refused %s: authenticated account identity is unavailable" % cmd
    while True:
        status, body = facade.dispatch(call.method, call.route, call.payload, operation,
                                       admin=True, wait_seconds=call.wait_seconds)
        if (call.fallback is not None and status >= 400
                and error_code(body) == call.on_code):
            call = call.fallback
            continue
        return render_debug(call, status, body)


__all__ = [
    "ADMIN_COMMANDS", "CHAT_COMMANDS", "ChatUsage", "DEVELOPER_COMMANDS", "DIGEST_USAGE",
    "DebugCall", "TEST_USAGE", "TypedCall", "error_code", "parse_chat_command",
    "render_debug", "render_refusal", "render_typed", "reply",
]
