"""Compose the crash and profile digest tools, their executor, their permission
evaluator and the crash-repro strategy observer.

Nothing here reads a capture, probes a tool or launches a process at
composition time: the inventory is lazy, planning happens per call, the
netns probe runs once on first use and the process provider and job
registry are passed as getters.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from ..adapters.debug_tools_executor import (
    DEBUG_TYPED_TOOLS,
    DebugToolExecutor,
    crash_digest_request,
    profile_request,
)
from ..application.debugging.ports import SYMBOL_SERVER_NEEDS_CONSOLE
from ..application.tools.typed_gateway import default_tool_context
from ..domain.common.errors import Forbidden, SonderError
from .developer_tools import DeveloperToolPermissionEvaluator, ToolResolver

logger = logging.getLogger(__name__)

PLANNED_DEBUG_TOOLS = frozenset({"crash_digest", "profile_capture_digest"})


def _distro() -> str:
    try:
        release = platform.freedesktop_os_release()
    except (OSError, AttributeError):
        return ""
    ident = str(release.get("ID", "")).lower()
    if ident in ("ubuntu", "debian", "fedora", "arch"):
        return ident
    for like in str(release.get("ID_LIKE", "")).lower().split():
        if like in ("ubuntu", "debian", "fedora", "arch"):
            return like
    return ""


def _project_roots() -> list[str]:
    """File roots that are projects: not the filesystem root, home or the state home."""
    from ..adapters.filesystem import file_ops
    from ..platform.paths import default_home

    home = os.path.realpath(os.path.expanduser("~"))
    state = os.path.realpath(str(default_home()))
    out = []
    for root in file_ops.allowed_roots():
        text = os.path.realpath(str(root))
        if Path(text).parent == Path(text) or text in (home, state):
            continue
        if home.startswith(text.rstrip(os.sep) + os.sep):
            continue  # an ancestor of home
        out.append(text)
    return out


def compose_debug_tools(*, config, inventory, digest_output_reader, test_runs=None,
                        process_job_provider: Callable[[], Any],
                        job_registry: Callable[[], Any], redactor):
    """Wire ``DebugDigestService`` over the inventory, the job registry and lanes A/B."""
    del config, test_runs  # the repro lookup (lane D) reads test runs itself
    from ..adapters.debugging.capture_source import GuardedCaptureSource
    from ..adapters.debugging.launcher import RUN_ROOT_NAME, ProcessDebugLauncher
    from ..adapters.debugging.planner import HostDebugPlanner
    from ..adapters.debugging.source_map import ProjectSourceMap
    from ..adapters.debugging.triage import PureCaptureTriage
    from ..adapters.host_tools.bounded_process import run_bounded
    from ..adapters.host_tools.guards import display_redactor, require_host_executable
    from ..adapters.security.permission_policy import permission_policy
    from ..application.debugging.service import DebugDigestService
    from ..platform.netns_probe import netns_available
    from ..platform.paths import state_path
    from ..platform.symbol_consent import SymbolConsentState

    run_root = str(Path(state_path(RUN_ROOT_NAME)))
    redact = getattr(redactor, "redact", None) or (lambda text: text)

    def redact_display(text: str) -> str:
        return redact(display_redactor()(text))

    source = GuardedCaptureSource()
    consent = SymbolConsentState(mode=lambda: permission_policy.current_mode())
    planner = HostDebugPlanner(
        inventory, redact=redact_display, system=platform.system(),
        isolation_probe=lambda path: netns_available(path, runner=run_bounded),
        source=source, state_dir=str(Path(run_root).parent), stores=consent.stores,
        distro=_distro() if platform.system() == "Linux" else "",
        system_root=os.environ.get("SystemRoot", "") if os.name == "nt" else "",
    )
    launcher = ProcessDebugLauncher(process_job_provider, job_registry,
                                    executable_guard=require_host_executable,
                                    run_root=run_root, source=source)
    service = DebugDigestService(
        source, PureCaptureTriage(), planner, launcher, digest_output_reader,
        consent=consent, source_map=ProjectSourceMap(_project_roots), redact=redact,
        clock=time.time,
    )
    logger.info("debug tools composed")
    return service


def debug_tool_executor(service, fallback) -> DebugToolExecutor:
    """The typed executor for the debug tools over ``fallback``."""
    return DebugToolExecutor(service, fallback)


def _refusal(tool: str, code: str, message: str, policy: str) -> Forbidden:
    error = Forbidden("%s refused before execution (%s): %s" % (tool, code, message))
    error.decision = {"tool": tool, "error_code": code, "stage": "plan"}
    error.policy_match = policy
    error.code = code
    return error


def debug_permission_resolvers(debug_service) -> dict[str, ToolResolver]:
    """Resolvers for ``crash_digest`` and ``profile_capture_digest``.

    Plugged into ``DeveloperToolPermissionEvaluator`` (``resolvers=``) next to
    the build tools' resolvers, so the main facade and the lane facade grade
    the debug tools the same way.

    ``symbol_server=true`` on ``crash_digest`` is refused in every mode, for
    every source and at every gate (``debug:network-console-only``): the
    permission modes degrade an unattended ASK to ALLOW, so a risk grade
    cannot carry this rule. The attended console calls the service directly
    with ``console_confirmed`` after its own y/N.

    Off the surface gate a caller-supplied ``resolved_command`` is discarded,
    the request is planned (the identity cache hashes the capture once for
    this plan and the executor's), and the plan's ``resolved_command()`` --
    placeholder argv, tool identities, input sha256, engines, network, stores
    and the command digest -- is what an approval binds. A plan the host
    refuses is refused here, before anyone is asked.
    """

    def resolve(request):
        name = request.tool_name
        if name == "crash_digest" and request.arguments.get("symbol_server"):
            raise _refusal(name, SYMBOL_SERVER_NEEDS_CONSOLE,
                           "symbol-server lookups run only from the attended console "
                           "(/crash <dump> --symbols-online)", "debug:network-console-only")
        if getattr(request.scope, "gate", "gateway") == "surface":
            return request
        arguments = dict(request.arguments)
        arguments.pop("resolved_command", None)
        if debug_service is None:
            return replace(request, arguments=arguments)
        context = default_tool_context(request)
        try:
            if name == "crash_digest":
                plan = debug_service.plan_crash(crash_digest_request(arguments), context)
            else:
                plan = debug_service.plan_profile(profile_request(arguments, capture=True), context)
        except (SonderError, PermissionError, OSError, ValueError, ImportError) as exc:
            code = getattr(exc, "code", "") or "INVALID_INPUT"
            raise _refusal(name, code, str(exc)[:200], "debug:plan-refused") from None
        return replace(request, arguments={**arguments, "resolved_command": plan.resolved_command()})

    # ``on_surface``: the console-only network rule holds on the surface gate too.
    return {name: ToolResolver(resolve, on_surface=True) for name in sorted(PLANNED_DEBUG_TOOLS)}


class DebugToolPermissionEvaluator(DeveloperToolPermissionEvaluator):
    """The developer evaluator with the debug tools' resolvers registered.

    See ``debug_permission_resolvers``; ``resolvers`` and ``grant_authorities``
    (the build tools') pass through, so one evaluator grades both features.
    ``test_run`` keeps the developer behaviour.
    """

    def __init__(self, developer_services, debug_service=None, *, policy_names,
                 resolvers=None, grant_authorities=()) -> None:
        super().__init__(developer_services, policy_names=policy_names,
                         resolvers={**dict(resolvers or {}),
                                    **debug_permission_resolvers(debug_service)},
                         grant_authorities=tuple(grant_authorities))
        self._debug_service = debug_service


def debug_http_authorizer(debug_service):
    """The permission decision for the admin HTTP debug routes that launch host tools.

    ``POST /v1/tools/crash-digest`` and ``/v1/tools/profile-capture-digest``
    call the service directly (``interfaces/http/facades/debug_tools.py``);
    this grades each call first, as the typed gateway would for an HTTP
    caller (``source="http"``, ``gate="gateway"``): the debug resolvers bind
    the host-resolved plan, and the permission modes apply the operator's
    rules, ``plan`` mode and one-shot approvals. Returns
    ``authorize(tool, arguments, context)``, which raises ``Forbidden``.
    """
    import uuid

    from ..application.tools.gateway_contract import (
        ToolGatewayRequest,
        ToolPermission,
        ToolScope,
    )
    from .typed_tools import POLICY_NAMES

    evaluator = DebugToolPermissionEvaluator(None, debug_service, policy_names=POLICY_NAMES)

    def authorize(tool: str, arguments, context) -> str:
        if tool not in PLANNED_DEBUG_TOOLS:
            raise Forbidden("%s is not a host-launching debug tool" % tool)
        request = ToolGatewayRequest(
            request_id="http-debug-" + uuid.uuid4().hex,
            tool_name=tool,
            arguments=dict(arguments),
            scope=ToolScope(
                principal_id=str(context.principal_id),
                workspace_roots=tuple(str(root) for root in context.workspace_roots),
                source="http", auth_level=context.auth_level,
            ),
            permission=ToolPermission(),
            deadline_monotonic=context.deadline_monotonic,
            execution_world="local",
        )
        return evaluator.authorize_request(request)

    return authorize


def observe_crash_repro(trace, *, run_id: str, attempt_number: int, attempt_limit: int,
                        project_dir: str, signature: str, signature_basis: str,
                        repro_selector: str, reproduced_before: bool, reproduced_after: bool,
                        route: str = "", code: str = ""):
    """Observe one crash-fix attempt: metric ``crash_reproduced`` (0 or 1, minimize).

    The failure digest is the full sha256 of the signature and its basis, so
    the same crash keeps one identity across attempts; nothing else from the
    crashed process enters the strategy trace.
    """
    from ..domain.strategy.models import (
        FailureClass,
        FailureObservation,
        ProgressMetric,
        ProgressVector,
        StrategyAction,
        StrategyAttempt,
        StrategyBudget,
        StrategySignature,
        StrategyUsage,
    )

    def digest(value) -> str:
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                         default=str).encode("ascii")).hexdigest()

    scope = digest((project_dir, repro_selector))
    crash_digest = hashlib.sha256((str(signature) + str(signature_basis)).encode("utf-8")).hexdigest()

    def vector(reproduced: bool) -> ProgressVector:
        return ProgressVector(scope, (ProgressMetric("crash_reproduced", 1 if reproduced else 0),),
                              complete=True)

    failure = None
    if reproduced_after:
        failure = FailureObservation(
            FailureClass.TEST_FAILURE if repro_selector else FailureClass.IMPLEMENTATION_FAILURE,
            "CRASH_REPRODUCED", crash_digest)
    attempt = StrategyAttempt(
        run_id, "crash-%s-attempt-%d" % (crash_digest[:12], int(attempt_number)),
        StrategySignature("patch", digest((project_dir, signature, signature_basis)),
                          ("project:" + scope,), digest(code or signature),
                          "fix crashing code path", "repro:" + digest(repro_selector)[:16]),
        "failed" if failure else "succeeded", failure,
        vector(reproduced_before), vector(reproduced_after),
        StrategyUsage(attempts=1, verifier_calls=1),
        model_route=str(route)[:128],
    )
    return trace.record(
        attempt, budget=StrategyBudget(attempts=int(attempt_limit)),
        available_actions=(StrategyAction.REPAIR, StrategyAction.INSPECT, StrategyAction.CRITIC),
        unresolved_effects=False, transport_replay_safe=False,
    )


__all__ = [
    "DEBUG_TYPED_TOOLS", "DebugToolPermissionEvaluator", "compose_debug_tools",
    "debug_http_authorizer", "debug_permission_resolvers",
    "debug_tool_executor", "observe_crash_repro",
]
