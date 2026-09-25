"""Crash and profile digests as owned, bounded runs.

``DebugDigestService`` composes the capture source (guarded open, identity,
sniffing), the pure triage (lane A/B readers), the planner (host-owned argv
templates), the launcher (durable step jobs in a private run directory) and
the -4 job output reader. It creates no threads: waiting is a bounded
``launcher.wait`` in short slices, so cancelling the caller's operation
cancels the run.

Rules this service enforces itself, whatever the permission mode says:

- Tier 0 always runs first; a plan with no steps returns immediately.
- ``symbol_server`` (network) needs the attended console (``ctx.source ==
  "repl"`` and ``console_confirmed``, which only the REPL facade sets after an
  explicit y/N on the rendered command), consent (environment or the session
  switch) and a mode other than plan. Every other caller is refused with
  ``SYMBOL_SERVER_NEEDS_CONSOLE``; this does not depend on the degradable
  ASK path of the permission modes.
- Every run-id method is owner-checked; anything else is ``JOB_NOT_FOUND``.
- The result JSON is cached in the run directory; staged inputs and tool
  caches are deleted by the launcher when the step chain ends.
"""
from __future__ import annotations

import re
import threading
import uuid
from typing import Any, Callable, Mapping

from ...domain.common.errors import Forbidden, InvalidInput, SonderError
from ..context import OperationContext
from .ports import (
    CAPTURE_FORMAT_UNKNOWN,
    CAPTURE_NEEDS_HOST_TOOL,
    CRASH_JOB_KIND,
    CRASH_KINDS,
    DEBUG_RUN_BUSY,
    HOST_PROFILE_KINDS,
    INPUT_CHANGED,
    JOB_ID_PREFIX,
    JOB_NOT_FOUND,
    OUTPUT_LIMIT,
    PARSE_FAILED,
    PROFILE_JOB_KIND,
    PROFILE_KINDS,
    PURE_PROFILE_KINDS,
    SYMBOL_SERVER_CONSENT_REQUIRED,
    SYMBOL_SERVER_NEEDS_CONSOLE,
    CaptureIdentity,
    CaptureSource,
    CrashDigestRequest,
    CrashTriageRequest,
    DebugLauncher,
    DebugPlan,
    DebugPlanner,
    DebugRunOutcome,
    DebugRunState,
    JobOutputReader,
    ProfileDigestRequest,
    PureTriage,
    SourceMap,
    SymbolConsent,
    debug_error,
)

MAX_RUN_WAIT_SECONDS = 120
MAX_RESULT_WAIT_SECONDS = 60
MAX_DIRECTORY_FILES = 64
DIRECTORY_READ_BUDGET = 256 << 20
# Opening a capture streams its sha256 (up to 5 s each); 64 large files would
# otherwise keep one tool call busy for minutes.
DIRECTORY_SECONDS = 30.0
OUTPUT_WINDOW_BYTES = 2_000_000
OUTPUT_HEAD_BYTES = 65_536
MAX_NOTES = 32
_RUN_ID = re.compile(r"^debug-run-[0-9a-f]{32}$")
_WAIT_SLICE_SECONDS = 1.0
_MAX_REMEMBERED = 64
_CANCEL_SOURCES = frozenset({"repl", "http"})


def _not_found() -> SonderError:
    return debug_error(JOB_NOT_FOUND, "debug run not found")


def _clip(text: Any, limit: int = 240) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _notes(values, redact: Callable[[str], str]) -> tuple[str, ...]:
    out = []
    for value in values:
        text = _clip(redact(str(value)), 240)
        if text and text not in out:
            out.append(text)
    return tuple(out[:MAX_NOTES])


def _extra_roots(context: OperationContext) -> str:
    # The digest tools read only inside the roots the operator already
    # authorized for the file tools (as the -4 output digest does); a
    # request scope never widens them.
    del context
    return ""


