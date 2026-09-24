"""Measured-spill feedback for automatic context selection."""

from sonder_runtime.adapters.inference.residency_feedback import ResidencyFeedback


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def tracker(rows, clock, calls=None, **kwargs):
    def fetch():
        if calls is not None:
            calls.append(1)
        if isinstance(rows, Exception):
            raise rows
        return {"models": list(rows)}
    return ResidencyFeedback(fetch, minimum_context=512, clock=clock, **kwargs)


def spill_row(context=None, spilled=4_000_000_000):
    row = {"name": "Qwen:27B", "size": 20_000_000_000, "size_vram": 20_000_000_000 - spilled}
    if context is not None:
        row["context_length"] = context
    return row


def test_no_probe_before_any_selection():
    calls = []
    feedback = tracker([spill_row()], Clock(), calls)
    assert feedback.refresh("qwen:27b", geometry=None, kv_type="f16") is None
    assert calls == []


def test_spill_sets_a_ceiling_attributed_to_last_selection():
    clock = Clock()
    feedback = tracker([spill_row()], clock)
    feedback.note_selection("qwen:27b", 16384)

    verdict = feedback.refresh("qwen:27b", geometry=None, kv_type="f16")

    assert verdict.placement == "gpu+ram-hybrid"
    assert verdict.attributed_by == "last-selection"
    assert feedback.ceiling("QWEN:27b") == 8192


def test_server_reported_context_wins_attribution():
    feedback = tracker([spill_row(context=32768)], Clock())
    feedback.note_selection("qwen:27b", 8192)

    verdict = feedback.refresh("qwen:27b", geometry=None, kv_type="f16")

    assert verdict.attributed_by == "server-reported"
    assert feedback.ceiling("qwen:27b") == 16384


def test_probe_is_throttled_and_ceiling_expires():
    clock, calls = Clock(), []
    feedback = tracker([spill_row()], clock, calls, check_interval=60, ceiling_ttl=600)
    feedback.note_selection("qwen:27b", 16384)
    feedback.refresh("qwen:27b", geometry=None, kv_type="f16")
    feedback.refresh("qwen:27b", geometry=None, kv_type="f16")
    assert len(calls) == 1

    clock.now += 601
    assert feedback.ceiling("qwen:27b") is None


def test_repeated_spill_only_lowers_the_ceiling():
    clock = Clock()
    feedback = tracker([spill_row()], clock, check_interval=0)
    feedback.note_selection("qwen:27b", 16384)
    feedback.refresh("qwen:27b", geometry=None, kv_type="f16")
    feedback.note_selection("qwen:27b", 8192)
    feedback.refresh("qwen:27b", geometry=None, kv_type="f16")
    assert feedback.ceiling("qwen:27b") == 4096


def test_resident_and_cpu_models_are_never_clamped():
    for row in (
        {"name": "m:7b", "size": 10, "size_vram": 10},
        {"name": "m:7b", "size": 10, "size_vram": 0},
    ):
        feedback = tracker([row], Clock())
        feedback.note_selection("m:7b", 16384)
        feedback.refresh("m:7b", geometry=None, kv_type="f16")
        assert feedback.ceiling("m:7b") is None


def test_transport_failure_is_swallowed():
    feedback = tracker(OSError("down"), Clock())
    feedback.note_selection("qwen:27b", 16384)
    assert feedback.refresh("qwen:27b", geometry=None, kv_type="f16") is None
    assert feedback.ceiling("qwen:27b") is None


def test_unloaded_model_produces_no_verdict():
    feedback = tracker([{"name": "other:7b", "size": 10, "size_vram": 5}], Clock())
    feedback.note_selection("qwen:27b", 16384)
    assert feedback.refresh("qwen:27b", geometry=None, kv_type="f16") is None


def test_server_auto_context_applies_measured_ceiling(monkeypatch):
    import server

    monkeypatch.delenv("SONDER_CONTEXT_SIZE", raising=False)
    monkeypatch.delenv("SONDER_SESSION_NUM_CTX", raising=False)
    monkeypatch.setenv("SONDER_KV_CACHE_TYPE", "q8_0")
    monkeypatch.setattr(server, "_model_context_metadata", lambda model: (262144, "7.6B"))
    feedback = tracker([spill_row(context=32768)], Clock(), check_interval=0)
    feedback._states.clear()
    monkeypatch.setattr(server, "_residency_feedback", lambda: feedback)
    monkeypatch.setattr(server, "_is_cloud_model_name", lambda model: False)

    assert server._auto_model_context("qwen:27b") == 32768
    assert server._auto_model_context("qwen:27b") == 16384


def test_server_disables_feedback_by_environment(monkeypatch):
    import server

    monkeypatch.setenv("SONDER_RESIDENCY_FEEDBACK", "0")
    assert server._residency_feedback() is None


def test_server_origin_change_discards_old_residency_ceiling(monkeypatch):
    from types import SimpleNamespace
    import server

    monkeypatch.setenv("SONDER_RESIDENCY_FEEDBACK", "1")
    monkeypatch.setattr(server, "_RESIDENCY_FEEDBACK", None)
    monkeypatch.setattr(server, "_get", lambda _path: {"models": [spill_row()]})
    first_origin = "http://127.0.0.1:11434"
    monkeypatch.setattr(server, "BASE", first_origin)
    monkeypatch.setattr(server, "OLLAMA_POOL", SimpleNamespace(configured_origins=(first_origin,)))
    first = server._residency_feedback()
    first.note_selection("qwen:27b", 8192)
    first.refresh("qwen:27b", geometry=None, kv_type="f16")
    assert first.ceiling("qwen:27b") == 4096

    second_origin = "http://127.0.0.1:11435"
    monkeypatch.setattr(server, "BASE", second_origin)
    monkeypatch.setattr(server, "OLLAMA_POOL", SimpleNamespace(configured_origins=(second_origin,)))
    second = server._residency_feedback()
    assert second is not first
    assert second.ceiling("qwen:27b") is None
