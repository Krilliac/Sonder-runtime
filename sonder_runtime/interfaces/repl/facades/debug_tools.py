"""REPL presentation for crash and profile digests: ``/crash`` and ``/profile``.

Interfaces layer: it imports only application modules. Reports and digests
are rendered through ``application.debugging.presenters`` (never domain
renderers), requests are built from ``application.debugging.ports``, and the
fix hand-off comes from ``application.debugging.crash_fix``. Every function
receives the composed ``DebugDigestService`` (or None when this runtime did
not compose it) plus an ``OperationContext`` the REPL built.

Symbol-server egress is console-only. ``--symbols-online`` first renders the
exact resolved command (engines, placeholder argv, stores, input sha256) and
launches nothing unless the operator answers "y" to ``confirm``; only then is
the service called with ``console_confirmed=True``. The service still refuses
without consent (``SYMBOL_SERVER_CONSENT_REQUIRED``), which ``/crash symbols
on`` grants for this attended session.

Ctrl+C while waiting cancels the run (the provider kills the process tree).
"""
from __future__ import annotations

import re
import shlex
import time
from collections.abc import Callable, Iterable
from typing import Any

from ....application.context import OperationContext
from ....application.debugging.crash_fix import (
    build_crash_fix_handoff,
    render_crash_fix_brief,
    repro_lookup_for,
)
from ....application.errors import InvalidInput, NotFound, SonderError


NOT_COMPOSED = "debug tools are not composed in this runtime"
CRASH_USAGE = (
    "usage: /crash <dump|core|log|dir> [--exe P] [--sym DIR]... [--engine E] "
    "[--repro NAME] [--symbols-online] | /crash triage <path> | /crash symbols on|off"
    " | /crash fix <run_id|last> | /crash status|result|cancel <run_id>"
)
PROFILE_USAGE = (
    "usage: /profile <capture> [--exe P] [--budget MS] [--top N] [--thread T] "
    "[--frame-zone Z] | /profile status|result|cancel <run_id>"
)
HELP_LINES = (
    "  /crash <dump|core|log> [--exe P] [--sym DIR] [--engine E] [--repro NAME]"
    "  digest a crash (minidump, core, sanitizer/valgrind log)",
    "  /crash triage <path|dir>  pure read, no debugger; a folder is bucketed by signature",
    "  /crash symbols on|off  allow symbol-server downloads for this console session",
    "  /crash fix <run_id|last>  fatal diagnostics, local source excerpt and a repro test",
    "  /crash status|result|cancel <run_id>  follow a debugger run",
    "  /profile <capture> [--budget MS] [--top N]  hot paths, frame spikes, allocations",
    "  /profile status|result|cancel <run_id>  follow a profiler run",
)
CRASH_ENGINES = (
    "auto", "pure", "cdb", "gdb", "lldb", "eu_stack", "minidump_stackwalk", "llvm_symbolizer",
)
PROFILE_ENGINES = ("auto", "perf", "heaptrack_print", "tracy_csvexport", "xperf", "wpaexporter")
RUN_ACTIONS = ("status", "result", "cancel")
TERMINAL_STATUSES = frozenset({
    "complete", "failed", "cancelled", "timed_out", "refused", "partial",
})
MAX_SYMBOL_DIRS = 8
MAX_PATH_CHARS = 1024
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_RENDER_MAX_CHARS = 12_000
_NEEDS_HOST_TOOL = "CAPTURE_NEEDS_HOST_TOOL"

# Per-console memory for ``/crash fix last`` and ``--repro``: the newest run
# and report this process rendered, and the selector given with each run.
_LAST: dict[str, Any] = {"run_id": "", "report": None}
_REPRO_BY_RUN: dict[str, str] = {}
_MAX_REMEMBERED = 32


class _Usage(Exception):
    pass


def _text(value: Any) -> str:
    return str(getattr(value, "value", value) if value is not None else "")


def _code(exc: BaseException) -> str:
    return str(getattr(exc, "code", "") or type(exc).__name__)


def _presenters():
    """``application.debugging.presenters``, or None when not installed."""
    try:
        from ....application.debugging import presenters
    except ImportError:
        return None
    return presenters


