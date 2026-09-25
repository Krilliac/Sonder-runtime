"""The bounded build-fix loop: diagnose, propose, validate, write, verify, keep best.

One ``build_fix`` call is one durable in-process job (``tool.build_fix``)
running on a host-injected worker. The loop:

0. builds the requested target once (the baseline) under the fix's build-dir
   reservation; child builds run under a child lease and are started by the
   service itself, never through the model-facing tool, and each must match
   the grant's template family and (build_dir, target, config, platform,
   world, network) tuple;
1. picks the focus: the requested file, else the first attributable error;
2. refuses a focus it may not edit (OUT_OF_SCOPE_FILE,
   BUILD_TIME_TOOL_SOURCE, NEEDS_BUILD_SCRIPT_CHANGE), and a linker-only
   failure (NEEDS_BUILD_SCRIPT_CHANGE);
3. reads the focus through the typed gateway and builds bounded evidence;
4. asks the candidate generator (residency-checked; a refused route stops
   with RESIDENCY_REFUSED before any model call). A malformed or refused
   candidate is HYPOTHESIS_REJECTED and nothing is written;
5. saves each file's pre-image before its first write, then writes through
   the gateway carrying the grant token, so the gateway's typed writes create
   the effect-journal intents;
6. verifies with ``compile_one`` of the focus and then the target build; a
   PCH or forced-include focus, or a unity file without a blob, goes
   straight to the target build;
7. keeps the best candidate by the lexicographic ``progress_key`` and
   reverts anything worse; the strategy controller sees the same attempt
   under its dominance rule (both readings are recorded);
8. stops on FIXED, two attempts without progress, the attempt, model-call
   or wall budget, cancellation, a strategy FAIL, a permission refusal or an
   uncertain side effect (which is never reverted automatically);
9. optionally builds the dependents (``all``) of a fixed target;
10. reports; ``revert_after`` then restores the originals. ``apply=False``
    only proposes (each candidate is validated, nothing is written or
    verified) and is refused unless the operator enabled it.

The job record has ``max_attempts=1``: a crashed fix is never retried. After
a restart ``recover()`` marks unfinished fixes interrupted and
``result``/``restore`` work from the private pre-image store.
"""
from __future__ import annotations

import json
import logging
import posixpath
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping

from ...domain.build.model import safe_rel
from ...domain.build.repair import (
    BuildProgress,
    EditScope,
    FixAttemptRecord,
    FixStopReason,
    MAX_LOOP_CHANGED_LINES,
    MAX_LOOP_FILES,
    RepairEvidence,
    fix_report_to_wire,
    make_file_changes,
    make_fix_report,
    progress_key,
    sha256_text,
    validate_patch,
)
from ...domain.build.tool_targets import classify_targets
from ...domain.common.errors import (
    Cancelled,
    DeadlineExceeded,
    DependencyUnavailable,
    Forbidden,
    InvalidInput,
    SonderError,
)
from ..context import OperationContext
from ..execution import effect_journal
from ..ports import runtime_threads
from ..ports.jobs import JobIdentity, JobStatus
from .fix_ports import (
    BUILD_FIX_ID_RE,
    FIX_SCOPE_REJECTED,
    JOB_NOT_FOUND,
    MAX_RESTORE_FILES,
    RESTORE_CONFLICT,
    BuildFixPlan,
    BuildFixRequest,
    BuildFixStatusView,
    EditConflict,
    EditContext,
    EditRefused,
    ResidencyRefused,
    clamp_fix_timeout,
    fix_error,
    fix_plan_digest,
)
from .grants import (
    BuildFixGrant,
    BuildFixGrantSpec,
    FileSetScope,
    match_child_build,
    restore_plan_digest,
    sha256_hex,
)
from .ports import (
    ACTION_BUILD,
    ACTION_COMPILE_ONE,
    BUILD_FIX_JOB_KIND,
    BUILD_FIX_PREFIX,
    BuildJobRequest,
)

logger = logging.getLogger(__name__)

COMPILE_ONE_TEMPLATES = frozenset({"ninja.compile_one", "msbuild.compile_one"})
BUILD_TEMPLATES = frozenset({"cmake.build", "cmake.build.preset", "make.build", "msbuild.build"})
DEPENDENTS_TARGETS = {"cmake": "all", "make": "all", "msbuild": "Build", "ninja": "all"}
MAX_RESULT_WAIT_SECONDS = 120.0
MAX_RETAINED_RUNS = 64
_CHILD_WAIT_SLICE = 10.0
_CHILD_CANCEL_GRACE = 30.0
_GRANT_GRACE_SECONDS = 60.0
_MAX_DIAGNOSTICS = 400
_MAX_FOCUS_DIAGNOSTICS = 24
_WINDOW_LINES = 80
_MAX_WINDOW_CHARS = 12_000
_MAX_PRIOR = 4
_MAX_NOTE_CHARS = 300
_ABORT_REASONS = frozenset({
    FixStopReason.UNCERTAIN_SIDE_EFFECT, FixStopReason.PERMISSION_DENIED,
    FixStopReason.RESIDENCY_REFUSED, FixStopReason.CANCELLED, FixStopReason.BUILD_DID_NOT_RUN,
})
_TERMINAL_BUILD = frozenset({"succeeded", "failed", "cancelled", "timed_out", "did_not_run"})
_LINK_RE = re.compile(
    r"undefined reference|unresolved external|ld returned|linker command failed|"
    r"multiple definition|cannot find -l|undefined symbol|duplicate symbol|\bLNK\d{4}\b",
    re.IGNORECASE,
)
_OBJECT_SUFFIXES = (".o", ".obj", ".a", ".lib", ".so", ".dll", ".exe", ".dylib")


class _Stop(Exception):
    """Internal: end the loop with a stop reason."""

    def __init__(self, reason: FixStopReason, note: str = "") -> None:
        super().__init__(reason.value)
        self.reason = reason
        self.note = note


class _NeverCancelled:
    @property
    def cancelled(self) -> bool:
        return False

    def wait(self, timeout: float | None = None) -> bool:
        return False


def _clip(text: object, limit: int = _MAX_NOTE_CHARS) -> str:
    value = " ".join(str(text or "").split())
    return value[:limit]


def _context_text(item: Any) -> str:
    """One navigator item as text (clangd returns wire mappings)."""
    if isinstance(item, Mapping):
        return json.dumps(dict(item), sort_keys=True, ensure_ascii=False, default=str)
    return str(item)


def _is_report(value: Any) -> bool:
    return getattr(value, "status", None) in _TERMINAL_BUILD and hasattr(value, "first_errors")


def _value(item: Any) -> str:
    return str(getattr(item, "value", item) or "")


# ---------------------------------------------------------------------------
# Measurement


def report_diagnostics(report: Any) -> tuple:
    """Every diagnostic a build report carries, deduplicated and bounded."""
    seen: set = set()
    out: list = []

    def add(diag: Any) -> None:
        if diag is None or len(out) >= _MAX_DIAGNOSTICS:
            return
        key = diag.dedupe_key() if callable(getattr(diag, "dedupe_key", None)) else id(diag)
        if key in seen:
            return
        seen.add(key)
        out.append(diag)

    for diag in getattr(report, "first_errors", ()) or ():
        add(diag)
    for item in getattr(report, "attributions", ()) or ():
        add(getattr(item, "first_error", None))
        for diag in getattr(item, "diagnostics", ()) or ():
            add(diag)
    return tuple(out)


def is_error(diag: Any) -> bool:
    return str(getattr(diag, "severity", "")) in ("error", "fatal")


def is_link_error(diag: Any) -> bool:
    if not is_error(diag):
        return False
    if str(getattr(diag, "tool", "")) == "msvc_link":
        return True
    if str(getattr(diag, "code", "") or "").upper().startswith("LNK"):
        return True
    label = str(getattr(diag, "file", "") or "").lower()
    if label.endswith(_OBJECT_SUFFIXES):
        return True
    return bool(_LINK_RE.search(str(getattr(diag, "message", "") or "")))