class DebugDigestService:
    def __init__(self, source: CaptureSource, triage: PureTriage, planner: DebugPlanner,
                 launcher: DebugLauncher, output: JobOutputReader, *, consent: SymbolConsent,
                 source_map: SourceMap | None, redact: Callable[[str], str],
                 clock: Callable[[], float], max_concurrent_per_principal: int = 2) -> None:
        if isinstance(max_concurrent_per_principal, bool) or max_concurrent_per_principal < 1:
            raise ValueError("max_concurrent_per_principal must be positive")
        self._source = source
        self._triage = triage
        self._planner = planner
        self._launcher = launcher
        self._output = output
        self._consent = consent
        self._source_map = source_map
        self._redact = redact
        self._clock = clock
        self._max_concurrent = max_concurrent_per_principal
        self._start_lock = threading.Lock()
        self._memo_lock = threading.Lock()
        self._tier0: dict[str, Any] = {}

    # -- pure --------------------------------------------------------------

    def triage(self, request: CrashTriageRequest, context: OperationContext):
        """A ``CrashReport`` for one capture, or bucketed ``CrashBucket``s for a directory."""
        result, _notes_ = self.triage_detail(request, context)
        return result

    def triage_detail(self, request: CrashTriageRequest, context: OperationContext):
        """``(report | buckets, notes)``; the notes name files a directory triage skipped."""
        if not isinstance(request, CrashTriageRequest):
            raise InvalidInput("request must be a CrashTriageRequest")
        path = self._path(request.path)
        roots = _extra_roots(context)
        if self._source.is_dir(path, extra_roots=roots):
            return self._triage_directory(path, request, roots)
        reader, identity = self._source.open_reader(path, extra_roots=roots)
        try:
            self._require_crash_kind(identity)
            report = self._triage.crash(identity, reader)
        finally:
            reader.close()
        return self._map(report), ()

    def profile_pure(self, request: ProfileDigestRequest, context: OperationContext):
        """A ``ProfileDigest`` from a pure format; binary captures need a host tool."""
        if not isinstance(request, ProfileDigestRequest):
            raise InvalidInput("request must be a ProfileDigestRequest")
        path = self._path(request.path)
        reader, identity = self._source.open_reader(path, extra_roots=_extra_roots(context))
        try:
            self._require_pure_profile(identity)
            digest = self._triage.profile(identity, reader, request)
        finally:
            reader.close()
        if digest is None:
            raise debug_error(CAPTURE_FORMAT_UNKNOWN, "no profile reader recognised this capture")
        return digest

    # -- planning ----------------------------------------------------------

    def plan_crash(self, request: CrashDigestRequest, context: OperationContext, *,
                   console_confirmed: bool = False) -> DebugPlan:
        plan, _base, _identity = self._prepare_crash(request, context, console_confirmed)
        return plan

    def plan_profile(self, request: ProfileDigestRequest, context: OperationContext) -> DebugPlan:
        plan, _identity = self._prepare_profile(request, context)
        return plan

    def _prepare_crash(self, request: CrashDigestRequest, context: OperationContext,
                       console_confirmed: bool):
        if not isinstance(request, CrashDigestRequest):
            raise InvalidInput("request must be a CrashDigestRequest")
        network = self._network_decision(request, context, console_confirmed)
        path = self._path(request.path)
        reader, identity = self._source.open_reader(path, extra_roots=_extra_roots(context))
        try:
            self._require_crash_kind(identity)
            try:
                base = self._triage.crash(identity, reader)
            except SonderError as exc:
                if getattr(exc, "code", "") not in (PARSE_FAILED, CAPTURE_FORMAT_UNKNOWN):
                    raise
                base = None
        finally:
            reader.close()
        plan = self._planner.plan_crash(request, context, network_allowed=network,
                                        identity=identity, tier0=base)
        return plan, base, identity

    def _prepare_profile(self, request: ProfileDigestRequest, context: OperationContext):
        if not isinstance(request, ProfileDigestRequest):
            raise InvalidInput("request must be a ProfileDigestRequest")
        path = self._path(request.path)
        reader, identity = self._source.open_reader(path, extra_roots=_extra_roots(context))
        reader.close()
        if identity.kind not in PROFILE_KINDS:
            self._require_pure_profile(identity)  # raises the precise refusal
        plan = self._planner.plan_profile(request, context, identity=identity)
        return plan, identity

    def _network_decision(self, request: CrashDigestRequest, context: OperationContext,
                          console_confirmed: bool) -> bool:
        if not request.symbol_server:
            return False
        if context.source != "repl" or not console_confirmed:
            raise debug_error(
                SYMBOL_SERVER_NEEDS_CONSOLE,
                "symbol-server lookups run only from the attended console: use "
                "/crash <dump> --symbols-online and confirm the rendered command")
        if not self._consent.mode_permits_network():
            raise debug_error(SYMBOL_SERVER_CONSENT_REQUIRED,
                              "symbol-server lookups are refused in plan mode")
        if not self._consent.allowed(context):
            raise debug_error(
                SYMBOL_SERVER_CONSENT_REQUIRED,
                "symbol-server lookups need consent: set SONDER_SYMBOL_SERVER_CONSENT=1 or "
                "run /crash symbols on")
        return True

    # -- runs --------------------------------------------------------------

    def crash(self, request: CrashDigestRequest, context: OperationContext, *,
              wait_seconds: float = 60, console_confirmed: bool = False) -> DebugRunOutcome:
        plan, base, identity = self._prepare_crash(request, context, console_confirmed)
        if not plan.steps:
            if base is None:
                return DebugRunOutcome("", "failed", error_code=PARSE_FAILED,
                                       notes=_notes(plan.notes, self._redact),
                                       command_digest=plan.command_digest)
            report = self._triage.finish_crash(
                base, engines=tuple(dict.fromkeys(("pure", *base.engines))),
                module_symbols=plan.module_symbols, egress_isolation="n/a",
                notes=_notes(plan.notes, self._redact), truncated=False)
            return DebugRunOutcome("", "complete", crash=self._map(report),
                                   notes=_notes(plan.notes, self._redact),
                                   command_digest=plan.command_digest, engines=plan.engines)
        context_payload = {"kind": "crash",
                           "tier0": self._triage.crash_to_wire(base) if base is not None else None}
        run_id = self._start(plan, context, context_payload, base)
        return self._await(run_id, context, self._bounded_wait(wait_seconds, MAX_RUN_WAIT_SECONDS),
                           cancel_on_abort=True)

    def profile(self, request: ProfileDigestRequest, context: OperationContext, *,
                wait_seconds: float = 60) -> DebugRunOutcome:
        plan, identity = self._prepare_profile(request, context)
        if not plan.steps:
            digest = self.profile_pure(request, context)
            return DebugRunOutcome("", "complete", profile=digest,
                                   notes=_notes(plan.notes, self._redact),
                                   command_digest=plan.command_digest, engines=("pure",))
        context_payload = {"kind": "profile", "request": {
            "top_n": request.top_n, "frame_budget_ms": request.frame_budget_ms,
            "thread": request.thread, "frame_zone": request.frame_zone,
        }}
        run_id = self._start(plan, context, context_payload, None)
        return self._await(run_id, context, self._bounded_wait(wait_seconds, MAX_RUN_WAIT_SECONDS),
                           cancel_on_abort=True)

    def result(self, run_id: str, context: OperationContext, *,
               wait_seconds: float = 0) -> DebugRunOutcome:
        self._owned(run_id, context)
        return self._await(run_id, context,
                           self._bounded_wait(wait_seconds, MAX_RESULT_WAIT_SECONDS),
                           cancel_on_abort=False)

    def cancel(self, run_id: str, context: OperationContext) -> DebugRunOutcome:
        if context.source not in _CANCEL_SOURCES:
            error = Forbidden("debug runs are cancelled from the console or the admin HTTP API")
            error.code = "FORBIDDEN"
            raise error
        self._owned(run_id, context)
        state, running = self._launcher.wait(run_id, 0)
        if running:
            self._launcher.cancel(run_id, "cancelled by the operator")
        return self._await(run_id, context, 5.0, cancel_on_abort=False)

    def set_session_symbol_consent(self, context: OperationContext, allowed: bool, *,
                                   attended: bool = False) -> bool:
        """``/crash symbols on|off``: accepted only from the attended console."""
        if context.source != "repl" or not attended:
            raise debug_error(SYMBOL_SERVER_NEEDS_CONSOLE,
                              "symbol-server consent is set only at the attended console")
        self._consent.set_session(context, bool(allowed))
        return bool(allowed)

    def symbol_consent(self, context: OperationContext) -> bool:
        return bool(self._consent.allowed(context))

    def symbol_stores(self) -> tuple[str, ...]:
        return tuple(self._consent.stores())

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _path(path: Any) -> str:
        if not isinstance(path, str) or not path.strip() or len(path) > 1024 or "\x00" in path:
            raise InvalidInput("path must be a non-empty string of at most 1024 characters")
        return path

    @staticmethod
    def _bounded_wait(value: Any, ceiling: float) -> float:
        try:
            number = float(value or 0)
        except (TypeError, ValueError):
            number = 0.0
        return max(0.0, min(float(ceiling), number))

    @staticmethod
    def _require_crash_kind(identity: CaptureIdentity) -> None:
        kind = identity.kind
        if kind in CRASH_KINDS:
            return
        if kind in PROFILE_KINDS:
            raise debug_error(CAPTURE_FORMAT_UNKNOWN,
                              "this is a profiler capture (%s); use profile_digest" % kind)
        if kind == "perfetto_protobuf":
            raise debug_error(CAPTURE_FORMAT_UNKNOWN,
                              "native Perfetto traces are protobuf; convert with `traceconv json`")
        raise debug_error(CAPTURE_FORMAT_UNKNOWN, "not a recognised crash capture")

    @staticmethod
    def _require_pure_profile(identity: CaptureIdentity) -> None:
        kind = identity.kind
        if kind in PURE_PROFILE_KINDS or kind == "profile_csv":
            return
        if kind in HOST_PROFILE_KINDS:
            raise debug_error(
                CAPTURE_NEEDS_HOST_TOOL,
                "%s is a binary capture; use profile_capture_digest (host tool)" % kind)
        if kind == "perfetto_protobuf":
            raise debug_error(CAPTURE_FORMAT_UNKNOWN,
                              "native Perfetto traces are protobuf; convert with `traceconv json`")
        if kind in CRASH_KINDS:
            raise debug_error(CAPTURE_FORMAT_UNKNOWN,
                              "this is a crash capture (%s); use crash_triage" % kind)
        raise debug_error(CAPTURE_FORMAT_UNKNOWN, "not a recognised profile format")

    def _triage_directory(self, path: str, request: CrashTriageRequest, roots: str):
        limit = max(1, min(MAX_DIRECTORY_FILES, int(request.max_files or MAX_DIRECTORY_FILES)))
        identities = self._source.list_dir(path, extra_roots=roots, max_files=limit)
        reports = []
        notes: list[str] = []
        used = 0
        deadline = self._clock() + DIRECTORY_SECONDS
        for listed in identities:
            if used >= DIRECTORY_READ_BUDGET or self._clock() > deadline:
                notes.append("directory read budget exhausted; later files were not read")
                break
            try:
                reader, identity = self._source.open_reader(listed.path, extra_roots=roots)
            except SonderError as exc:
                notes.append("%s: %s" % (listed.label, getattr(exc, "code", "CAPTURE_REJECTED")))
                continue
            try:
                if identity.kind not in CRASH_KINDS:
                    notes.append("%s: not a crash capture (%s)" % (listed.label, identity.kind))
                    continue
                reports.append(self._triage.crash(identity, reader))
            except SonderError as exc:
                notes.append("%s: %s" % (listed.label, getattr(exc, "code", PARSE_FAILED)))
            finally:
                used += int(getattr(reader, "bytes_read", 0) or 0)
                reader.close()
        return self._triage.bucket(reports), _notes(notes, self._redact)

    def _map(self, report):
        if report is None or self._source_map is None:
            return report
        try:
            return self._source_map.map_report(report)
        except (OSError, ValueError, SonderError):
            return report

    def _start(self, plan: DebugPlan, context: OperationContext, payload: Mapping,
               base) -> str:
        if context.expired or context.cancellation.cancelled:
            raise InvalidInput("the operation was cancelled or expired before the run started")
        run_id = JOB_ID_PREFIX + uuid.uuid4().hex
        with self._start_lock:
            if self._launcher.running_for(context.principal_id) >= self._max_concurrent:
                raise debug_error(DEBUG_RUN_BUSY,
                                  "at most %d debug runs may run at once per caller"
                                  % self._max_concurrent)
            self._launcher.start(plan, context, run_id)
        self._launcher.store_json(run_id, "context.json", dict(payload))
        with self._memo_lock:
            self._tier0[run_id] = base
            while len(self._tier0) > _MAX_REMEMBERED:
                self._tier0.pop(next(iter(self._tier0)))
        return run_id

    def _owned(self, run_id: str, context: OperationContext) -> Mapping[str, str]:
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise _not_found()
        meta = self._launcher.metadata(run_id)
        if (
            meta is None
            or meta.get("kind") not in (CRASH_JOB_KIND, PROFILE_JOB_KIND)
            or meta.get("principal_id") != context.principal_id
        ):
            raise _not_found()
        return meta

    def _await(self, run_id: str, context: OperationContext, wait: float, *,
               cancel_on_abort: bool) -> DebugRunOutcome:
        remaining = context.remaining_seconds
        if remaining is not None:
            wait = max(0.0, min(wait, remaining - 1.0))
        state, running = self._launcher.wait(run_id, 0)
        deadline = self._clock() + wait
        while running:
            if context.cancellation.cancelled or context.expired:
                if cancel_on_abort:
                    self._launcher.cancel(run_id, "caller operation cancelled")
                    state, running = self._launcher.wait(run_id, 5.0)
                    if not running:
                        break
                return self._running(state)
            left = deadline - self._clock()
            if left <= 0:
                return self._running(state)
            state, running = self._launcher.wait(run_id, min(_WAIT_SLICE_SECONDS, left))
        return self._assemble(run_id, state)

    def _running(self, state: DebugRunState) -> DebugRunOutcome:
        return DebugRunOutcome(
            state.run_id, "running",
            notes=("call debug_run_result with this run_id to wait for the result",),
            command_digest=state.command_digest, egress_isolation=state.egress_isolation,
            staging=state.staging)

    # -- result assembly -----------------------------------------------------

    def _assemble(self, run_id: str, state: DebugRunState) -> DebugRunOutcome:
        cached = self._launcher.load_json(run_id, "result.json")
        if cached is not None:
            outcome = self._from_cache(run_id, cached, state)
            if outcome is not None:
                return outcome
        plan_info = self._launcher.load_json(run_id, "plan.json") or {}
        ctx_info = self._launcher.load_json(run_id, "context.json") or {}
        kind = str(plan_info.get("kind") or ctx_info.get("kind") or "crash")
        notes = [str(item) for item in plan_info.get("notes", ()) if isinstance(item, str)]
        notes.extend(state.notes)
        status, error_code, truncated = self._status(state)
        if error_code == OUTPUT_LIMIT:
            notes.append("OUTPUT_LIMIT: a step printed more than the output cap and was stopped")
        if error_code == INPUT_CHANGED:
            notes.append("INPUT_CHANGED: the capture changed during the run; the result is discarded")
        steps = [item for item in plan_info.get("steps", ()) if isinstance(item, Mapping)]
        engines = tuple(str(item) for item in plan_info.get("engines", ()) if isinstance(item, str))
        crash = profile = None
        if error_code != INPUT_CHANGED:
            if kind == "profile":
                profile = self._assemble_profile(run_id, state, plan_info, ctx_info, steps,
                                                 engines, notes, truncated)
            else:
                crash = self._assemble_crash(run_id, state, plan_info, ctx_info, steps,
                                             engines, notes, truncated)
                if crash is None and not error_code:
                    status, error_code = "failed", PARSE_FAILED
        outcome = DebugRunOutcome(
            run_id, status, crash=crash, profile=profile, error_code=error_code,
            notes=_notes(notes, self._redact), command_digest=state.command_digest,
            engines=engines, egress_isolation=state.egress_isolation,
            network=bool(plan_info.get("network")), staging=state.staging,
            display_argvs=tuple(tuple(str(a) for a in item.get("display_argv", ()))
                                for item in steps),
        )
        self._store(run_id, outcome)
        return outcome

    @staticmethod
    def _status(state: DebugRunState) -> tuple[str, str, bool]:
        if state.input_changed:
            return "failed", INPUT_CHANGED, False
        if state.output_limit:
            return "partial", OUTPUT_LIMIT, True
        if state.status in ("cancelled", "timed_out"):
            return state.status, "", False
        if state.status == "failed":
            return "partial", "", False
        return ("partial" if state.status == "partial" else "complete"), "", False

    def _step_text(self, run_id: str, state: DebugRunState, index: int, step: Mapping) -> str | None:
        via = str(step.get("reads_output_via") or "argv")
        if via != "argv":
            text = self._launcher.step_output(run_id, index)
        else:
            if index >= len(state.step_job_ids):
                return None
            try:
                window = self._output.read_output(state.step_job_ids[index],
                                                  max_bytes=OUTPUT_WINDOW_BYTES,
                                                  head_bytes=OUTPUT_HEAD_BYTES)
            except SonderError:
                return None
            text = str(getattr(window, "text", "") or "")
        return self._redact(text) if text is not None else None

    def _base_report(self, run_id: str, ctx_info: Mapping):
        with self._memo_lock:
            base = self._tier0.get(run_id)
        if base is not None:
            return base
        wire = ctx_info.get("tier0")
        if isinstance(wire, Mapping):
            try:
                return self._triage.crash_from_wire(wire)
            except (KeyError, TypeError, ValueError, SonderError):
                return None
        return None

    def _assemble_crash(self, run_id, state, plan_info, ctx_info, steps, engines, notes, truncated):
        report = self._base_report(run_id, ctx_info)
        if report is None:
            notes.append("PARSE_FAILED: the Tier-0 reader could not read this capture")
            return None
        usable = state.status not in ("cancelled", "timed_out")
        for index, step in enumerate(steps):
            parser = str(step.get("parser") or "")
            if not usable or not parser or parser == "dump_syms":
                continue
            text = self._step_text(run_id, state, index, step)
            if text is None:
                notes.append("step %d produced no readable output" % index)
                continue
            try:
                report = self._triage.merge_crash(report, parser, text, state.nonce,
                                                  str(step.get("engine") or ""))
            except SonderError as exc:
                notes.append("PARSE_FAILED: %s output: %s" % (step.get("engine"), _clip(exc, 160)))
        module_symbols = tuple(
            (str(item[0]), str(item[1])) for item in plan_info.get("module_symbols", ())
            if isinstance(item, (list, tuple)) and len(item) == 2)
        if usable and state.status in ("complete", "partial"):
            module_symbols += tuple((str(name), "loaded")
                                    for name in plan_info.get("verified_modules", ())
                                    if isinstance(name, str))
        report = self._triage.finish_crash(
            report, engines=tuple(dict.fromkeys(("pure", *engines))) if usable else ("pure",),
            module_symbols=module_symbols, egress_isolation=state.egress_isolation,
            notes=_notes(notes, self._redact), truncated=truncated)
        return self._map(report)

    def _assemble_profile(self, run_id, state, plan_info, ctx_info, steps, engines, notes, truncated):
        if state.status in ("cancelled", "timed_out"):
            return None
        outputs = []
        for index, step in enumerate(steps):
            text = self._step_text(run_id, state, index, step)
            if text is not None:
                outputs.append((str(step.get("parser") or ""), text))
        raw = ctx_info.get("request") if isinstance(ctx_info.get("request"), Mapping) else {}
        request = ProfileDigestRequest(
            path=str(plan_info.get("input_label") or ""),
            top_n=int(raw.get("top_n") or 25),
            frame_budget_ms=raw.get("frame_budget_ms"),
            thread=str(raw.get("thread") or ""), frame_zone=str(raw.get("frame_zone") or ""))
        try:
            return self._triage.profile_from_steps(
                str(plan_info.get("input_label") or ""), state.input_sha256,
                str(plan_info.get("source_kind") or ""), tuple(outputs), request,
                engines=engines, egress_isolation=state.egress_isolation,
                notes=_notes(notes, self._redact), truncated=truncated)
        except SonderError as exc:
            notes.append("PARSE_FAILED: %s" % _clip(exc, 160))
            return None

    def _store(self, run_id: str, outcome: DebugRunOutcome) -> None:
        payload: dict[str, Any] = {
            "status": outcome.status, "error_code": outcome.error_code,
            "notes": list(outcome.notes), "engines": list(outcome.engines),
            "network": outcome.network, "staging": outcome.staging,
            "egress_isolation": outcome.egress_isolation,
            "display_argvs": [list(item) for item in outcome.display_argvs],
        }
        try:
            if outcome.crash is not None:
                payload["crash"] = self._triage.crash_to_wire(outcome.crash)
            if outcome.profile is not None:
                payload["profile"] = self._triage.profile_to_wire(outcome.profile)
            self._launcher.store_json(run_id, "result.json", payload)
        except (OSError, ValueError, TypeError, SonderError):
            pass  # an uncached result is recomputed from the job output next time
        with self._memo_lock:
            self._tier0.pop(run_id, None)

    def _from_cache(self, run_id: str, cached: Mapping, state: DebugRunState) -> DebugRunOutcome | None:
        """A cached result, with identity from the durable run state and text re-redacted."""
        try:
            crash = profile = None
            if isinstance(cached.get("crash"), Mapping):
                crash = self._triage.crash_from_wire(cached["crash"])
            if isinstance(cached.get("profile"), Mapping):
                profile = self._triage.profile_from_wire(cached["profile"])
            status = str(cached.get("status") or "complete")
            if status not in ("complete", "failed", "cancelled", "timed_out", "partial"):
                return None
            return DebugRunOutcome(
                run_id, status, crash=crash, profile=profile,
                error_code=str(cached.get("error_code") or "")[:64],
                notes=_notes([n for n in cached.get("notes", ()) if isinstance(n, str)], self._redact),
                command_digest=state.command_digest,
                engines=tuple(str(e)[:32] for e in cached.get("engines", ()) if isinstance(e, str))[:8],
                egress_isolation=state.egress_isolation, network=bool(cached.get("network")),
                staging=state.staging,
                display_argvs=tuple(
                    tuple(self._redact(str(a)) for a in item)
                    for item in cached.get("display_argvs", ()) if isinstance(item, list))[:8],
            )
        except (KeyError, TypeError, ValueError, RecursionError, SonderError):
            return None


__all__ = [
    "DIRECTORY_READ_BUDGET", "DIRECTORY_SECONDS", "DebugDigestService", "MAX_RESULT_WAIT_SECONDS",
    "MAX_RUN_WAIT_SECONDS",
]
