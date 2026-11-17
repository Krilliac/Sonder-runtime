"""Bounded temperature errors, cross-principal eviction protection, and gateway validation."""
import pytest

from sonder_runtime.adapters.inference.ollama_gateway import OllamaGateway
from sonder_runtime.adapters.inference.openai_compat_gateway import (
    OpenAICompatibleConfig,
    OpenAICompatibleGateway,
)
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.application.ports.model_target import ModelTarget
from sonder_runtime.application.session import temperature_settings
from sonder_runtime.application.session.temperature_settings import (
    MAX_OWNER_SESSIONS,
    SessionTemperatureStore,
    temp_command,
)
from sonder_runtime.domain.common.errors import InvalidInput
from sonder_runtime.domain.sampling_temperature import check_temperature
from sonder_runtime.interfaces.http import serve as ts


def test_huge_integer_temperature_has_a_bounded_typed_refusal():
    with pytest.raises(InvalidInput) as caught:
        check_temperature(10 ** 5000)
    assert "from 0 to 2" in str(caught.value)
    assert len(str(caught.value)) < 512


def test_auto_clear_describes_the_lane_fallback_instead_of_an_unconditional_preset():
    store = SessionTemperatureStore()
    store.set("chat", 0.9)
    reply = temp_command("auto", "chat", lane=0.3, store=store)
    assert store.get("chat") is None
    assert "lane" in reply.lower()
    assert "model preset applies" not in reply.lower()


def test_store_churn_by_one_owner_evicts_own_oldest_not_another_tenant():
    store = SessionTemperatureStore(max_sessions=6, max_per_owner=3)
    store.set("alice-main", 1.4, owner="account:alice")
    for idx in range(20):
        store.set(f"bob-{idx}", 0.5, owner="account:bob")

    assert store.get("alice-main") == 1.4
    assert store.get("bob-0") is None
    assert store.get("bob-19") == 0.5


def test_store_global_cap_evicts_heaviest_owner_protecting_quiet_tenant():
    store = SessionTemperatureStore(max_sessions=6, max_per_owner=4)
    store.set("alice-main", 1.2, owner="account:alice")
    for idx in range(4):
        store.set(f"bob-{idx}", 0.4, owner="account:bob")
    for idx in range(4):
        store.set(f"carol-{idx}", 0.6, owner="account:carol")

    assert store.get("alice-main") == 1.2


def test_http_slash_temp_churn_does_not_evict_another_account_session(monkeypatch):
    store = SessionTemperatureStore()
    monkeypatch.setattr(temperature_settings, "STORE", store)
    monkeypatch.setattr(ts._chat_temperature, "STORE", store)
    alice = {
        "mode": "account", "authorized": True, "api_key": False,
        "account": {"username": "alice", "role": "user"},
    }
    bob = {
        "mode": "account", "authorized": True, "api_key": False,
        "account": {"username": "bob", "role": "user"},
    }
    alice_storage = ts._hosted_storage_id(alice, "alice-chat", "session")
    assert "1.4" in ts._handle_slash("/temp 1.4", context=alice, storage_session=alice_storage)
    assert store.get(alice_storage) == 1.4

    for idx in range(MAX_OWNER_SESSIONS + 50):
        bob_storage = ts._hosted_storage_id(bob, f"bob-{idx}", "session")
        ts._handle_slash("/temp 0.3", context=bob, storage_session=bob_storage)

    assert store.get(alice_storage) == 1.4
    assert "1.4" in ts._handle_slash("/temp", context=alice, storage_session=alice_storage)


_BAD_TEMPS = [
    -0.1, 2.5, 1e308, float("inf"), float("-inf"), float("nan"), True, "0.5",
    pytest.param(10 ** 5000, id="huge-int"),
]


@pytest.mark.parametrize("bad_temperature", _BAD_TEMPS)
def test_ollama_gateway_rejects_invalid_temperature_before_factory(bad_temperature):
    called = []

    def resolve(tier, strict=None):
        return ModelTarget("m", False, "code", True, provider_id="ollama")

    def make_generate(*_args, **_kwargs):
        called.append(True)
        raise AssertionError("generate_factory must not be called for invalid temperature")

    gateway = OllamaGateway(target_resolver=resolve, generate_factory=make_generate)
    with pytest.raises(InvalidInput, match="temperature must be a finite number from 0 to 2"):
        gateway.generate(
            ModelRequest("hello", "code", options={"temperature": bad_temperature}),
            local_owner_context(correlation_id="req_temp_bad"),
        )
    assert called == []


@pytest.mark.parametrize("bad_temperature", _BAD_TEMPS)
def test_openai_compat_gateway_rejects_invalid_temperature_before_transport(bad_temperature):
    posted = []

    def transport(_url, payload, _headers, _timeout):
        posted.append(payload)
        raise AssertionError("transport must not be called for invalid temperature")

    gateway = OpenAICompatibleGateway(
        OpenAICompatibleConfig("http://127.0.0.1:8080", model="fixture"),
        transport=transport,
    )
    with pytest.raises(InvalidInput, match="temperature must be a finite number from 0 to 2"):
        gateway.generate(
            ModelRequest("hello", "code", options={"temperature": bad_temperature}),
            local_owner_context(correlation_id="req_oai_bad"),
        )
    assert posted == []
