"""Per-row routing metadata on GET /v1/models (the ``sonder`` field).

Non-administrators cannot read /v1/sonder/ecosystem, so each /v1/models row
says which provider serves it and, for a route, which model: only provider
ids and model ids, never an endpoint URL or credential.
"""
from types import SimpleNamespace

from sonder_runtime.interfaces.http.facades.model_catalog import (
    ROUTE_FIELD,
    annotate_model_rows,
)

TIER_MODELS = {"fast": "qwen3:14b", "general": "qwen3:14b", "code": "qwen3:14b",
               "reasoning": "deepseek-r1:14b", "vision": "qwen3:14b"}


def _serve_target(route_id, _strict):
    return {
        "sonder": ("qwen3:14b", False, True, "general"),
        "general": ("qwen3:14b", False, False, "general"),
        "reasoning": ("deepseek-r1:8b", False, False, "reasoning"),
        "code": ("gemma3:12b", False, True, "code"),
        "cloud-code": ("qwen3-coder:480b-cloud", True, False, "cloud-code"),
    }[route_id]


def _bridge(bound):
    def provider(label, cloud):
        return None if cloud or label not in bound else "sonder_inference"
    return provider


def _rows(*ids):
    return [{"id": i, "object": "model", "owned_by": "local"} for i in ids]


def _gateway():
    return SimpleNamespace(served_tier_models=lambda: {"sonder_inference": TIER_MODELS})


def test_routes_name_provider_and_served_model_and_exact_models_run_on_ollama():
    rows = annotate_model_rows(
        _rows("sonder", "general", "reasoning", "code", "cloud-code", "gemma3:12b"),
        {"sonder", "general", "reasoning", "code", "cloud-code"},
        serve_target=_serve_target,
        bridge_provider=_bridge({"general", "reasoning"}),
        gateway=_gateway(),
    )
    by_id = {row["id"]: row[ROUTE_FIELD] for row in rows}
    assert by_id == {
        "sonder": {"kind": "route", "provider": "sonder_inference", "served_model": "qwen3:14b"},
        "general": {"kind": "route", "provider": "sonder_inference", "served_model": "qwen3:14b"},
        "reasoning": {"kind": "route", "provider": "sonder_inference",
                      "served_model": "deepseek-r1:14b"},
        "code": {"kind": "route", "provider": "ollama", "served_model": "gemma3:12b"},
        "cloud-code": {"kind": "route", "provider": "ollama",
                       "served_model": "qwen3-coder:480b-cloud"},
        "gemma3:12b": {"kind": "model", "provider": "ollama"},
    }
    # The OpenAI fields are untouched.
    assert [set(row) for row in rows] == [{"id", "object", "owned_by", ROUTE_FIELD}] * 6


def test_route_ids_match_case_insensitively():
    rows = annotate_model_rows(
        _rows("General"), {"general"}, serve_target=lambda *_: ("m", False, False, "general"),
        bridge_provider=lambda *_: None, gateway=None,
    )
    assert rows[0][ROUTE_FIELD] == {"kind": "route", "provider": "ollama", "served_model": "m"}


def test_unresolvable_routes_and_failures_never_guess_or_raise():
    def broken_target(route_id, _strict):
        if route_id == "general":
            raise RuntimeError("policy unreadable")
        return None, True, False, "cloud-disabled"

    def broken_bridge(label, cloud):
        raise ValueError("bad binding")

    rows = annotate_model_rows(
        _rows("general", "cloud-code"), {"general", "cloud-code"},
        serve_target=broken_target, bridge_provider=broken_bridge,
        gateway=SimpleNamespace(served_tier_models=lambda: 1 / 0),
    )
    unknown = {"kind": "route", "provider": None, "served_model": None}
    assert [row[ROUTE_FIELD] for row in rows] == [unknown, unknown]


def test_bound_route_without_a_reported_model_names_only_the_provider():
    rows = annotate_model_rows(
        _rows("general"), {"general"}, serve_target=_serve_target,
        bridge_provider=_bridge({"general"}), gateway=SimpleNamespace(),
    )
    assert rows[0][ROUTE_FIELD] == {
        "kind": "route", "provider": "sonder_inference", "served_model": None,
    }


def test_nothing_but_provider_and_model_ids_is_exposed():
    gateway = SimpleNamespace(served_tier_models=lambda: {"sonder_inference": {
        "general": "qwen3:14b", "base_url": "http://10.0.0.5:18437",
    }})
    rows = annotate_model_rows(
        _rows("general"), {"general"}, serve_target=_serve_target,
        bridge_provider=_bridge({"general"}), gateway=gateway,
    )
    assert set(rows[0][ROUTE_FIELD]) == {"kind", "provider", "served_model"}
    assert "http" not in repr(rows)
