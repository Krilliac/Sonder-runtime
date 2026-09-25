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
import posixpath
import secrets
import stat
import threading
import time
from dataclasses import dataclass, field, replace
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
from ..application.context import LOCAL_OWNER
from ..application.tools.typed_gateway import default_tool_context
from ..domain.common.errors import Forbidden, SonderError
from .developer_tools import ToolResolver

logger = logging.getLogger(__name__)

GRANT_POLICY_PREFIX = "build_fix_grant:"
GRANT_TOKEN_PREFIX = "bfg-"
GRANT_TOOLS = frozenset({"text_patch", "write_file", "read_file"})
MAX_GRANTS = 64
MAX_PENDING_PLANS = 64
CLAIM_WINDOW_SECONDS = 300.0
MAX_GRANT_SECONDS = 14_400 + 600
DEFAULT_MAX_FILES = 6
DEFAULT_MAX_CHANGED_LINES = 400
MAX_GRANT_FILE_BYTES = 2 * 1024 * 1024
MAX_PATCH_CHARS = 1_000_000
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


def _value(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


@dataclass
class _Grant:
    """One minted build-fix grant (process memory only, never persisted)."""

    token: str
    principal_id: str
    request_id: str
    plan_digest: str
    project_root: str
    build_dir: str
    scope: Any
    max_files: int
    max_changed_lines: int
    expires_at: float
    minted_at: float
    network: str
    template_ids: frozenset[str]
    target: str
    config: str
    platform: str
    world: str
    job_id: str = ""
    claimed: bool = False
    revoked: bool = False
    files_used: set[str] = field(default_factory=set)
    lines_used: int = 0

    @property
    def policy_match(self) -> str:
        return GRANT_POLICY_PREFIX + self.plan_digest


class BuildFixGrantRegistry:
    """Mint, bind, match and expire build-fix grants (F1).

    Life of a grant:

    1. The permission evaluator allows ``build_fix`` (an operator answered the
       console prompt, an allow rule matched, ``auto`` mode, or a one-shot
       approval of exactly this planned call) and ``mint``s a grant for that
       request, from the fix plan's ``grant_spec`` and ``scope``.
    2. The executor ``claim``s it for the same request and principal, and
       ``bind``s it to the job it starts. An unclaimed grant dies after
       ``CLAIM_WINDOW_SECONDS``.
    3. The fix loop's typed writes carry the token as the request's
       ``approval_token``; ``authorize_granted`` admits a write only when the
       principal, job liveness, path and budgets all match. Anything else is
       ``""`` -- normal grading, which refuses an unattended write.
    4. The grant ends when its job ends (``job_active``), at ``expires_at``,
       or on ``revoke``/``revoke_job``.
    """

    def __init__(self, *, clock: Callable[[], float] = time.time,
                 current_mode: Callable[[], str] | None = None,
                 job_active: Callable[[str, str], bool] | None = None) -> None:
        self._clock = clock
        self._current_mode = current_mode
        self._job_active = job_active
        self._lock = threading.Lock()
        self._grants: dict[str, _Grant] = {}

    def set_job_liveness(self, job_active: Callable[[str, str], bool] | None) -> None:
        """``job_active(principal_id, job_id)``: the fix job is still running."""
        self._job_active = job_active

    # -- minting ---------------------------------------------------------------------

    def mint(self, *, principal_id: str, request_id: str, plan: Any) -> str:
        """A new grant for one approved ``build_fix`` request; returns its token."""
        spec = _value(plan, "grant_spec")
        scope = _value(plan, "scope")
        if spec is None or scope is None or not callable(getattr(scope, "allows", None)):
            raise Forbidden("build_fix plan carries no grant spec or edit scope")
        project_root = str(_value(spec, "project_root", "") or "")
        build_dir = str(_value(spec, "build_dir", "") or "")
        real_root = _realpath_unchanged(project_root) if project_root else None
        if not real_root or not os.path.isabs(real_root):
            raise Forbidden("build_fix grant needs a real, absolute project root")
        now = self._clock()
        expires = _value(spec, "expires_at", None)
        limit = now + MAX_GRANT_SECONDS
        try:
            expires_at = min(float(expires), limit) if expires is not None else limit
        except (TypeError, ValueError):
            expires_at = limit
        if expires_at <= now:
            raise Forbidden("build_fix grant would already be expired")

        def bounded(name: str, default: int) -> int:
            value = _value(spec, name, default)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return default
            return min(value, default)

        grant = _Grant(
            token=GRANT_TOKEN_PREFIX + secrets.token_hex(24),
            principal_id=str(principal_id),
            request_id=str(request_id),
            plan_digest=str(_value(plan, "plan_digest", "") or "")[:128] or "unknown",
            project_root=real_root,
            build_dir=os.path.realpath(build_dir) if build_dir else "",
            scope=scope,
            max_files=bounded("max_files", DEFAULT_MAX_FILES),
            max_changed_lines=bounded("max_changed_lines", DEFAULT_MAX_CHANGED_LINES),
            expires_at=expires_at,
            minted_at=now,
            network=str(_value(spec, "network", "") or ""),
            template_ids=frozenset(str(item) for item in (_value(spec, "template_ids", ()) or ())),
            target=str(_value(spec, "target", "") or ""),
            config=str(_value(spec, "config", "") or ""),
            platform=str(_value(spec, "platform", "") or ""),
            world=str(_value(spec, "world", "") or ""),
        )
        with self._lock:
            self._purge_locked(now)
            if len(self._grants) >= MAX_GRANTS:
                raise Forbidden("too many live build_fix grants")
            self._grants[grant.token] = grant
        logger.info("build_fix grant minted plan=%s", grant.plan_digest[:16])
        return grant.token

    def claim(self, request_id: str, principal_id: str) -> str:
        """The token minted for exactly this request and principal, once."""
        now = self._clock()
        with self._lock:
            self._purge_locked(now)
            for grant in self._grants.values():
                if (grant.request_id == request_id and grant.principal_id == principal_id
                        and not grant.claimed and not grant.revoked
                        and now - grant.minted_at <= CLAIM_WINDOW_SECONDS):
                    grant.claimed = True
                    return grant.token
        return ""

    def bind(self, token: str, job_id: str) -> None:
        with self._lock:
            grant = self._grants.get(token)
            if grant is not None and grant.claimed and not grant.job_id:
                grant.job_id = str(job_id)

    def revoke(self, token: str) -> None:
        with self._lock:
            grant = self._grants.pop(token, None)
            if grant is not None:
                grant.revoked = True

    def revoke_job(self, job_id: str) -> None:
        with self._lock:
            for token in [t for t, g in self._grants.items() if g.job_id == job_id]:
                self._grants.pop(token).revoked = True

    def token_for(self, principal_id: str, job_id: str) -> str:
        """The live token bound to a principal's fix job (for the fix service)."""
        with self._lock:
            for grant in self._grants.values():
                if grant.principal_id == principal_id and grant.job_id == job_id and not grant.revoked:
                    return grant.token
        return ""

    def __len__(self) -> int:
        with self._lock:
            return len(self._grants)

    def _purge_locked(self, now: float) -> None:
        for token in [t for t, g in self._grants.items()
                      if g.revoked or g.expires_at <= now
                      or (not g.claimed and now - g.minted_at > CLAIM_WINDOW_SECONDS)]:
            self._grants.pop(token, None)

    # -- matching --------------------------------------------------------------------

    def _live(self, token: str, principal_id: str) -> _Grant | None:
        now = self._clock()
        with self._lock:
            grant = self._grants.get(token)
            if grant is None or grant.revoked or grant.expires_at <= now:
                return None
            if grant.principal_id != principal_id or not grant.claimed or not grant.job_id:
                return None
        active = self._job_active
        if active is not None:
            try:
                alive = bool(active(grant.principal_id, grant.job_id))
            except Exception:
                logger.warning("build_fix grant liveness check failed", exc_info=True)
                alive = False
            if not alive:
                self.revoke(token)
                return None
        return grant

    def authorize_granted(self, request) -> str:
        """Grant authority protocol: a policy match when the grant covers the call."""
        token = getattr(request, "approval_token", None)
        if not isinstance(token, str) or not token.startswith(GRANT_TOKEN_PREFIX):
            return ""
        if request.tool_name not in GRANT_TOOLS:
            return ""
        if self._current_mode is not None:
            try:
                if self._current_mode() == "plan":
                    return ""  # the grant never lifts plan mode
            except Exception:
                return ""
        grant = self._live(token, request.scope.principal_id)
        if grant is None:
            return ""
        arguments = dict(request.arguments)
        if any(arguments.get(knob) for knob in _GUARD_KNOBS):
            return ""
        try:
            files, lines = self._effect(grant, request.tool_name, arguments)
        except _OutOfScope as exc:
            logger.info("build_fix grant does not cover %s: %s", request.tool_name, exc)
            return ""
        if files:
            with self._lock:
                used = grant.files_used | set(files)
                if len(used) > grant.max_files or grant.lines_used + lines > grant.max_changed_lines:
                    logger.info("build_fix grant budget exhausted (files=%d lines=%d)",
                                len(used), grant.lines_used + lines)
                    return ""
                grant.files_used = used
                grant.lines_used += lines
        return grant.policy_match

    def covers_child_build(self, token: str, principal_id: str, build_plan: Any) -> bool:
        """Whether a child build plan matches the grant's template family and tuple."""
        grant = self._live(token, principal_id)
        if grant is None:
            return False
        template = str(_value(build_plan, "template_id", "") or "")
        if grant.template_ids and template not in grant.template_ids:
            return False
        network = str(_value(build_plan, "network", "") or "")
        if network == "allowed" and grant.network != "allowed":
            return False
        for name in ("target", "config", "platform", "world"):
            expected = getattr(grant, name)
            if expected and str(_value(build_plan, name, "") or "") not in ("", expected):
                return False
        build_dir = str(_value(build_plan, "build_dir", "") or "")
        return not grant.build_dir or (bool(build_dir) and
                                       _norm(os.path.realpath(build_dir)) == _norm(grant.build_dir))

    def _in_scope(self, grant: _Grant, path: str) -> str:
        """The project-relative path of an editable file, or raise ``_OutOfScope``."""
        if not isinstance(path, str) or not path or "\x00" in path or not os.path.isabs(path):
            raise _OutOfScope("grant writes need an absolute path")
        real = _realpath_unchanged(path)
        if real is None:
            raise _OutOfScope("path traverses a link")
        if not _inside(real, grant.project_root) or _norm(real) == _norm(grant.project_root):
            raise _OutOfScope("path is outside the project root")
        if grant.build_dir and _inside(real, grant.build_dir):
            raise _OutOfScope("path is inside the build directory")
        rel = os.path.relpath(real, grant.project_root).replace(os.sep, "/")
        if rel.startswith("../") or rel == "..":
            raise _OutOfScope("path escapes the project root")
        try:
            allowed, reason = grant.scope.allows(rel)
        except Exception as exc:
            raise _OutOfScope("edit scope refused: %s" % type(exc).__name__) from None
        if not allowed:
            raise _OutOfScope(str(reason or "not editable"))
        return rel

    def _effect(self, grant: _Grant, tool: str, arguments: Mapping[str, Any]) -> tuple[tuple[str, ...], int]:
        if tool == "read_file":
            self._in_scope(grant, arguments.get("path"))
            return (), 0
        if tool == "write_file":
            if arguments.get("mode", "overwrite") != "overwrite":
                raise _OutOfScope("only overwrite writes are covered")
            path = arguments.get("path")
            rel = self._in_scope(grant, path)
            content = arguments.get("content")
            if not isinstance(content, str) or len(content) > MAX_GRANT_FILE_BYTES:
                raise _OutOfScope("content is not bounded text")
            before = _read_bounded(path)
            return (rel,), _changed_lines(before, content)
        if tool == "text_patch":
            root = arguments.get("root")
            patch = arguments.get("patch")
            if not isinstance(root, str) or not os.path.isabs(root) \
                    or not isinstance(patch, str) or len(patch) > MAX_PATCH_CHARS:
                raise _OutOfScope("patch needs an absolute root and bounded text")
            real_root = _realpath_unchanged(root)
            if real_root is None or not _inside(real_root, grant.project_root):
                raise _OutOfScope("patch root is outside the project root")
            targets, lines = _patch_targets(patch)
            rels = tuple(dict.fromkeys(
                self._in_scope(grant, os.path.join(real_root, *target.split("/")))
                for target in targets))
            return rels, lines
        raise _OutOfScope("tool is not covered")


class _OutOfScope(Exception):
    pass


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


def _changed_lines(before: str, after: str) -> int:
    matcher = difflib.SequenceMatcher(None, before.splitlines(), after.splitlines(), autojunk=False)
    changed = 0
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op != "equal":
            changed += max(i2 - i1, j2 - j1)
    return changed


def _patch_path(header: str) -> str:
    raw = header[4:].split("\t", 1)[0].strip()
    if raw.startswith('"') or raw == "/dev/null" or not raw:
        raise _OutOfScope("patch creates, deletes or quotes a path")
    if raw.startswith(("a/", "b/")):
        raw = raw[2:]
    rel = posixpath.normpath(raw.replace("\\", "/"))
    if rel.startswith(("/", "../")) or rel in ("..", ".") or (len(rel) > 1 and rel[1] == ":"):
        raise _OutOfScope("patch path is not project-relative")
    return rel


def _patch_targets(patch: str) -> tuple[tuple[str, ...], int]:
    targets: list[str] = []
    lines = 0
    for line in patch.splitlines():
        if line.startswith("--- "):
            source = _patch_path(line)
            targets.append(source)
        elif line.startswith("+++ "):
            dest = _patch_path(line)
            if targets and targets[-1] != dest:
                raise _OutOfScope("patch renames a file")
            if not targets:
                targets.append(dest)
        elif line.startswith(("rename ", "copy ", "new file", "deleted file", "old mode", "new mode",
                              "Binary files", "GIT binary patch")):
            raise _OutOfScope("patch carries a file-level operation")
        elif line.startswith(("+", "-")):
            lines += 1
    if not targets:
        raise _OutOfScope("patch names no file")
    return tuple(targets), lines


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

    return {
        "build_job": ToolResolver(resolve_job, on_surface=True, after_allow=after_job),
        "build_fix": ToolResolver(resolve_fix, on_surface=True, after_allow=after_fix),
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
                       grants=grants, tools_getter=tools_getter,
                       model_gateway_getter=model_gateway_getter, job_registry=job_registry,
                       cancellation_tree=cancellation_tree, redact=redact_display,
                       candidate_generator=candidate_generator)
    logger.info("build tools composed (fix loop %s)", "on" if fix is not None else "off")
    return BuildToolServices(model=models, jobs=jobs, fix=fix)


def _compose_fix(*, settings, jobs, models, state_dir, grants, tools_getter,
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
    except ImportError:
        logger.info("build_fix is not composed: the fix-loop package is absent")
        return None
    try:
        navigator_factory = clangd_navigator_factory(settings)

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
                model_gateway_getter, route=settings.fix_model_route, redact=redact),
            StrategyFixAdapter(),
            navigator_factory,
            FilePreimageStore(os.path.join(state_dir, "build-fix")),
            job_registry,
            cancellation_tree,
            clock=time.time,
            grants=grants,
            propose_only_ok=settings.fix_propose_only_ok,
        )
    except Exception:
        logger.error("build_fix could not be composed; it will report unavailable", exc_info=True)
        return None
    if grants is not None:
        status = getattr(fix, "is_active", None)
        if callable(status):
            grants.set_job_liveness(lambda principal, job_id: bool(status(principal, job_id)))
    return fix


def clangd_navigator_factory(settings) -> Callable[..., Any] | None:
    """A ``BuildNavigator`` factory over clangd, or None when clangd is absent (lane D).

    Nothing is launched here: the factory starts a session per fix job, only
    when the inventory resolves ``clangd``.
    """
    try:
        from ..adapters.build.clangd import ClangdNavigator
    except ImportError:
        return None
    enable_config = bool(getattr(settings, "clangd_config", False))

    def factory(*, inventory, project_root: str, build_dir: str, **kwargs):
        record = inventory.lookup("clangd") if inventory is not None else None
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
