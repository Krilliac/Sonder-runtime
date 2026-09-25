"""Compose the C++ build tools: build model, build jobs and the build-fix loop.

What lives here (docs/architecture/CPP-BUILD-FIX.md, docs/security/BUILD-TOOLS.md):

* ``compose_build_tools`` wires the services over the host tool inventory,
  the durable job registry and the process-job provider. It is lazy: nothing
  probes, reads a project or launches a process at composition time. A
  runtime without the build packages, or one whose composition fails, gets
  ``None`` and every surface answers ``BUILD_TOOLS_UNAVAILABLE``.
* ``build_tool_executor`` puts ``BuildToolExecutor`` in front of the
  developer-tool executor.
* ``build_permission_resolvers`` plans ``build_job`` and ``build_fix`` before
  the permission modes decide, so an approval binds to the host-resolved
  command (``resolved_command``: template id, command digest, target, config,
  platform, world and network) and a plan the host refuses is refused before
  anyone is asked. A build asking for the network gets a second, separate
  decision on ``build_network``.
* ``BuildFixGrantRegistry`` is the narrow authority a ``build_fix`` approval
  mints (F1): the fix's own in-scope source writes may proceed unattended,
  within the plan's edit scope and budgets, while the fix job lives. It never
  adds roots, never changes the mode, never covers the network, and every
  write it admits is recorded on the receipt as
  ``build_fix_grant:<plan_digest>``.
* ``install_build_brief`` chains a principal-keyed build line onto the host
  capability summary of the model-context brief.
"""
from __future__ import annotations

import contextlib
import contextvars
import difflib
import logging
import os
import stat
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from ..adapters.build.executor import (
    BUILD_TYPED_TOOLS,
    BuildToolExecutor,
    build_fix_request,
    build_job_request,
    error_code_for,
    os_error_text,
)
from ..adapters.security.permission_evaluator import SURFACES
from ..application.build.grants import (
    FILE_TOOLS,
    GRANT_SOURCE_PREFIX,
    BuildFixGrantBook,
    BuildFixGrantSpec,
    diff_files_and_lines,
    grant_token_from,
    restore_plan_digest,
)
from ..application.context import LOCAL_OWNER
from ..application.tools.typed_gateway import default_tool_context
from ..domain.common.errors import Forbidden, SonderError
from .developer_tools import ToolResolver

logger = logging.getLogger(__name__)

GRANT_POLICY_PREFIX = GRANT_SOURCE_PREFIX
MAX_PENDING_PLANS = 64
CLAIM_WINDOW_SECONDS = 300.0
MAX_GRANT_FILE_BYTES = 2 * 1024 * 1024
MAX_PROFILES_BYTES = 256 * 1024
BRIEF_MAX_CHARS = 480  # environment_probe caps the capability summary at 480
BUILD_BRIEF_MAX_CHARS = 220
_GUARD_KNOBS = ("extra_roots", "bypass", "developer_authorized")


# --- the build-fix grant ------------------------------------------------------------


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _inside(child: str, parent: str) -> bool:
    child_n, parent_n = _norm(child), _norm(parent)
    return child_n == parent_n or child_n.startswith(parent_n.rstrip(os.sep) + os.sep)


def _realpath_unchanged(path: str) -> str | None:
    """The real path, or None when any component is a link or reparse point."""
    lexical = os.path.normpath(os.path.abspath(path))
    try:
        real = os.path.realpath(lexical)
    except OSError:
        return None
    if _norm(real) != _norm(lexical):
        return None
    return real


@dataclass
class _Pending:
    """An approval the evaluator recorded for one request, not yet claimed."""

    kind: str  # "fix" | "restore"
    principal_id: str
    plan_digest: str
    minted_at: float
    plan: Any = None


class _OutOfScope(Exception):
    pass


