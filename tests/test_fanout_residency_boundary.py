"""The fanout no-load fence lives in the domain; the root name is a delegate."""
import server
from sonder_runtime.domain import fanout_residency

LOADED = {"selection_profile": "loaded-local-chat"}


def test_only_the_no_load_profile_is_fenced():
    assert fanout_residency.dispatch_residency_reason({"selection_profile": ""}, "m", fetch_resident=lambda: 1 / 0) == ""
    assert fanout_residency.dispatch_residency_reason({}, "m", fetch_resident=lambda: 1 / 0) == ""


def test_resident_targets_pass_and_missing_or_unverifiable_ones_are_skipped():
    payload = {"models": [{"name": "Phi4:latest"}, {"name": ""}, "junk"]}
    reason = fanout_residency.dispatch_residency_reason
    assert reason(LOADED, "phi4:LATEST", fetch_resident=lambda: payload) == ""
    assert reason(LOADED, "gemma3:12b", fetch_resident=lambda: payload) == "model is no longer resident at dispatch"
    assert reason(LOADED, "phi4:latest", fetch_resident=lambda: {"models": "x"}) == "could not verify model residency at dispatch"
    assert reason(LOADED, "phi4:latest", fetch_resident=lambda: 1 / 0) == "could not verify model residency at dispatch"


def test_root_delegate_fetches_live_residency_through_the_server_seam(monkeypatch):
    fetched = []

    def fake_get(path):
        fetched.append(path)
        return {"models": [{"name": "phi4:latest"}]}

    monkeypatch.setattr(server, "_get", fake_get)
    assert server._fanout_dispatch_residency_reason(LOADED, "phi4:latest") == ""
    assert server._fanout_dispatch_residency_reason(LOADED, "other") == "model is no longer resident at dispatch"
    assert server._fanout_dispatch_residency_reason({}, "other") == ""
    assert fetched == ["/api/ps", "/api/ps"]
