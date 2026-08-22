import json

import pytest

from sonder_runtime.application.protocol.a2a import (
    A2AAgentCard,
    A2ARemoteTaskRef,
    A2ASkill,
    A2ATaskState,
    card_from_registrations,
)
from sonder_runtime.application.agent_registry.workbench_review import AgentRegistration
from sonder_runtime.domain.common.errors import InvalidInput


def test_agent_card_is_discoverable_digest_bound_and_capability_scoped():
    card = A2AAgentCard(
        "sonder", "local agent", "https://example.test/a2a", "1",
        (A2ASkill("review", "Review", "Review changes", examples=("check diff",)),),
        streaming=True,
    )
    body = card.to_dict()
    assert body["supportedInterfaces"][0]["protocolBinding"] == "JSONRPC"
    assert body["capabilities"]["streaming"] is True
    assert len(card.digest) == 64
    assert json.loads(json.dumps(body)) == body


def test_card_from_existing_registrations_does_not_grant_execution():
    registrations = [AgentRegistration("review", "reviewer", "read_only", capabilities=("inspect",))]
    card = card_from_registrations("sonder", "agent", "https://example.test/a2a", registrations)
    assert card.skills[0].id == "review"
    assert "execute" not in json.dumps(card.to_dict()).lower()


def test_remote_task_ref_binds_card_and_delegation_lineage():
    ref = A2ARemoteTaskRef("task-1", "context-1", "a" * 64, A2ATaskState.AUTH_REQUIRED, ("root", "child"))
    assert ref.to_dict()["state"] == "TASK_STATE_AUTH_REQUIRED"
    assert ref.to_dict()["delegationChain"] == ["root", "child"]


@pytest.mark.parametrize("url", ["/relative", "file:///secret", "https://"])
def test_card_url_and_state_fail_closed(url):
    with pytest.raises(InvalidInput):
        A2AAgentCard("sonder", "agent", url, "1")
    with pytest.raises(InvalidInput):
        A2ARemoteTaskRef("task", "ctx", "digest", "UNKNOWN")
