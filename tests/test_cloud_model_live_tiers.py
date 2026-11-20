"""A refresh (or an editor write) reaches a running ``server`` module without a reload.

``server.TIERS`` and the consumers' applied-defaults snapshot are process
globals: every test below patches them through ``monkeypatch`` so they are
restored, and drives the refresh against a loopback fake Ollama and a tmp
overlay (``SONDER_MODEL_LISTS``), never the real state home.
"""

import json

import pytest
import server

from sonder_runtime.adapters import model_list_consumers as consumers
from sonder_runtime.adapters import model_lists
from sonder_runtime.adapters import model_lists_editor as editor
from tests.support.cloud_refresh_harness import make_world, standard_shows

SHIPPED = model_lists.load_shipped_document()
DEFAULT_CODE = SHIPPED["default_cloud_models"]["code"]
DEFAULT_GENERAL = SHIPPED["default_cloud_models"]["general"]


@pytest.fixture
def live(tmp_path, monkeypatch):
    world = make_world(tmp_path, monkeypatch, tags=("kimi-k3-code:cloud",), shows=standard_shows())
    world.write_preferences({"code": ["kimi-k3-code:cloud"]})
    monkeypatch.delenv("SONDER_CLOUD_CODE", raising=False)
    monkeypatch.delenv("SONDER_CLOUD_GENERAL", raising=False)
    monkeypatch.delenv("SONDER_PRESERVE_LEGACY_CLOUD_GENERAL", raising=False)
    monkeypatch.setitem(server.TIERS, "cloud-code", DEFAULT_CODE)
    monkeypatch.setitem(server.TIERS, "cloud-general", DEFAULT_GENERAL)
    monkeypatch.setattr(consumers, "_APPLIED_CLOUD_DEFAULTS", {
        "code": {DEFAULT_CODE.casefold()}, "general": {DEFAULT_GENERAL.casefold()}})
    yield world
    world.fake.close()
    model_lists.clear_cache()


def test_applied_rebind_reaches_available_tiers_without_a_reload(live):
    assert server.available_tiers(include_disabled=True)["cloud-code"] == DEFAULT_CODE
    assert live.run(apply=True)["outcome"] == "ok"
    tiers = server.available_tiers(include_disabled=True)
    assert tiers["cloud-code"] == "kimi-k3-code:cloud"
    assert tiers["cloud-general"] == DEFAULT_GENERAL  # nothing to rebind to: untouched, still pending


def test_env_bound_live_tier_is_not_moved_by_a_refresh(live, monkeypatch):
    monkeypatch.setenv("SONDER_CLOUD_CODE", "mine:cloud")
    monkeypatch.setitem(server.TIERS, "cloud-code", "mine:cloud")
    live.env["SONDER_CLOUD_CODE"] = "mine:cloud"
    live.run(apply=True)
    assert server.available_tiers(include_disabled=True)["cloud-code"] == "mine:cloud"


def test_retired_binding_moves_to_the_new_default_on_the_next_read(live):
    live.run(apply=True)  # DEFAULT_CODE is now retired and the overlay default is kimi-k3-code
    server.TIERS["cloud-code"] = DEFAULT_CODE  # a stale binding to the retired model
    assert server.available_tiers(include_disabled=True)["cloud-code"] == "kimi-k3-code:cloud"


def test_extra_usage_fallback_follows_the_new_code_default(live, monkeypatch):
    calls = []

    def fake_chat(payload, *, model, cloud, **_kwargs):
        calls.append(model)
        if model == "kimi-k3:cloud":
            raise server.ModelCallError("http", "extra usage balance is empty", status=402, cloud=True)
        return {"message": {"content": "ok"}}, "ok"

    monkeypatch.setattr(server, "_chat_request", fake_chat)
    payload = {"model": "kimi-k3:cloud", "messages": [{"role": "user", "content": "x"}], "stream": False}

    def call():
        calls.clear()
        return server._chat_request_with_cloud_fallback(payload, model="kimi-k3:cloud", timeout=30)[2]

    assert call() == server.CLOUD_EXTRA_USAGE_FALLBACK_MODEL == DEFAULT_CODE
    live.run(apply=True)
    assert call() == "kimi-k3-code:cloud"
    assert calls == ["kimi-k3:cloud", "kimi-k3-code:cloud"]


def test_live_cloud_default_never_returns_a_retired_model_or_raises(live):
    assert consumers.live_cloud_default("code", "fallback:cloud") == DEFAULT_CODE
    live.write_overlay({"version": 1, "default_cloud_models": {"code": "new:cloud"}})
    assert consumers.live_cloud_default("code", "fallback:cloud") == "new:cloud"
    live.write_overlay({"version": 1, "default_cloud_models": {"code": "new:cloud"},
                        "retired_cloud_models": ["new:cloud"]})
    assert consumers.live_cloud_default("code", "fallback:cloud") == "fallback:cloud"
    assert consumers.live_cloud_default("nope", "fallback:cloud") == "fallback:cloud"
    live.overlay.write_text("{", encoding="utf-8")
    model_lists.clear_cache()
    assert consumers.live_cloud_default("code", "fallback:cloud") == DEFAULT_CODE  # invalid overlay is ignored whole


def test_an_editor_default_change_also_reaches_the_running_tier(live):
    text = editor.command('set default_cloud_models {"code": "edited:cloud"}', operator_attended=True)
    assert "set default_cloud_models" in text
    assert server.available_tiers(include_disabled=True)["cloud-code"] == "edited:cloud"


def test_editor_success_text_states_the_live_versus_restart_split(live):
    text = editor.command('set retired_cloud_models ["a:cloud"]', operator_attended=True)
    assert "next tier read" in text and "restart" in text
    assert json.loads(live.overlay.read_text())["retired_cloud_models"] == ["a:cloud"]


def test_unusable_model_lists_leave_the_tiers_as_they_are(live):
    live.overlay.write_text('{"version": 1, "typo": 1}', encoding="utf-8")
    model_lists.clear_cache()
    server.TIERS["cloud-code"] = "pinned:cloud"
    assert server.available_tiers(include_disabled=True)["cloud-code"] == "pinned:cloud"