def source_rel(label: object) -> str:
    """The source-relative path a diagnostic label names ('' for build/external files)."""
    text = str(label or "")
    if not text or text.startswith("<"):
        return ""
    rel = safe_rel(text)
    return rel or ""


def warnings_by_file(report: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for diag in report_diagnostics(report):
        if str(getattr(diag, "severity", "")) == "warning":
            rel = source_rel(getattr(diag, "file", ""))
            if rel:
                counts[rel] = counts.get(rel, 0) + 1
    return counts


def measure(report: Any, *, focus: str, edited: frozenset[str] | set[str],
            baseline_warnings: Mapping[str, int], complete: bool | None = None) -> BuildProgress:
    """One report as a ``BuildProgress``; a failed build is never 'fixed'."""
    status = str(getattr(report, "status", ""))
    build_ran = status in ("succeeded", "failed")
    diags = report_diagnostics(report)
    counts = dict(getattr(report, "counts", ()) or ())
    errors_total = int(counts.get("error", 0)) + int(counts.get("fatal", 0))
    listed_errors = [diag for diag in diags if is_error(diag)]
    errors_total = max(errors_total, len(listed_errors))
    focus_errors = sum(1 for diag in listed_errors if source_rel(diag.file) == focus) if focus else 0
    failed_units = sum(1 for item in getattr(report, "attributions", ()) or ()
                       if int(getattr(item, "error_count", 0) or 0) > 0)
    if status == "failed":
        failed_units = max(1, failed_units)
        errors_total = max(1, errors_total)
    link_errors = sum(1 for diag in listed_errors if is_link_error(diag))
    warnings_now: dict[str, int] = {}
    for diag in diags:
        if str(getattr(diag, "severity", "")) == "warning":
            rel = source_rel(diag.file)
            if rel in edited:
                warnings_now[rel] = warnings_now.get(rel, 0) + 1
    new_warnings = sum(max(0, count - int(baseline_warnings.get(rel, 0)))
                       for rel, count in warnings_now.items())
    if complete is None:
        complete = build_ran and _value(getattr(report, "action", "")) != ACTION_COMPILE_ONE
    return BuildProgress(build_ran=build_ran, complete=bool(complete and build_ran),
                         errors_total=errors_total if build_ran else 0,
                         focus_errors=focus_errors if build_ran else 0,
                         failed_units=failed_units if build_ran else 0,
                         link_errors=link_errors if build_ran else 0,
                         new_warnings=new_warnings if build_ran else 0)


# ---------------------------------------------------------------------------
# Run state


@dataclass
class _Run:
    job_id: str
    plan: BuildFixPlan
    ctx: OperationContext
    caller_principal: str
    lease: Any
    grant: BuildFixGrant | None
    authority: BuildFixGrant
    edit_ctx: EditContext
    strategy: Any
    started_at: float
    node_id: str
    done: threading.Event = field(default_factory=threading.Event)
    attempt: int = 0
    child_job_id: str = ""
    report: Any = None
    status: str = "running"
    notes: list = field(default_factory=list)


class BuildFixService:
    """Plan, start, observe, cancel and restore bounded build fixes."""

    def __init__(self, jobs: Any, models: Any, editor: Any, generator: Any, strategy: Any,
                 navigator_factory: Callable[[Any, OperationContext], Any] | None, preimages: Any,
                 registry: Any, cancellation_tree: Any, *, clock: Callable[[], float],
                 max_model_calls: int = 12, grants: Any = None,
                 thread_factory: Callable[..., Any] | None = None,
                 propose_only_ok: bool = False, operator_max_timeout: int = 3600,
                 effect_journal_store: Any = None,
                 monotonic: Callable[[], float] = time.monotonic,
                 utility_allow: frozenset[str] = frozenset()) -> None:
        if isinstance(max_model_calls, bool) or not 1 <= int(max_model_calls) <= 64:
            raise ValueError("max_model_calls must be within 1..64")
        self._jobs = jobs
        self._models = models
        self._editor = editor
        self._generator = generator
        # A strategy port holds one run's history; accept a factory or a port.
        self._strategy_factory = strategy if not hasattr(strategy, "observe") else (lambda: strategy)
        self._navigator_factory = navigator_factory
        self._preimages = preimages
        self._registry = registry
        self._tree = cancellation_tree
        self._clock = clock
        self._monotonic = monotonic
        self._max_model_calls = int(max_model_calls)
        self._grants = grants
        self._thread_factory = thread_factory or runtime_threads.Thread
        self._propose_only_ok = bool(propose_only_ok)
        self._operator_max_timeout = int(operator_max_timeout)
        self._journal = effect_journal_store
        self._utility_allow = frozenset(utility_allow)
        self._runs: dict[str, _Run] = {}
        self._lock = threading.Lock()

    # -- planning ----------------------------------------------------------------

    def plan(self, request: BuildFixRequest, context: OperationContext) -> BuildFixPlan:
        if not isinstance(request, BuildFixRequest):
            raise InvalidInput("request must be a BuildFixRequest")
        if not request.apply and not self._propose_only_ok:
            raise fix_error(FIX_SCOPE_REJECTED,
                            "apply=false (propose only) is disabled; the operator enables it with "
                            "SONDER_BUILD_FIX_PROPOSE_ONLY_OK=1")
        if request.revert_after and not request.apply:
            raise fix_error(FIX_SCOPE_REJECTED, "revert_after needs apply=true")
        job_request = BuildJobRequest(
            project=request.project, build_dir=request.build_dir, action=ACTION_BUILD,
            target=request.target, config=request.config, platform=request.platform,
            allow_network=request.allow_network,
        )
        build_plan = self._jobs.plan(job_request, context)
        model = self._models.model(job_request.model_request(), context)
        safety = classify_targets(model)
        project_root = str(getattr(model, "source_root", "") or build_plan.project_root)
        build_dir = str(build_plan.build_dir)
        excluded_dirs: tuple[str, ...] = ()
        build_rel = _relative_dir(build_dir, project_root)
        if build_rel:
            excluded_dirs = (build_rel,)
        excluded = set(getattr(safety, "tool_sources", ()) or ())
        excluded |= {item for item in (getattr(model, "build_inputs", ()) or ())
                     if isinstance(item, str) and safe_rel(item)}
        try:
            scope = EditScope(
                roots=(project_root,), globs=tuple(request.editable_globs),
                excluded_rel=frozenset(excluded),
                generated_rel=frozenset(getattr(model, "generated_rel", ()) or ()),
                excluded_dirs=excluded_dirs,
            )
        except SonderError as exc:
            raise fix_error(FIX_SCOPE_REJECTED, str(exc)) from None
        notes: list[str] = []
        if request.focus_file:
            rel = safe_rel(request.focus_file.replace("\\", "/"))
            if rel is None:
                raise fix_error(FIX_SCOPE_REJECTED, "focus_file must be relative to the project")
            allowed, why = scope.allows(rel)
            if not allowed:
                raise fix_error(FIX_SCOPE_REJECTED, "focus_file is not editable (%s)" % why)
        system = _value(getattr(build_plan, "system", "")) or _value(getattr(model, "system", ""))
        template_ids = tuple(sorted({build_plan.template_id} | set(COMPILE_ONE_TEMPLATES)))
        extra_targets: tuple[str, ...] = ()
        if request.verify_dependents:
            extra_targets = (DEPENDENTS_TARGETS.get(system, "all"),)
        timeout = clamp_fix_timeout(request.timeout_seconds, self._operator_max_timeout)
        if request.timeout_seconds is not None and request.timeout_seconds > timeout:
            notes.append("timeout clamped to the operator maximum (%ds)" % timeout)
        grant_spec = BuildFixGrantSpec(
            project_root=project_root, build_dir=build_dir,
            target=str(build_plan.target or request.target),
            config=str(build_plan.config or ""), platform=str(build_plan.platform or ""),
            template_ids=template_ids, world=str(build_plan.world),
            network=str(build_plan.network), scope_digest=scope.digest(),
            max_files=MAX_LOOP_FILES, max_changed_lines=MAX_LOOP_CHANGED_LINES,
            expires_at=float(self._clock()) + timeout + _GRANT_GRACE_SECONDS,
            extra_targets=extra_targets,
            max_writes=min(512, request.attempts * MAX_LOOP_FILES * 2 + MAX_LOOP_FILES * 2),
            allow_network=bool(request.allow_network), scope=scope,
        )
        plan_digest = fix_plan_digest(build_plan.resolved_command(), request=request,
                                      grant_spec=grant_spec, timeout_seconds=timeout)
        isolation = str(getattr(build_plan, "isolation_truth", "unverified") or "unverified")
        if isolation not in ("unverified", "failure_isolation_only"):
            isolation = "unverified"
        return BuildFixPlan(
            request=request, build_plan=build_plan, scope=scope, attempts=request.attempts,
            apply=request.apply, revert_after=request.revert_after, grant_spec=grant_spec,
            plan_digest=plan_digest, timeout_seconds=timeout, project_root=project_root,
            project_label=str(getattr(model, "project_label", "") or getattr(build_plan, "project_label", "")),
            model_digest=str(getattr(model, "digest", "") or ""), template_ids=template_ids,
            world=str(build_plan.world), network=str(build_plan.network),
            isolation_truth=isolation,
            notes=tuple(notes) + tuple(str(item) for item in (getattr(safety, "notes", ()) or ()))[:8],
        )

    # -- launch ------------------------------------------------------------------

    def start(self, request: BuildFixRequest, context: OperationContext, *,
              plan: BuildFixPlan | None = None) -> str:
        if context.expired or context.cancellation.cancelled:
            raise InvalidInput("the operation was cancelled or expired before the fix started")
        if plan is not None and (not isinstance(plan, BuildFixPlan) or plan.request != request):
            # An approval binds to the plan of exactly this request.
            raise InvalidInput("the approved fix plan does not match this request")
        plan = plan if plan is not None else self.plan(request, context)
        job_id = BUILD_FIX_PREFIX + uuid.uuid4().hex
        lease = self._jobs.reserve(plan.build_plan.build_dir, job_id, context)
        node_created = False
        grant: BuildFixGrant | None = None
        try:
            if self._registry is not None:
                self._registry.start(
                    JobIdentity(job_id, BUILD_FIX_JOB_KIND, context.correlation_id or job_id, job_id),
                    max_attempts=1,
                    metadata={
                        "kind": BUILD_FIX_JOB_KIND, "principal_id": context.principal_id,
                        "project_root": plan.project_root, "build_dir": plan.build_plan.build_dir,
                        "target": plan.request.target, "plan_digest": plan.plan_digest,
                        "started_at": str(self._clock()),
                    },
                )
            self._preimages.begin(job_id, {
                "job_id": job_id, "principal_id": context.principal_id,
                "project_root": plan.project_root, "project_label": plan.project_label,
                "target": plan.request.target, "config": plan.grant_spec.config,
                "plan_digest": plan.plan_digest, "status": "running",
                "created_at": float(self._clock()),
            })
            if self._grants is not None:
                grant = self._grants.issue(plan.grant_spec, principal_id=context.principal_id,
                                           job_id=job_id, plan_digest=plan.plan_digest)
            node = self._tree.create_child(node_id=job_id)
            node_created = True
            run_ctx = OperationContext(
                correlation_id=job_id, principal_id=context.principal_id,
                auth_level=context.auth_level, source=context.source,
                deadline_monotonic=self._monotonic() + plan.timeout_seconds,
                cancellation=node, workspace_roots=context.workspace_roots,
                cloud_allowed=context.cloud_allowed,
                remote_ollama_allowed=context.remote_ollama_allowed,
                session_id=context.session_id,
            )
            authority = grant or BuildFixGrant(
                token=secrets.token_urlsafe(32), principal_id=context.principal_id, job_id=job_id,
                plan_digest=plan.plan_digest, spec=plan.grant_spec, issued_at=self._clock(),
            )
            run = _Run(
                job_id=job_id, plan=plan, ctx=run_ctx, caller_principal=context.principal_id,
                lease=lease, grant=grant, authority=authority,
                edit_ctx=EditContext(operation=run_ctx, project_root=plan.project_root,
                                     job_id=job_id, grant_token=grant.token if grant else ""),
                strategy=self._strategy_factory(), started_at=self._monotonic(), node_id=job_id,
            )
            if grant is None:
                run.notes.append("no fix grant was minted: every write is graded by the "
                                 "permission mode (unattended callers are refused)")
            with self._lock:
                self._prune()
                self._runs[job_id] = run
            worker = self._thread_factory(target=self._worker, args=(run,),
                                          name="build-fix-%s" % job_id[-8:], daemon=True)
            worker.start()
        except BaseException:
            with self._lock:
                self._runs.pop(job_id, None)
            self._jobs.release(lease)
            if grant is not None and self._grants is not None:
                self._grants.revoke(grant)
            if node_created:
                self._discard_node(job_id)
            self._set_registry(job_id, JobStatus.FAILED, error="the fix could not start")
            self._set_preimage_status(job_id, "failed")
            raise
        return job_id

    def _prune(self) -> None:
        if len(self._runs) < MAX_RETAINED_RUNS:
            return
        finished = sorted((run.started_at, key) for key, run in self._runs.items() if run.done.is_set())
        for _, key in finished[: max(1, len(self._runs) - MAX_RETAINED_RUNS + 1)]:
            self._runs.pop(key, None)

    # -- observation -------------------------------------------------------------

    def _owned_run(self, job_id: str, context: OperationContext) -> _Run | None:
        if not isinstance(job_id, str) or not BUILD_FIX_ID_RE.fullmatch(job_id):
            raise fix_error(JOB_NOT_FOUND, "build fix not found")
        with self._lock:
            run = self._runs.get(job_id)
        if run is not None:
            if run.caller_principal != context.principal_id:
                raise fix_error(JOB_NOT_FOUND, "build fix not found")
            return run
        manifest = self._manifest(job_id)
        if manifest is None or manifest.get("principal_id") != context.principal_id:
            raise fix_error(JOB_NOT_FOUND, "build fix not found")
        return None

    def _manifest(self, job_id: str) -> Mapping[str, Any] | None:
        try:
            return self._preimages.manifest(job_id)
        except SonderError:
            return None

    def status(self, job_id: str, context: OperationContext) -> BuildFixStatusView:
        run = self._owned_run(job_id, context)
        if run is None:
            manifest = self._manifest(job_id) or {}
            return BuildFixStatusView(job_id=job_id, status=str(manifest.get("status", "interrupted")),
                                      attempt=0, attempts=0, elapsed_seconds=0.0,
                                      preimage_label=self._preimages.label(job_id))
        return self._view(run)

    def _view(self, run: _Run) -> BuildFixStatusView:
        return BuildFixStatusView(
            job_id=run.job_id, status=run.status, attempt=run.attempt,
            attempts=run.plan.attempts, elapsed_seconds=max(0.0, self._monotonic() - run.started_at),
            child_job_id=run.child_job_id, preimage_label=self._preimages.label(run.job_id),
        )

    def result(self, job_id: str, context: OperationContext, *, wait_seconds: float = 0) -> Any:
        run = self._owned_run(job_id, context)
        if run is None:
            return self._interrupted_report(job_id, context)
        wait = max(0.0, min(MAX_RESULT_WAIT_SECONDS, float(wait_seconds or 0)))
        remaining = context.remaining_seconds
        if remaining is not None:
            wait = max(0.0, min(wait, remaining - 1.0))
        deadline = self._monotonic() + wait
        while not run.done.is_set():
            left = deadline - self._monotonic()
            if left <= 0 or context.cancellation.cancelled:
                return self._view(run)
            run.done.wait(min(1.0, left))
        return run.report

    def cancel(self, job_id: str, context: OperationContext, *,
               reason: str = "cancelled") -> BuildFixStatusView:
        run = self._owned_run(job_id, context)
        if run is None:
            return self.status(job_id, context)
        if not run.done.is_set():
            try:
                self._tree.cancel(run.node_id, reason=_clip(reason or "cancelled", 120))
            except KeyError:
                pass
            child = run.child_job_id
            if child:
                try:
                    self._jobs.cancel(child, self._cleanup_ctx(run), reason="build fix cancelled")
                except SonderError as exc:
                    logger.debug("child build cancel: %s", exc)
            if self._registry is not None:
                try:
                    self._registry.request_cancellation(job_id, reason="build fix cancelled")
                except (KeyError, ValueError):
                    pass
        return self._view(run)

    # -- recovery and restore ----------------------------------------------------

    def recover(self) -> tuple[str, ...]:
        """Mark fixes a crash left unfinished as interrupted; never retry or revert them."""
        interrupted = []
        for job_id in self._preimages.jobs():
            with self._lock:
                live = job_id in self._runs
            if live:
                continue
            manifest = self._manifest(job_id)
            if manifest is None or manifest.get("status") not in ("running", "planned"):
                continue
            self._set_preimage_status(job_id, "interrupted")
            if self._registry is not None:
                try:
                    record = self._registry.poll(job_id)
                    if not record.is_terminal and record.status is not JobStatus.INTERRUPTED:
                        self._registry.transition(job_id, JobStatus.INTERRUPTED,
                                                  error="runtime restarted during the fix")
                except (KeyError, ValueError):
                    pass
            interrupted.append(job_id)
        return tuple(interrupted)

    def _interrupted_report(self, job_id: str, context: OperationContext) -> dict:
        manifest = self._manifest(job_id) or {}
        files = []
        edit_ctx = EditContext(operation=context, project_root=str(manifest.get("project_root", "")),
                               job_id=job_id)
        for entry in self._preimages.list(job_id):
            current = ""
            try:
                _, current = self._editor.read(entry.rel, edit_ctx)
            except (SonderError, OSError) as exc:
                current = "unreadable: %s" % _clip(exc, 80)
            files.append({"rel": entry.rel, "original_sha256": entry.original_sha256,
                          "last_written_sha256": entry.last_written_sha256,
                          "current_sha256": current})
        return {
            "object": "build_fix_report",
            "status": str(manifest.get("status", "interrupted")),
            "job_id": job_id,
            "target": str(manifest.get("target", "")),
            "preimage_label": self._preimages.label(job_id),
            "files": files,
            "notes": ["the fix did not finish; nothing was reverted automatically",
                      "call build_fix_restore with this job_id to restore the originals"],
        }

    def restore(self, job_id: str, context: OperationContext, *, files: tuple[str, ...] = ()) -> dict:
        """Write the pre-images back through the editor; all-or-nothing preflight."""
        run = self._owned_run(job_id, context)
        if run is not None and not run.done.is_set():
            raise fix_error(RESTORE_CONFLICT, "the fix is still running; cancel it first")
        manifest = self._manifest(job_id)
        if manifest is None:
            raise fix_error(JOB_NOT_FOUND, "build fix not found")
        entries = {entry.rel: entry for entry in self._preimages.list(job_id)}
        wanted = tuple(files) if files else tuple(entries)
        if len(wanted) > MAX_RESTORE_FILES and files:
            raise fix_error(FIX_SCOPE_REJECTED, "restore at most %d files per call" % MAX_RESTORE_FILES)
        unknown = [rel for rel in wanted if rel not in entries]
        if unknown:
            raise fix_error(FIX_SCOPE_REJECTED, "no pre-image for %s" % ", ".join(unknown[:3]))
        project_root = str(manifest.get("project_root", ""))
        grant = None
        if self._grants is not None and wanted:
            spec = BuildFixGrantSpec(
                project_root=project_root, build_dir="", target="",
                scope_digest=FileSetScope(wanted).digest(), max_files=min(len(wanted), 6),
                expires_at=float(self._clock()) + 120.0, max_writes=len(wanted),
                scope=FileSetScope(wanted),
            )
            grant = self._grants.issue(spec, principal_id=context.principal_id, job_id=job_id + ":restore",
                                       plan_digest=restore_plan_digest(job_id, tuple(files)))
        edit_ctx = EditContext(operation=context, project_root=project_root, job_id=job_id,
                               grant_token=grant.token if grant else "")
        try:
            plan: list[tuple[str, str, str]] = []
            conflicts: list[str] = []
            already: list[str] = []
            for rel in wanted:
                entry = entries[rel]
                _, current = self._editor.read(rel, edit_ctx)
                if current == entry.original_sha256:
                    already.append(rel)
                elif entry.last_written_sha256 and current == entry.last_written_sha256:
                    plan.append((rel, current, entry.original_sha256))
                else:
                    conflicts.append(rel)
            if conflicts:
                raise fix_error(RESTORE_CONFLICT, "changed since the fix wrote them: %s"
                                % ", ".join(conflicts[:6]))
            restored = []
            for rel, current, original_sha in plan:
                text, sha = self._preimages.load(job_id, rel)
                if sha != original_sha:
                    raise fix_error(RESTORE_CONFLICT, "pre-image of %s does not match its record" % rel)
                self._editor.replace(rel, text, expected_sha256=current, ctx=edit_ctx)
                self._preimages.record_write(job_id, rel, original_sha)
                restored.append(rel)
        finally:
            if grant is not None:
                self._grants.revoke(grant)
        if restored or already:
            remaining = [rel for rel, entry in entries.items() if rel not in restored and rel not in already]
            if not remaining and not files:
                self._set_preimage_status(job_id, "restored")
        return {"object": "build_fix_restore", "job_id": job_id, "restored": restored,
                "already_original": already, "preimage_label": self._preimages.label(job_id)}

    # -- worker ------------------------------------------------------------------

    def _worker(self, run: _Run) -> None:
        report = None
        terminal = JobStatus.FAILED
        try:
            self._set_registry(run.job_id, JobStatus.RUNNING)
            binding = None
            if self._journal is not None:
                binding = effect_journal.JournalBinding(self._journal, run.job_id, "build-fix", 1,
                                                        run.plan.project_root)
            if binding is not None:
                with effect_journal.bound(binding):
                    report = self._loop(run)
            else:
                report = self._loop(run)
            if report.stop_reason is FixStopReason.CANCELLED:
                terminal = JobStatus.CANCELLED
            elif report.status in ("fixed", "improved", "unchanged"):
                terminal = JobStatus.SUCCEEDED
        except BaseException as exc:  # noqa: BLE001 - the job must end with a report
            logger.exception("build fix %s failed", run.job_id)
            report = self._crash_report(run, exc)
        finally:
            run.report = report
            run.status = getattr(report, "status", "aborted")
            try:
                self._jobs.release(run.lease)
            finally:
                if run.grant is not None and self._grants is not None:
                    self._grants.revoke(run.grant)
                self._discard_node(run.node_id)
                self._set_preimage_status(run.job_id, run.status)
                wire = None
                try:
                    wire = fix_report_to_wire(report) if report is not None else None
                except Exception:  # noqa: BLE001 - the record keeps the status either way
                    wire = None
                self._set_registry(run.job_id, terminal, result=wire,
                                   error="" if terminal is JobStatus.SUCCEEDED else run.status)
                run.done.set()

    def _crash_report(self, run: _Run, exc: BaseException) -> Any:
        return make_fix_report(
            status="aborted", stop_reason=FixStopReason.UNCERTAIN_SIDE_EFFECT, job_id=run.job_id,
            target=run.plan.request.target, config=run.plan.grant_spec.config,
            world=run.plan.world, network=run.plan.network,
            isolation_truth=run.plan.isolation_truth,
            preimage_label=self._preimages.label(run.job_id),
            notes=("internal failure (%s); files may be partly edited: check build_fix_result and "
                   "build_fix_restore" % type(exc).__name__,),
        )

    # -- the loop ----------------------------------------------------------------

    def _loop(self, run: _Run) -> Any:
        state = _LoopState(run)
        try:
            self._run_loop(run, state)
        except _Stop as stop:
            state.stop = stop.reason
            if stop.note:
                state.notes.append(stop.note)
        except (Cancelled,) as exc:
            state.stop = FixStopReason.CANCELLED
            state.notes.append(_clip(exc, 160))
        except DeadlineExceeded:
            state.stop = FixStopReason.BUDGET_EXHAUSTED
            state.notes.append("the fix's wall budget ran out")
        self._revert_unjudged(run, state)
        return self._finish(run, state)

    def _revert_unjudged(self, run: _Run, state: "_LoopState") -> None:
        """A candidate written but never judged (the loop stopped while verifying
        it) is not the best: put the best back. An uncertain side effect is
        never reverted blindly."""
        pending = tuple(sorted(state.pending))
        state.pending = set()
        if not pending or state.stop is FixStopReason.UNCERTAIN_SIDE_EFFECT or state.uncertain:
            return
        try:
            self._revert_files(run, state, pending)
            state.notes.append("the unverified candidate was reverted to the best state")
        except _Stop as stop:
            state.stop = FixStopReason.UNCERTAIN_SIDE_EFFECT
            state.notes.append(stop.note or stop.reason.value)

    def _check_live(self, run: _Run) -> None:
        if run.ctx.cancellation.cancelled:
            raise _Stop(FixStopReason.CANCELLED, "cancelled")
        if run.ctx.expired:
            raise _Stop(FixStopReason.BUDGET_EXHAUSTED, "the fix's wall budget ran out")

    def _run_loop(self, run: _Run, state: "_LoopState") -> None:
        plan, request = run.plan, run.plan.request
        run.strategy.begin(run.job_id, plan.plan_digest, attempts=plan.attempts,
                           max_model_calls=self._max_model_calls, wall_seconds=plan.timeout_seconds)
        self._check_live(run)
        # Step 0: the baseline.
        baseline = self._child_build(run, ACTION_BUILD, target=request.target)
        state.initial_report = baseline
        state.baseline_warnings = warnings_by_file(baseline)
        initial = measure(baseline, focus="", edited=frozenset(), baseline_warnings={})
        state.initial = initial
        state.best = initial
        state.best_report = baseline
        if not initial.build_ran:
            if baseline.status == "cancelled" and run.ctx.cancellation.cancelled:
                raise _Stop(FixStopReason.CANCELLED, "cancelled during the baseline build")
            raise _Stop(FixStopReason.BUILD_DID_NOT_RUN,
                        "the baseline build did not run (%s)" % baseline.status)
        if baseline.status == "succeeded":
            state.notes.append("the target already builds; nothing to fix")
            raise _Stop(FixStopReason.FIXED)
        state.model = self._models.cached_model(run.ctx.principal_id, plan.project_root,
                                                plan.build_plan.build_dir) \
            if hasattr(self._models, "cached_model") else None
        navigator = None
        if self._navigator_factory is not None:
            try:
                navigator = self._navigator_factory(state.model, run.ctx)
            except Exception as exc:  # noqa: BLE001 - navigation is optional
                state.notes.append("navigator unavailable: %s" % _clip(exc, 120))
        try:
            self._attempts(run, state, navigator)
        finally:
            if navigator is not None:
                try:
                    navigator.close()
                except Exception:  # noqa: BLE001
                    pass

    def _attempts(self, run: _Run, state: "_LoopState", navigator: Any) -> None:
        plan = run.plan
        route_hint = ""
        inspect = False
        critic = False
        latest = state.initial_report
        for n in range(1, plan.attempts + 1):
            self._check_live(run)
            run.attempt = n
            if state.model_calls >= self._max_model_calls:
                raise _Stop(FixStopReason.BUDGET_EXHAUSTED, "model-call budget used up")
            focus, focus_diags = self._focus(run, state, latest)
            # Re-read the best measurement against this focus so the keys compare.
            state.best = measure(state.best_report, focus=focus, edited=frozenset(state.files_used),
                                 baseline_warnings=state.baseline_warnings)
            allowed, why = plan.scope.allows(focus)
            if not allowed:
                reason = FixStopReason.__members__.get(why or "", FixStopReason.OUT_OF_SCOPE_FILE)
                raise _Stop(reason, "the focus %s is not editable (%s)" % (focus, why))
            text, sha = self._read(run, state, focus)
            evidence = self._evidence(run, state, focus, text, focus_diags, navigator, inspect, critic)
            inspect = critic = False
            try:
                candidate = self._generator.propose(evidence, run.ctx, route_hint=route_hint)
            except ResidencyRefused as exc:
                raise _Stop(FixStopReason.RESIDENCY_REFUSED, _clip(exc, 200)) from None
            except (Cancelled, DeadlineExceeded):
                raise
            except (SonderError, ValueError, TypeError) as exc:
                state.model_calls += 1
                decision = self._rejected(run, state, n, focus, ("CANDIDATE_REJECTED: %s" % _clip(exc, 200),),
                                          hypothesis="")
                route_hint, inspect, critic = self._apply_decision(decision, route_hint)
                continue
            state.model_calls += 1
            hypothesis = sha256_hex({"hunks": [(h.file_rel, h.anchor, h.replacement)
                                               for h in candidate.hunks]})
            for rel in candidate.files():
                clean = safe_rel(rel)
                if clean and clean not in state.current and plan.scope.allows(clean)[0] \
                        and len(state.current) < MAX_LOOP_FILES:
                    try:
                        self._read(run, state, clean)
                    except EditRefused:
                        pass
            new_texts, reasons = validate_patch(
                candidate, scope=plan.scope, current=dict(state.current),
                loop_changed_lines_used=state.lines_used,
                loop_files_used=frozenset(state.files_used),
            )
            if reasons:
                decision = self._rejected(run, state, n, focus, reasons, hypothesis=hypothesis)
                route_hint, inspect, critic = self._apply_decision(decision, route_hint)
                continue
            if not plan.apply:
                state.proposals = {rel: new for rel, new in new_texts.items()}
                state.attempts.append(FixAttemptRecord(
                    n=n, files=tuple(sorted(new_texts)), outcome="proposed", progress=None,
                    reasons=("propose only: validated, not written or verified",), action="repair"))
                raise _Stop(FixStopReason.ATTEMPTS_EXHAUSTED,
                            "propose only: the first valid candidate is reported, not applied")
            state.pending = set(new_texts)
            intents = self._write(run, state, new_texts)
            state.lines_used += _changed_lines(state, new_texts)
            state.files_used |= set(new_texts)
            report, progress, verifier_calls, scope_used = self._verify(run, state, focus, frozenset(new_texts))
            improved = progress_key(progress) < progress_key(state.best)
            rebaseline = False
            if not improved and self._focus_fixed_elsewhere(state, progress, report, focus, new_texts):
                improved = rebaseline = True
            before = state.best
            state.pending = set()
            if improved:
                state.best = progress
                state.best_report = report
                state.best_texts = dict(state.current)
                state.no_progress = 0
                state.verification_scope = scope_used
                outcome = "fixed" if progress.fixed else "improved"
                failure = None if progress.fixed else "BUILD_FAILURE"
                latest = report
            else:
                self._revert_to_best(run, state, new_texts)
                state.no_progress += 1
                if scope_used == "compile_one" and progress.build_ran:
                    # Only the focus unit was compiled: judge it on the focus.
                    neutral = progress.focus_errors == before.focus_errors
                else:
                    neutral = progress_key(progress) == progress_key(before)
                outcome = "no_progress" if neutral else "regressed"
                failure = "NO_PROGRESS" if neutral else (
                    "VERIFIER_FAILURE" if not progress.build_ran else "BUILD_FAILURE")
                latest = state.best_report
            reasons_out = ("focus fixed; re-baselined on errors revealed in untouched units",) \
                if rebaseline else ()
            record = FixAttemptRecord(
                n=n, files=tuple(sorted(new_texts)), outcome=outcome, progress=progress,
                build_job_id=getattr(report, "job_id", "") or "", reasons=reasons_out,
                action="", effect_intent_ids=tuple(item for item in intents if item),
            )
            if progress.fixed and improved:
                state.attempts.append(replace(record, action="stop"))
                raise _Stop(FixStopReason.FIXED)
            if state.no_progress >= 2:
                state.attempts.append(replace(record, action="stop"))
                raise _Stop(FixStopReason.NO_PROGRESS, "two attempts in a row made no progress")
            decision = run.strategy.observe(record, before=before, after=progress, failure=failure,
                                            hypothesis_digest=hypothesis, focus=focus,
                                            model_calls=1, verifier_calls=verifier_calls)
            state.decisions.append(decision)
            state.attempts.append(replace(record, action=decision.action))
            route_hint, inspect, critic = self._apply_decision(decision, route_hint)
        raise _Stop(FixStopReason.ATTEMPTS_EXHAUSTED)

    def _apply_decision(self, decision: Any, route_hint: str) -> tuple[str, bool, bool]:
        action = getattr(decision, "action", "fail")
        if action == "fail":
            raise _Stop(FixStopReason.STRATEGY_FAIL,
                        "strategy: %s" % _clip(getattr(decision, "reason", ""), 160))
        if action == "switch_model":
            return "alternate", False, False
        return route_hint, action == "inspect", action == "critic"

    def _rejected(self, run: _Run, state: "_LoopState", n: int, focus: str,
                  reasons: tuple[str, ...], *, hypothesis: str) -> Any:
        record = FixAttemptRecord(n=n, files=(), outcome="rejected", progress=None,
                                  reasons=tuple(_clip(item, 240) for item in reasons[:8]))
        decision = run.strategy.observe(record, before=state.best, after=state.best,
                                        failure="HYPOTHESIS_REJECTED", hypothesis_digest=hypothesis,
                                        focus=focus, model_calls=1, verifier_calls=0)
        state.decisions.append(decision)
        state.attempts.append(replace(record, action=decision.action))
        return decision

    # -- focus, evidence ---------------------------------------------------------

    def _focus(self, run: _Run, state: "_LoopState", report: Any) -> tuple[str, tuple]:
        request = run.plan.request
        diags = report_diagnostics(report)
        errors = [diag for diag in diags if is_error(diag)]
        if request.focus_file:
            focus = safe_rel(request.focus_file.replace("\\", "/")) or request.focus_file
            return focus, tuple(diag for diag in diags if source_rel(diag.file) == focus)[:_MAX_FOCUS_DIAGNOSTICS]
        for diag in errors:
            if is_link_error(diag):
                continue
            rel = source_rel(diag.file)
            if rel:
                related = tuple(item for item in diags if source_rel(item.file) == rel)
                return rel, related[:_MAX_FOCUS_DIAGNOSTICS]
        if errors and all(is_link_error(diag) for diag in errors):
            raise _Stop(FixStopReason.NEEDS_BUILD_SCRIPT_CHANGE,
                        "only linker errors remain: the fix edits sources, not link settings")
        if errors:
            raise _Stop(FixStopReason.OUT_OF_SCOPE_FILE,
                        "the errors point at generated or external files")
        raise _Stop(FixStopReason.NEEDS_BUILD_SCRIPT_CHANGE,
                    "the build failed without a source diagnostic")

    def _read(self, run: _Run, state: "_LoopState", rel: str) -> tuple[str, str]:
        if rel in state.current:
            return state.current[rel], state.current_sha[rel]
        try:
            text, sha = self._editor.read(rel, run.edit_ctx)
        except EditRefused as exc:
            raise _Stop(FixStopReason.PERMISSION_DENIED, "reading %s was refused: %s"
                        % (rel, _clip(exc, 160))) from None
        except EditConflict as exc:
            raise _Stop(FixStopReason.OUT_OF_SCOPE_FILE, "%s is not editable: %s"
                        % (rel, _clip(exc, 160))) from None
        state.current[rel] = text
        state.current_sha[rel] = sha
        state.originals.setdefault(rel, (text, sha))
        state.best_texts.setdefault(rel, text)
        return text, sha

    def _evidence(self, run: _Run, state: "_LoopState", focus: str, text: str, diags: tuple,
                  navigator: Any, inspect: bool, critic: bool) -> RepairEvidence:
        lines = [getattr(diag, "line", None) for diag in diags if is_error(diag)]
        line = next((item for item in lines if isinstance(item, int) and item > 0), 1)
        window = _source_window(text, line)
        include_context: tuple[str, ...] = ()
        if navigator is not None:
            try:
                include_context = tuple(_clip(_context_text(item), 600) for item in
                                        navigator.context_for(focus, diags, run.ctx,
                                                              max_items=8)[:8])
            except Exception as exc:  # noqa: BLE001 - navigation is optional
                state.notes.append("navigator failed: %s" % _clip(exc, 100))
        elif inspect:
            state.notes.append("inspect requested; no navigator is configured")
        prior = []
        for item in state.attempts[-_MAX_PRIOR:]:
            summary = "attempt %d: %s" % (item.n, item.outcome)
            if item.reasons:
                summary += " (%s)" % "; ".join(item.reasons[:2])
            prior.append(_clip(summary, 400))
        if critic and prior:
            prior[-1] = _clip("CRITIC: the previous approach did not work; try a materially "
                              "different change. " + prior[-1], 400)
        compiler = ""
        model = state.model
        for chain in getattr(model, "toolchains", ()) or ():
            if getattr(chain, "language", "") in ("CXX", "C"):
                compiler = "%s %s" % (getattr(chain, "compiler_id", ""), getattr(chain, "version", ""))
                break
        return RepairEvidence(
            target=run.plan.request.target, config=run.plan.grant_spec.config, focus_file=focus,
            source_window=window, diagnostics=tuple(diags[:_MAX_FOCUS_DIAGNOSTICS]),
            include_context=include_context, prior_attempts=tuple(prior[-_MAX_PRIOR:]),
            progress=state.best, compiler=_clip(compiler, 120),
        )

    # -- writes ------------------------------------------------------------------

    def _write(self, run: _Run, state: "_LoopState", new_texts: Mapping[str, str]) -> list[str]:
        intents: list[str] = []
        written: list[str] = []
        self._check_live(run)
        for rel in sorted(new_texts):
            before_sha = state.current_sha[rel]
            if rel not in state.preimaged:
                original, original_sha = state.originals[rel]
                self._preimages.save(run.job_id, rel, original, original_sha)
                state.preimaged.add(rel)
            try:
                receipt = self._editor.replace(rel, new_texts[rel], expected_sha256=before_sha,
                                               ctx=run.edit_ctx)
            except EditConflict as exc:
                state.uncertain = exc.uncertain or bool(written)
                if exc.uncertain:
                    state.uncertain_files.add(rel)
                raise _Stop(FixStopReason.UNCERTAIN_SIDE_EFFECT,
                            "%s changed underneath the fix (%s); nothing was reverted"
                            % (rel, _clip(exc, 120))) from None
            except EditRefused as exc:
                self._revert_files(run, state, written)
                raise _Stop(FixStopReason.PERMISSION_DENIED, "writing %s was refused: %s"
                            % (rel, _clip(exc, 160))) from None
            state.current[rel] = new_texts[rel]
            state.current_sha[rel] = receipt.after
            self._preimages.record_write(run.job_id, rel, receipt.after)
            written.append(rel)
            intents.append(receipt.effect_intent_id)
            state.effect_intents.append(receipt.effect_intent_id)
        return intents

    def _revert_files(self, run: _Run, state: "_LoopState", rels: list[str] | tuple[str, ...]) -> None:
        for rel in rels:
            target = state.best_texts.get(rel, state.originals[rel][0])
            if state.current.get(rel) == target:
                continue
            try:
                receipt = self._editor.replace(rel, target, expected_sha256=state.current_sha[rel],
                                               ctx=self._cleanup_edit_ctx(run))
            except EditConflict as exc:
                raise _Stop(FixStopReason.UNCERTAIN_SIDE_EFFECT,
                            "reverting %s failed (%s); nothing further was reverted"
                            % (rel, _clip(exc, 120))) from None
            except EditRefused as exc:
                raise _Stop(FixStopReason.UNCERTAIN_SIDE_EFFECT,
                            "reverting %s was refused (%s); the file keeps the candidate"
                            % (rel, _clip(exc, 120))) from None
            state.current[rel] = target
            state.current_sha[rel] = receipt.after
            self._preimages.record_write(run.job_id, rel, receipt.after)

    def _revert_to_best(self, run: _Run, state: "_LoopState", new_texts: Mapping[str, str]) -> None:
        self._revert_files(run, state, tuple(sorted(new_texts)))

    # -- verification ------------------------------------------------------------

    def _unit_for(self, state: "_LoopState", rel: str) -> Any:
        model = state.model
        if model is None:
            return None
        finder = getattr(model, "units_for", None)
        units = finder(rel) if callable(finder) else ()
        return units[0] if units else None

    def _needs_target_build(self, state: "_LoopState", unit: Any) -> str:
        if unit is None:
            return "the focus is not a compile unit (a header or unmodelled file)"
        if _value(getattr(unit, "pch", "none")) not in ("", "none"):
            return "the focus uses a precompiled header"
        if getattr(unit, "forced_includes", ()):
            return "the focus has forced includes"
        target = None
        finder = getattr(state.model, "target", None)
        if callable(finder) and getattr(unit, "target", ""):
            target = finder(unit.target)
        if target is not None and getattr(target, "unity", False) and not getattr(unit, "unity_blob_rel", ""):
            return "unity build without a blob for the focus"
        return ""

    def _verify(self, run: _Run, state: "_LoopState", focus: str,
                edited: frozenset[str]) -> tuple[Any, BuildProgress, int, str]:
        request = run.plan.request
        calls = 0
        unit = self._unit_for(state, focus)
        forced = self._needs_target_build(state, unit)
        if forced:
            state.note_once("verification by target build: %s" % forced)
        else:
            report = self._child_build(run, ACTION_COMPILE_ONE, file=focus)
            if isinstance(report, _Skipped):
                state.note_once("verification by target build: no per-file build edge")
            elif report.status == "failed":
                calls += 1
                progress = measure(report, focus=focus, edited=edited,
                                   baseline_warnings=state.baseline_warnings, complete=False)
                if progress.focus_errors >= state.best.focus_errors or progress.focus_errors == 0:
                    # No gain on the focus: the full build cannot beat the best.
                    return report, progress, calls, "compile_one"
            elif report.status != "succeeded":
                progress = measure(report, focus=focus, edited=edited,
                                   baseline_warnings=state.baseline_warnings, complete=False)
                if not progress.build_ran and run.ctx.cancellation.cancelled:
                    raise _Stop(FixStopReason.CANCELLED, "cancelled during verification")
                return report, progress, calls, "compile_one"
        report = self._child_build(run, ACTION_BUILD, target=request.target)
        calls += 1
        progress = measure(report, focus=focus, edited=edited,
                           baseline_warnings=state.baseline_warnings)
        if not progress.build_ran and run.ctx.cancellation.cancelled:
            raise _Stop(FixStopReason.CANCELLED, "cancelled during verification")
        return report, progress, calls, "target"

    def _focus_fixed_elsewhere(self, state: "_LoopState", progress: BuildProgress, report: Any,
                               focus: str, new_texts: Mapping[str, str]) -> bool:
        """The focus compiles now and only untouched units fail: keep it and re-baseline.

        A build without keep-going stops at the first failing units, so fixing
        the focus reveals errors in units that never compiled before; by the
        bare key that reads as a regression. The exception is narrow: only
        source (not header) edits, a complete target build, no error in any
        edited file, and at least one baseline error was on the focus.
        """
        if not progress.build_ran or not progress.complete or progress.focus_errors:
            return False
        if state.best.focus_errors == 0 and focus:
            baseline_focus = sum(1 for diag in report_diagnostics(state.best_report)
                                 if is_error(diag) and source_rel(diag.file) == focus)
            if not baseline_focus:
                return False
        if any(posixpath.splitext(rel)[1].lower() not in _SOURCE_SUFFIXES for rel in new_texts):
            return False
        for diag in report_diagnostics(report):
            if is_error(diag) and source_rel(diag.file) in state.edited_all | set(new_texts):
                return False
        return progress.link_errors <= state.best.link_errors

    def _cleanup_ctx(self, run: _Run) -> OperationContext:
        return replace(run.ctx, cancellation=_NeverCancelled(),
                       deadline_monotonic=self._monotonic() + _CHILD_CANCEL_GRACE)

    def _cleanup_edit_ctx(self, run: _Run) -> EditContext:
        """Reverts must still land after a cancel or an exhausted wall budget:
        the gateway refuses a cancelled or expired request, which would leave
        a rejected candidate (or revert_after's edits) on disk. Cleanup edits
        get a fresh bounded grace deadline and are not cancellable; they still
        carry the grant, so the grant's scope and budgets apply unchanged."""
        return replace(run.edit_ctx, operation=self._cleanup_ctx(run))

    def _child_build(self, run: _Run, action: str, *, target: str = "", file: str = "") -> Any:
        request = run.plan.request
        job_request = BuildJobRequest(
            project=request.project, build_dir=request.build_dir, action=action,
            target=target if action == ACTION_BUILD else "", config=request.config,
            platform=request.platform, file=file, allow_network=request.allow_network,
        )
        self._check_live(run)
        plan = self._jobs.plan(job_request, run.ctx, lease=run.lease)
        compile_one = plan.template_id in COMPILE_ONE_TEMPLATES
        if action == ACTION_COMPILE_ONE and not compile_one:
            # The planner fell back to a target build (no per-file edge): that
            # is the next verification step anyway; do not build twice.
            return _Skipped(action)
        decision = match_child_build(
            run.authority, principal_id=run.ctx.principal_id, template_id=plan.template_id,
            build_dir=plan.build_dir, target=str(plan.target or ""), config=str(plan.config or ""),
            platform=str(plan.platform or ""), world=str(plan.world), network=str(plan.network),
            now=self._clock(), compile_one=compile_one, file_rel=file,
        )
        if not decision.allowed:
            raise _Stop(FixStopReason.PERMISSION_DENIED, "child build outside the grant: %s"
                        % decision.reason)
        child = self._jobs.start(job_request, run.ctx, plan=plan, lease=run.lease,
                                 parent_job_id=run.job_id)
        run.child_job_id = child
        try:
            while True:
                if run.ctx.cancellation.cancelled or run.ctx.expired:
                    cleanup = self._cleanup_ctx(run)
                    try:
                        self._jobs.cancel(child, cleanup, reason="build fix cancelled")
                    except SonderError as exc:
                        logger.debug("child cancel: %s", exc)
                    result = self._jobs.result(child, cleanup, wait_seconds=_CHILD_CANCEL_GRACE)
                    if _is_report(result):
                        return result
                    raise _Stop(FixStopReason.CANCELLED if run.ctx.cancellation.cancelled
                                else FixStopReason.BUDGET_EXHAUSTED,
                                "the child build did not stop in time")
                remaining = run.ctx.remaining_seconds
                wait = _CHILD_WAIT_SLICE if remaining is None else max(0.0, min(_CHILD_WAIT_SLICE, remaining))
                result = self._jobs.result(child, run.ctx, wait_seconds=wait)
                if _is_report(result):
                    return result
        finally:
            run.child_job_id = ""

    # -- finishing -----------------------------------------------------------------

    def _finish(self, run: _Run, state: "_LoopState") -> Any:
        plan = run.plan
        stop = state.stop or FixStopReason.ATTEMPTS_EXHAUSTED
        best = state.best
        initial = state.initial
        notes = list(run.notes) + list(plan.notes) + list(state.notes)
        verification = state.verification_scope
        final_build = state.best_report
        # Dependents of a fixed target.
        if stop is FixStopReason.FIXED and best is not None and best.fixed and state.attempts \
                and plan.request.verify_dependents and not run.ctx.cancellation.cancelled:
            try:
                target = plan.grant_spec.extra_targets[0] if plan.grant_spec.extra_targets else "all"
                dependents = self._child_build(run, ACTION_BUILD, target=target)
                if dependents.status == "succeeded":
                    verification = "target+dependents"
                else:
                    # "fixed" names the requested target only (F24); the
                    # dependents' state is reported, not folded into it.
                    state.dependents_failed = True
                    first = next((diag for diag in report_diagnostics(dependents) if is_error(diag)), None)
                    where = first.location() if first is not None and first.file else "no source location"
                    notes.append("the target builds but its dependents (%s) do not: first error at %s"
                                 % (target, where))
                    edited_headers = [rel for rel in state.files_used
                                      if posixpath.splitext(rel)[1].lower() not in _SOURCE_SUFFIXES]
                    if edited_headers or (first is not None and source_rel(first.file) in state.files_used):
                        notes.append("the fix edited %s; the dependents' failure may come from that edit"
                                     % ", ".join(sorted(state.files_used))[:200])
            except _Stop as extra:
                notes.append("dependents build skipped: %s" % (extra.note or extra.reason.value))
        changes = []
        for rel in sorted(state.originals):
            original = state.originals[rel][0]
            final = state.current.get(rel, original)
            if final != original:
                changes.append((rel, original, final))
        for rel, proposed in sorted(state.proposals.items()):
            changes.append((rel, state.originals.get(rel, ("", ""))[0], proposed))
        files = make_file_changes(tuple(changes[:MAX_LOOP_FILES * 2]))
        applied = plan.apply and any(state.current.get(rel) != state.originals[rel][0]
                                     for rel in state.originals)
        if stop in _ABORT_REASONS:
            status = "aborted"
        elif best is not None and best.fixed and stop is FixStopReason.FIXED and state.attempts:
            status = "fixed"
        elif best is not None and initial is not None and progress_key(best) < progress_key(initial):
            status = "improved"
        else:
            status = "unchanged"
        if stop is FixStopReason.UNCERTAIN_SIDE_EFFECT:
            notes.append("uncertain side effect: nothing was reverted; compare the files with "
                         "build_fix_result and use build_fix_restore")
        if plan.revert_after and applied and stop is not FixStopReason.UNCERTAIN_SIDE_EFFECT:
            try:
                self._revert_all(run, state)
                notes.append("revert_after: the originals were restored after verification")
            except _Stop as exc:
                notes.append("revert_after failed: %s" % (exc.note or exc.reason.value))
        if state.effect_intents and not any(state.effect_intents):
            notes.append("no effect journal was bound; writes carry gateway receipts only")
        return make_fix_report(
            status=status, stop_reason=stop, job_id=run.job_id, target=plan.request.target,
            config=plan.grant_spec.config, verification_scope=verification,
            attempts=tuple(state.attempts[-8:]), initial=initial, best=best, applied=applied,
            revert_after=plan.revert_after, files=files,
            final_build=final_build if _is_report(final_build) else None,
            world=plan.world, network=plan.network, isolation_truth=plan.isolation_truth,
            preimage_label=self._preimages.label(run.job_id) if state.preimaged else "",
            notes=tuple(_clip(item, _MAX_NOTE_CHARS) for item in notes)[:16],
        )

    def _revert_all(self, run: _Run, state: "_LoopState") -> None:
        for rel in sorted(state.originals):
            original, _ = state.originals[rel]
            if state.current.get(rel) == original:
                continue
            try:
                receipt = self._editor.replace(rel, original, expected_sha256=state.current_sha[rel],
                                               ctx=self._cleanup_edit_ctx(run))
            except (EditConflict, EditRefused) as exc:
                raise _Stop(FixStopReason.UNCERTAIN_SIDE_EFFECT,
                            "restoring %s failed: %s" % (rel, _clip(exc, 120))) from None
            state.current[rel] = original
            state.current_sha[rel] = receipt.after
            self._preimages.record_write(run.job_id, rel, receipt.after)

    # -- bookkeeping -------------------------------------------------------------

    def _discard_node(self, node_id: str) -> None:
        try:
            self._tree.discard(node_id)
        except (KeyError, ValueError):
            pass

    def _set_registry(self, job_id: str, status: JobStatus, *, result: Any = None, error: str = "") -> None:
        if self._registry is None:
            return
        try:
            self._registry.transition(job_id, status, result=result, error=error)
        except (KeyError, ValueError) as exc:
            logger.debug("fix registry transition %s: %s", status, exc)

    def _set_preimage_status(self, job_id: str, status: str) -> None:
        try:
            self._preimages.set_status(job_id, status)
        except (SonderError, OSError, ValueError) as exc:
            logger.warning("pre-image status for %s: %s", job_id, exc)


_SOURCE_SUFFIXES = frozenset({".c", ".cc", ".cpp", ".cxx", ".c++", ".ixx", ".cppm"})


class _Skipped:
    """A verification step that did not apply (the planner fell back)."""

    status = "did_not_run"
    first_errors = ()
    attributions = ()
    counts = ()
    job_id = ""

    def __init__(self, action: str) -> None:
        self.action = action


class _LoopState:
    def __init__(self, run: _Run) -> None:
        self.run = run
        self.stop: FixStopReason | None = None
        self.notes: list[str] = []
        self.initial: BuildProgress | None = None
        self.best: BuildProgress | None = None
        self.initial_report: Any = None
        self.best_report: Any = None
        self.baseline_warnings: dict[str, int] = {}
        self.model: Any = None
        self.current: dict[str, str] = {}
        self.current_sha: dict[str, str] = {}
        self.originals: dict[str, tuple[str, str]] = {}
        self.best_texts: dict[str, str] = {}
        self.preimaged: set[str] = set()
        self.proposals: dict[str, str] = {}
        self.pending: set[str] = set()
        self.attempts: list[FixAttemptRecord] = []
        self.decisions: list[Any] = []
        self.effect_intents: list[str] = []
        self.model_calls = 0
        self.lines_used = 0
        self.files_used: set[str] = set()
        self.no_progress = 0
        self.uncertain = False
        self.uncertain_files: set[str] = set()
        self.verification_scope = "target"
        self.dependents_failed = False
        self._noted: set[str] = set()

    @property
    def edited_all(self) -> set[str]:
        return set(self.files_used)

    def note_once(self, text: str) -> None:
        if text not in self._noted:
            self._noted.add(text)
            self.notes.append(text)


def _changed_lines(state: _LoopState, new_texts: Mapping[str, str]) -> int:
    import difflib

    total = 0
    for rel, text in new_texts.items():
        before = state.best_texts.get(rel, state.originals[rel][0]).splitlines()
        after = text.splitlines()
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, before, after,
                                                           autojunk=False).get_opcodes():
            if tag != "equal":
                total += max(i2 - i1, j2 - j1)
    return total


def _source_window(text: str, line: int) -> str:
    if len(text) <= _MAX_WINDOW_CHARS:
        return text
    lines = text.splitlines(keepends=True)
    start = max(0, line - 1 - _WINDOW_LINES // 2)
    end = min(len(lines), start + _WINDOW_LINES)
    window = "".join(lines[start:end])
    return window[:_MAX_WINDOW_CHARS]


def _relative_dir(path: str, root: str) -> str:
    from .grants import relative_in_root

    rel = relative_in_root(path, root)
    return rel or ""


__all__ = [
    "BuildFixService", "COMPILE_ONE_TEMPLATES", "is_error", "is_link_error", "measure",
    "report_diagnostics", "source_rel", "warnings_by_file",
]
