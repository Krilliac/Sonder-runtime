"""Pure admission policy: lanes, num_ctx resolution, host memory, queue order."""
import dataclasses

import pytest

from sonder_runtime.domain import gpu_admission as ga

GIB = 1024 ** 3
POLICY = ga.GpuAdmissionPolicy(
    min_available_ram_bytes=4 * GIB, min_commit_headroom_bytes=4 * GIB,
    on_host_floor=ga.HostFloorMode.REFUSE,
)


@pytest.mark.parametrize("path,payload,method,prewarm,lane", [
    ("/api/show", {"model": "m"}, "POST", False, ga.AdmissionLane.PROBE),
    ("/api/ps", None, "GET", False, ga.AdmissionLane.PROBE),
    ("/api/tags", None, "GET", False, ga.AdmissionLane.PROBE),
    ("/api/version", None, "GET", False, ga.AdmissionLane.PROBE),
    ("/api/tags?x=1", None, "GET", False, ga.AdmissionLane.PROBE),
    ("/api/generate", {"model": "m", "keep_alive": 0}, "POST", False, ga.AdmissionLane.UNLOAD),
    ("/api/generate", {"model": "m", "keep_alive": "0"}, "POST", False, ga.AdmissionLane.UNLOAD),
    ("/api/generate", {"model": "m", "keep_alive": "0s"}, "POST", False, ga.AdmissionLane.UNLOAD),
    ("/api/chat", {"model": "m", "messages": [], "keep_alive": 0}, "POST", False, ga.AdmissionLane.UNLOAD),
    ("/api/generate", {"model": "m"}, "POST", False, ga.AdmissionLane.LOAD),
    ("/api/generate", {"model": "m", "keep_alive": "5m"}, "POST", False, ga.AdmissionLane.LOAD),
    ("/api/chat", {"model": "m", "messages": []}, "POST", False, ga.AdmissionLane.LOAD),
    ("/api/chat", {"model": "m", "messages": [{"role": "user", "content": "x"}]}, "POST", True,
     ga.AdmissionLane.LOAD),
    ("/api/chat", {"model": "m", "messages": [{"role": "user", "content": "x"}]}, "POST", False,
     ga.AdmissionLane.INFERENCE),
    ("/api/generate", {"model": "m", "prompt": "hi"}, "POST", False, ga.AdmissionLane.INFERENCE),
    ("/api/generate", {"model": "m", "prompt": "", "images": ["aa"]}, "POST", False,
     ga.AdmissionLane.INFERENCE),
    ("/api/embeddings", {"model": "m", "prompt": "x"}, "POST", False, ga.AdmissionLane.EMBED),
    ("/api/embed", {"model": "m", "input": "x"}, "POST", False, ga.AdmissionLane.EMBED),
    ("/api/embeddings", {"model": "m", "keep_alive": 0}, "POST", False, ga.AdmissionLane.UNLOAD),
    ("/api/pull", {"name": "m"}, "POST", False, ga.AdmissionLane.PROBE),
])
def test_lane_classification(path, payload, method, prewarm, lane):
    assert ga.classify_operation(path, payload, method=method, prewarm=prewarm) is lane


def test_keep_alive_zero_with_content_still_runs_inference():
    payload = {"model": "m", "keep_alive": 0, "messages": [{"role": "user", "content": "x"}]}
    assert ga.classify_operation("/api/chat", payload) is ga.AdmissionLane.INFERENCE


@pytest.mark.parametrize("value,zero", [
    (0, True), (0.0, True), ("0", True), ("0s", True), ("0m", True), ("00", True),
    (False, False), ("5m", False), (-1, False), ("-1", False), (None, False), ("", False),
])
def test_zero_keep_alive_forms(value, zero):
    assert ga.is_zero_keep_alive(value) is zero


def _resolve(**kwargs):
    base = dict(requested=2048, current=16384, policy=ga.CtxPolicy.SERVE_PINNED,
                holders=0, prompt_tokens=100, reserve_tokens=0,
                repins_in_window=0, max_repins=2)
    base.update(kwargs)
    return ga.resolve_num_ctx(**base)


def test_unloaded_model_pins_the_requested_window():
    decision = _resolve(current=None)
    assert (decision.action, decision.num_ctx) == (ga.CtxAction.PIN_NEW, 2048)


