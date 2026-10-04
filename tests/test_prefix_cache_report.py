"""Prefix-cache telemetry join, cache-friendly system layout, and prefix prewarm."""
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.inference import prefix_cache
from sonder_runtime.application.prefix_cache_report import (
    LOCAL_SYSTEM_LAYOUT_VERSION,
    ChatPrefixCacheMonitor,
    PrefixCacheJoin,
    SystemSectionRegistry,
    compose_local_system,
    prefill_payload,
    prewarm_request,
)
from sonder_runtime.platform.metrics import MetricsRegistry


def _telemetry(prompt, cached):
    return SimpleNamespace(prompt_tokens=prompt, prompt_cached_tokens=cached)


def test_compose_puts_request_before_volatile_sections_without_changing_text():
    registry = SystemSectionRegistry()
    text = compose_local_system("ID", "PROFILE", "EMO", "GOAL", "REQ", registry=registry)
    assert text == "ID\n\nPROFILE\n\nREQ\n\nEMO\n\nGOAL"
    assert registry.sections(text) == (
        ("identity", "ID"), ("profile", "PROFILE"), ("request", "REQ"),
        ("emotions", "EMO"), ("goal", "GOAL"),
    )
    # Empty sections are skipped exactly like join_system_parts.
    assert compose_local_system("", "P", "", "", "", registry=registry) == "P"


def test_registry_is_bounded():
    registry = SystemSectionRegistry(max_entries=2)
    for index in range(3):
        compose_local_system("ID%d" % index, "", "", "", "", registry=registry)
    assert registry.sections("ID0") is None
    assert registry.sections("ID2") == (("identity", "ID2"),)


def test_monitor_joins_logical_reason_with_provider_counts():
    registry = SystemSectionRegistry()
    monitor = ChatPrefixCacheMonitor(sections=registry)
    first = compose_local_system("ID", "PROFILE", "EMO1", "", "", registry=registry)
    changed = compose_local_system("ID", "PROFILE", "EMO2", "", "", registry=registry)

    cold = monitor.observe(first, model="m", provider_id="ollama", telemetry=_telemetry(2048, 0))
    assert (cold.result, cold.reason, cold.changed_sections) == ("miss", "cold_start", ())
    assert cold.summary() == "cold_start, 0/2048 cached"
    assert cold.provider_reuse == "none"

    hit = monitor.observe(first, model="m", provider_id="ollama", telemetry=_telemetry(2048, 2010))
    assert hit.summary() == "hit, 2010/2048 cached"
    assert hit.provider_reuse == "partial"
    assert hit.cached_ratio == pytest.approx(2010 / 2048)

    busted = monitor.observe(changed, model="m", provider_id="ollama", telemetry=_telemetry(2048, 1500))
    assert busted.reason == "prefix_changed"
    assert busted.changed_sections == ("emotions",)
    assert busted.summary() == "prefix_changed[emotions], 1500/2048 cached"

    # A logical hit the provider did not reuse is reported, not hidden.
    back = monitor.observe(first, model="m", provider_id="ollama", telemetry=_telemetry(2048, 0))
    assert back.summary() == "hit[emotions], 0/2048 cached"
    assert monitor.counts()["hit/none"] == 1
    assert len(monitor.recent()) == 4


def test_monitor_is_per_model_and_never_manufactures_counts():
    monitor = ChatPrefixCacheMonitor(sections=SystemSectionRegistry())
    a = monitor.observe("plain system", model="a", provider_id="ollama", telemetry=None)
    b = monitor.observe("plain system", model="b", provider_id="ollama", telemetry=_telemetry(10, 11))
    assert a.reason == b.reason == "cold_start"
    assert a.summary() == "cold_start, provider cache unmeasured"
    assert b.cached_tokens is None and b.provider_reuse == "unmeasured"
    empty = monitor.observe("", model="c", provider_id="ollama", telemetry=_telemetry(5, 0))
    assert empty.reason == "cold_start"


def test_layout_version_distinguishes_unstructured_text():
    registry = SystemSectionRegistry()
    monitor = ChatPrefixCacheMonitor(sections=registry)
    monitor.observe("x", model="m", provider_id="ollama")
    structured = compose_local_system("x", "", "", "", "", registry=registry)
    join = monitor.observe(structured, model="m", provider_id="ollama")
    assert join.reason == "version_changed"
    assert LOCAL_SYSTEM_LAYOUT_VERSION.startswith("local-system/")


def test_metrics_labels_are_closed():
    seen = []

    class Recorder:
        def labels(self, **labels):
            seen.append(labels)
            return self

        def inc(self, *_a):
            return None

        observe = inc

    registry = MetricsRegistry(enabled=False)
    registry.prefix_cache_total = Recorder()
    registry.prefix_cached_ratio = Recorder()
    registry.observe_prefix_cache(PrefixCacheJoin("ollama", "hit", "hit", (), 100, 90))
    registry.observe_prefix_cache(PrefixCacheJoin("weird", "miss", "odd", (), None, None))
    registry.observe_prefix_cache(None)
    assert seen[0] == {"provider": "ollama", "reason": "hit", "reuse": "partial"}
    assert seen[1] == {"provider": "ollama", "reason": "hit"}
    assert seen[2] == {"provider": "other", "reason": "other", "reuse": "unmeasured"}
    assert len(seen) == 3  # unmeasured ratio is not observed