def _ports():
    """``application.debugging.ports``, or None when not installed."""
    try:
        from ....application.debugging import ports
    except ImportError:
        return None
    return ports


def _split(arg: str) -> list[str]:
    try:
        # posix=False keeps Windows backslashes; quotes are stripped below.
        words = shlex.split(str(arg or ""), posix=False)
    except ValueError:
        raise _Usage() from None
    return [w[1:-1] if len(w) >= 2 and w[0] == w[-1] and w[0] in "\"'" else w for w in words]


def _path(value: str) -> str:
    if not value or len(value) > MAX_PATH_CHARS or "\x00" in value:
        raise _Usage()
    return value


def _int(value: str, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise _Usage() from None
    if not low <= number <= high:
        raise _Usage()
    return number


def _remember(run_id: str, report: Any, repro: str = "") -> None:
    if run_id:
        _LAST["run_id"] = run_id
        if repro:
            _REPRO_BY_RUN[run_id] = repro
            while len(_REPRO_BY_RUN) > _MAX_REMEMBERED:
                _REPRO_BY_RUN.pop(next(iter(_REPRO_BY_RUN)))
    if report is not None:
        _LAST["report"] = report
        _LAST["report_run_id"] = run_id


def _render_report(report: Any) -> str:
    presenters = _presenters()
    if presenters is None:
        return NOT_COMPOSED
    return presenters.render_report(report, max_chars=_RENDER_MAX_CHARS)


def _render_digest(digest: Any) -> str:
    presenters = _presenters()
    if presenters is None:
        return NOT_COMPOSED
    return str(presenters.render_digest(digest))[:_RENDER_MAX_CHARS]


def _render_outcome(outcome: Any, label: str) -> str:
    run_id = _text(getattr(outcome, "run_id", ""))
    status = _text(getattr(outcome, "status", "")) or "?"
    header = "%s run %s: %s" % (label, run_id or "-", status)
    code = _text(getattr(outcome, "error_code", ""))
    if code:
        header += " (%s)" % code
    lines = [header]
    for note in tuple(getattr(outcome, "notes", ()) or ())[:12]:
        lines.append("  note: %s" % _text(note)[:240])
    crash = getattr(outcome, "crash", None)
    profile = getattr(outcome, "profile", None)
    if crash is not None:
        lines.append(_render_report(crash))
    elif profile is not None:
        lines.append(_render_digest(profile))
    elif status not in TERMINAL_STATUSES:
        lines.append("  still running; /%s status %s, Ctrl+C while waiting cancels" % (
            label, run_id))
    return "\n".join(lines)[:_RENDER_MAX_CHARS + 2_000]


def _render_resolved(resolved: Any) -> str:
    data = resolved if isinstance(resolved, dict) else {}
    lines = ["resolved command (%s):" % _text(data.get("kind", "crash"))]
    engines = data.get("engines") or ()
    lines.append("  engines: %s" % (", ".join(_text(e) for e in engines) or "pure"))
    for argv in tuple(data.get("display_argvs") or ())[:4]:
        lines.append("  argv: %s" % " ".join(_text(part) for part in tuple(argv)[:64]))
    lines.append("  input: %s sha256=%s" % (
        _text(data.get("input_label", "")), _text(data.get("input_sha256", ""))))
    lines.append("  network: %s" % ("yes" if data.get("network") else "no"))
    stores = tuple(data.get("stores_display") or ())
    for store in stores[:8]:
        lines.append("  symbol store: %s" % _text(store))
    if data.get("isolation"):
        lines.append("  isolation: %s" % _text(data.get("isolation")))
    if data.get("command_digest"):
        lines.append("  command digest: %s" % _text(data.get("command_digest"))[:64])
    return "\n".join(lines)


def _wait(
    service: Any, outcome: Any, context: OperationContext, label: str, *,
    out: Callable[[str], None], wait_seconds: float, poll_seconds: float,
    clock: Callable[[], float],
) -> Any:
    """Poll ``service.result`` until terminal; Ctrl+C cancels the run."""
    run_id = _text(getattr(outcome, "run_id", ""))
    if not run_id or _text(getattr(outcome, "status", "")) in TERMINAL_STATUSES:
        return outcome
    started = clock()
    try:
        while clock() - started < wait_seconds:
            polled_at = clock()
            outcome = service.result(run_id, context, wait_seconds=poll_seconds)
            if _text(getattr(outcome, "status", "")) in TERMINAL_STATUSES:
                return outcome
            if clock() - polled_at < poll_seconds / 4:
                time.sleep(min(0.25, poll_seconds))
    except KeyboardInterrupt:
        try:
            outcome = service.cancel(run_id, context)
            out("cancelled %s run %s" % (label, run_id))
        except SonderError as exc:
            out("cancel of %s failed: %s" % (run_id, _code(exc)))
        return outcome
    return outcome


def _followup(service: Any, action: str, run_id: str, context: OperationContext,
              label: str) -> str:
    if action not in RUN_ACTIONS or not _RUN_ID_RE.match(run_id or ""):
        return CRASH_USAGE if label == "crash" else PROFILE_USAGE
    try:
        if action == "cancel":
            outcome = service.cancel(run_id, context)
            return "cancelled " + _render_outcome(outcome, label).split("\n", 1)[0]
        outcome = service.result(run_id, context, wait_seconds=0)
    except NotFound:
        return "no %s run %s" % (label, run_id)
    except SonderError as exc:
        return "%s %s failed: %s" % (label, action, _code(exc))
    if label == "crash":
        _remember(run_id, getattr(outcome, "crash", None))
    if action == "status":
        return _render_outcome(outcome, label).split("\n", 1)[0]
    return _render_outcome(outcome, label)


# --- /crash --------------------------------------------------------------------------


def _parse_crash(words: list[str]) -> dict:
    options: dict[str, Any] = {
        "path": "", "executable": "", "symbol_dirs": [], "engine": "auto",
        "repro": "", "online": False,
    }
    index = 0
    while index < len(words):
        word = words[index]
        if word == "--symbols-online":
            options["online"] = True
            index += 1
            continue
        if word in ("--exe", "--sym", "--engine", "--repro"):
            if index + 1 >= len(words):
                raise _Usage()
            value = words[index + 1]
            if word == "--exe":
                options["executable"] = _path(value)
            elif word == "--sym":
                options["symbol_dirs"].append(_path(value))
                if len(options["symbol_dirs"]) > MAX_SYMBOL_DIRS:
                    raise _Usage()
            elif word == "--engine":
                if value.lower() not in CRASH_ENGINES:
                    raise _Usage()
                options["engine"] = value.lower()
            else:
                options["repro"] = value
            index += 2
            continue
        if word.startswith("--") or options["path"]:
            raise _Usage()
        options["path"] = _path(word)
        index += 1
    if not options["path"]:
        raise _Usage()
    return options


def _refused(label: str, exc: BaseException) -> str:
    detail = str(exc)[:300]
    code = _code(exc)
    if detail and detail != code:
        return "%s refused: %s: %s" % (label, code, detail)
    return "%s refused: %s" % (label, code)


def _crash_symbols(service: Any, words: list[str], context: OperationContext) -> str:
    if len(words) != 2 or words[1].lower() not in ("on", "off"):
        return CRASH_USAGE
    allowed = words[1].lower() == "on"
    try:
        service.set_session_symbol_consent(context, allowed)
    except (SonderError, PermissionError) as exc:
        return _refused("symbol consent", exc)
    if allowed:
        return ("symbol-server downloads allowed for this console session; each "
                "--symbols-online run still shows its exact command and asks y/N")
    return "symbol-server downloads off for this console session"


def _crash_triage(service: Any, words: list[str], context: OperationContext) -> str:
    ports = _ports()
    presenters = _presenters()
    if ports is None or presenters is None:
        return NOT_COMPOSED
    if len(words) != 2:
        return CRASH_USAGE
    try:
        result = service.triage(ports.CrashTriageRequest(path=_path(words[1])), context)
    except _Usage:
        return CRASH_USAGE
    except (SonderError, PermissionError) as exc:
        return _refused("crash triage", exc)
    if isinstance(result, tuple):
        return str(presenters.render_bucket_table(result))[:_RENDER_MAX_CHARS]
    _remember("", result)
    return _render_report(result)


def crash_fix_brief(
    service: Any,
    run_id: str,
    context: OperationContext,
    *,
    source_lookup: Callable | None = None,
    test_reports: Callable[[], Iterable[Any]] | None = None,
    repro: str = "",
) -> str | None:
    """Diagnostics, the untrusted source excerpt and a repro for one crash run.

    ``run_id`` may be ``last``. Returns None when there is no report. Nothing
    is launched: the operator (or the model) runs the build and test tools.
    """
    identifier = str(run_id or "").strip()
    report = None
    if identifier == "last":
        identifier = _text(_LAST.get("run_id", ""))
        if _LAST.get("report") is not None and _LAST.get("report_run_id", "") == identifier:
            report = _LAST["report"]
    if report is None:
        if not identifier or not _RUN_ID_RE.match(identifier) or service is None:
            return None
        try:
            outcome = service.result(identifier, context, wait_seconds=0)
        except SonderError:
            return None
        report = getattr(outcome, "crash", None)
        if report is None:
            return None
    selector = repro or _REPRO_BY_RUN.get(identifier, "")
    try:
        handoff = build_crash_fix_handoff(
            report,
            source_lookup=source_lookup,
            repro_lookup=repro_lookup_for(explicit=selector, reports=test_reports),
        )
    except InvalidInput as exc:
        return "crash fix refused: %s: %s" % (_code(exc), str(exc)[:200])
    return render_crash_fix_brief(handoff, run_id=identifier)


def crash_command(
    service: Any,
    arg: str,
    context: OperationContext,
    *,
    out: Callable[[str], None] = print,
    confirm: Callable[[str], str] = input,
    wait_seconds: float = 120,
    poll_seconds: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
    source_lookup: Callable | None = None,
    test_reports: Callable[[], Iterable[Any]] | None = None,
) -> None:
    """``/crash`` and its subcommands; output goes through ``out``."""
    if service is None:
        out(NOT_COMPOSED)
        return
    try:
        words = _split(arg)
    except _Usage:
        out(CRASH_USAGE)
        return
    if not words:
        out(CRASH_USAGE)
        return
    head = words[0].lower()
    if head in RUN_ACTIONS and len(words) == 2:
        out(_followup(service, head, words[1], context, "crash"))
        return
    if head == "symbols":
        out(_crash_symbols(service, words, context))
        return
    if head == "triage":
        out(_crash_triage(service, words, context))
        return
    if head == "fix":
        if len(words) != 2:
            out(CRASH_USAGE)
            return
        brief = crash_fix_brief(service, words[1], context, source_lookup=source_lookup,
                                test_reports=test_reports)
        out(brief if brief is not None else
            "no crash report for %s (run /crash <dump> first)" % words[1][:80])
        return
    ports = _ports()
    if ports is None or _presenters() is None:
        out(NOT_COMPOSED)
        return
    try:
        options = _parse_crash(words)
    except _Usage:
        out(CRASH_USAGE)
        return
    if options["repro"]:
        from ....application.debugging.crash_fix import explicit_repro

        try:
            explicit_repro(options["repro"])
        except InvalidInput as exc:
            out("crash digest refused: %s: %s" % (_code(exc), str(exc)[:200]))
            return
    request = ports.CrashDigestRequest(
        path=options["path"], executable=options["executable"],
        symbol_dirs=tuple(options["symbol_dirs"]), engine=options["engine"],
        symbol_server=options["online"],
    )
    confirmed = False
    if options["online"]:
        try:
            # Planning never launches; it only resolves what would run so the
            # operator can read it before answering.
            plan = service.plan_crash(request, context, console_confirmed=True)
        except (SonderError, PermissionError) as exc:
            out(_refused("crash digest", exc))
            return
        out(_render_resolved(plan.resolved_command()))
        try:
            answer = confirm("download symbols from these servers for this run? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if str(answer or "").strip().lower() not in ("y", "yes"):
            out("symbol-server run declined; nothing was launched")
            return
        confirmed = True
    try:
        outcome = service.crash(request, context, wait_seconds=0, console_confirmed=confirmed)
    except (SonderError, PermissionError) as exc:
        out(_refused("crash digest", exc))
        return
    run_id = _text(getattr(outcome, "run_id", ""))
    _remember(run_id, getattr(outcome, "crash", None), options["repro"])
    if _text(getattr(outcome, "status", "")) not in TERMINAL_STATUSES:
        out("started crash run %s; Ctrl+C cancels" % run_id)
    outcome = _wait(service, outcome, context, "crash", out=out, wait_seconds=wait_seconds,
                    poll_seconds=poll_seconds, clock=clock)
    _remember(run_id, getattr(outcome, "crash", None))
    out(_render_outcome(outcome, "crash"))


# --- /profile -------------------------------------------------------------------------


def _parse_profile(words: list[str]) -> dict:
    options: dict[str, Any] = {
        "path": "", "executable": "", "frame_budget_ms": None, "top_n": 25,
        "thread": "", "frame_zone": "", "engine": "auto",
    }
    index = 0
    while index < len(words):
        word = words[index]
        if word in ("--exe", "--budget", "--top", "--thread", "--frame-zone", "--engine"):
            if index + 1 >= len(words):
                raise _Usage()
            value = words[index + 1]
            if word == "--exe":
                options["executable"] = _path(value)
            elif word == "--budget":
                options["frame_budget_ms"] = _int(value, 1, 1000)
            elif word == "--top":
                options["top_n"] = _int(value, 5, 50)
            elif word == "--engine":
                if value.lower() not in PROFILE_ENGINES:
                    raise _Usage()
                options["engine"] = value.lower()
            else:
                if len(value) > 64:
                    raise _Usage()
                options["thread" if word == "--thread" else "frame_zone"] = value
            index += 2
            continue
        if word.startswith("--") or options["path"]:
            raise _Usage()
        options["path"] = _path(word)
        index += 1
    if not options["path"]:
        raise _Usage()
    return options


def profile_command(
    service: Any,
    arg: str,
    context: OperationContext,
    *,
    out: Callable[[str], None] = print,
    wait_seconds: float = 120,
    poll_seconds: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """``/profile``: pure formats in-process, binary captures via a host tool run."""
    if service is None:
        out(NOT_COMPOSED)
        return
    try:
        words = _split(arg)
    except _Usage:
        out(PROFILE_USAGE)
        return
    if not words:
        out(PROFILE_USAGE)
        return
    head = words[0].lower()
    if head in RUN_ACTIONS and len(words) == 2:
        out(_followup(service, head, words[1], context, "profile"))
        return
    ports = _ports()
    if ports is None or _presenters() is None:
        out(NOT_COMPOSED)
        return
    try:
        options = _parse_profile(words)
    except _Usage:
        out(PROFILE_USAGE)
        return
    request = ports.ProfileDigestRequest(
        path=options["path"], executable=options["executable"], engine=options["engine"],
        top_n=options["top_n"], frame_budget_ms=options["frame_budget_ms"],
        thread=options["thread"], frame_zone=options["frame_zone"],
    )
    try:
        out(_render_digest(service.profile_pure(request, context)))
        return
    except (SonderError, PermissionError) as exc:
        if _code(exc) != _NEEDS_HOST_TOOL:
            out(_refused("profile digest", exc))
            return
    try:
        outcome = service.profile(request, context, wait_seconds=0)
    except (SonderError, PermissionError) as exc:
        out(_refused("profile digest", exc))
        return
    run_id = _text(getattr(outcome, "run_id", ""))
    if _text(getattr(outcome, "status", "")) not in TERMINAL_STATUSES:
        out("started profile run %s; Ctrl+C cancels" % run_id)
    outcome = _wait(service, outcome, context, "profile", out=out, wait_seconds=wait_seconds,
                    poll_seconds=poll_seconds, clock=clock)
    out(_render_outcome(outcome, "profile"))


__all__ = [
    "CRASH_USAGE", "HELP_LINES", "NOT_COMPOSED", "PROFILE_USAGE",
    "crash_command", "crash_fix_brief", "profile_command",
]
