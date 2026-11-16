"""A fully local batch-job harness: real ``fanout.db`` and vault key, fake model generation.

``build`` isolates the durable store in ``tmp_path``, replaces the server's
model catalog and ``_make_generate`` with recorders, registers the real
``batch_job_*`` tools on a fake MCP and returns a :class:`Harness`.  Nothing
touches the network or a real model.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import server
from sonder_runtime.bootstrap import batch_job_tools

ITEM_TEXT = "the quick brown fox jumps over the lazy dog 0123456789"
MARKER = "MARKER-9f3a7c1e"


class SimulatedCrash(BaseException):
    """Escapes every ``except Exception`` the way a killed process would."""


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn
        return register


@dataclass
class Call:
    model: str
    system: str
    prompt: str
    num_ctx: int
    temperature: float
    think: object


@dataclass
class Harness:
    mcp: FakeMCP
    service: object
    database: object
    calls: list = field(default_factory=list)
    posts: list = field(default_factory=list)
    answer: object = None
    served: object = None
    fail: object = None
    crash_on_call: int | None = None

    def tool(self, tool_name, **kwargs):
        return json.loads(self.mcp.tools[tool_name](**kwargs))

    def create(self, stages, *, items=3, start=False, token="", fields=("text",)):
        payload = [{"key": "item-%d" % index, "fields": {"text": "%s #%d" % (ITEM_TEXT, index)}}
                   for index in range(items)]
        return self.tool("batch_job_create", name="job", stages=stages, fields=list(fields),
                         items=payload, start=start, token=token)

    def wait(self, timeout=60.0):
        assert self.service.worker.wait_idle(timeout), "batch worker did not finish"


def two_stages(first="m:1", second="m:2"):
    return [
        {"name": "r1", "model": first, "prompt": "R1 {{text}}", "system": "judge", "num_ctx": 2048,
         "temperature": 0.0, "think": False},
        {"name": "r2", "model": second, "prompt": "R2 {{text}} || {{stage.r1}}"},
    ]


def build(monkeypatch, tmp_path, *, models=("m:1", "m:2"), config=None):
    database = tmp_path / "fanout.db"
    monkeypatch.setattr(server.fanout_store, "database_path", lambda: str(database))
    monkeypatch.setenv("SONDER_FANOUT_KEY_FILE", str(tmp_path / "fanout.key"))
    server.fanout_store.reset_schema_cache_for_tests()
    server.fanout_prompt_vault.reset_cache_for_tests()
    monkeypatch.setattr(batch_job_tools, "load_config_or_none", lambda: config)
    harness = Harness(mcp=FakeMCP(), service=None, database=database)

    def get(path):
        if path == "/api/tags":
            return {"models": [{"name": name, "capabilities": ["chat"]} for name in models]}
        return {"models": []}

    def fake_make(model, system, temperature, num_predict, num_ctx, **kwargs):
        def generate(prompt, history=None):
            call = Call(model, system, prompt, num_ctx, temperature, kwargs.get("think"))
            harness.calls.append(call)
            if harness.crash_on_call == len(harness.calls):
                raise SimulatedCrash()
            if harness.fail is not None:
                harness.fail(call)
            text = harness.answer(call) if harness.answer else "answer to " + prompt[:12]
            generate.last_usage = {"tokens_in": len(prompt), "tokens_out": len(text), "token_source": "ollama"}
            served = harness.served(call) if harness.served else model
            generate.last_response_meta = {"served_model": served}
            return text
        generate.last_usage = {}
        generate.last_response_meta = {}
        return generate

    monkeypatch.setattr(server, "_get", get)
    monkeypatch.setattr(server, "_make_generate", fake_make)
    monkeypatch.setattr(server, "_post", lambda path, payload, **_kw: harness.posts.append((path, payload)) or {})
    monkeypatch.setattr(server, "_developer_gate", lambda *_a, **_k: None)
    harness.service = batch_job_tools.register(harness.mcp, lambda *a, **k: None, runtime=lambda: server)
    return harness