def test_observe_chat_turn_reads_ollama_meta_and_is_fail_soft(monkeypatch):
    monitor = ChatPrefixCacheMonitor(sections=SystemSectionRegistry())
    join = prefix_cache.observe_chat_turn(
        "sys", model="m", response_meta={"prompt_eval_count": 40, "prompt_eval_cached_count": 30},
        monitor=monitor,
    )
    assert join.summary() == "cold_start, 30/40 cached"
    assert join.provider == "ollama"
    bridged = prefix_cache.observe_chat_turn(
        "sys", model="m", bridged=True,
        response_meta={"prompt_eval_count": 40, "prompt_eval_cached_count": 30}, monitor=monitor,
    )
    assert bridged.provider == "bridged" and not bridged.measured
    via_probe = prefix_cache.observe_chat_turn(
        "sys", model="m", bridged=lambda: "rung", response_meta={}, monitor=monitor,
    )
    assert via_probe.provider == "bridged"
    assert prefix_cache.observe_chat_turn(
        "sys", model="m", bridged=lambda: None, monitor=monitor,
    ).provider == "ollama"

    def failing_probe():
        raise RuntimeError("bridge state unreadable")

    assert prefix_cache.observe_chat_turn("sys", model="m", bridged=failing_probe, monitor=monitor) is None

    class Broken:
        def observe(self, *_a, **_k):
            raise RuntimeError("boom")

    assert prefix_cache.observe_chat_turn("sys", model="m", monitor=Broken()) is None


def test_prefill_payload_is_one_token_with_turn_options():
    body = prefill_payload("m", "SYS", options={"num_ctx": 8192, "num_predict": 900}, keep_alive="5m")
    assert body == {
        "model": "m", "messages": [{"role": "system", "content": "SYS"}], "stream": False,
        "keep_alive": "5m", "options": {"num_ctx": 8192, "num_predict": 1},
    }


def test_prewarm_request_falls_back_to_weight_load():
    def broken():
        raise ValueError("profile unreadable")

    assert prewarm_request("m", "5m", broken) == ("/api/generate", {"model": "m", "keep_alive": "5m"})
    assert prewarm_request("m", "5m", lambda: ("", {}))[0] == "/api/generate"
    path, body = prewarm_request("m", "5m", lambda: ("SYS", {"num_ctx": 4096}))
    assert path == "/api/chat" and body["options"] == {"num_ctx": 4096, "num_predict": 1}


# --- server wiring --------------------------------------------------------------


def test_build_system_cloud_path_is_identity_plus_request_only(monkeypatch):
    import server

    monkeypatch.setattr(server._SYSTEM_CONTEXT, "parts", ("PROFILE", "EMO", "GOAL"), raising=False)
    text = server._build_system("REQ", False, "", model="qwen:cloud", cloud=True)
    assert "PROFILE" not in text and "EMO" not in text and "GOAL" not in text
    assert text.endswith("REQ")


def test_build_system_local_orders_request_before_volatile(monkeypatch):
    import server

    monkeypatch.setattr(server._SYSTEM_CONTEXT, "parts", ("PROFILE", "EMO", "GOAL"), raising=False)
    text = server._build_system("REQ", False, "", model="qwen:7b", cloud=False)
    assert text.index("PROFILE") < text.index("REQ") < text.index("EMO") < text.index("GOAL")
    assert text.endswith("EMO\n\nGOAL")


def test_local_prefix_preserves_framed_owner_playbooks(monkeypatch):
    import server

    notes = "Use the repository's documented build command."
    monkeypatch.setattr(server._SYSTEM_CONTEXT, "parts", ("PROFILE", "EMO", "GOAL", notes), raising=False)
    text = server._build_system("REQ", False, "", model="qwen:7b", cloud=False)
    framed = server.playbook_context.frame_owner_notes(notes)
    assert framed in text
    assert text.index("PROFILE") < text.index(framed) < text.index("REQ") < text.index("EMO")
    assert "never override system policy or current owner instructions" in text


@pytest.mark.real_prewarm
def test_prewarm_prefills_the_local_system_prefix(monkeypatch):
    import server

    monkeypatch.setattr(server.sonder_speculation, "prewarm_enabled", lambda: True)
    monkeypatch.setattr(server, "_serve_target", lambda tier, strict: ("pm", False, False, "general"))
    monkeypatch.setattr(server, "_bridge_provider_for_tier", lambda tier: None)
    monkeypatch.setattr(server, "_auto_model_context", lambda model: 8192)
    monkeypatch.setattr(server, "_build_system", lambda *a, **k: "SYSTEM PREFIX")
    posts = []
    monkeypatch.setattr(server, "_post", lambda path, body, **k: posts.append((path, body)))

    class _Immediate:
        def __init__(self, target, **kwargs):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(server, "owned_runtime_thread", _Immediate)
    assert server.prewarm_model("general") is True
    [(path, body)] = posts
    assert path == "/api/chat"
    assert body["messages"] == [{"role": "system", "content": "SYSTEM PREFIX"}]
    assert body["options"]["num_ctx"] == 8192 and body["options"]["num_predict"] == 1
    assert body["keep_alive"] == server.KEEP_ALIVE