def test_unloaded_model_with_no_requested_window_serves_as_asked():
    decision = _resolve(current=None, requested=None)
    assert (decision.action, decision.num_ctx) == (ga.CtxAction.SERVE_AS_REQUESTED, None)


def test_equal_window_is_served_unchanged():
    decision = _resolve(requested=16384)
    assert (decision.action, decision.num_ctx) == (ga.CtxAction.SERVE_AS_REQUESTED, 16384)


def test_request_without_a_window_inherits_the_pin():
    decision = _resolve(requested=None)
    assert (decision.action, decision.num_ctx) == (ga.CtxAction.INHERIT_PINNED, 16384)


def test_serve_pinned_rewrites_to_the_pin_when_the_prompt_fits():
    decision = _resolve()
    assert (decision.action, decision.num_ctx) == (ga.CtxAction.SERVE_PINNED, 16384)


def test_serve_pinned_reserves_num_predict():
    decision = _resolve(current=2048, requested=16384, prompt_tokens=1500, reserve_tokens=1000)
    assert decision.action is ga.CtxAction.REPIN


@pytest.mark.parametrize("policy", [ga.CtxPolicy.WAIT_UNLOAD, ga.CtxPolicy.EXACT])
def test_wait_and_exact_never_serve_a_different_window(policy):
    decision = _resolve(policy=policy)
    assert decision.action is ga.CtxAction.WAIT
    assert decision.num_ctx is None
    assert decision.reason is ga.RefusalReason.CTX_PIN_CONFLICT


def test_prompt_that_does_not_fit_repins_only_with_no_holders():
    big = dict(current=2048, requested=16384, prompt_tokens=5000)
    assert _resolve(**big).action is ga.CtxAction.REPIN
    held = _resolve(holders=1, **big)
    assert held.action is ga.CtxAction.REFUSE
    assert held.reason is ga.RefusalReason.PROMPT_EXCEEDS_PINNED_CTX


def test_repin_budget_is_enforced():
    big = dict(current=2048, requested=16384, prompt_tokens=5000, max_repins=2)
    assert _resolve(repins_in_window=1, **big).action is ga.CtxAction.REPIN
    exhausted = _resolve(repins_in_window=2, **big)
    assert exhausted.action is ga.CtxAction.REFUSE
    assert exhausted.reason is ga.RefusalReason.PROMPT_EXCEEDS_PINNED_CTX
    assert _resolve(max_repins=0, **{k: v for k, v in big.items() if k != "max_repins"}
                    ).action is ga.CtxAction.REFUSE


def test_prompt_token_estimate_counts_messages_prompt_and_tools():
    assert ga.estimate_prompt_tokens(None) == 0
    assert ga.estimate_prompt_tokens({"prompt": "x" * 350}) == 100
    messages = {"messages": [{"role": "user", "content": "a" * 70}, {"role": "system", "content": "b" * 35}]}
    assert ga.estimate_prompt_tokens(messages) == 30
    assert ga.estimate_prompt_tokens({"tools": [{"name": "t"}]}) > 0


def test_requested_window_and_reserve_extraction():
    assert ga.requested_num_ctx({"options": {"num_ctx": 4096}}) == 4096
    for bad in ({}, {"options": {}}, {"options": {"num_ctx": 0}}, {"options": {"num_ctx": True}},
                {"options": {"num_ctx": "4096"}}, None):
        assert ga.requested_num_ctx(bad) is None
    assert ga.requested_reserve_tokens({"options": {"num_predict": 256}}) == 256
    assert ga.requested_reserve_tokens({"options": {"num_predict": -1}}) == 0


def test_estimate_scales_file_size_and_adds_overhead():
    estimate = ga.estimate_host_footprint(10 * GIB, factor_pct=125, overhead_bytes=1 * GIB)
    assert estimate.working_set_bytes == int(10 * GIB * 1.25) + GIB
    assert estimate.source is ga.FootprintSource.ESTIMATED


