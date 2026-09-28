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