class BuildFixGrantRegistry(BuildFixGrantBook):
    """The evaluator side of the build-fix grant (F1), over the lane-B2 book.

    The policy is ``application.build.grants``: the grant value, its spec and
    the pure scope, template and budget matching. This class adds what only
    the host can check and the request binding of the permission evaluator:

    1. ``mint``: the evaluator allowed one ``build_fix`` call (console answer,
       allow rule, ``auto`` or a one-shot approval of exactly this planned
       call); the approved plan is parked under that request id.
    2. ``claim``: the executor serving the same request (and principal) takes
       the plan once, within ``CLAIM_WINDOW_SECONDS``, and the book records
       the approval of its ``plan_digest``. ``BuildFixService.start(plan=)``
       then ``issue``s the grant for its job, consuming the approval; the
       service revokes it when the job ends. A start that fails ``withdraw``s
       the approval, so one approval yields at most one grant.
    3. ``authorize_granted``: a typed ``read_file``/``text_patch``/
       ``write_file`` call carrying ``grant_carrier(token)`` is admitted only
       when the book covers it (principal, expiry, edit scope, budgets) *and*
       the host checks hold: no links in the path, an existing bounded
       regular file, the build directory excluded, no guard knobs, not
       ``plan`` mode, and at most ``max_changed_lines`` per write. Anything
       else is ``""`` -- normal grading, which refuses an unattended write.
       Deny rules and fences are checked after a match by the evaluator's
       preflight and still refuse.

    ``build_fix_restore`` approvals follow the same path (``mint_restore`` /
    ``claim_restore``), bound to ``restore_plan_digest(job_id, files)``.
    """

    def __init__(self, *, clock: Callable[[], float] = time.time,
                 current_mode: Callable[[], str] | None = None) -> None:
        super().__init__(clock=clock)
        self._current_mode = current_mode
        self._pending_lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self._lines_lock = threading.Lock()
        self._lines_used: dict[str, int] = {}

    # -- approvals -------------------------------------------------------------------

    def _park(self, request_id: str, pending: _Pending) -> None:
        with self._pending_lock:
            self._purge_pending_locked(pending.minted_at)
            self._pending.pop(request_id, None)
            while len(self._pending) >= MAX_PENDING_PLANS:
                self._pending.pop(next(iter(self._pending)))
            self._pending[request_id] = pending

    def _purge_pending_locked(self, now: float) -> None:
        for key in [key for key, item in self._pending.items()
                    if now - item.minted_at > CLAIM_WINDOW_SECONDS]:
            self._pending.pop(key, None)

    def mint(self, *, principal_id: str, request_id: str, plan: Any) -> None:
        """Record the approval of one ``build_fix`` request for its planned fix."""
        spec = getattr(plan, "grant_spec", None)
        digest = str(getattr(plan, "plan_digest", "") or "")
        if not isinstance(spec, BuildFixGrantSpec) or spec.scope is None:
            raise Forbidden("build_fix plan carries no grant spec or edit scope")
        real_root = _realpath_unchanged(spec.project_root) if spec.project_root else None
        if not real_root or _norm(real_root) != _norm(spec.project_root):
            raise Forbidden("build_fix grant needs a real, absolute project root")
        if not principal_id or not request_id or len(digest) != 64:
            raise Forbidden("build_fix approval needs a principal, a request and a plan digest")
        self._park(str(request_id), _Pending("fix", str(principal_id), digest, self._clock(), plan))
        logger.info("build_fix approval recorded plan=%s", digest[:16])

    def mint_restore(self, *, principal_id: str, request_id: str, job_id: str,
                     files: tuple[str, ...]) -> None:
        """Record the approval of one ``build_fix_restore`` request."""
        if not principal_id or not request_id or not isinstance(job_id, str) or not job_id:
            return
        digest = restore_plan_digest(job_id, tuple(files))
        self._park(str(request_id), _Pending("restore", str(principal_id), digest, self._clock()))

    def _claim(self, request_id: str, principal_id: str, kind: str) -> _Pending | None:
        now = self._clock()
        with self._pending_lock:
            self._purge_pending_locked(now)
            pending = self._pending.get(request_id)
            if pending is None or pending.kind != kind or pending.principal_id != principal_id:
                return None
            self._pending.pop(request_id, None)
        self.approve(pending.plan_digest, principal_id)
        return pending

    def claim(self, request_id: str, principal_id: str) -> Any:
        """The approved plan of exactly this request and principal, once (or None)."""
        pending = self._claim(str(request_id), str(principal_id), "fix")
        return pending.plan if pending is not None else None

    def claim_restore(self, request_id: str, principal_id: str, job_id: str,
                      files: tuple[str, ...]) -> bool:
        """Whether this restore request was approved for exactly these files."""
        pending = self._claim(str(request_id), str(principal_id), "restore")
        if pending is None:
            return False
        if pending.plan_digest != restore_plan_digest(job_id, tuple(files)):
            self.withdraw(pending.plan_digest, str(principal_id))
            return False
        return True

    def __len__(self) -> int:
        with self._pending_lock:
            self._purge_pending_locked(self._clock())
            pending = len(self._pending)
        return pending + self.live()

    # -- matching --------------------------------------------------------------------

    def authorize_granted(self, request) -> str:
        """Grant authority protocol: a policy match when the grant covers the call."""
        token = grant_token_from(getattr(request, "approval_token", None))
        if not token or request.tool_name not in FILE_TOOLS:
            return ""
        if self._current_mode is not None:
            try:
                if self._current_mode() == "plan":
                    return ""  # the grant never lifts plan mode
            except Exception:
                return ""
        principal_id = request.scope.principal_id
        grant = self.lookup(token)
        if grant is None or grant.principal_id != principal_id:
            return ""
        arguments = dict(request.arguments)
        if any(arguments.get(knob) for knob in _GUARD_KNOBS):
            return ""
        try:
            lines = self._host_checks(grant.spec, request.tool_name, arguments)
        except _OutOfScope as exc:
            logger.info("build_fix grant does not cover %s: %s", request.tool_name, exc)
            return ""
        with self._lines_lock:
            # Cumulative line budget over the job's writes ('-' and '+' each
            # count): the loop validates at most ``max_changed_lines`` of
            # candidate change, and each applied line is written once and
            # reverted at most once, so 4x bounds every legitimate job.
            for stale in [key for key in self._lines_used if self.lookup(key) is None]:
                self._lines_used.pop(stale, None)
            used = self._lines_used.get(token, 0)
            if used + lines > grant.spec.max_changed_lines * 4:
                logger.info("build_fix grant line budget exhausted (%d + %d)", used, lines)
                return ""
            decision = self.authorize(token, principal_id=principal_id,
                                      tool_name=request.tool_name, arguments=arguments)
            if not decision.allowed:
                logger.info("build_fix grant does not cover %s: %s", request.tool_name,
                            decision.reason)
                return ""
            if lines:
                self._lines_used[token] = used + lines
        return decision.source

    def covers_child_build(self, token: str, principal_id: str, build_plan: Any) -> bool:
        """Whether a child build plan matches the grant's template family and tuple.

        ``token`` is the raw grant token or its ``grant_carrier`` form. (The fix
        service matches its own child builds with ``match_child_build``.)
        """
        def text(name: str) -> str:
            return str(getattr(build_plan, name, "") or "")

        decision = self.authorize_build(
            grant_token_from(token) or token, principal_id=principal_id, template_id=text("template_id"),
            build_dir=text("build_dir"), target=text("target"), config=text("config"),
            platform=text("platform"), world=text("world"), network=text("network"))
        return decision.allowed

    def _host_checks(self, spec: BuildFixGrantSpec, tool: str, arguments: Mapping[str, Any]) -> int:
        """What only the host can check: links, file kind, build dir, per-write lines.

        Returns the call's changed lines ('-' and '+' each count; 0 for a read).
        """
        if tool == "text_patch":
            root = arguments.get("root")
            if not isinstance(root, str) or not os.path.isabs(root):
                raise _OutOfScope("patch needs an absolute root")
            real_root = _realpath_unchanged(root)
            if real_root is None or not _inside(real_root, spec.project_root):
                raise _OutOfScope("patch root is outside the project root")
            parsed = diff_files_and_lines(arguments.get("patch"))
            if parsed is None:
                raise _OutOfScope("patch is not a well-formed diff")
            files, changed = parsed
            for rel in files:
                self._in_root(spec, os.path.join(real_root, *rel.split("/")))
            if changed > spec.max_changed_lines * 2:  # a replaced line is one '-' and one '+'
                raise _OutOfScope("the patch changes more lines than the fix may")
            return changed
        path = arguments.get("path")
        self._in_root(spec, path)
        if tool == "write_file":
            content = arguments.get("content")
            if not isinstance(content, str) or len(content) > MAX_GRANT_FILE_BYTES:
                raise _OutOfScope("content is not bounded text")
            before = _read_bounded(path)
            if _changed_lines(before, content) > spec.max_changed_lines:
                raise _OutOfScope("the write changes more lines than the fix may")
            return _changed_lines(before, content, both=True)
        return 0

    @staticmethod
    def _in_root(spec: BuildFixGrantSpec, path: Any) -> str:
        if not isinstance(path, str) or not path or "\x00" in path or not os.path.isabs(path):
            raise _OutOfScope("grant file calls need an absolute path")
        real = _realpath_unchanged(path)
        if real is None:
            raise _OutOfScope("path traverses a link")
        if not _inside(real, spec.project_root) or _norm(real) == _norm(spec.project_root):
            raise _OutOfScope("path is outside the project root")
        build_dir = spec.build_dir
        if build_dir and _inside(real, build_dir):
            raise _OutOfScope("path is inside the build directory")
        if not os.path.isfile(real):
            raise _OutOfScope("grant file calls touch existing regular files only")
        return real


