"""Fanout model-health recording lives in the adapters layer; the root name is a delegate."""
import time

import server
from sonder_runtime.adapters import fanout_health
from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.adapters.persistence import fanout_store


def _capture(monkeypatch, previous=None):
    calls = []
    monkeypatch.setattr(fanout_store, "record_model_health", lambda model, **kw: calls.append((model, kw)))
    monkeypatch.setattr(fanout_store, "get_model_health", lambda _model: previous)
    return calls


def _record(model, exc, cloud=False):
    return fanout_health.record_health(model, exc, "prompt", is_cloud_model_name=lambda _m: cloud)


def test_root_delegate_uses_the_server_cloud_classifier(monkeypatch):
    calls = _capture(monkeypatch)
    monkeypatch.setattr(server, "_is_cloud_model_name", lambda _m: True)
    server._fanout_health("m:cloud", None, "p")
    assert calls == [("m:cloud", {"model_class": "cloud", "success": True})]


def test_success_records_the_model_class(monkeypatch):
    calls = _capture(monkeypatch)
    _record("local:7b", None)
    assert calls == [("local:7b", {"model_class": "local", "success": True})]


def test_cloud_failures_use_provider_hints(monkeypatch):
    calls = _capture(monkeypatch)
    before = time.time()
    _record("k:cloud", ModelCallError("http", "x", status=402), cloud=True)
    _record("k:cloud", ModelCallError("http", "x", status=429, retry_after_seconds=12), cloud=True)
    _record("k:cloud", ModelCallError("transport", "x", transient=True, retry_after_seconds=7), cloud=True)
    assert [kw["model_class"] for _m, kw in calls] == ["cloud"] * 3
    assert calls[0][1]["disabled_until"] >= before + 3599
    assert abs(calls[1][1]["disabled_until"] - (before + 12)) < 5
    assert abs(calls[2][1]["disabled_until"] - (before + 7)) < 5
    assert all(kw["counts_toward_backoff"] is False for _m, kw in calls)
    assert calls[0][1]["error"] == "ERROR: fanout model failure (http HTTP 402)"


def test_local_availability_failures_back_off_exponentially(monkeypatch):
    calls = _capture(monkeypatch, previous={"availability_failure_count": 2})
    before = time.time()
    _record("local:7b", ModelCallError("http", "gone", status=404))
    _record("local:7b", ModelCallError("timeout", "slow"))
    assert calls[0][1]["counts_toward_backoff"] is True
    assert calls[0][1]["disabled_until"] >= before + 3599
    assert calls[1][1]["counts_toward_backoff"] is True
    assert abs(calls[1][1]["disabled_until"] - (before + 1200)) < 5
    fresh = _capture(monkeypatch, previous=None)
    _record("local:7b", ModelCallError("protocol", "garbled"))
    assert abs(fresh[0][1]["disabled_until"] - (time.time() + 300)) < 5


def test_caller_errors_stay_eligible(monkeypatch):
    calls = _capture(monkeypatch)
    _record("local:7b", ModelCallError("configuration", "bad prompt"))
    _record("local:7b", RuntimeError("boom"))
    assert all(kw["disabled_until"] is None and kw["counts_toward_backoff"] is False for _m, kw in calls)
    assert calls[1][1]["error"] == "ERROR: model request failed (RuntimeError)"
