"""Bounded temperature errors and truthful Auto lane-fallback messages."""
import pytest

from sonder_runtime.application.session.temperature_settings import (
    SessionTemperatureStore,
    temp_command,
)
from sonder_runtime.domain.common.errors import InvalidInput
from sonder_runtime.domain.sampling_temperature import check_temperature


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
