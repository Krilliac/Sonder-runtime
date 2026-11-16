"""``response_metadata`` reports the model that actually answered (served model)."""
from __future__ import annotations

from sonder_runtime.domain.model_usage import response_metadata


def test_served_model_comes_from_the_response_model_field():
    assert response_metadata({"model": "m:1"})["served_model"] == "m:1"


def test_served_model_falls_back_to_the_passed_value_when_the_field_is_missing():
    assert response_metadata({}, served_model="req:1")["served_model"] == "req:1"
    assert response_metadata({"model": ""}, served_model="req:1")["served_model"] == "req:1"
    assert response_metadata({"model": 7}, served_model="req:1")["served_model"] == "req:1"


def test_response_field_wins_over_the_passed_value_and_is_bounded():
    assert response_metadata({"model": "served:2"}, served_model="req:1")["served_model"] == "served:2"
    assert len(response_metadata({"model": "x" * 500})["served_model"]) == 200


def test_served_model_is_omitted_when_nothing_is_known():
    assert "served_model" not in response_metadata({})
