"""Host-only artifact evidence when unattended execution is refused.

This module never calls a tool or spends an approval. The dispatcher remains
the authority for effects; a preflight here only selects the evidence/report
route. Execution-capable modes keep their existing real-verifier requirement.
"""
from __future__ import annotations

import json
import re

from sonder_runtime.adapters.security.permission_policy import permission_policy
from sonder_runtime.adapters.static_artifact_validation import StaticArtifactEvidence


EXECUTION_VERIFIERS = (
    "test_run", "workspace_run", "script_run", "build_run", "run_code",
    "lint_run", "typecheck_run", "run_project",
)


def refused_execution_verifiers(allowed_tools=None) -> tuple[str, ...]:
    """Return candidates only when the permission gate refuses every one.

    An empty lane allowlist is not proof of permission refusal. A permitted
    candidate, including one allowed by an explicit rule, keeps the strict
    execution gate. No argument-specific one-shot approval is consumed here.
    """
    candidates = tuple(tool for tool in EXECUTION_VERIFIERS
                       if allowed_tools is None or tool in allowed_tools)
    for tool in candidates:
        decision = permission_policy.decide_for_caller(
            tool, interactive=False, gate_control_exempt=False,
            surface="autopilot-verification-preflight", record=False,
        )
        if decision is None or decision.allowed:
            return ()
    return candidates


class AgentArtifactGate:
    """Select a static/deferred receipt from host observations, never prose."""

    def __init__(self, project_root, objective, allowed_tools=None, *, enabled=True):
        self.enabled = enabled and bool(project_root)
        self.allowed_tools = allowed_tools
        self.static = StaticArtifactEvidence(project_root, objective) if self.enabled else None
        self.failed_effect = False

    def observe(self, tool, args, output, *, success, mutation=False, execution=False):
        if self.static is None:
            return
        # An attempted execution or failed mutator may leave arbitrary partial
        # state. It cannot be rescued by inspecting only one known file.
        if execution or (mutation and not success):
            self.failed_effect = True
        self.static.observe(tool, args, output, success=success, mutation=mutation)

    def assess(self):
        empty = {"attempted": False, "passed": False, "evidence": (),
                 "deferred": False, "required": "", "error": ""}
        if self.static is None or self.failed_effect:
            return empty
        refused = refused_execution_verifiers(self.allowed_tools)
        if not refused:
            return empty
        result = self.static.assess()
        result["deferred"] = bool(result.get("deferable", False))
        result["required"] = _verification_command(refused[0], self.static.project_root) if result["deferred"] else ""
        return result

    def receipt_fields(self, assessment):
        if not assessment["attempted"]:
            return {}
        return {
            "validation_evidence": tuple(assessment["evidence"]),
            "verification_deferred": assessment["deferred"],
            "verification_required": assessment["required"],
        }

    def guidance(self):
        if self.enabled and refused_execution_verifiers(self.allowed_tools):
            return ("Execution verifiers need approval. Read back each changed file with "
                    "file_read or file_read_range; the host checks supported syntax statically. "
                    "Report remaining execution checks as pending approval.")
        return "Run or retry an exact validator now."


def _verification_command(tool, project):
    return "%s(root=%s)" % (tool, json.dumps(str(project))) if tool in {
        "test_run", "build_run", "lint_run", "typecheck_run",
    } else tool


def validation_deferral(allowed_tools, task=None, project=""):
    """Fields for a validate task awaiting an execution approval."""
    refused = refused_execution_verifiers(allowed_tools)
    if not refused:
        return None
    instruction = " ".join(str((task or {}).get(key) or "") for key in ("title", "instruction"))
    # A read-only validation remains runnable. Only instructions that actually
    # request execution are deferred; pure inspection keeps its existing path.
    execution = re.search(
        r"\b(?:run|execute|launch|pytest|ctest|npm|tests?|build|lint|typecheck|"
        r"browser|animate|animation|runtime|functional|smoke)\b", instruction, re.I,
    )
    named = next((tool for tool in refused if re.search(r"\b" + tool + r"\b", instruction)), None)
    static_only = re.search(r"\b(?:static|syntax|well-formed|read[- ]?back)\b", instruction, re.I)
    dynamic = re.search(r"\b(?:execute|launch|pytest|ctest|npm|tests?|build|lint|typecheck|browser|animate|animation|runtime|functional|smoke)\b", instruction, re.I)
    if static_only and not (named or dynamic):
        return None
    if task is not None and not (execution or named):
        return None
    tool = named or refused[0]
    command = _verification_command(tool, project)
    if instruction.strip():
        command += " — " + instruction.strip()[:500]
    return {
        "output": "needs approval to run %s" % command,
        "tools": ("permission_preflight",),
        "verification_deferred": True,
        "verification_required": command,
    }
