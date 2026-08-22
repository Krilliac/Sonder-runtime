from types import SimpleNamespace

import server
from sonder_runtime.domain.context import compaction


def _payload():
    messages = [{"role": "system", "content": "be terse"}]
    for index in range(4):
        messages.extend([
            {"role": "user", "content": "q%d" % index},
            {"role": "assistant", "content": "a%d" % index},
        ])
    messages.append({"role": "user", "content": "live request"})
    return {
        "model": "local",
        "messages": messages,
        "options": {"num_ctx": 8192, "num_predict": 256},
    }


def test_context_compaction_policy_owns_overflow_payload_transformation():
    payload = _payload()
    result = compaction.compact_overflow_payload(
        payload, SimpleNamespace(overflow=True),
    )

    assert result is not payload
    assert result["options"] == payload["options"]
    assert len(result["messages"]) < len(payload["messages"])
    assert result["messages"][-1] == payload["messages"][-1]


def test_context_compaction_policy_rejects_non_overflow_and_invalid_payloads():
    payload = _payload()

    assert compaction.compact_overflow_payload(
        payload, SimpleNamespace(overflow=False),
    ) is None
    assert compaction.compact_overflow_payload(
        "not a payload", SimpleNamespace(overflow=True),
    ) is None


def test_server_helper_remains_a_compatibility_delegate():
    payload = _payload()
    verdict = SimpleNamespace(overflow=True)

    packaged = compaction.compact_overflow_payload(payload, verdict)
    legacy = server._compacted_overflow_payload(payload, verdict)

    assert legacy == packaged
