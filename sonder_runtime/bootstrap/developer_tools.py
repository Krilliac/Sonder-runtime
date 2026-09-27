"""Compose the developer tools: host tool inventory, structured test runs and
the output digest, their typed executor and their permission evaluator.

Nothing here probes, reads a project or launches a process at composition
time; the inventory is lazy, planning happens per call and the process
provider and job registry are passed as getters.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping

from ..adapters.developer_tools_executor import (
    DEVELOPER_TYPED_TOOLS,
    DeveloperToolExecutor,
    test_run_request,
)
from ..adapters.execution import effect_fence
from ..adapters.security.permission_evaluator import SURFACES, PermissionModesEvaluator
from ..adapters.testing.artifacts import ReportArtifactCollector
from ..adapters.testing.detection import REPORT_ROOT_NAME, ProjectTestPlanner
from ..adapters.testing.launcher import ProcessTestLauncher
from ..application.developer_tools import DeveloperToolServices
from ..application.testing.service import TestRunService
from ..application.tools.typed_gateway import default_tool_context
from ..domain.common.errors import Forbidden, SonderError

logger = logging.getLogger(__name__)


def compose_developer_tools(*, config, inventory, digest,
                            process_job_provider: Callable[[], Any],
                            job_registry: Callable[[], Any],
                            redactor) -> DeveloperToolServices:
    """Wire the test-run service over the inventory (lane A) and digest (lane C)."""
    del config  # the services read no configuration beyond the state home today
    from ..adapters.host_tools.guards import display_redactor, require_host_executable
    from ..platform.paths import state_path
    from .diagnostics import job_output_reader

    report_root = str(Path(state_path(REPORT_ROOT_NAME)))
    redact = getattr(redactor, "redact", None) or (lambda text: text)

    def redact_display(text: str) -> str:
        # Allowed roots can change at runtime; resolve them per use.
        return redact(display_redactor()(text))

    planner = ProjectTestPlanner(inventory, state_dir=str(Path(report_root).parent),
                                 redact=redact_display)
    launcher = ProcessTestLauncher(process_job_provider, job_registry,
                                   executable_guard=require_host_executable,
                                   report_root=report_root)

    def summarize(text: str, label: str):
        return digest.digest_text(text, label=label).to_wire()

    test_runs = TestRunService(
        planner, launcher, ReportArtifactCollector(report_root),
        output=job_output_reader(job_registry), summarize=summarize,
        redact=redact, clock=time.time,
    )
    logger.info("developer tools composed")
    return DeveloperToolServices(inventory=inventory, test_runs=test_runs, digest=digest)


def developer_tool_executor(services: DeveloperToolServices | None, fallback):
    """The typed executor for the developer tools over ``fallback``."""
    inventory_wire = None
    if services is not None:
        from ..domain.host_tools.model import view_to_wire as inventory_wire
    return DeveloperToolExecutor(services, fallback, inventory_wire=inventory_wire)


@dataclass(frozen=True)
class ToolResolver:
    """How one tool's request is resolved before the permission modes decide.

    ``resolve(request) -> request`` plans the call and returns the request the
    modes grade (typically with ``resolved_command`` injected, so a one-shot
    approval binds to the host-resolved command). It raises ``Forbidden`` for
    a plan the host refuses, before anyone is asked.

    ``on_surface``: also resolve when an in-process surface already decided
    the call (``gate="surface"``). The decision is then the surface's, but a
    resolver whose ``after_allow`` mints authority (``build_fix``) still needs
    the plan.

    ``after_allow(request, verdict)`` runs once the call is allowed, with the
    resolved request and the policy match; it may raise ``Forbidden`` to add a
    second, separate decision (``build_network``) or record authority tied to
    this approval (a build-fix grant).
    """

    resolve: Callable[[Any], Any]
    on_surface: bool = False
    after_allow: Callable[[Any, str], None] | None = None


def _as_resolver(value) -> ToolResolver:
    if isinstance(value, ToolResolver):
        return value
    if callable(value):
        return ToolResolver(value)
    raise TypeError("a permission resolver must be callable or a ToolResolver")


class DeveloperToolPermissionEvaluator(PermissionModesEvaluator):
    """Grade host-planned tools on the host-resolved command, not their arguments.

    Resolution is a map from tool name to ``ToolResolver``; ``test_run`` is one
    entry, and ``build_job``/``build_fix`` are added by the build tools
    (``bootstrap.build_tools.build_permission_resolvers``). Before the
    permission modes decide, a resolved request carries ``resolved_command``
    (runner or template, redacted display argv, project label, command
    digest). An operator's one-shot approval of a call is therefore bound to
    the exact command: another project, selector, target, config or platform
    digests differently and needs its own approval. A plan the host refuses
    (bad selector, unknown target, project outside the roots) is refused here,
    before anyone is asked.

    ``grant_authorities`` are consulted first, for requests that carry an
    in-process grant token (``approval_token``) minted by an earlier approval
    -- the build-fix grant. An authority answers with a policy match for a call
    its grant covers, or ``""`` to fall through to normal grading; it can
    never widen a call it does not recognise.
    """

    def __init__(self, services: DeveloperToolServices | None, *, policy_names,
                 resolvers: Mapping[str, Any] | None = None,
                 grant_authorities: tuple[Any, ...] = ()) -> None:
        super().__init__(policy_names=policy_names)
        self._developer_services = services
        table: dict[str, ToolResolver] = {"test_run": ToolResolver(self._resolved)}
        for name, resolver in dict(resolvers or {}).items():
            table[str(name)] = _as_resolver(resolver)
        self._resolvers = table
        self._grant_authorities = tuple(grant_authorities)

    @property
    def resolvers(self) -> Mapping[str, ToolResolver]:
        return dict(self._resolvers)

    def add_resolvers(self, resolvers: Mapping[str, Any]) -> None:
        """Register more resolvers (composition only; never from a tool call)."""
        for name, resolver in dict(resolvers or {}).items():
            self._resolvers[str(name)] = _as_resolver(resolver)

    def add_grant_authority(self, authority) -> None:
        """Register a grant authority (composition only)."""
        if not callable(getattr(authority, "authorize_granted", None)):
            raise TypeError("a grant authority must define authorize_granted(request)")
        self._grant_authorities = (*self._grant_authorities, authority)

    def authorize_request(self, request):
        if getattr(request, "approval_token", None):
            for authority in self._grant_authorities:
                match = authority.authorize_granted(request)
                if match:
                    self._grant_preflight(request)
                    return match
        resolver = self._resolvers.get(request.tool_name)
        surface = getattr(request.scope, "gate", "gateway") == "surface"
        resolved = request
        if resolver is not None and (not surface or resolver.on_surface):
            resolved = resolver.resolve(request)
        verdict = super().authorize_request(resolved)
        if resolver is not None and resolver.after_allow is not None \
                and (not surface or resolver.on_surface):
            resolver.after_allow(resolved, verdict)
        return verdict

    def _grant_preflight(self, request) -> None:
        """Refuse a granted call anything but the unattended ask would refuse.

        A grant stands in for the operator's answer to the mode's ``ask`` --
        nothing more. An explicit deny rule, ``plan``, a lost effect fence or a
        missing privilege still refuse the call, exactly as without a grant.
        The preflight neither records a receipt nor spends a one-shot approval.
        """
        if getattr(request.scope, "gate", "gateway") == "surface":
            return
        name = self._policy_names.get(request.tool_name, request.tool_name)
        surface, exempt = SURFACES.get(getattr(request.scope, "source", "repl"), ("system", False))
        decision = self._policy.decide_for_caller(
            name, interactive=False, gate_control_exempt=exempt, surface=surface,
            record=False, arguments=dict(request.arguments), fence=effect_fence.current(),
        )
        if decision is None or decision.action == self._policy.allow_action():
            return
        if getattr(decision, "source", "") == "unattended":
            return  # the ask nobody is present to answer: what the grant answers
        error = Forbidden("permission gate refused %s despite the build-fix grant: %s"
                          % (name, decision.reason))
        error.decision = {
            "tool": name, "mode": decision.mode, "risk": decision.risk,
            "source": decision.source, "action": decision.action,
            "call_id": getattr(decision, "call_id", ""),
        }
        error.policy_match = "permission:%s" % decision.source
        raise error

    def _resolved(self, request):
        services = self._developer_services
        if services is None:
            return request
        arguments = dict(request.arguments)
        arguments.pop("resolved_command", None)
        try:
            plan = services.test_runs.plan(test_run_request(arguments), default_tool_context(request))
        except (SonderError, PermissionError, OSError, ValueError) as exc:
            code = getattr(exc, "code", "") or "INVALID_INPUT"
            error = Forbidden("test_run refused before execution (%s): %s" % (code, exc))
            error.decision = {"tool": "test_run", "error_code": code, "stage": "plan"}
            error.policy_match = "developer:plan-refused"
            raise error from None
        return replace(request, arguments={**arguments, "resolved_command": plan.resolved_command()})


__all__ = [
    "DEVELOPER_TYPED_TOOLS", "DeveloperToolPermissionEvaluator", "ToolResolver",
    "compose_developer_tools", "developer_tool_executor",
]
