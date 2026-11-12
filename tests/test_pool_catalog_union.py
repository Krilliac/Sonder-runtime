"""Tier validation must see every pool member's catalog, not whichever one answered.

Measured 2026-09-28: with the workstation and Node1 pooled, ``runtime_policy_update``
refused a model installed on the workstation ("local model(s) are not installed")
because ``_get("/api/tags")`` is one pool request, and Node1 answered it.
The reasoning tier legitimately lives only on Node1, so the check must use the
union of every member's catalog.
"""
from __future__ import annotations

from urllib.error import URLError

import pytest

from sonder_runtime.adapters.inference.ollama_pool import OllamaWorkerPool

LOCAL = "http://127.0.0.1:11434"
NODE1 = "https://node1.example:8443"
CATALOGS = {
    LOCAL: {"models": [{"name": "hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q3_K_XL",
                        "capabilities": ["completion", "vision"]},
                       {"name": "shared:latest", "capabilities": ["completion"]}]},
    NODE1: {"models": [{"name": "qwen3.6:35b"},
                       {"name": "shared:latest", "capabilities": ["embedding"]}]},
}


def _pool():
    return OllamaWorkerPool(LOCAL, (NODE1,), allow_remote=True)


def test_union_holds_every_members_models_once():
    union = _pool().catalog_union(lambda origin: CATALOGS[origin])
    names = [row["name"] for row in union["models"]]
    assert sorted(names) == sorted({"hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q3_K_XL",
                                    "qwen3.6:35b", "shared:latest"})
    # The primary's record wins for a model both members hold.
    shared = next(row for row in union["models"] if row["name"] == "shared:latest")
    assert shared["capabilities"] == ["completion"]


def test_an_unreachable_member_is_skipped_but_all_unreachable_fails():
    def one_down(origin):
        if origin == NODE1:
            raise URLError("node1 offline")
        return CATALOGS[origin]

    names = [row["name"] for row in _pool().catalog_union(one_down)["models"]]
    assert "qwen3.6:35b" not in names and "shared:latest" in names

    def all_down(origin):
        raise URLError("down")

    with pytest.raises(URLError):
        _pool().catalog_union(all_down)


def test_policy_update_accepts_models_spread_across_the_pool(monkeypatch):
    import server

    pool = _pool()
    monkeypatch.setattr(server, "OLLAMA_POOL", pool)
    monkeypatch.setattr(server, "_pool_member_tags", lambda origin: CATALOGS[origin])
    names = {name for name, _record in server._runtime_installed_model_records()}
    assert {"hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q3_K_XL", "qwen3.6:35b"} <= names


def test_a_member_the_scheduler_cannot_route_to_is_left_out(monkeypatch):
    pool = _pool()
    node1 = next(s for s in pool._states if s.endpoint.origin == NODE1)
    monkeypatch.setattr(pool, "_membership_admissible",
                        lambda state, now: state is not node1)
    names = [row["name"] for row in pool.catalog_union(lambda origin: CATALOGS[origin])["models"]]
    assert "qwen3.6:35b" not in names, "a probationary/expired/draining worker's models do not count"
    node1_ok = next(s for s in pool._states if s.endpoint.origin == NODE1)
    monkeypatch.setattr(pool, "_membership_admissible", lambda state, now: True)
    node1_ok.compatibility_error = "incompatible"
    names = [row["name"] for row in pool.catalog_union(lambda origin: CATALOGS[origin])["models"]]
    assert "qwen3.6:35b" not in names


def test_routable_only_skips_a_member_in_cooldown_but_default_keeps_it():
    pool = _pool()
    node1 = next(s for s in pool._states if s.endpoint.origin == NODE1)
    node1.cooldown_until = pool._clock() + 1000.0
    fetched = []

    def fetch(origin):
        fetched.append(origin)
        return CATALOGS[origin]

    routable = [r["name"] for r in pool.catalog_union(fetch, routable_only=True)["models"]]
    assert "qwen3.6:35b" not in routable and "shared:latest" in routable
    assert NODE1 not in fetched, "a member in cooldown must not even be read"

    default = [r["name"] for r in pool.catalog_union(fetch)["models"]]
    assert "qwen3.6:35b" in default, "default behaviour (policy validation) keeps the member"

    node1.cooldown_until = 0.0
    again = [r["name"] for r in pool.catalog_union(fetch, routable_only=True)["models"]]
    assert "qwen3.6:35b" in again, "an expired cooldown makes the member routable again"


@pytest.mark.parametrize("routable_only", [False, True])
@pytest.mark.parametrize("bad", [["x"], "x", {"models": "x"}, 5])
def test_malformed_member_catalog_is_a_failed_read_not_an_exception(bad, routable_only):
    def fetch(origin):
        return bad if origin == NODE1 else CATALOGS[origin]

    union = _pool().catalog_union(fetch, routable_only=routable_only)
    names = {row["name"] for row in union["models"]}
    assert "qwen3.6:35b" not in names and "shared:latest" in names

    solo = OllamaWorkerPool(LOCAL)
    with pytest.raises(ValueError):
        solo.catalog_union(lambda _origin: bad, routable_only=routable_only)


@pytest.mark.parametrize("routable_only", [False, True])
@pytest.mark.parametrize("empty", [None, {}, {"models": []}, {"models": None}])
def test_none_and_empty_payloads_count_as_answered_empty(empty, routable_only):
    solo = OllamaWorkerPool(LOCAL)
    assert solo.catalog_union(lambda _o: empty, routable_only=routable_only) == {"models": []}


@pytest.mark.parametrize("routable_only", [False, True])
def test_non_dict_rows_are_skipped_but_the_member_still_counts_as_answered(routable_only):
    # Current behaviour: {"models": [1, "y"]} is a well-formed list, so the
    # member answered; its non-dict rows contribute nothing and nothing raises.
    def fetch(origin):
        return {"models": [1, "y", {"name": "ok:1"}]} if origin == NODE1 else CATALOGS[origin]

    names = {r["name"] for r in _pool().catalog_union(fetch, routable_only=routable_only)["models"]}
    assert "ok:1" in names and "shared:latest" in names and "y" not in names
    solo = OllamaWorkerPool(LOCAL)
    assert solo.catalog_union(lambda _o: {"models": [1, "y"]}, routable_only=routable_only) \
        == {"models": []}


@pytest.mark.parametrize("routable_only", [False, True])
@pytest.mark.parametrize("bad_name", [["x"], 5, {"k": "v"}, None, "", 0, 1.5])
def test_rows_with_non_string_names_are_skipped_and_valid_rows_still_merge(bad_name, routable_only):
    def fetch(origin):
        if origin == NODE1:
            return {"models": [{"name": bad_name}, {"model": bad_name}, {"name": "good:1"}]}
        return CATALOGS[origin]

    union = _pool().catalog_union(fetch, routable_only=routable_only)
    names = [row["name"] for row in union["models"] if isinstance(row.get("name"), str)]
    assert "good:1" in names and "shared:latest" in names
    assert len(union["models"]) == len(CATALOGS[LOCAL]["models"]) + 1, "bad rows contributed nothing"
