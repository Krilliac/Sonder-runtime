"""Cross-lane seams of the C++ build feature: every call site binds to the real callee.

The build feature was built in parallel lanes (A domain, B1 model/job services,
B2 fix loop, C composition and surfaces), each against the spec and its own
test doubles. This module lists the calls one lane makes into another -- by
the argument shapes the caller actually uses -- and binds each to the real
callee's ``inspect.signature``. A lane that renames a parameter or a caller
that passes a keyword the callee lacks fails here, not in a live run.

It also checks that the service doubles the surface tests share
(``tests.test_build_executor``) keep the real services' signatures, so a
double can no longer mask a seam mismatch.
"""
from __future__ import annotations

import inspect

import pytest

from sonder_runtime.adapters.build import candidates, clangd, collector, environment, executor  # noqa: E402
from sonder_runtime.adapters.build import launcher, network, planner, preimages  # noqa: E402
from sonder_runtime.adapters.build import source_editor, tree_reader  # noqa: E402
from sonder_runtime.adapters.persistence.sqlite.job_registry import SQLiteDurableJobRegistry  # noqa: E402
from sonder_runtime.application.build import fix_service, grants, model_service, run_service  # noqa: E402
from sonder_runtime.application.build import strategy_bridge  # noqa: E402
from sonder_runtime.application.cancellation_tree import CancellationTree  # noqa: E402
from sonder_runtime.bootstrap import build_tools  # noqa: E402
from sonder_runtime.domain.build import repair, tool_targets  # noqa: E402

pytestmark = pytest.mark.unit

X = object()  # any positional value

