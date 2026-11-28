"""The base capability probe must not depend on /api/show latency or failures."""
from __future__ import annotations

import io
import json
import threading
import time

from sonder_runtime.adapters.inference import ollama_pool as op

ORIGIN = "https://192.0.2.10:8443"


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def _make_transport(names, show_handler, ps_models=()):
    show_calls: list[str] = []
    ordered = sorted(names)
    call_idx = [0]
    fitted_by_name = {
        str(row.get("id") or row.get("name") or ""): row["context_length"]
        for row in ps_models
        if isinstance(row, dict) and isinstance(row.get("context_length"), int)
    }
    health_models = [
        {
            "id": str(row.get("id") or row.get("name") or ""),
            "state": "resident",
            "resident": True,
        }
        for row in ps_models
        if isinstance(row, dict)
    ]

    def fake(request, **kwargs):
        url = request.full_url
        if url.endswith("/v1/sonder/health"):
            return _Resp(json.dumps({"status": "ready", "version": "0.12.6", "models": health_models}).encode())
        if url.endswith("/v1/models"):
            is_show = float(kwargs.get("timeout", 0.0) or 0.0) >= 5.0
            if not is_show:
                data_rows = []
                for n in names:
                    entry = {"id": n, "name": n, "digest": "sha256:" + n}
                    if n in fitted_by_name:
                        entry["sonder"] = {
                            "backend": "llamacpp",
                            "runtime": {"context": {"fitted_ctx": fitted_by_name[n]}},
                        }
                    data_rows.append(entry)
                return _Resp(json.dumps({"object": "list", "data": data_rows}).encode())
            idx = call_idx[0]
            call_idx[0] += 1
            model = ordered[idx % len(ordered)]
            show_calls.append(model)
            meta = show_handler(model)
            row = {"id": model, "name": model, "digest": "sha256:" + model}
            if isinstance(meta, dict):
                row.update(meta)
            return _Resp(json.dumps({"object": "list", "data": [row]}).encode())
        raise AssertionError(url)

    return fake, show_calls


def test_slow_show_does_not_fail_probe_with_many_models():
    names = [f"m{i}:latest" for i in range(12)]

    def slow(_model):
        time.sleep(0.3)
        return {"capabilities": ["completion", "tools"]}

    fake, show_calls = _make_transport(names, slow)
    probe = op._default_capability_prober(allow_remote=True, timeout=2.0, open_url=fake)
    started = time.monotonic()
    value, error = op._bounded_capability_probe(probe, ORIGIN, 2.0, lambda: None)
    elapsed = time.monotonic() - started
    assert error is None
    assert value["version"] == "0.12.6"
    assert sorted(value["models"]) == sorted(names)
    assert elapsed < 1.0
    probe.wait_show_fill(10.0)
    assert len(show_calls) == 8
    value, error = op._bounded_capability_probe(probe, ORIGIN, 2.0, lambda: None)
    assert error is None
    assert len(value["supports_tools"]) == 8
    probe.wait_show_fill(10.0)
    assert len(show_calls) == 12


def test_hanging_show_never_fails_the_probe():
    release = threading.Event()

    def hang(_model):
        release.wait(5.0)
        raise TimeoutError("timed out")

    fake, _calls = _make_transport(["a:latest", "b:latest"], hang)
    probe = op._default_capability_prober(allow_remote=True, timeout=2.0, open_url=fake)
    try:
        for _ in range(3):
            started = time.monotonic()
            value, error = op._bounded_capability_probe(probe, ORIGIN, 2.0, lambda: None)
            assert error is None
            assert value["version"] == "0.12.6"
            assert tuple(value["models"]) == ("a:latest", "b:latest")
            assert time.monotonic() - started < 1.0
    finally:
        release.set()
        probe.wait_show_fill(10.0)


def test_failed_show_is_cached_with_backoff():
    def handler(model):
        if model.startswith("a-"):
            raise TimeoutError("timed out")
        return {"capabilities": ["completion"]}

    fake, show_calls = _make_transport(["a-broken:latest", "b:latest"], handler)
    probe = op._default_capability_prober(allow_remote=True, timeout=2.0, open_url=fake)
    for _ in range(4):
        probe(ORIGIN)
        probe.wait_show_fill(10.0)
    assert show_calls.count("a-broken:latest") == 1
    assert show_calls.count("b:latest") == 1


def test_loaded_model_context_is_runtime_value_not_trained_maximum():
    """ps context_length (what is loaded now) wins for loaded models; show
    only supplies the trained maximum for models that are not loaded."""
    def handler(_model):
        return {"model_info": {"llama.context_length": 262144},
                "capabilities": ["completion"]}

    fake, _calls = _make_transport(
        ["loaded:latest", "idle:latest"], handler,
        ps_models=[{"name": "loaded:latest", "context_length": 32768}],
    )
    probe = op._default_capability_prober(allow_remote=True, timeout=2.0, open_url=fake)
    probe(ORIGIN)
    probe.wait_show_fill(10.0)
    report = probe(ORIGIN)
    assert report["model_contexts"]["loaded:latest"] == 32768
    assert report["model_contexts"]["idle:latest"] == 262144
