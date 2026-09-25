"""Compose the developer tools: host tool inventory, structured test runs and
the output digest, their typed executor and their permission evaluator.

Nothing here probes, reads a project or launches a process at composition
time; the inventory is lazy, planning happens per call and the process
provider and job registry are passed as getters.
"""
from __future__ import annotations

import logging
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from ..adapters.developer_tools_executor import (
    DEVELOPER_TYPED_TOOLS,
    DeveloperToolExecutor,
    test_run_request,
)
from ..adapters.security.permission_evaluator import PermissionModesEvaluator
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


class DeveloperToolPermissionEvaluator(PermissionModesEvaluator):
    """Grade ``test_run`` on the host-resolved command, not on its arguments.

    Before the permission modes decide, a ``test_run`` request is planned and
    its arguments carry ``resolved_command`` (runner, redacted display argv,
    project label, command digest). An operator's one-shot approval of a call
    is therefore bound to the exact command: another project, selector or
    runner digests differently and needs its own approval. A plan the host
    refuses (bad selector, no runner, project outside the roots) is refused
    here, before anyone is asked.
    """

    def __init__(self, services: DeveloperToolServices | None, *, policy_names) -> None:
        super().__init__(policy_names=policy_names)
        self._developer_services = services

    def authorize_request(self, request):
        if request.tool_name == "test_run" and getattr(request.scope, "gate", "gateway") != "surface":
            request = self._resolved(request)
        return super().authorize_request(request)

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
    "DEVELOPER_TYPED_TOOLS", "DeveloperToolPermissionEvaluator", "compose_developer_tools",
    "developer_tool_executor",
]
