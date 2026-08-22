from __future__ import annotations

from sonder_runtime.adapters import runtime_policy
from sonder_runtime.adapters.runtime_policy_repository import RuntimePolicyRepository
from sonder_runtime.adapters.unit_of_work import UnitOfWorkAdapter


def test_policy_repository_is_owned_by_named_adapter_module():
    assert RuntimePolicyRepository.__module__ == (
        "sonder_runtime.adapters.runtime_policy_repository"
    )


def test_policy_repository_preserves_load_and_update_forwarding(monkeypatch):
    calls = []

    def load():
        calls.append(("load",))
        return {"revision": 4}

    def update(**kwargs):
        calls.append(("update", kwargs))
        return {"revision": 5, **kwargs}

    monkeypatch.setattr(runtime_policy, "load", load)
    monkeypatch.setattr(runtime_policy, "update", update)

    repository = RuntimePolicyRepository()
    assert repository.load() == {"revision": 4}
    assert repository.update(
        local_models={"general": "model"},
        routing={"chat": "local"},
        npu={"mode": "off"},
        expected_revision=4,
        source="test",
    ) == {
        "revision": 5,
        "local_models": {"general": "model"},
        "routing": {"chat": "local"},
        "npu": {"mode": "off"},
        "source": "test",
        "expected_revision": 4,
    }
    assert calls == [
        ("load",),
        (
            "update",
            {
                "local_models": {"general": "model"},
                "routing": {"chat": "local"},
                "npu": {"mode": "off"},
                "source": "test",
                "expected_revision": 4,
            },
        ),
    ]


def test_unit_of_work_uses_canonical_policy_repository():
    unit_of_work = UnitOfWorkAdapter()
    assert isinstance(unit_of_work.policy, RuntimePolicyRepository)