# (caller, callee, args, kwargs): the argument shapes each caller uses.
SEAMS = [
    # C -> B1 (bootstrap/build_tools.compose_build_tools)
    ("C->B1", environment.ScrubbedEnvironmentProvider, (), {"passthrough": X}),
    ("C->B1", network.network_wrapper, (), {"mode": X, "lookup": X}),
    ("C->B1", tree_reader.GuardedBuildTreeReader, (), {"user_presets": X}),
    ("C->B1", planner.ProjectBuildPlanner, (X, X, X, X),
     {"run_root": X, "redact": X, "operator_max_timeout": X, "utility_allow": X, "profiles": X,
      "executable_guard": X}),
    ("C->B1", launcher.ProcessBuildLauncher, (X, X), {"executable_guard": X, "run_root": X}),
    ("C->B1", collector.BuildOutputCollector, (X,), {"redact": X, "output_reader": X}),
    ("C->B1", model_service.BuildModelService, (X, X, X), {"clock": X}),
    ("C->B1", model_service.LruBuildModelCache, (), {}),
    ("C->B1", run_service.BuildJobService, (X, X, X, X, X), {"clock": X}),
    ("C->B1", run_service.InMemoryBuildDirLeases, (), {"is_active": X}),
    ("C->B1", run_service.build_job_liveness, (X,), {}),
    # C -> B2 (bootstrap/build_tools._compose_fix)
    ("C->B2", fix_service.BuildFixService, (X,) * 9,
     {"clock": X, "grants": X, "propose_only_ok": X, "operator_max_timeout": X}),
    ("C->B2", source_editor.GatewaySourceEditor, (X,), {}),
    ("C->B2", candidates.ModelCandidateGenerator, (build_tools._LazyModelGateway(None),),
     {"route": "codegen", "redact": X}),
    ("C->B2", strategy_bridge.StrategyFixAdapter, (), {}),
    ("C->B2", preimages.FilePreimageStore, (X,), {}),
    ("C->D", clangd.ClangdNavigator, (X,),
     {"project_root": X, "compile_commands_dir": X, "enable_config": X}),
    # C (executor) -> B1/B2 services
    ("C->B1", model_service.BuildModelService.view, (X, X, X), {"detail": X, "target": X, "max_items": X}),
    ("C->B1", model_service.BuildModelService.cached_summary, (X, X, X), {}),
    ("C->B1", run_service.BuildJobService.plan, (X, X, X), {}),
    ("C->B1", run_service.BuildJobService.run, (X, X, X), {"wait_seconds": X}),
    ("C->B1", run_service.BuildJobService.result, (X, X, X), {"wait_seconds": X}),
    ("C->B1", run_service.BuildJobService.cancel, (X, X, X), {"reason": X}),
    ("C->B2", fix_service.BuildFixService.plan, (X, X, X), {}),
    ("C->B2", fix_service.BuildFixService.start, (X, X, X), {"plan": X}),
    ("C->B2", fix_service.BuildFixService.result, (X, X, X), {"wait_seconds": X}),
    ("C->B2", fix_service.BuildFixService.cancel, (X, X, X), {}),
    ("C->B2", fix_service.BuildFixService.restore, (X, X, X), {"files": X}),
    # C (grant registry) -> B2 book
    ("C->B2", grants.BuildFixGrantBook.approve, (X, X, X), {}),
    ("C->B2", grants.BuildFixGrantBook.withdraw, (X, X, X), {}),
    ("C->B2", grants.BuildFixGrantBook.authorize, (X, X),
     {"principal_id": X, "tool_name": X, "arguments": X}),
    ("C->B2", grants.match_child_build, (X,),
     {"principal_id": X, "template_id": X, "build_dir": X, "target": X, "config": X,
      "platform": X, "world": X, "network": X, "now": X}),
    # B2 -> B1
    ("B2->B1", run_service.BuildJobService.plan, (X, X, X), {"lease": X}),
    ("B2->B1", run_service.BuildJobService.start, (X, X, X),
     {"plan": X, "lease": X, "parent_job_id": X}),
    ("B2->B1", run_service.BuildJobService.reserve, (X, X, X, X), {}),
    ("B2->B1", run_service.BuildJobService.release, (X, X), {}),
    ("B2->B1", model_service.BuildModelService.model, (X, X, X), {}),
    ("B2->B1", model_service.BuildModelService.cached_model, (X, X, X, X), {}),
    # B2 -> B2 adapters and grant book
    ("B2->B2", grants.BuildFixGrantBook.issue, (X, X),
     {"principal_id": X, "job_id": X, "plan_digest": X}),
    ("B2->B2", grants.BuildFixGrantBook.revoke, (X, X), {}),
    ("B2->B2", source_editor.GatewaySourceEditor.read, (X, X, X), {}),
    ("B2->B2", source_editor.GatewaySourceEditor.replace, (X, X, X), {"expected_sha256": X, "ctx": X}),
    ("B2->B2", candidates.ModelCandidateGenerator.propose, (X, X, X), {"route_hint": X}),
    ("B2->B2", strategy_bridge.StrategyFixAdapter.begin, (X, X, X),
     {"attempts": X, "max_model_calls": X, "wall_seconds": X}),
    ("B2->B2", strategy_bridge.StrategyFixAdapter.observe, (X, X),
     {"before": X, "after": X, "failure": X, "hypothesis_digest": X, "focus": X,
      "model_calls": X, "verifier_calls": X}),
    ("B2->B2", preimages.FilePreimageStore.begin, (X, X, X), {}),
    ("B2->B2", preimages.FilePreimageStore.save, (X, X, X, X, X), {}),
    ("B2->B2", preimages.FilePreimageStore.record_write, (X, X, X, X), {}),
    ("B2->B2", preimages.FilePreimageStore.load, (X, X, X), {}),
    ("B2->B2", preimages.FilePreimageStore.set_status, (X, X, X), {}),
    ("B2->D", clangd.ClangdNavigator.context_for, (X, X, X, X), {"max_items": X}),
    # B2 -> runtime ports (job registry, cancellation tree)
    ("B2->jobs", SQLiteDurableJobRegistry.start, (X, X), {"max_attempts": X, "metadata": X}),
    ("B2->jobs", SQLiteDurableJobRegistry.transition, (X, X, X), {"result": X, "error": X}),
    ("B2->jobs", SQLiteDurableJobRegistry.poll, (X, X), {}),
    ("B2->jobs", SQLiteDurableJobRegistry.request_cancellation, (X, X), {"reason": X}),
    ("B2->tree", CancellationTree.create_child, (X,), {"node_id": X}),
    ("B2->tree", CancellationTree.cancel, (X, X), {"reason": X}),
    ("B2->tree", CancellationTree.discard, (X, X), {}),
    # B2 -> A
    ("B2->A", repair.EditScope, (),
     {"roots": X, "globs": X, "excluded_rel": X, "generated_rel": X, "excluded_dirs": X}),
    ("B2->A", tool_targets.classify_targets, (X,), {}),
]


