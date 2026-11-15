"""Shared fake Ollama and wiring for the GPU admission tests.

``FakeOllama`` behaves like the parts of Ollama admission depends on: it keeps
one loaded window per model (and counts a reload when a request asks for a
different one), reports ``/api/ps`` and ``/api/tags``, and records concurrency.
``wire`` points ``server`` at it with the single-host pool shape the production
primary path uses. ``make_service`` builds the admission service with injected
readers and returns ``None`` while the module does not exist, so a behavioural
test written before the implementation fails on its own assertion rather than
on an import.
"""
from __future__ import annotations

import io
import json
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

GIB = 1024 ** 3
MODEL = "qwen3:9b-q8_0"
DIGEST = "ab" * 32


class FakeMetrics:
    def __init__(self):
        self.mismatches = 0

    def observe_num_ctx_mismatch(self):
        self.mismatches += 1


class FakeOllama:
    def __init__(self, *, file_size=int(9.5 * GIB)):
        self.lock = threading.Lock()
        self.requests: list[tuple[str, str, dict | None]] = []
        self.loaded: dict[str, int] = {}
        self.reloads = 0
        self.file_size = file_size
        self.report_context_length = True
        self.ps_context_override: dict[str, int] = {}
        self.probe_active = 0
        self.max_probe_active = 0
        self.show_gate: threading.Event | None = None
        self.gate_timeout = 1.5

    # -- what admission reads --------------------------------------------
    def ps(self, origin=None):
        models = []
        for name, ctx in self.loaded.items():
            row = {"name": name, "model": name, "size": 11 * GIB, "size_vram": 11 * GIB}
            if self.report_context_length:
                row["context_length"] = self.ps_context_override.get(name, ctx)
            models.append(row)
        return {"models": models}

    def tags(self, origin=None):
        return {"models": [{"name": MODEL, "model": MODEL, "digest": DIGEST,
                            "size": self.file_size}]}

    # -- the wire ---------------------------------------------------------
    def open_url(self, request, timeout=None, **_kwargs):
        path = urlsplit(request.full_url).path
        body = json.loads(request.data) if request.data else None
        with self.lock:
            self.requests.append((request.get_method(), path, body))
        probe = path in {"/api/show", "/api/version", "/api/tags", "/api/ps"}
        if probe:
            with self.lock:
                self.probe_active += 1
                self.max_probe_active = max(self.max_probe_active, self.probe_active)
        try:
            if path == "/api/show" and self.show_gate is not None:
                self.show_gate.wait(self.gate_timeout)
            return io.BytesIO(json.dumps(self._respond(path, body)).encode())
        finally:
            if probe:
                with self.lock:
                    self.probe_active -= 1

    def _respond(self, path, body):
        if path == "/api/version":
            return {"version": "0.0-test"}
        if path == "/api/tags":
            return {"models": [{"name": MODEL}]}
        if path == "/api/ps":
            return self.ps()
        if path == "/api/show":
            return {"model_info": {"llama.context_length": 32768}, "details": {}}
        model = (body or {}).get("model", "")
        if (body or {}).get("keep_alive") == 0 and not (body or {}).get("messages"):
            with self.lock:
                self.loaded.pop(model, None)
            return {"model": model, "done": True}
        ctx = ((body or {}).get("options") or {}).get("num_ctx", 4096)
        with self.lock:
            if model in self.loaded and self.loaded[model] != ctx:
                self.reloads += 1
            self.loaded[model] = ctx
        return {"model": model, "message": {"role": "assistant", "content": "ok"}, "done": True}

    @property
    def chat_bodies(self):
        return [body for _method, path, body in self.requests if path == "/api/chat"]

    def count(self, path):
        return sum(1 for _method, seen, _body in self.requests if seen == path)


def chat_body(num_ctx, *, model=MODEL, content="hello"):
    return {"model": model, "stream": False,
            "messages": [{"role": "user", "content": content}],
            "options": {"num_ctx": num_ctx}}


def wire(monkeypatch, server, fake, service, *, base="http://127.0.0.1:11434"):
    """Point ``server`` at ``fake`` with the production single-host pool shape."""
    monkeypatch.setattr(server, "BASE", base)
    monkeypatch.setattr(server, "_require_ollama_endpoint", lambda **_kwargs: None)
    monkeypatch.setattr(server, "_residency_feedback", lambda: None)
    monkeypatch.setattr(server, "dispatch_provider", lambda _provider, _path, _payload, send: send())
    pool = SimpleNamespace(
        enabled=False, open_url=fake.open_url,
        request_primary=lambda send, *, origin=None, **_kwargs: send(origin),
        gpu_admission=service,
    )
    monkeypatch.setattr(server, "OLLAMA_POOL", pool)
    return pool


def make_service(tmp_path, fake, *, host=None, metrics=None, **config):
    """An enforcing service on a tmp store, or ``None`` before it exists."""
    try:
        from sonder_runtime.adapters.inference import gpu_admission as adapter
        from sonder_runtime.domain import gpu_admission as domain
        from sonder_runtime.platform.gpu_admission_config import GpuAdmissionConfig
    except ImportError:
        return None
    available, commit = host if host is not None else (64 * GIB, 64 * GIB)
    reading = domain.HostMemoryReading(
        available_physical_bytes=available, total_physical_bytes=64 * GIB,
        commit_limit_bytes=96 * GIB, commit_available_bytes=commit, commit_enforced=True)
    values = {"mode": "enforce", "store_path": str(tmp_path / "gpu-admission.sqlite3"),
              "poll_interval_ms": 20}
    values.update(config)
    return adapter.GpuAdmission(
        GpuAdmissionConfig(**values),
        host_reader=lambda: reading, ps_reader=fake.ps, tags_reader=fake.tags,
        metrics=metrics, background=lambda work: work(),
    )


def wait_until(predicate, timeout=3.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()
