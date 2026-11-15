"""The ``security_eval`` MCP tool: discovery, gateway wrapping, typed failures and score_only."""
from __future__ import annotations

import json
import socket
from dataclasses import replace
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.provider_bindings import ProviderBindings
from sonder_runtime.bootstrap import agent_help_tools
from sonder_runtime.bootstrap import security_eval_agent_tools as tool_module
from sonder_runtime.platform.security_eval_config import SecurityEvalConfig
from tests.support.security_eval_fixtures import build_repo, fixture_records, record, write_dataset
from tests.test_security_eval_runner import FakeGateway, truth

pytestmark = pytest.mark.unit


class FakeMcp:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorate(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorate


def bindings():
    return ProviderBindings("ollama", {"fast": "ollama", "general": "ollama", "code": "ollama",
                                       "reasoning": "ollama", "vision": "ollama"}, "ollama")


@pytest.fixture
def world(tmp_path):
    repo = build_repo(tmp_path / "repo")
    items = []
    for item in fixture_records(repo):
        item["hypothesis"] = "claim %s" % item["hid"]
        items.append(item)
    dataset = write_dataset(tmp_path / "set.jsonl", items, extra_lines=["{broken"])
    return SimpleNamespace(tmp=tmp_path, repo=repo, dataset=dataset, out=tmp_path / "out")


def build(world, gateway, **overrides):
    config = replace(SecurityEvalConfig(), repo_roots=(str(world.tmp),), num_ctx=4096, **overrides)
    app = SimpleNamespace(config=SimpleNamespace(security_eval=config), model_gateway=gateway,
                          provider_bindings=bindings())
    mcp = FakeMcp()
    calls = []
    tool_module.register(mcp, lambda name, args, **kw: calls.append((name, args, kw)),
                         runtime=lambda: SimpleNamespace(_application=lambda: app))
    return mcp.tools["security_eval"], calls, app


def call(fn, **kwargs):
    return json.loads(fn(**kwargs))


def test_the_registrar_is_discovered():
    found = dict(agent_help_tools.discover_agent_tool_registrars())
    assert found["security_eval_agent_tools"] is tool_module.register


def test_a_full_run_reports_scores_served_model_and_tokens(world):
    gateway = FakeGateway(truth)
    fn, calls, _ = build(world, gateway)
    result = call(fn, model="demo:9b", out_dir=str(world.out), dataset=str(world.dataset))
    assert result["ok"] and result["complete"] and result["stopped_reason"] == "done" and result["remaining"] == 0
    assert result["scored"] == 20 and result["invalid_dataset_lines"] == 1
    assert result["arms"]["retrieved"]["precision"] == 1.0 and result["arms"]["none"]["recall"] == pytest.approx(0.25)
    assert result["served_models"] == {"demo:9b-q8": 20} and result["served_model_source"] == "gateway_resolved"
    assert result["tokens"]["rows_measured"] == 10 and result["paired"]["c"] == 5
    assert result["report"].endswith("report.json") and result["summary"].endswith("summary.md")
    assert calls and calls[0][0] == "security_eval" and calls[0][2]["ok"] is True
    assert len(gateway.generate_calls) == 20


def test_the_tool_wraps_the_application_gateway_in_the_text_only_wrapper(world, monkeypatch):
    seen = {}

    class Spy(tool_module.ServedTierGateway):
        def __init__(self, gateway, *, bindings):
            seen["gateway"], seen["bindings"] = gateway, bindings
            super().__init__(gateway, bindings=bindings)

    monkeypatch.setattr(tool_module, "ServedTierGateway", Spy)
    gateway = FakeGateway()
    fn, _, app = build(world, gateway)
    assert call(fn, model="demo:9b", out_dir=str(world.out), dataset=str(world.dataset), limit=2)["ok"]
    assert seen["gateway"] is app.model_gateway and seen["bindings"] is app.provider_bindings
    assert len(gateway.generate_calls) == 2


def test_the_dataset_argument_wins_over_the_environment_setting(world):
    gateway = FakeGateway()
    fn, _, _ = build(world, gateway, dataset=str(world.tmp / "missing.jsonl"))
    assert call(fn, model="demo:9b", out_dir=str(world.out))["error_type"] == "DatasetError"
    assert call(fn, model="demo:9b", out_dir=str(world.out), dataset=str(world.dataset), limit=1)["ok"]
    fn2, _, _ = build(world, FakeGateway(), dataset=str(world.dataset), model="demo:9b")
    assert call(fn2, out_dir=str(world.tmp / "out2"), limit=1)["ok"]


@pytest.mark.parametrize("kwargs,error_type", [
    ({"out_dir": "OUT"}, "InvalidInput"),
    ({"model": "demo:9b"}, "InvalidInput"),
    ({"model": "bad name!", "out_dir": "OUT"}, "InvalidInput"),
    ({"model": "demo:9b", "out_dir": "OUT", "mode": "weird"}, "InvalidInput"),
    ({"model": "demo:9b", "out_dir": "OUT", "context": "weird"}, "InvalidInput"),
    ({"model": "demo:9b", "out_dir": "relative/out"}, "OutputDirRefused"),
])
def test_bad_arguments_return_a_typed_error(world, kwargs, error_type):
    gateway = FakeGateway()
    fn, calls, _ = build(world, gateway)
    kwargs = {key: (str(world.out) if value == "OUT" else value) for key, value in kwargs.items()}
    result = call(fn, dataset=str(world.dataset), **kwargs)
    assert result["ok"] is False and result["error_type"] == error_type and result["error"]
    assert len(gateway.generate_calls) == 0 and calls[-1][2]["ok"] is False


def test_an_unlisted_repo_root_and_an_output_inside_a_repo_are_refused(world):
    gateway = FakeGateway()
    config_fn, _, _ = build(world, gateway)
    outside = replace(SecurityEvalConfig(), repo_roots=())
    app = SimpleNamespace(config=SimpleNamespace(security_eval=outside), model_gateway=gateway,
                          provider_bindings=bindings())
    mcp = FakeMcp()
    tool_module.register(mcp, lambda *a, **k: None, runtime=lambda: SimpleNamespace(_application=lambda: app))
    refused = call(mcp.tools["security_eval"], model="demo:9b", out_dir=str(world.out), dataset=str(world.dataset))
    assert refused["error_type"] == "RepoRootRefused" and len(gateway.generate_calls) == 0
    inside = call(config_fn, model="demo:9b", out_dir=str(world.repo / "results"), dataset=str(world.dataset))
    assert inside["error_type"] == "OutputDirRefused"


def test_a_cloud_route_returns_a_residency_error_and_sends_nothing(world):
    gateway = FakeGateway(cloud=True)
    fn, _, _ = build(world, gateway)
    result = call(fn, model="demo:9b", out_dir=str(world.out), dataset=str(world.dataset))
    assert result["error_type"] == "ResidencyRefused" and len(gateway.generate_calls) == 0


def test_a_changed_setting_returns_resume_mismatch(world):
    fn, _, _ = build(world, FakeGateway())
    assert call(fn, model="demo:9b", out_dir=str(world.out), dataset=str(world.dataset), limit=1)["ok"]
    changed, _, _ = build(world, FakeGateway(), num_predict=999)
    result = call(changed, model="demo:9b", out_dir=str(world.out), dataset=str(world.dataset))
    assert result["ok"] is False and result["error_type"] == "ResumeMismatch"


def test_score_only_makes_no_model_call(world, monkeypatch):
    fn, _, _ = build(world, FakeGateway(truth))
    assert call(fn, model="demo:9b", out_dir=str(world.out), dataset=str(world.dataset))["complete"]
    (world.out / "report.json").unlink()
    silent = FakeGateway()
    quiet, _, _ = build(world, silent)

    def refuse(*args, **kwargs):
        raise AssertionError("a socket was opened")

    monkeypatch.setattr(socket, "socket", refuse)
    result = call(quiet, model="demo:9b", out_dir=str(world.out), dataset=str(world.dataset), score_only=True)
    assert result["ok"] and result["complete"] and len(silent.generate_calls) == 0 and silent.resolve_calls == 0
    assert (world.out / "report.json").exists()


def test_filters_and_chunk_mode_run_through_the_tool(world):
    fn, _, _ = build(world, FakeGateway(lambda h, c: "[]"))
    result = call(fn, model="demo:9b", out_dir=str(world.out), dataset=str(world.dataset), mode="chunk",
                  context="none")
    assert result["ok"] and result["chunk_scores"]["none"]["chunks"] == 1
    fn2, _, _ = build(world, FakeGateway())
    filtered = call(fn2, model="demo:9b", out_dir=str(world.tmp / "o2"), dataset=str(world.dataset),
                    context="none", categories="race", stages="new", hids="h07,h08")
    assert filtered["ok"] and filtered["scored"] == 2


def test_a_missing_runtime_is_a_dependency_error():
    mcp = FakeMcp()
    tool_module.register(mcp, lambda *a, **k: None, runtime=None)
    result = call(mcp.tools["security_eval"], model="demo:9b", out_dir="/x")
    assert result["ok"] is False and result["error_type"] == "DependencyUnavailable"
    with pytest.raises(TypeError):
        tool_module.register(mcp, lambda *a, **k: None, runtime="not callable")
