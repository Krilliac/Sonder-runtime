"""Production-boundary evidence for the live typed selfmod application service."""
from __future__ import annotations

import pytest

from sonder_runtime.application.selfmod.governance import GovernancePhase, SelfmodGovernance, WorktreeMetadata


pytestmark = pytest.mark.integration


def test_production_governance_intent_is_local_even_with_unrestricted_authority() -> None:
    governance = SelfmodGovernance()
    governance.propose("candidate", "bounded repair", "a" * 64, unrestricted=True)
    governance.attach_worktree("candidate", WorktreeMetadata("C:/candidate", "selfmod/candidate", "commit"))
    governance.mark_unrestricted_bypass("candidate", "verification")
    governance.mark_unrestricted_bypass("candidate", "reproducer")
    governance.mark_unrestricted_bypass("candidate", "review")
    governance.approve("candidate")

    intent = governance.deployment_intent("candidate")

    assert intent.allowed
    assert intent.remote_push_allowed is False
    assert intent.automatic_push is False
    assert governance.get("candidate").phase is GovernancePhase.DEPLOYMENT_INTENDED


def test_production_governance_rejects_automatic_push() -> None:
    governance = SelfmodGovernance()
    governance.propose("candidate", "bounded repair", "a" * 64)

    intent = governance.deployment_intent("candidate", automatic_push=True)

    assert intent.allowed is False
    assert intent.reason == "automatic_remote_push_forbidden"
    assert governance.get("candidate").phase is GovernancePhase.PROPOSED
