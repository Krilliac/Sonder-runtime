from dataclasses import replace
from types import SimpleNamespace
import pytest
from tests.test_delegated_verification import lanes
from tests.test_managed_standalone_session import setup
from sonder_runtime.bootstrap.managed_conversation import ManagedConversationLifetime
from sonder_runtime.application.ports.host_final import HostFinalFacts
from sonder_runtime.interfaces.standalone_agent_lanes import HostTerminalDraft
from sonder_runtime.adapters.agent_terminal_evidence import HostObservationLedger


@pytest.mark.parametrize(
    "delegated,terminal_class,code,eligible",
    [
        (False, "NORMAL", "", True),
        (None, "NORMAL", "", False),
        (True, "NORMAL", "", False),
        (True, "UNVERIFIED", "ORIGINAL_PARENT_EVIDENCE_FAILED", True),
    ],
)
def test_no_pending_requires_explicit_sealed_delegation_fact(
    lanes, delegated, terminal_class, code, eligible
):
    app = object()
    lifetime = ManagedConversationLifetime(
        application=app,
        session_factory=lambda c, a: setup(lanes, c)[0],
        require_current=lambda: None,
    )
    try:
        turn = lifetime.factory(SimpleNamespace(run_id="eligibility-turn"), app)
        link = turn.turn_link()
        turn.capture_terminal(
            HostTerminalDraft(
                HostObservationLedger(project_scope=str(lanes[3])).seal(),
                "original",
                "NORMAL",
                (),
            )
        )
        output = (
            "UNVERIFIED: original" if terminal_class == "UNVERIFIED" else "original"
        )
        turn.stage_final(
            HostFinalFacts(
                (),
                str(lanes[3]),
                False,
                False,
                False,
                terminal_class,
                certificate_code=code,
                delegated_work=delegated,
            )
        )
        turn.close()
        finalized = lifetime.finalize_result_with_receipt(output)

        def forbidden(*args):
            pytest.fail("no-pending eligibility must not construct a verifier")

        view = lifetime.terminal_eligibility(link, verifier_factory=forbidden)
        assert view.eligible is eligible
        assert view.evidence.result == finalized and view.published is None
        with pytest.raises(PermissionError):
            lifetime.terminal_eligibility(
                replace(link, parent_session_id="foreign"), verifier_factory=forbidden
            )
    finally:
        lifetime.close()