def test_measured_beats_other_window_beats_estimate():
    exact = ga.Footprint(17 * GIB, 17 * GIB, ga.FootprintSource.MEASURED, "d", 16384)
    near = ga.Footprint(15 * GIB, 15 * GIB, ga.FootprintSource.MEASURED, "d", 8192)
    far = ga.Footprint(13 * GIB, 13 * GIB, ga.FootprintSource.MEASURED, "d", 2048)
    kwargs = dict(file_size_bytes=9 * GIB, requested_ctx=16384, policy=POLICY)
    assert ga.select_footprint(measured_exact=exact, measured_other=[near], **kwargs) is exact
    assert ga.select_footprint(measured_exact=None, measured_other=[far, near], **kwargs) is near
    estimated = ga.select_footprint(measured_exact=None, measured_other=[], **kwargs)
    assert estimated.source is ga.FootprintSource.ESTIMATED


def _reading(avail=12, commit=10, enforced=True):
    return ga.HostMemoryReading(avail * GIB, 32 * GIB, 64 * GIB, commit * GIB, enforced)


def _footprint(gib=17):
    return ga.Footprint(gib * GIB, gib * GIB, ga.FootprintSource.MEASURED)


def test_seventeen_gb_footprint_refuses_on_ram_with_the_evidence():
    verdict = ga.host_memory_verdict(_reading(), _footprint(), policy=POLICY,
                                     evictable=("other:9b (11.0 GiB)",))
    assert isinstance(verdict, ga.Refused)
    assert verdict.reason is ga.RefusalReason.HOST_RAM_FLOOR
    assert verdict.reading == _reading()
    assert verdict.footprint == _footprint()
    assert verdict.evictable == ("other:9b (11.0 GiB)",)


def test_commit_floor_is_reported_when_ram_fits():
    verdict = ga.host_memory_verdict(_reading(avail=40, commit=10), _footprint(), policy=POLICY)
    assert isinstance(verdict, ga.Refused)
    assert verdict.reason is ga.RefusalReason.COMMIT_FLOOR


def test_unenforced_commit_is_ignored():
    verdict = ga.host_memory_verdict(_reading(avail=40, commit=0, enforced=False), _footprint(),
                                     policy=POLICY)
    assert isinstance(verdict, ga.Admitted)


def test_a_fitting_footprint_is_admitted():
    verdict = ga.host_memory_verdict(_reading(avail=40, commit=40), _footprint(), policy=POLICY)
    assert isinstance(verdict, ga.Admitted)


def test_queue_mode_returns_queued_not_refused():
    policy = dataclasses.replace(POLICY, on_host_floor=ga.HostFloorMode.QUEUE)
    verdict = ga.host_memory_verdict(_reading(), _footprint(), policy=policy, retry_after_seconds=2.5)
    assert isinstance(verdict, ga.Queued)
    assert verdict.reason is ga.RefusalReason.HOST_RAM_FLOOR
    assert verdict.retry_after_seconds == 2.5


def test_floor_is_inclusive_at_exactly_the_floor():
    verdict = ga.host_memory_verdict(_reading(avail=21, commit=21), _footprint(), policy=POLICY)
    assert isinstance(verdict, ga.Admitted)


def test_queue_order_serves_interactive_first_then_fifo():
    entries = [
        ga.QueueEntry("bg-1", ga.priority_rank("agent"), 1),
        ga.QueueEntry("ui-2", ga.priority_rank("interactive"), 2),
        ga.QueueEntry("bg-0", ga.priority_rank("autopilot"), 0),
        ga.QueueEntry("ui-3", ga.priority_rank("Interactive"), 3),
    ]
    assert [e.ticket_id for e in ga.queue_order(entries)] == ["ui-2", "ui-3", "bg-0", "bg-1"]


def test_effective_window_check():
    assert ga.check_effective_ctx(16384, 16384) is ga.CtxCheck.MATCH
    assert ga.check_effective_ctx(16384, 8192) is ga.CtxCheck.MISMATCH
    assert ga.check_effective_ctx(16384, None) is ga.CtxCheck.UNVERIFIABLE
    assert ga.check_effective_ctx(None, 8192) is ga.CtxCheck.UNVERIFIABLE


def test_types_are_frozen_and_the_policy_is_validated():
    with pytest.raises(dataclasses.FrozenInstanceError):
        _reading().available_physical_bytes = 1
    with pytest.raises(ValueError):
        ga.GpuAdmissionPolicy(min_available_ram_bytes=-1)
    with pytest.raises(ValueError):
        ga.GpuAdmissionPolicy(ctx_conflict=ga.CtxPolicy.EXACT)
    with pytest.raises(ValueError):
        ga.GpuAdmissionPolicy(unmeasured_host_factor_pct=0)
