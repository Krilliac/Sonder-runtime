"""The runner end to end over a fake gateway, a real confined repo and a real output store.

No network (``socket.socket`` is patched to raise), no model, no subprocess: every request goes to a
recording fake gateway that stands in for the Sonder model path.
"""
from __future__ import annotations

import json
import os
import socket
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.security_eval.model_client import GatewayVerdictModel
from sonder_runtime.adapters.security_eval.output_store import OutputStore
from sonder_runtime.adapters.security_eval.repo_reader import ConfinedRepo, RepoLimits, RetrievalSettings
from sonder_runtime.application.ports.model_gateway import InferenceTelemetry, ModelResponse
from sonder_runtime.application.security_eval.runner import RunPlan, SecurityEvalRunner
from sonder_runtime.domain.common.errors import DependencyUnavailable, Forbidden
from sonder_runtime.domain.security_eval.errors import (
    OutputDirRefused, RepoRootRefused, ResidencyRefused, ResumeMismatch)
from sonder_runtime.domain.security_eval.prompts import DEFAULT_SYSTEM_PROMPT
from sonder_runtime.domain.security_eval.records import collect_records
from tests.support.security_eval_fixtures import build_repo, fixture_records, record

pytestmark = pytest.mark.unit

MODEL = "demo:9b"


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t


class FakeGateway:
    """Records every request; answers from a function of (hypothesis, has_context)."""

    def __init__(self, reply=None, *, cloud=False, worker=False, served="demo:9b-q8", clock=None,
                 step=0.0, fail_on=()):
        self.generate_calls = []
        self.resolve_calls = 0
        self.cloud, self.worker, self.served = cloud, worker, served
        self.clock, self.step, self.fail_on = clock, step, set(fail_on)
        self.reply = reply or (lambda hypothesis, has_context: json.dumps(
            {"verdict": "REFUTED", "reason": "fake"}))

    def resolve_route(self, request, context):
        self.resolve_calls += 1
        if self.worker and not context.remote_ollama_allowed:
            raise Forbidden("Ollama route may leave this machine but this operation context does not allow remote Ollama")
        return SimpleNamespace(cloud=self.cloud)

    def generate(self, request, context):
        self.generate_calls.append((request, context))
        document = json.loads(request.prompt)
        hypothesis = document["data"].get("hypothesis", "")
        if hypothesis in self.fail_on:
            raise DependencyUnavailable("the model provider is unavailable")
        if self.clock is not None:
            self.clock.t += self.step
        index = len(self.generate_calls)
        telemetry = InferenceTelemetry(prompt_tokens=100, output_tokens=20) if index % 2 == 0 else None
        return ModelResponse(text=self.reply(hypothesis, bool(document["data"].get("context"))), model=self.served,
                             tier="by-name", duration_ms=5, tokens_in=90, tokens_out=19, telemetry=telemetry)


class GenerateOnly:
    """A gateway with no ``resolve_route``: its residency cannot be classified."""

    def __init__(self):
        self.generate_calls = []

    def generate(self, request, context):
        self.generate_calls.append(request)
        raise AssertionError("generate must not be reached")


@pytest.fixture
def world(tmp_path):
    repo_root = build_repo(tmp_path / "repo")
    items = []
    for item in fixture_records(repo_root):
        item["hypothesis"] = "claim %s" % item["hid"]
        items.append(item)
    records, invalid = collect_records([(n, json.dumps(item)) for n, item in enumerate(items, start=1)])
    return SimpleNamespace(tmp=tmp_path, repo=repo_root, records=records, out=tmp_path / "out")


def make_runner(world, gateway, *, clock=None, allow_remote=False, num_ctx=4096, out=None):
    clock = clock or FakeClock()
    options = {"temperature": 0.0, "num_predict": 256}
    if num_ctx is not None:
        options["num_ctx"] = num_ctx

    def repos(root):
        return ConfinedRepo(root, [str(world.tmp)], RepoLimits(), RetrievalSettings())

    store = OutputStore(str(out or world.out), protected_roots=[str(world.repo)], dataset_path=None)
    model = GatewayVerdictModel(gateway, MODEL, options=options, allow_remote_ollama=allow_remote,
                                item_timeout_seconds=30)
    return SecurityEvalRunner(repos=repos, model=model, store=store, clock=clock), clock