def _read_bounded(path: str) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise _OutOfScope("grant writes replace existing files only") from None
    except OSError:
        raise _OutOfScope("file cannot be opened without following links") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_GRANT_FILE_BYTES:
            raise _OutOfScope("not a bounded regular file")
        chunks, total = [], 0
        while total <= MAX_GRANT_FILE_BYTES:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > MAX_GRANT_FILE_BYTES:
            raise _OutOfScope("file grew beyond the bound")
    finally:
        os.close(fd)
    return b"".join(chunks).decode("utf-8", errors="replace")


def _changed_lines(before: str, after: str, *, both: bool = False) -> int:
    """Changed lines: a replaced line counts once, or as '-' and '+' with ``both``."""
    matcher = difflib.SequenceMatcher(None, before.splitlines(), after.splitlines(), autojunk=False)
    changed = 0
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op != "equal":
            changed += (i2 - i1) + (j2 - j1) if both else max(i2 - i1, j2 - j1)
    return changed


# --- permission resolvers -------------------------------------------------------------


class _PlanStash:
    """Plans a resolver made, kept only until the same call's ``after_allow``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._plans: dict[str, Any] = {}

    def put(self, request_id: str, plan: Any) -> None:
        with self._lock:
            self._plans.pop(request_id, None)
            while len(self._plans) >= MAX_PENDING_PLANS:
                self._plans.pop(next(iter(self._plans)))
            self._plans[request_id] = plan

    def take(self, request_id: str) -> Any:
        with self._lock:
            return self._plans.pop(request_id, None)


def _plan_refused(tool: str, exc: BaseException) -> Forbidden:
    code = error_code_for(exc) if isinstance(exc, (SonderError, ValueError, TypeError)) else (
        "PROJECT_OUTSIDE_ROOTS" if isinstance(exc, PermissionError) else "BUILD_MODEL_UNAVAILABLE")
    error = Forbidden("%s refused before execution (%s): %s" % (tool, code, os_error_text(exc)))
    error.decision = {"tool": tool, "error_code": code, "stage": "plan"}
    error.policy_match = "build:plan-refused"
    return error


def _default_network_decider(request, arguments: Mapping[str, Any]) -> None:
    """The separate ``build_network`` decision, as the unattended caller kind."""
    from ..adapters.execution import effect_fence
    from ..adapters.security.permission_policy import permission_policy

    surface, exempt = SURFACES.get(getattr(request.scope, "source", "repl"), ("system", False))
    decision = permission_policy.decide_for_caller(
        "build_network", interactive=False, gate_control_exempt=exempt, surface=surface,
        arguments=dict(arguments), fence=effect_fence.current(),
    )
    if decision is not None and decision.action != permission_policy.allow_action():
        error = Forbidden("permission gate refused build_network: %s" % decision.reason)
        error.decision = {
            "tool": "build_network", "mode": decision.mode, "risk": decision.risk,
            "source": decision.source, "action": decision.action,
            "call_id": getattr(decision, "call_id", ""),
        }
        error.policy_match = "permission:%s" % decision.source
        raise error


def _default_mode() -> str:
    from ..adapters.security.permission_policy import permission_policy

    return str(permission_policy.current_mode())


def build_permission_resolvers(services, *, grants: BuildFixGrantRegistry | None = None,
                               network_decider: Callable[[Any, Mapping[str, Any]], None] | None = None,
                               current_mode: Callable[[], str] | None = None,
                               ) -> dict[str, ToolResolver]:
    """Resolvers for ``build_job`` and ``build_fix`` (plugged into the evaluator).

    ``services`` may be None (the executor then reports the tools unavailable
    and the modes grade the bare call). Under ``plan`` mode nothing is planned:
    the modes refuse both tools before any tree is read.
    """
    decide_network = network_decider or _default_network_decider
    mode = current_mode or _default_mode
    stash = _PlanStash()

    def planned(request, tool: str, plan_call: Callable[[Mapping[str, Any]], Any]):
        surface = getattr(request.scope, "gate", "gateway") == "surface"
        if services is None:
            return request, None
        try:
            if mode() == "plan":
                return request, None
        except Exception:
            return request, None
        arguments = dict(request.arguments)
        arguments.pop("resolved_command", None)
        try:
            plan = plan_call(arguments)
        except (SonderError, PermissionError, OSError, ValueError, TypeError, ImportError) as exc:
            if surface:
                # The surface already decided; the executor reports the same
                # refusal as a typed error instead of a permission denial.
                return request, None
            raise _plan_refused(tool, exc) from None
        resolved = replace(request, arguments={**arguments, "resolved_command": plan.resolved_command()})
        return resolved, plan

    def resolve_job(request):
        resolved, _ = planned(request, "build_job", lambda arguments: services.jobs.plan(
            build_job_request(arguments), default_tool_context(request)))
        return resolved

    def after_job(resolved, verdict) -> None:
        del verdict
        if resolved.arguments.get("allow_network") is True:
            decide_network(resolved, {"tool": "build_job",
                                      "resolved_command": resolved.arguments.get("resolved_command")})

    def resolve_fix(request):
        fix = getattr(services, "fix", None) if services is not None else None
        if fix is None:
            return request
        resolved, plan = planned(request, "build_fix", lambda arguments: fix.plan(
            build_fix_request(arguments), default_tool_context(request)))
        if plan is not None:
            stash.put(request.request_id, plan)
        return resolved

    def after_fix(resolved, verdict) -> None:
        del verdict
        plan = stash.take(resolved.request_id)
        if resolved.arguments.get("allow_network") is True:
            decide_network(resolved, {"tool": "build_fix",
                                      "resolved_command": resolved.arguments.get("resolved_command")})
        if plan is not None and grants is not None:
            grants.mint(principal_id=resolved.scope.principal_id,
                        request_id=resolved.request_id, plan=plan)

    def resolve_restore(request):
        return request

    def after_restore(resolved, verdict) -> None:
        # The restore's own writes are covered by a grant bound to exactly
        # this job and file list (restore_plan_digest), minted only here.
        del verdict
        if grants is None:
            return
        files = resolved.arguments.get("files") or ()
        job_id = resolved.arguments.get("job_id")
        if isinstance(job_id, str) and isinstance(files, (list, tuple)) \
                and all(isinstance(item, str) for item in files):
            grants.mint_restore(principal_id=resolved.scope.principal_id,
                                request_id=resolved.request_id, job_id=job_id, files=tuple(files))

    return {
        "build_job": ToolResolver(resolve_job, on_surface=True, after_allow=after_job),
        "build_fix": ToolResolver(resolve_fix, on_surface=True, after_allow=after_fix),
        "build_fix_restore": ToolResolver(resolve_restore, on_surface=True,
                                          after_allow=after_restore),
    }


# --- composition ----------------------------------------------------------------------


def load_build_profiles(path: str, *, platform_name: str | None = None) -> tuple[Any, ...]:
    """Parse ``SONDER_BUILD_PROFILES`` when the file is private to its owner.

    POSIX: a regular file, no link, owned by the runtime's uid, mode without
    group/other bits. Windows: refused (with a log line) until ACL
    verification exists -- a profile is argv, so an unverifiable file is not
    loaded rather than trusted.
    """
    if not path:
        return ()
    system = platform_name or os.name
    if system == "nt":
        logger.warning("SONDER_BUILD_PROFILES is ignored on Windows: the file ACL cannot be verified")
        return ()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        logger.warning("SONDER_BUILD_PROFILES could not be opened without following links")
        return ()
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_PROFILES_BYTES:
            logger.warning("SONDER_BUILD_PROFILES is not a bounded regular file")
            return ()
        if stat.S_IMODE(info.st_mode) & 0o077 or info.st_uid != os.geteuid():
            logger.warning("SONDER_BUILD_PROFILES must be owned by the runtime user with mode 0600")
            return ()
        data = os.read(fd, MAX_PROFILES_BYTES + 1)
    finally:
        os.close(fd)
    try:
        from ..domain.build.templates import parse_build_profiles
    except ImportError:
        logger.warning("SONDER_BUILD_PROFILES is ignored: the build domain is absent")
        return ()
    try:
        return tuple(parse_build_profiles(data))
    except (SonderError, ValueError) as exc:
        logger.warning("SONDER_BUILD_PROFILES rejected: %s", exc)
        return ()


def compose_build_tools(*, config, inventory, digest, process_job_provider: Callable[[], Any],
                        job_registry: Callable[[], Any], redactor,
                        grants: BuildFixGrantRegistry | None = None,
                        tools_getter: Callable[[], Any] | None = None,
                        model_gateway_getter: Callable[[], Any] | None = None,
                        cancellation_tree: Any = None,
                        candidate_generator: Any = None) -> Any:
    """Wire the build model, build jobs and (when present) the fix loop.

    Nothing here probes, reads a project or launches: the planner resolves
    tools per plan, the network probe runs on the first job, vcvars capture on
    the first job that needs it. Returns ``BuildToolServices`` or ``None``.

    ``candidate_generator`` replaces the model-backed candidate generator
    (tests and evaluations script the fix loop with it); the runtime leaves
    it unset.
    """
    from ..platform.config import BuildToolsConfig

    settings = getattr(config, "build_tools", None) or BuildToolsConfig()
    try:
        from ..adapters.build.collector import BuildOutputCollector
        from ..adapters.build.environment import ScrubbedEnvironmentProvider
        from ..adapters.build.launcher import RUN_ROOT_NAME, ProcessBuildLauncher
        from ..adapters.build.network import network_wrapper
        from ..adapters.build.planner import ProjectBuildPlanner
        from ..adapters.build.tree_reader import GuardedBuildTreeReader
        from ..adapters.host_tools.guards import display_redactor, require_host_executable
        from ..application.build import BuildToolServices
        from ..application.build.model_service import BuildModelService, LruBuildModelCache
        from ..application.build.run_service import (
            BuildJobService,
            InMemoryBuildDirLeases,
            build_job_liveness,
        )
        from ..platform.paths import state_path
        from .diagnostics import job_output_reader
    except ImportError:
        logger.warning("build tools are not composed: a build package is missing", exc_info=True)
        return None
    redact = getattr(redactor, "redact", None) or (lambda text: text)

    def redact_display(text: str) -> str:
        return redact(display_redactor()(text))

    run_root = str(Path(state_path(RUN_ROOT_NAME)))
    state_dir = str(Path(run_root).parent)
    profiles = load_build_profiles(settings.profiles_file)
    environment = ScrubbedEnvironmentProvider(passthrough=settings.env_passthrough)
    network = network_wrapper(mode=settings.network, lookup=inventory)
    reader = GuardedBuildTreeReader(user_presets=settings.user_presets)
    planner = ProjectBuildPlanner(
        inventory, reader, environment, network, run_root=run_root, redact=redact_display,
        operator_max_timeout=settings.max_timeout_seconds,
        utility_allow=settings.utility_targets, profiles=profiles,
        executable_guard=require_host_executable,
    )
    launcher = ProcessBuildLauncher(process_job_provider, job_registry,
                                    executable_guard=require_host_executable, run_root=run_root)

    # The collector digests the scanned log itself (domain ``digest_text``
    # with the scan's byte count); ``digest`` is the runtime's digest service,
    # kept for the surfaces that summarize arbitrary text.
    del digest
    collector = BuildOutputCollector(run_root, redact=redact_display,
                                     output_reader=job_output_reader(job_registry))
    models = BuildModelService(reader, planner, LruBuildModelCache(), clock=time.time)
    jobs = BuildJobService(planner, launcher, collector, models,
                           InMemoryBuildDirLeases(is_active=build_job_liveness(launcher)),
                           clock=time.time)
    fix = _compose_fix(settings=settings, jobs=jobs, models=models, state_dir=state_dir,
                       inventory=inventory,
                       grants=grants, tools_getter=tools_getter,
                       model_gateway_getter=model_gateway_getter, job_registry=job_registry,
                       cancellation_tree=cancellation_tree, redact=redact_display,
                       candidate_generator=candidate_generator)
    logger.info("build tools composed (fix loop %s)", "on" if fix is not None else "off")
    return BuildToolServices(model=models, jobs=jobs, fix=fix)


def _compose_fix(*, settings, jobs, models, state_dir, grants, tools_getter, inventory,
                 model_gateway_getter, job_registry, cancellation_tree, redact,
                 candidate_generator=None):
    """The build-fix loop (lane B2), or None when its packages are absent."""
    if tools_getter is None:
        return None
    try:
        from ..adapters.build.candidates import ModelCandidateGenerator
        from ..adapters.build.preimages import FilePreimageStore
        from ..adapters.build.source_editor import GatewaySourceEditor
        from ..application.build.fix_service import BuildFixService
        from ..application.build.strategy_bridge import StrategyFixAdapter
        from ..application.cancellation_tree import CancellationTree
    except ImportError:
        logger.info("build_fix is not composed: the fix-loop package is absent")
        return None
    try:
        navigator_factory = clangd_navigator_factory(settings, inventory)

        def execute(request):
            # The typed-tool facade is composed after the build tools, so the
            # editor resolves it per call. Grant enforcement happens in the
            # gateway's permission evaluator, not in the editor.
            facade = tools_getter()
            if facade is None:
                raise RuntimeError("the typed tool gateway is not composed yet")
            return facade.execute(request)

        fix = BuildFixService(
            jobs, models,
            GatewaySourceEditor(execute),
            candidate_generator if candidate_generator is not None else ModelCandidateGenerator(
                _LazyModelGateway(model_gateway_getter), route=settings.fix_model_route,
                redact=redact),
            StrategyFixAdapter(),
            navigator_factory,
            FilePreimageStore(os.path.join(state_dir, "build-fix")),
            _LazyJobRegistry(job_registry),
            cancellation_tree if cancellation_tree is not None else CancellationTree(),
            clock=time.time,
            grants=grants,
            propose_only_ok=settings.fix_propose_only_ok,
            operator_max_timeout=settings.max_timeout_seconds,
        )
    except Exception:
        logger.error("build_fix could not be composed; it will report unavailable", exc_info=True)
        return None
    # The service issues each grant from the approval its start() claims and
    # revokes it when the job ends, so the grant lives exactly as long as it.
    return fix


class _LazyJobRegistry:
    """The durable job registry, resolved on first use (composition stays lazy).

    ``compose_build_tools`` receives the registry as a getter; the fix service
    takes the registry itself (``start``/``transition``/``poll``/...).
    """

    def __init__(self, getter: Callable[[], Any]) -> None:
        self._getter = getter

    def __getattr__(self, name: str) -> Any:
        registry = self._getter()
        if registry is None:
            raise AttributeError(name)
        return getattr(registry, name)


class _LazyModelGateway:
    """The runtime model gateway, resolved per call (composition stays lazy).

    ``ModelCandidateGenerator`` takes a gateway with ``generate`` (and, for
    the residency check, ``resolve_route``); the runtime hands the build tools
    a getter. A missing gateway or resolver raises, and the generator treats
    an unclassifiable route as refused (no source leaves the host).
    """

    def __init__(self, getter: Callable[[], Any] | None) -> None:
        self._getter = getter

    def _gateway(self) -> Any:
        gateway = self._getter() if self._getter is not None else None
        if gateway is None:
            raise RuntimeError("the model gateway is not composed")
        return gateway

    def generate(self, request: Any, context: Any) -> Any:
        return self._gateway().generate(request, context)

    def resolve_route(self, request: Any, context: Any) -> Any:
        resolve = getattr(self._gateway(), "resolve_route", None)
        if not callable(resolve):
            raise RuntimeError("the model gateway cannot classify routes")
        return resolve(request, context)


def clangd_navigator_factory(settings, inventory: Any = None) -> Callable[..., Any] | None:
    """A ``BuildNavigator`` factory over clangd, or None when clangd is absent (lane D).

    The fix service calls ``factory(model, ctx)`` once per fix job with the
    job's cached build model (``source_root``/``build_dir``); composition and
    tests may also pass ``inventory``/``project_root``/``build_dir`` by name.
    Nothing is launched here: the navigator's session starts on first use,
    and only when the inventory resolves ``clangd``.
    """
    try:
        from ..adapters.build.clangd import ClangdNavigator
    except ImportError:
        return None
    enable_config = bool(getattr(settings, "clangd_config", False))
    bound_inventory = inventory

    def factory(model: Any = None, ctx: Any = None, *, inventory: Any = None,
                project_root: str = "", build_dir: str = "", **kwargs):
        del ctx
        lookup = inventory if inventory is not None else bound_inventory
        if model is not None:
            project_root = project_root or str(getattr(model, "source_root", "") or "")
            build_dir = build_dir or str(getattr(model, "build_dir", "") or "")
        if lookup is None or not project_root:
            return None
        record = lookup.lookup("clangd")
        if record is None:
            return None
        return ClangdNavigator(str(record.path), project_root=project_root,
                               compile_commands_dir=build_dir, enable_config=enable_config,
                               **kwargs)

    return factory


def build_tool_executor(services, fallback, *, grants: BuildFixGrantRegistry | None = None):
    """The typed executor for the build tools over ``fallback``."""
    return BuildToolExecutor(services, fallback, grants=grants)


# --- model-context brief --------------------------------------------------------------

_BRIEF_PRINCIPAL: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "sonder_build_brief_principal", default=None)
_BRIEF_PROJECT: contextvars.ContextVar[str] = contextvars.ContextVar(
    "sonder_build_brief_project", default="")
_BRIEF_LOCK = threading.Lock()
_BRIEF_INSTALLED: tuple[Any, Any] | None = None


@contextlib.contextmanager
def build_brief_principal(principal_id: str, *, project_label: str = "") -> Iterator[None]:
    """Declare whose turn the brief is being built for (surfaces call this).

    Without a declared principal the build line is left out (F22): the brief
    provider is process-global, and guessing would show one caller's project
    model to another.
    """
    principal_token = _BRIEF_PRINCIPAL.set(str(principal_id or "") or None)
    project_token = _BRIEF_PROJECT.set(str(project_label or ""))
    try:
        yield
    finally:
        _BRIEF_PROJECT.reset(project_token)
        _BRIEF_PRINCIPAL.reset(principal_token)


def build_brief_line(services) -> str:
    """The principal-keyed build line: cache only, never reads or probes."""
    principal = _BRIEF_PRINCIPAL.get()
    if not principal or services is None:
        return ""
    models = getattr(services, "model", None)
    summary = getattr(models, "cached_summary", None)
    if not callable(summary):
        return ""
    try:
        text = summary(principal, _BRIEF_PROJECT.get())
    except Exception:
        return ""
    if not isinstance(text, str):
        return ""
    text = " ".join(text.split())
    return text[:BUILD_BRIEF_MAX_CHARS]


def install_build_brief(services, inventory_service) -> None:
    """Chain the build line onto the host capability summary (-2 provider).

    Idempotent. The combined line stays within the brief's 480-character
    capability budget (the whole brief stays far under 1,100 characters).
    """
    global _BRIEF_INSTALLED
    import sonder_runtime.platform.environment_probe as environment_probe

    with _BRIEF_LOCK:
        if _BRIEF_INSTALLED == (services, inventory_service):
            return
        _BRIEF_INSTALLED = (services, inventory_service)

        def provider() -> str:
            build = build_brief_line(services)
            budget = BRIEF_MAX_CHARS - (len(build) + 9 if build else 0)
            base = ""
            if inventory_service is not None:
                try:
                    base = inventory_service.capability_summary(max_chars=max(0, budget))
                except Exception:
                    base = ""
            base = str(base or "")[:max(0, budget)]
            if build:
                return (base + " | build: " + build) if base else "build: " + build
            return base

        environment_probe.set_capability_summary_provider(provider)


def uninstall_build_brief() -> None:
    global _BRIEF_INSTALLED
    with _BRIEF_LOCK:
        _BRIEF_INSTALLED = None


# --- HTTP -----------------------------------------------------------------------------


def register_build_http_routes(tools_getter: Callable[[], Any]):
    """The ``/v1/build/*`` dispatcher bound to the typed gateway of the runtime."""
    from ..interfaces.http.facades.build_tools import BuildHttpRoutes

    return BuildHttpRoutes(tools_getter)


__all__ = [
    "BUILD_TYPED_TOOLS", "BuildFixGrantRegistry", "GRANT_POLICY_PREFIX", "build_brief_line",
    "build_brief_principal", "build_permission_resolvers", "build_tool_executor",
    "clangd_navigator_factory", "compose_build_tools", "install_build_brief",
    "load_build_profiles", "register_build_http_routes", "uninstall_build_brief",
]