@pytest.mark.parametrize("seam, callee, args, kwargs", SEAMS,
                         ids=["%s:%s" % (seam[0], getattr(seam[1], "__qualname__", seam[1]))
                              for seam in SEAMS])
def test_call_site_binds_to_the_real_callee(seam, callee, args, kwargs):
    inspect.signature(callee).bind(*args, **kwargs)


def test_the_fix_service_calls_the_navigator_factory_as_composed(tmp_path):
    """B2 calls ``navigator_factory(model, ctx)``; C's clangd factory must accept it."""
    from types import SimpleNamespace

    class Inventory:
        def lookup(self, name):
            return SimpleNamespace(path="/usr/bin/clangd") if name == "clangd" else None

    factory = build_tools.clangd_navigator_factory(SimpleNamespace(clangd_config=False),
                                                   Inventory())
    model = SimpleNamespace(source_root=str(tmp_path), build_dir=str(tmp_path / "build"))
    navigator = factory(model, None)
    assert navigator is not None and navigator.session.pid is None, "nothing is launched"
    navigator.close()
    assert factory(None, None) is None, "no model, no project root: no navigator"


def test_the_executor_starts_the_fix_with_the_approved_plan():
    """C's executor passes the claimed plan by the keyword B2's start() takes."""
    source = inspect.getsource(executor.BuildToolExecutor._build_fix)
    assert "fix.start(request, context, plan=plan)" in source
    assert "plan" in inspect.signature(fix_service.BuildFixService.start).parameters


SERVICE_DOUBLES = [
    ("FakeFix", fix_service.BuildFixService, ("plan", "start", "result", "cancel", "restore")),
    ("FakeJobs", run_service.BuildJobService, ("plan", "run", "result", "cancel")),
    ("FakeModels", model_service.BuildModelService, ("view", "cached_summary")),
]


def _shape(function) -> list[tuple[str, str]]:
    return [(name, str(parameter.kind)) for name, parameter in
            inspect.signature(function).parameters.items()
            if parameter.kind not in (parameter.VAR_KEYWORD, parameter.VAR_POSITIONAL)]


@pytest.mark.parametrize("double, real, methods", SERVICE_DOUBLES,
                         ids=[item[0] for item in SERVICE_DOUBLES])
def test_service_doubles_match_the_real_signatures(double, real, methods):
    import tests.test_build_executor as doubles

    fake = getattr(doubles, double)
    for method in methods:
        fake_shape = _shape(getattr(fake, method))
        real_shape = _shape(getattr(real, method))
        # A double may accept less (unused keywords), never a name or kind the
        # real service lacks.
        assert set(fake_shape) <= set(real_shape), (double, method, fake_shape, real_shape)
        required = [name for name, parameter in inspect.signature(getattr(real, method)).parameters
                    .items() if parameter.default is parameter.empty
                    and parameter.kind not in (parameter.VAR_KEYWORD, parameter.VAR_POSITIONAL)]
        assert [name for name, _ in fake_shape][:len(required)] == required, (double, method)