def plan(**overrides):
    identity = {"model": MODEL, "num_predict": 256, "num_ctx": 4096, "pad": 20}
    identity.update(overrides.pop("identity", {}))
    return RunPlan(requested_model=MODEL, identity=identity, pad_lines=20, **overrides)


def truth(hypothesis, has_context):
    """arm 'retrieved' (context present) answers the label; arm 'none' is wrong on three items."""
    from tests.support.security_eval_fixtures import fixture_records as _fr
    labels = {"claim %s" % item["hid"]: item["label"] for item in _fr("x")}
    label = labels[hypothesis]
    if has_context:
        return json.dumps({"verdict": "REFUTED" if label == "UNCERTAIN" else label, "reason": "ctx"})
    wrong_confirm = {"claim h01", "claim h03", "claim h07"}
    return json.dumps({"verdict": "CONFIRMED" if hypothesis in wrong_confirm else "REFUTED", "reason": "plain"})


def test_every_request_has_the_pinned_shape_and_the_run_is_scored(world):
    gateway = FakeGateway(truth)
    runner, _ = make_runner(world, gateway)
    result = runner.run(world.records, plan())
    assert result.complete and result.stopped_reason == "done" and result.remaining == 0
    assert len(gateway.generate_calls) == 20
    for request, context in gateway.generate_calls:
        assert request.tier == MODEL
        assert request.system == DEFAULT_SYSTEM_PROMPT and request.system.strip()
        assert request.routing_metadata["purpose"] == "security_eval"
        assert request.options["num_ctx"] == 4096 and request.options["num_predict"] == 256
        assert context.source == "mcp" and not context.cloud_allowed and context.deadline_monotonic is not None
    assert {call[0].options.get("num_ctx") for call in gateway.generate_calls} == {4096}
    contexts = [json.loads(call[0].prompt)["data"]["context"] for call in gateway.generate_calls]
    assert any(contexts) and not all(contexts)
    report = result.report
    assert report["served_models"] == {"demo:9b-q8": 20} and report["served_model_source"] == "gateway_resolved"
    assert report["requested_model"] == MODEL
    tokens = report["tokens"]
    assert (tokens["rows"], tokens["rows_measured"], tokens["rows_unverified"]) == (20, 10, 10)
    assert tokens["tokens_in"] == 10 * 100 + 10 * 90 and tokens["tokens_out"] == 10 * 20 + 10 * 19
    none = report["scores"]["overall"]["none"]
    assert none["precision"] == pytest.approx(1 / 3) and none["recall"] == pytest.approx(1 / 4)
    retrieved = report["scores"]["overall"]["retrieved"]
    assert retrieved["precision"] == 1.0 and retrieved["recall"] == 1.0
    assert report["paired"]["c"] == 5 and report["paired"]["b"] == 0 and report["complete"] is True
    rows = [json.loads(line) for line in (world.out / "items.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 20 and {row["token_source"] for row in rows} == {"measured", "gateway_unverified"}
    assert all(row["served_model_source"] == "gateway_resolved" for row in rows)
    assert (world.out / "summary.md").read_text(encoding="utf-8").startswith("# Security eval: demo:9b")


def test_num_ctx_is_omitted_when_not_configured(world):
    gateway = FakeGateway()
    runner, _ = make_runner(world, gateway, num_ctx=None)
    runner.run(world.records[:2], plan(arms=("none",)))
    assert all("num_ctx" not in call[0].options for call in gateway.generate_calls)


def test_a_cloud_route_is_refused_before_any_generate(world):
    gateway = FakeGateway(cloud=True)
    runner, _ = make_runner(world, gateway)
    with pytest.raises(ResidencyRefused):
        runner.run(world.records, plan())
    assert len(gateway.generate_calls) == 0


def test_a_gateway_with_no_resolver_is_refused_before_any_generate(world):
    gateway = GenerateOnly()
    runner, _ = make_runner(world, gateway)
    with pytest.raises(ResidencyRefused):
        runner.run(world.records, plan())
    assert gateway.generate_calls == []


def test_a_lexically_cloud_model_name_is_refused_without_resolving(world):
    gateway = FakeGateway()
    clock = FakeClock()
    store = OutputStore(str(world.out), protected_roots=[str(world.repo)], dataset_path=None)
    model = GatewayVerdictModel(gateway, "big-model:cloud", options={}, allow_remote_ollama=False,
                                item_timeout_seconds=30)
    runner = SecurityEvalRunner(repos=lambda root: ConfinedRepo(root, [str(world.tmp)]), model=model,
                                store=store, clock=clock)
    with pytest.raises(ResidencyRefused):
        runner.run(world.records, RunPlan(requested_model="big-model:cloud", identity={"model": "big"}))
    assert len(gateway.generate_calls) == 0


def test_a_worker_pool_route_needs_the_remote_consent(world):
    refused = FakeGateway(worker=True)
    runner, _ = make_runner(world, refused, allow_remote=False)
    with pytest.raises(ResidencyRefused):
        runner.run(world.records, plan())
    assert len(refused.generate_calls) == 0
    allowed = FakeGateway(worker=True)
    runner, _ = make_runner(world, allowed, allow_remote=True)
    result = runner.run(world.records[:2], plan(arms=("none",)))
    assert result.complete and len(allowed.generate_calls) == 2
    assert all(call[1].remote_ollama_allowed for call in allowed.generate_calls)


def test_a_model_error_marks_one_item_and_the_run_continues(world):
    gateway = FakeGateway(truth, fail_on=("claim h03",))
    runner, _ = make_runner(world, gateway)
    result = runner.run(world.records, plan())
    assert result.complete and result.report["outcomes"]["MODEL_ERROR"] == 2
    rows = [json.loads(line) for line in (world.out / "items.jsonl").read_text(encoding="utf-8").splitlines()]
    broken = [row for row in rows if row["outcome"] == "MODEL_ERROR"]
    assert {row["hid"] for row in broken} == {"h03"} and broken[0]["error_type"] == "DependencyUnavailable"
    assert result.report["scores"]["overall"]["none"]["model_error"] == 1


def test_a_policy_refusal_from_the_gateway_aborts_the_run(world):
    class Refusing(FakeGateway):
        def generate(self, request, context):
            self.generate_calls.append((request, context))
            raise Forbidden("model is denied for text lanes")

    gateway = Refusing()
    runner, _ = make_runner(world, gateway)
    with pytest.raises(Forbidden):
        runner.run(world.records, plan())
    assert len(gateway.generate_calls) == 1


def test_an_unparseable_reply_is_unparsed_not_dropped(world):
    gateway = FakeGateway(lambda hypothesis, has_context: "I have no idea.")
    runner, _ = make_runner(world, gateway)
    result = runner.run(world.records, plan(arms=("none",)))
    assert result.report["outcomes"] == {"UNPARSED": 10}
    none = result.report["scores"]["overall"]["none"]
    assert none["unparsed"] == 10 and none["precision"] is None and none["fn"] == 4


def test_a_source_error_skips_the_model_and_is_counted(world):
    records, _ = collect_records([(1, json.dumps(record("m1", world.repo, file="src/Missing.cs"))),
                                  (2, json.dumps(record("m2", world.repo, label="REFUTED")))])
    gateway = FakeGateway()
    runner, _ = make_runner(world, gateway)
    result = runner.run(records, plan(arms=("none",)))
    assert len(gateway.generate_calls) == 1
    assert result.report["outcomes"] == {"SOURCE_ERROR": 1, "SCORED": 1}
    rows = [json.loads(line) for line in (world.out / "items.jsonl").read_text(encoding="utf-8").splitlines()]
    assert next(row for row in rows if row["hid"] == "m1")["error_type"] == "SourceUnavailable"


def test_the_time_budget_stops_between_items_and_the_next_call_resumes(world):
    clock = FakeClock()
    gateway = FakeGateway(truth, clock=clock, step=100.0)
    runner, _ = make_runner(world, gateway, clock=clock)
    first = runner.run(world.records, plan(time_budget_seconds=250))
    assert (first.complete, first.stopped_reason, first.processed, first.remaining) == (False, "time_budget", 3, 17)
    assert first.report["complete"] is False and first.report["stopped_reason"] == "time_budget"
    assert first.report["remaining"] == 17
    assert "INCOMPLETE" in (world.out / "summary.md").read_text(encoding="utf-8")
    runner2, _ = make_runner(world, gateway, clock=clock)
    second = runner2.run(world.records, plan(time_budget_seconds=1_000_000))
    assert second.complete and second.processed == 17 and second.skipped_done == 3
    assert len(gateway.generate_calls) == 20
    keys = [(json.loads(call[0].prompt)["data"]["hypothesis"], bool(json.loads(call[0].prompt)["data"]["context"]))
            for call in gateway.generate_calls]
    assert len(set(keys)) == 20
    third = runner2.run(world.records, plan())
    assert third.complete and third.processed == 0 and len(gateway.generate_calls) == 20


def test_the_limit_stops_the_run(world):
    gateway = FakeGateway()
    runner, _ = make_runner(world, gateway)
    result = runner.run(world.records, plan(limit=4))
    assert (result.complete, result.stopped_reason, result.processed, result.remaining) == (False, "limit", 4, 16)


def test_cancellation_stops_the_run(world):
    gateway = FakeGateway()
    runner, _ = make_runner(world, gateway)
    result = runner.run(world.records, plan(cancelled=lambda: True))
    assert result.stopped_reason == "cancelled" and result.processed == 0 and not gateway.generate_calls


def test_a_changed_configuration_refuses_to_mix_rows(world):
    gateway = FakeGateway()
    runner, _ = make_runner(world, gateway)
    runner.run(world.records, plan(limit=2))
    calls = len(gateway.generate_calls)
    runner2, _ = make_runner(world, gateway)
    with pytest.raises(ResumeMismatch):
        runner2.run(world.records, plan(identity={"num_predict": 999}))
    with pytest.raises(ResumeMismatch):
        runner2.score_only(world.records, plan(identity={"num_predict": 999}))
    assert len(gateway.generate_calls) == calls


def test_score_only_makes_no_model_call_and_rewrites_the_report(world):
    gateway = FakeGateway(truth)
    runner, _ = make_runner(world, gateway)
    runner.run(world.records, plan())
    calls = len(gateway.generate_calls)
    (world.out / "report.json").unlink()
    (world.out / "summary.md").unlink()
    silent = FakeGateway()
    runner2, _ = make_runner(world, silent)
    result = runner2.score_only(world.records, plan())
    assert len(silent.generate_calls) == 0 and silent.resolve_calls == 0 and len(gateway.generate_calls) == calls
    assert result.complete and (world.out / "report.json").exists() and (world.out / "summary.md").exists()
    assert result.report["scores"]["overall"]["retrieved"]["precision"] == 1.0


def test_a_filter_selects_items_and_unselected_items_are_not_pending(world):
    gateway = FakeGateway()
    runner, _ = make_runner(world, gateway)
    result = runner.run(world.records, plan(arms=("none",), categories=frozenset({"race"})))
    assert result.complete and len(gateway.generate_calls) == 3
    result = make_runner(world, FakeGateway(), out=world.tmp / "out2")[0].run(
        world.records, plan(arms=("none",), hids=frozenset({"h01", "h02"}), stages=frozenset({"r1"})))
    assert result.processed == 1


def test_a_root_outside_the_allowlist_aborts_before_any_call(world):
    other = build_repo(world.tmp.parent / (world.tmp.name + "-other") / "repo")
    records, _ = collect_records([(1, json.dumps(record("o1", other)))])
    gateway = FakeGateway()
    runner, _ = make_runner(world, gateway)
    with pytest.raises(RepoRootRefused):
        runner.run(records, plan())
    assert gateway.generate_calls == []


def test_only_the_three_report_files_appear_and_only_under_out_dir(world):
    before = {str(path) for path in world.tmp.rglob("*")}
    gateway = FakeGateway(truth)
    runner, _ = make_runner(world, gateway)
    runner.run(world.records, plan())
    after = {str(path) for path in world.tmp.rglob("*")}
    new = {os.path.relpath(path, world.tmp) for path in after - before}
    assert {name.replace("\\", "/") for name in new} == {
        "out", "out/items.jsonl", "out/report.json", "out/summary.md"}


def test_the_run_needs_no_network(world, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a socket was opened")

    monkeypatch.setattr(socket, "socket", refuse)
    gateway = FakeGateway(truth)
    runner, _ = make_runner(world, gateway)
    assert runner.run(world.records, plan()).complete


def test_the_output_dir_is_refused_inside_a_repo_root_or_as_the_dataset_or_through_a_link(world):
    with pytest.raises(OutputDirRefused):
        OutputStore(str(world.repo / "results"), protected_roots=[str(world.repo)], dataset_path=None)
    with pytest.raises(OutputDirRefused):
        OutputStore("relative/out", protected_roots=[], dataset_path=None)
    dataset = world.tmp / "set.jsonl"
    dataset.write_text("{}\n", encoding="utf-8")
    with pytest.raises(OutputDirRefused):
        OutputStore(str(dataset), protected_roots=[], dataset_path=str(dataset))
    clash = world.tmp / "clash"
    clash.mkdir()
    (clash / "items.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(OutputDirRefused):
        OutputStore(str(clash), protected_roots=[], dataset_path=str(clash / "items.jsonl"))
    from tests.test_security_eval_repo_reader import make_dir_link
    real = world.tmp / "real"
    real.mkdir()
    make_dir_link(world.tmp / "linked", real)
    with pytest.raises(OutputDirRefused):
        OutputStore(str(world.tmp / "linked" / "out"), protected_roots=[], dataset_path=None)


def test_chunk_mode_matches_findings_to_labels_and_reports_a_precision_floor(world):
    reply = json.dumps([
        {"file": "src/PacketReader.cs", "lines": "13-14", "category": "bounds", "hypothesis": "first"},
        {"file": "src/PacketReader.cs", "lines": "13-14", "category": "race", "hypothesis": "second"},
        {"file": "src/PacketReader.cs", "lines": "21-22", "category": "bounds", "hypothesis": "third"},
        {"hypothesis": "fourth, with no location"},
    ])
    gateway = FakeGateway(lambda hypothesis, has_context: reply)
    runner, _ = make_runner(world, gateway)
    result = runner.run(world.records, plan(mode="chunk", arms=("none",), chunk_chars=100_000, match_line_tolerance=0))
    assert result.complete and len(gateway.generate_calls) == 1
    chunk = result.report["chunk_scores"]["none"]
    assert chunk["generated"] == 4 and chunk["matched_confirmed"] == 2 and chunk["matched_refuted"] == 1
    assert chunk["precision_floor"] == pytest.approx(2 / 4) and chunk["precision_labelled"] == pytest.approx(2 / 3)
    assert chunk["scope_confirmed"] == 4 and chunk["recall"] == pytest.approx(2 / 4)
    assert "floor" in chunk["note"]
    request = gateway.generate_calls[0][0]
    assert request.tier == MODEL and request.system.strip() and request.routing_metadata["purpose"] == "security_eval"


def test_chunk_mode_with_an_unparseable_reply_is_a_failed_chunk(world):
    gateway = FakeGateway(lambda hypothesis, has_context: "nothing structured")
    runner, _ = make_runner(world, gateway)
    result = runner.run(world.records, plan(mode="chunk", arms=("none",), chunk_chars=100_000))
    chunk = result.report["chunk_scores"]["none"]
    assert chunk["failed_chunks"] == 1 and chunk["recall"] == 0.0 and chunk["precision_floor"] is None
