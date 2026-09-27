"""Issue #510 section 3: bounded productive parallelism.

Pure-policy tests for ``sonder_runtime.domain.adaptive_concurrency`` plus
guard canaries that deliberately trip every shrink trigger (retry storm,
churn, resource pressure) and the ownership-serialization path, both in the
pure policy and through the live ``master_orchestrator`` dispatch seam.
"""
from __future__ import annotations

import threading

import pytest

import master_orchestrator
from sonder_runtime.domain import adaptive_concurrency as ac
from sonder_runtime.domain.adaptive_concurrency import (
    ConcurrencyPolicy,
    LaneAccess,
    LaneClaim,
    Outcome,
    ResourceSnapshot,
)


def setup_function():
    master_orchestrator.reset_for_tests()


def _run(policy, outcomes, state=None, resources=None):
    state = state or ac.initial_state(policy)
    decisions = []
    for outcome in outcomes:
        state, decision = ac.observe(state, outcome, policy, resources)
        decisions.append(decision)
    return state, decisions


# --- ownership --------------------------------------------------------------


def test_readers_never_conflict_even_on_the_same_file():
    left = LaneClaim("a", frozenset({"src/app.py"}), LaneAccess.READ)
    right = LaneClaim("b", frozenset({"src/app.py"}), LaneAccess.READ)

    assert not ac.claims_conflict(left, right)


def test_writer_conflicts_with_overlapping_reader_or_writer():
    writer = LaneClaim("w", frozenset({"src"}), LaneAccess.WRITE)
    reader = LaneClaim("r", frozenset({"src/app.py"}), LaneAccess.READ)
    other_writer = LaneClaim("x", frozenset({"src/app.py"}), LaneAccess.WRITE)

    assert ac.claims_conflict(writer, reader)
    assert ac.claims_conflict(reader, writer)
    assert ac.claims_conflict(writer, other_writer)


def test_path_overlap_is_component_wise_not_string_prefix():
    left = LaneClaim("a", frozenset({"src/app"}), LaneAccess.WRITE)
    right = LaneClaim("b", frozenset({"src/application.py"}), LaneAccess.WRITE)

    assert not ac.claims_conflict(left, right)


def test_path_normalization_folds_case_and_separators():
    left = LaneClaim("a", frozenset({"Src\\App.py"}), LaneAccess.WRITE)
    right = LaneClaim("b", frozenset({"./src/app.py"}), LaneAccess.WRITE)

    assert ac.claims_conflict(left, right)


def test_root_claim_covers_everything_and_empty_claim_covers_nothing():
    root = LaneClaim("root", frozenset({"."}), LaneAccess.WRITE)
    deep = LaneClaim("deep", frozenset({"a/b/c.py"}), LaneAccess.READ)
    unscoped = LaneClaim("free", frozenset(), LaneAccess.WRITE)

    assert ac.claims_conflict(root, deep)
    assert not ac.claims_conflict(root, unscoped)


@pytest.mark.parametrize("bad", ["", "   ", "../outside.py", "a/../../b"])
def test_claims_reject_empty_or_escaping_paths(bad):
    with pytest.raises(ValueError):
        LaneClaim("a", frozenset({bad}), LaneAccess.WRITE)


def test_conflict_graph_rejects_duplicate_lane_ids():
    with pytest.raises(ValueError):
        ac.conflict_graph([LaneClaim("a"), LaneClaim("a")])


def test_ownership_canary_conflicting_writers_are_serialized():
    claims = [
        LaneClaim("a", frozenset({"core/x.py"}), LaneAccess.WRITE),
        LaneClaim("b", frozenset({"core"}), LaneAccess.WRITE),
        LaneClaim("c", frozenset({"docs/readme.md"}), LaneAccess.WRITE),
    ]
    graph = ac.conflict_graph(claims)

    first = ac.admissible_lanes(["a", "b", "c"], (), 3, graph)
    # b overlaps a, so it waits; independent c is not head-of-line blocked.
    assert first == ("a", "c")
    assert ac.admissible_lanes(["b"], {"a", "c"}, 3, graph) == ()
    assert ac.admissible_lanes(["b"], {"c"}, 3, graph) == ("b",)
    assert ac.coupled_lane_count(graph) == 2


def test_admission_respects_cap_and_stable_order():
    graph = ac.conflict_graph([LaneClaim(name) for name in "abcde"])

    assert ac.admissible_lanes(list("abcde"), (), 2, graph) == ("a", "b")
    assert ac.admissible_lanes(list("cde"), {"a"}, 2, graph) == ("c",)
    with pytest.raises(ValueError):
        ac.admissible_lanes(["a"], (), 0, graph)


def test_admission_always_progresses_when_nothing_runs():
    claims = [LaneClaim(name, frozenset({"."}), LaneAccess.WRITE) for name in "abc"]
    graph = ac.conflict_graph(claims)

    assert ac.admissible_lanes(["b", "c"], (), 4, graph) == ("b",)


# --- adaptive cap -----------------------------------------------------------


def test_policy_validates_bounds():
    with pytest.raises(ValueError):
        ConcurrencyPolicy(ceiling=0)
    with pytest.raises(ValueError):
        ConcurrencyPolicy(ceiling=4, memory_pressure_enter=0.3, memory_pressure_exit=0.2)
    with pytest.raises(ValueError):
        ConcurrencyPolicy(ceiling=4, window=2, retry_storm_threshold=3)
    with pytest.raises(ValueError):
        ac.observe(ac.initial_state(ConcurrencyPolicy(ceiling=2)), "succeeded",
                   ConcurrencyPolicy(ceiling=2))


def test_initial_state_clamps_to_policy_bounds():
    policy = ConcurrencyPolicy(ceiling=8, floor=2)

    assert ac.initial_state(policy).cap == 8
    assert ac.initial_state(policy, 50).cap == 8
    assert ac.initial_state(policy, 1).cap == 2


def test_healthy_run_holds_at_ceiling():
    policy = ConcurrencyPolicy(ceiling=4)
    state, decisions = _run(policy, [Outcome.SUCCEEDED] * 10)

    assert state.cap == 4
    assert {d.action for d in decisions} == {"hold"}


def test_retry_storm_canary_shrinks_cap_multiplicatively():
    policy = ConcurrencyPolicy(ceiling=8)
    state, decisions = _run(policy, [Outcome.TRANSIENT_RETRY] * 3)

    assert [d.action for d in decisions] == ["hold", "hold", "shrink"]
    assert decisions[-1].reasons == (ac.REASON_RETRY_STORM,)
    assert decisions[-1].previous_cap == 8 and state.cap == 4


def test_isolated_retries_below_threshold_do_not_shrink():
    policy = ConcurrencyPolicy(ceiling=8)
    pattern = [Outcome.TRANSIENT_RETRY] + [Outcome.SUCCEEDED] * 7
    state, decisions = _run(policy, pattern * 3)

    assert state.cap == 8
    assert "shrink" not in {d.action for d in decisions}


def test_one_burst_is_not_counted_twice():
    policy = ConcurrencyPolicy(ceiling=8)
    state, decisions = _run(policy, [Outcome.TRANSIENT_RETRY] * 4)

    # Three retries shrink once; the fourth alone does not re-trigger.
    assert state.cap == 4
    assert [d.action for d in decisions].count("shrink") == 1


def test_churn_canary_shrinks_cap():
    policy = ConcurrencyPolicy(ceiling=6)
    state, decisions = _run(policy, [Outcome.SUCCEEDED, Outcome.CHURN, Outcome.CHURN])

    assert decisions[-1].action == "shrink"
    assert decisions[-1].reasons == (ac.REASON_CHURN,)
    assert state.cap == 3


def test_resource_pressure_canary_shrinks_with_cooldown():
    policy = ConcurrencyPolicy(ceiling=16, pressure_cooldown=2)
    tight = ResourceSnapshot(memory_available_fraction=0.05)
    state, decisions = _run(policy, [Outcome.SAMPLE] * 7, resources=tight)

    assert [d.action for d in decisions] == [
        "shrink", "hold", "hold", "shrink", "hold", "hold", "shrink",
    ]
    assert all(
        d.reasons == (ac.REASON_RESOURCE_PRESSURE,)
        for d in decisions if d.action == "shrink"
    )
    assert state.cap == 2 and state.pressured


def test_pressure_band_alone_is_a_resource_trigger():
    policy = ConcurrencyPolicy(ceiling=4)
    state, decisions = _run(
        policy, [Outcome.SAMPLE], resources=ResourceSnapshot(pressure_band="critical"),
    )

    assert decisions[0].reasons == (ac.REASON_RESOURCE_PRESSURE,)
    assert state.cap == 2


def test_pressure_hysteresis_blocks_growth_inside_the_band():
    policy = ConcurrencyPolicy(ceiling=4, recovery_streak=2)
    state, _ = _run(policy, [Outcome.SAMPLE],
                    resources=ResourceSnapshot(memory_available_fraction=0.05))
    assert state.cap == 2 and state.pressured

    # Between enter (0.10) and exit (0.20): still pressured, so no growth.
    state, decisions = _run(
        policy, [Outcome.SUCCEEDED] * 4, state=state,
        resources=ResourceSnapshot(memory_available_fraction=0.15),
    )
    assert state.pressured
    assert "grow" not in {d.action for d in decisions}

    # At/above exit the pressure releases and a healthy streak regrows.
    state, decisions = _run(
        policy, [Outcome.SUCCEEDED] * 2, state=state,
        resources=ResourceSnapshot(memory_available_fraction=0.5),
    )
    assert not state.pressured
    assert decisions[-1].action == "grow" and state.cap == 3


def test_unknown_resources_never_enter_pressure():
    policy = ConcurrencyPolicy(ceiling=4)
    state, decisions = _run(policy, [Outcome.SAMPLE] * 10, resources=ResourceSnapshot())

    assert not state.pressured and state.cap == 4
    assert {d.action for d in decisions} == {"hold"}


def test_brief_unknown_gap_keeps_latched_pressure():
    policy = ConcurrencyPolicy(ceiling=4, unknown_release_after=3)
    state, _ = _run(policy, [Outcome.SAMPLE],
                    resources=ResourceSnapshot(memory_available_fraction=0.01))
    state, _ = _run(policy, [Outcome.SUCCEEDED] * 2, state=state,
                    resources=ResourceSnapshot())
    assert state.pressured

    # A known reading resets the unknown streak.
    state, _ = _run(policy, [Outcome.SUCCEEDED], state=state,
                    resources=ResourceSnapshot(memory_available_fraction=0.15))
    state, _ = _run(policy, [Outcome.SUCCEEDED] * 2, state=state,
                    resources=ResourceSnapshot())
    assert state.pressured


def test_recovery_grows_additively_to_ceiling_and_no_further():
    policy = ConcurrencyPolicy(ceiling=4, recovery_streak=3)
    state, _ = _run(policy, [Outcome.TRANSIENT_RETRY] * 3)
    state, _ = _run(policy, [Outcome.TRANSIENT_RETRY] * 3, state=state)
    assert state.cap == 1

    state, decisions = _run(policy, [Outcome.SUCCEEDED] * 20, state=state)
    grows = [d for d in decisions if d.action == "grow"]
    assert [(d.previous_cap, d.cap) for d in grows] == [(1, 2), (2, 3), (3, 4)]
    assert state.cap == 4


def test_failure_does_not_reset_or_advance_the_healthy_streak():
    policy = ConcurrencyPolicy(ceiling=4, recovery_streak=3)
    state = ac.initial_state(policy, 2)
    state, decisions = _run(
        policy, [Outcome.SUCCEEDED, Outcome.FAILED, Outcome.SUCCEEDED, Outcome.SUCCEEDED],
        state=state,
    )

    assert decisions[-1].action == "grow" and state.cap == 3


def test_retry_resets_the_healthy_streak():
    policy = ConcurrencyPolicy(ceiling=4, recovery_streak=3)
    state = ac.initial_state(policy, 2)
    state, decisions = _run(
        policy,
        [Outcome.SUCCEEDED, Outcome.SUCCEEDED, Outcome.TRANSIENT_RETRY, Outcome.SUCCEEDED],
        state=state,
    )

    assert state.cap == 2
    assert "grow" not in {d.action for d in decisions}


def test_floor_is_never_crossed_and_trigger_still_reported():
    policy = ConcurrencyPolicy(ceiling=2, floor=1)
    state, decisions = _run(policy, [Outcome.CHURN] * 6)

    assert state.cap == 1
    assert decisions[-1].reasons == (ac.REASON_CHURN,)
    assert decisions[-1].action == "hold"


def test_observe_is_deterministic():
    policy = ConcurrencyPolicy(ceiling=8)
    sequence = [
        Outcome.SUCCEEDED, Outcome.TRANSIENT_RETRY, Outcome.CHURN,
        Outcome.TRANSIENT_RETRY, Outcome.TRANSIENT_RETRY, Outcome.CHURN,
        Outcome.SUCCEEDED, Outcome.SUCCEEDED, Outcome.SUCCEEDED,
    ]

    assert _run(policy, sequence) == _run(policy, sequence)


# --- live seam: AdaptiveLaneScheduler + dispatch_lanes ------------------------


def _scheduler(claims, ceiling, **kwargs):
    kwargs.setdefault("resources", lambda: ResourceSnapshot())
    return master_orchestrator.AdaptiveLaneScheduler(claims, ceiling, **kwargs)


def test_scheduler_retry_storm_canary_limits_next_admissions():
    claims = [LaneClaim("lane-%d" % index) for index in range(6)]
    scheduler = _scheduler(claims, 4)

    assert scheduler.admit() == ("lane-0", "lane-1", "lane-2", "lane-3")
    for lane in ("lane-0", "lane-1", "lane-2"):
        scheduler.sink(lane)(Outcome.TRANSIENT_RETRY)
    scheduler.sink("lane-0")(Outcome.SUCCEEDED)
    scheduler.complete("lane-0")

    assert scheduler.cap == 2
    # Three lanes still run above the shrunk cap: nothing new is admitted
    # until the running count falls below it.
    assert scheduler.admit() == ()
    scheduler.complete("lane-1")
    assert scheduler.admit() == ()
    scheduler.complete("lane-2")
    assert scheduler.admit() == ("lane-4",)
    summary = scheduler.summary()
    assert summary["decisions"][0]["reasons"] == [ac.REASON_RETRY_STORM]
    assert summary["decisions"][0]["previous_cap"] == 4


def test_scheduler_churn_canary():
    scheduler = _scheduler([LaneClaim("a"), LaneClaim("b"), LaneClaim("c")], 3)
    scheduler.admit()
    scheduler.sink("a")(Outcome.CHURN)
    scheduler.sink("b")(Outcome.CHURN)
    scheduler.complete("a")

    assert scheduler.cap == 1
    assert scheduler.summary()["decisions"][0]["reasons"] == [ac.REASON_CHURN]


def test_scheduler_resource_pressure_canary_samples_on_silent_completion():
    scheduler = _scheduler(
        [LaneClaim("a"), LaneClaim("b")], 4,
        resources=lambda: ResourceSnapshot(memory_available_fraction=0.02),
    )
    scheduler.admit()
    # A cancelled lane reports nothing; the completion still samples memory.
    scheduler.complete("a")

    assert scheduler.cap == 2
    assert scheduler.summary()["decisions"][0]["reasons"] == [ac.REASON_RESOURCE_PRESSURE]


def test_scheduler_resource_probe_failure_is_unknown_not_pressure():
    def broken():
        raise OSError("probe failed")

    scheduler = _scheduler([LaneClaim("a")], 4, resources=broken)
    scheduler.admit()
    scheduler.sink("a")(Outcome.SUCCEEDED)
    scheduler.complete("a")

    assert scheduler.cap == 4 and scheduler.summary()["decisions"] == []


def test_scheduler_disabled_pins_cap():
    scheduler = _scheduler([LaneClaim("a"), LaneClaim("b")], 4, adaptive=False)
    scheduler.admit()
    for _ in range(5):
        scheduler.sink("a")(Outcome.TRANSIENT_RETRY)
    scheduler.complete("a")

    assert scheduler.cap == 4 and scheduler.summary()["decisions"] == []


def test_dispatch_lanes_serializes_conflicting_writers_with_real_threads():
    claims = [
        LaneClaim("w1", frozenset({"pkg/mod.py"}), LaneAccess.WRITE),
        LaneClaim("w2", frozenset({"pkg"}), LaneAccess.WRITE),
        LaneClaim("free-1", frozenset({"other/a.py"}), LaneAccess.WRITE),
        LaneClaim("free-2", frozenset({"other/b.py"}), LaneAccess.WRITE),
    ]
    scheduler = _scheduler(claims, 4)
    lock = threading.Lock()
    active: set[str] = set()
    overlaps = []
    peak = {"value": 0}
    independent_started = threading.Barrier(3, timeout=5)
    results = {}

    def run_lane(lane_id, sink):
        with lock:
            if {"w1", "w2"} <= active | {lane_id}:
                overlaps.append(lane_id)
            active.add(lane_id)
            peak["value"] = max(peak["value"], len(active))
        try:
            if lane_id != "w2":
                # w1 and both free lanes must be live together: proves the
                # conflict did not throttle independent work.
                independent_started.wait()
            sink(Outcome.SUCCEEDED)
            return lane_id.upper()
        finally:
            with lock:
                active.discard(lane_id)

    master_orchestrator.dispatch_lanes(
        scheduler, 4, run_lane,
        lambda lane, result: results.__setitem__(lane, result),
        lambda lane, exc: results.__setitem__(lane, exc),
    )

    assert results == {"w1": "W1", "w2": "W2", "free-1": "FREE-1", "free-2": "FREE-2"}
    assert overlaps == []
    assert peak["value"] == 3
    summary = scheduler.summary()
    assert summary["coupled_lanes"] == 2
    assert summary["serialized_waits"] >= 1


def test_dispatch_lanes_reports_worker_exceptions_as_failures():
    scheduler = _scheduler([LaneClaim("a"), LaneClaim("b")], 2)
    errors = {}

    def run_lane(lane_id, _sink):
        if lane_id == "a":
            raise RuntimeError("boom")
        return "ok"

    collected = {}
    master_orchestrator.dispatch_lanes(
        scheduler, 2, run_lane,
        lambda lane, result: collected.__setitem__(lane, result),
        lambda lane, exc: errors.__setitem__(lane, str(exc)),
    )

    assert errors == {"a": "boom"} and collected == {"b": "ok"}
    assert scheduler.running == frozenset() and scheduler.pending == ()


def test_dispatch_lanes_surfaces_stalled_lane_without_waiting_for_executor_shutdown(
    monkeypatch, caplog,
):
    monkeypatch.setenv("SONDER_FLEET_PROGRESS_DEADLINE_SECONDS", "0.1")
    scheduler = _scheduler([LaneClaim("hung")], 1)
    release = threading.Event()
    errors = {}

    def run_lane(_lane_id, _sink):
        release.wait(5)
        return "late"

    stalled = []
    with caplog.at_level("WARNING"), pytest.raises(master_orchestrator.FleetStalledError):
        master_orchestrator.dispatch_lanes(
            scheduler, 1, run_lane, lambda _lane, _result: None,
            lambda lane, exc: errors.__setitem__(lane, str(exc)),
            on_stall=stalled.extend,
        )
    release.set()

    assert stalled == ["hung"]
    assert not errors
    assert scheduler.running == frozenset({"hung"})
    assert "fleet lanes stalled" in caplog.text and "hung" in caplog.text


def test_run_delegated_stall_is_uncertain_and_retains_child_capacity(monkeypatch):
    monkeypatch.setenv("SONDER_FLEET_PROGRESS_DEADLINE_SECONDS", "0.1")
    # The worker hangs inside its model call, and a lane in a model call is
    # live until the call's own timeout plus a margin (finding 39).  Shrink
    # both so the hang outlives them quickly.
    import sonder_runtime.adapters.persistence.fleet_store as fleet_store

    monkeypatch.setenv("SONDER_TIMEOUT", "1")
    monkeypatch.setattr(fleet_store, "MODEL_CALL_PROGRESS_MARGIN_SECONDS", 0)
    monkeypatch.setattr(master_orchestrator, "parallel_worker_slots", lambda requested: 1)
    release = threading.Event()
    entered = threading.Event()
    audited = threading.Event()
    result_box = {}

    def worker(_prompt):
        entered.set()
        release.wait(5)
        return "late"

    def audit(_prompt):
        audited.set()
        return "false success"

    thread = threading.Thread(
        target=lambda: result_box.setdefault(
            "result", master_orchestrator.run_delegated(
                "fan out", worker_fn=worker, audit_fn=audit, agents=1,
            ),
        ),
    )
    thread.start()
    try:
        assert entered.wait(5)
        thread.join(5)
        assert not thread.is_alive()
        result = result_box["result"]
        assert result["stalled"] is True
        assert result["uncertain"] is True
        assert result["output"].startswith("STALLED:")
        assert not audited.is_set()
        assert master_orchestrator.reserved_slot_count() == 1
    finally:
        release.set()
    for _ in range(50):
        if master_orchestrator.reserved_slot_count() == 0:
            break
        threading.Event().wait(0.1)
    assert master_orchestrator.reserved_slot_count() == 0


def test_delegated_lane_claims_keep_read_fanout_unserialized():
    objective = type("Objective", (), {"path": "src/app.py"})()
    claims = master_orchestrator.delegated_lane_claims(
        ["a", "b", "c"], ((objective,), (objective,), (objective,)),
    )
    graph = ac.conflict_graph(claims)

    assert all(claim.access is LaneAccess.READ for claim in claims)
    assert ac.admissible_lanes(["a", "b", "c"], (), 3, graph) == ("a", "b", "c")


def test_concurrency_resources_reports_unknown_memory_as_unknown(monkeypatch):
    monkeypatch.setattr(master_orchestrator, "physical_memory_bytes", lambda: (0, 0))
    assert master_orchestrator.concurrency_resources() == ResourceSnapshot()

    monkeypatch.setattr(master_orchestrator, "physical_memory_bytes", lambda: (100, 25))
    assert master_orchestrator.concurrency_resources().memory_available_fraction == 0.25


@pytest.mark.parametrize("value, expected", [
    ("", True), ("1", True), ("0", False), ("off", False), ("FALSE", False),
])
def test_adaptive_switch(monkeypatch, value, expected):
    monkeypatch.setenv("SONDER_FLEET_ADAPTIVE_CONCURRENCY", value)
    assert master_orchestrator.adaptive_concurrency_enabled() is expected


# --- live seam: run_delegated -----------------------------------------------


def _storm_worker(storm_width):
    """Each of the first ``storm_width`` lanes times out once, then waits
    until every one of them has retried before succeeding -- so the first
    completion deterministically folds a full retry storm."""
    lock = threading.Lock()
    attempts: dict[str, int] = {}
    retried = threading.Event()
    state = {"retries": 0}

    def worker(prompt):
        with lock:
            attempts[prompt] = attempts.get(prompt, 0) + 1
            attempt = attempts[prompt]
            lane_index = len([p for p in attempts if attempts[p] >= 1])
        if attempt == 1 and lane_index <= storm_width:
            with lock:
                state["retries"] += 1
                if state["retries"] >= storm_width:
                    retried.set()
            raise TimeoutError("model call timed out")
        if attempt == 2:
            assert retried.wait(5)
        return "ok"

    return worker


def test_run_delegated_retry_storm_canary_shrinks_live_cap(monkeypatch):
    monkeypatch.setattr(master_orchestrator, "parallel_worker_slots", lambda requested: 4)
    monkeypatch.setenv("SONDER_FLEET_TRANSIENT_RETRIES", "1")
    monkeypatch.setattr(master_orchestrator, "physical_memory_bytes", lambda: (100, 80))

    result = master_orchestrator.run_delegated(
        "fan out", worker_fn=_storm_worker(4), audit_fn=lambda prompt: "merged", agents=6,
    )

    assert result["output"] == "merged"
    report = result["concurrency"]
    shrinks = [d for d in report["decisions"] if d["action"] == "shrink"]
    assert shrinks and shrinks[0]["reasons"] == [ac.REASON_RETRY_STORM]
    assert (shrinks[0]["previous_cap"], shrinks[0]["cap"]) == (4, 2)
    assert report["ceiling"] == 4 and report["peak_running"] <= 4
    events = master_orchestrator.snapshot()["events"]
    assert any("adaptive concurrency shrink: cap 4 -> 2 (retry_storm)" in str(event)
               for event in events)


def test_run_delegated_resource_pressure_canary(monkeypatch):
    monkeypatch.setattr(master_orchestrator, "parallel_worker_slots", lambda requested: 4)
    monkeypatch.setattr(master_orchestrator, "physical_memory_bytes", lambda: (100, 3))

    result = master_orchestrator.run_delegated(
        "fan out", worker_fn=lambda prompt: "ok", audit_fn=lambda prompt: "merged", agents=4,
    )

    assert result["output"] == "merged"
    first = result["concurrency"]["decisions"][0]
    assert first["action"] == "shrink"
    assert first["reasons"] == [ac.REASON_RESOURCE_PRESSURE]


def test_run_delegated_adaptive_switch_off_keeps_static_cap(monkeypatch):
    monkeypatch.setattr(master_orchestrator, "parallel_worker_slots", lambda requested: 4)
    monkeypatch.setattr(master_orchestrator, "physical_memory_bytes", lambda: (100, 3))
    monkeypatch.setenv("SONDER_FLEET_ADAPTIVE_CONCURRENCY", "0")

    result = master_orchestrator.run_delegated(
        "fan out", worker_fn=lambda prompt: "ok", audit_fn=lambda prompt: "merged", agents=4,
    )

    assert result["concurrency"]["adaptive"] is False
    assert result["concurrency"]["decisions"] == []
    assert result["concurrency"]["final_cap"] == 4


def test_run_delegated_healthy_fanout_keeps_full_width(monkeypatch):
    monkeypatch.setattr(master_orchestrator, "parallel_worker_slots", lambda requested: 3)
    monkeypatch.setattr(master_orchestrator, "physical_memory_bytes", lambda: (100, 80))
    barrier = threading.Barrier(3, timeout=5)

    def worker(prompt):
        barrier.wait()  # all three independent lanes must be live at once
        return "ok"

    result = master_orchestrator.run_delegated(
        "fan out", worker_fn=worker, audit_fn=lambda prompt: "merged", agents=3,
    )

    assert result["output"] == "merged"
    assert result["concurrency"]["peak_running"] == 3
    assert result["concurrency"]["decisions"] == []


# --- review fixes (PR #552): each test failed before its fix ---------------


def test_p2_1_unknown_probe_releases_latched_pressure_and_regrows():
    policy = ConcurrencyPolicy(ceiling=8)
    state, _ = _run(policy, [Outcome.SAMPLE],
                    resources=ResourceSnapshot(memory_available_fraction=0.01))
    assert state.cap == 4 and state.pressured

    # The probe then goes dark: every reading is unknown.  Latched pressure
    # must not pin the cap forever.
    state, decisions = _run(policy, [Outcome.SUCCEEDED] * 39, state=state,
                            resources=ResourceSnapshot())
    assert not state.pressured
    assert state.cap == 8
    assert "grow" in {d.action for d in decisions}


def test_p2_1_unsampled_observations_do_not_count_as_unknown_readings():
    policy = ConcurrencyPolicy(ceiling=8)
    state, _ = _run(policy, [Outcome.SAMPLE],
                    resources=ResourceSnapshot(memory_available_fraction=0.01))
    state, _ = _run(policy, [Outcome.SUCCEEDED] * 20, state=state, resources=None)

    assert state.pressured and state.cap == 4


def test_p2_1_scheduler_with_dark_probe_recovers():
    readings = iter(
        [ResourceSnapshot(memory_available_fraction=0.01)] + [ResourceSnapshot()] * 60
    )
    claims = [LaneClaim("lane-%d" % index) for index in range(40)]
    scheduler = _scheduler(claims, 8, resources=lambda: next(readings))
    for _ in range(40):
        scheduler.admit()
        lane = sorted(scheduler.running)[0]
        scheduler.sink(lane)(Outcome.SUCCEEDED)
        scheduler.complete(lane)

    assert scheduler.cap == 8


def test_p2_2_dispatch_survives_raising_error_handler_and_runs_every_lane():
    scheduler = _scheduler([LaneClaim(name) for name in ("a", "b", "c", "d")], 1)
    ran = []

    def run_lane(lane_id, _sink):
        ran.append(lane_id)
        if lane_id == "a":
            raise RuntimeError("worker boom")
        return lane_id

    def on_error(_lane, _exc):
        raise RuntimeError("fleet store down")

    collected = []
    master_orchestrator.dispatch_lanes(
        scheduler, 1, run_lane, lambda lane, result: collected.append(lane), on_error,
        on_abandon=lambda lane: None,
    )

    assert ran == ["a", "b", "c", "d"]
    assert collected == ["b", "c", "d"]


def test_p2_2_dispatch_abandons_never_started_lanes_when_loop_aborts():
    scheduler = _scheduler([LaneClaim(name) for name in ("a", "b", "c")], 1)
    original_admit = scheduler.admit
    calls = {"n": 0}

    def flaky_admit():
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("scheduler broke")
        return original_admit()

    scheduler.admit = flaky_admit
    abandoned = []
    with pytest.raises(RuntimeError, match="scheduler broke"):
        master_orchestrator.dispatch_lanes(
            scheduler, 1, lambda lane, sink: lane, lambda lane, result: None,
            lambda lane, exc: None, on_abandon=abandoned.append,
        )

    assert abandoned == ["b", "c"]


def test_p2_2_run_delegated_does_not_strand_lanes_when_finish_fails(monkeypatch):
    monkeypatch.setattr(master_orchestrator, "parallel_worker_slots", lambda requested: 1)
    real_finish = master_orchestrator._finish
    doomed = {}

    def worker(prompt):
        if "subagent 1/" in prompt:
            raise ValueError("permanent worker failure")
        return "ok"

    def finish(agent_id, *args, **kwargs):
        if kwargs.get("error") and not doomed:
            doomed["id"] = agent_id
        if doomed.get("id") == agent_id:
            raise OSError("fleet store down")
        return real_finish(agent_id, *args, **kwargs)

    monkeypatch.setattr(master_orchestrator, "_finish", finish)
    result = master_orchestrator.run_delegated(
        "fan out", worker_fn=worker, audit_fn=lambda prompt: "merged", agents=3,
    )

    assert result["output"] == "merged"
    rows = master_orchestrator.snapshot(limit=20)["agents"]
    children = [row for row in rows if row["role"] == "agent" and row["id"] != doomed["id"]]
    assert len(children) == 2
    assert {row["status"] for row in children} == {"done"}


def test_p2_3_linux_memory_uses_memavailable(tmp_path, monkeypatch):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        "MemTotal:       16000000 kB\n"
        "MemFree:          500000 kB\n"
        "MemAvailable:    9000000 kB\n"
        "Cached:          8000000 kB\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(master_orchestrator, "_PROC_MEMINFO", str(meminfo))
    monkeypatch.setattr(master_orchestrator, "_is_windows", lambda: False)

    total, available = master_orchestrator.physical_memory_bytes()

    assert total == 16000000 * 1024
    assert available == 9000000 * 1024


def test_p2_3_meminfo_without_memavailable_falls_back(tmp_path, monkeypatch):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 100 kB\nMemFree: 10 kB\n", encoding="utf-8")
    monkeypatch.setattr(master_orchestrator, "_PROC_MEMINFO", str(meminfo))

    assert master_orchestrator._linux_meminfo_bytes() is None


def test_p3_absolute_and_relative_claims_anchor_to_project_root():
    objective = type("Objective", (), {})
    rel, absolute = objective(), objective()
    rel.path, absolute.path = "pkg/a.py", "C:\\Repo\\pkg\\a.py"
    claims = master_orchestrator.delegated_lane_claims(
        ["x", "y"], ((rel,), (absolute,)), project_root="C:/repo",
    )

    assert claims[0].paths == claims[1].paths == frozenset({"pkg/a.py"})
    write = [LaneClaim(c.lane_id, frozenset(c.paths), LaneAccess.WRITE) for c in claims]
    assert ac.claims_conflict(*write)


@pytest.mark.parametrize("bad", ["../escape.py", "D:/elsewhere/a.py", "/etc/passwd"])
def test_p3_invalid_claim_paths_are_rejected_not_made_independent(bad):
    objective = type("Objective", (), {"path": bad})()
    with pytest.raises(ValueError):
        master_orchestrator.delegated_lane_claims(
            ["a"], ((objective,),), project_root="C:/repo",
        )


def test_p3_lane_claim_rejects_unanchored_absolute_paths():
    for bad in ("/abs/a.py", "C:/abs/a.py"):
        with pytest.raises(ValueError):
            LaneClaim("a", frozenset({bad}), LaneAccess.WRITE)


def test_p3_one_flaky_lane_cannot_trigger_a_retry_storm_alone():
    policy = ConcurrencyPolicy(ceiling=8)
    state = ac.initial_state(policy)
    for _ in range(4):
        state, decision = ac.observe(state, Outcome.TRANSIENT_RETRY, policy, source="flaky")
        assert decision.action == "hold"
    for lane in ("b", "c"):
        state, decision = ac.observe(state, Outcome.TRANSIENT_RETRY, policy, source=lane)
    assert decision.action == "shrink" and decision.reasons == (ac.REASON_RETRY_STORM,)


def test_p3_scheduler_counts_one_retry_per_lane():
    scheduler = _scheduler([LaneClaim("a"), LaneClaim("b")], 4)
    scheduler.admit()
    for _ in range(4):
        scheduler.sink("a")(Outcome.TRANSIENT_RETRY)
    scheduler.complete("a")

    assert scheduler.cap == 4


def test_p3_cancel_return_path_reports_concurrency(monkeypatch):
    monkeypatch.setattr(master_orchestrator, "parallel_worker_slots", lambda requested: 1)
    started = threading.Event()
    release = threading.Event()
    box = {}

    def worker(prompt):
        started.set()
        assert release.wait(5)
        return "late"

    def run():
        box["result"] = master_orchestrator.run_delegated(
            "cancel fleet", worker_fn=worker, audit_fn=lambda p: "merged", agents=2,
        )

    thread = threading.Thread(target=run)
    thread.start()
    assert started.wait(5)
    snap = master_orchestrator.snapshot(include_finished=False, limit=20)
    master_id = next(row["id"] for row in snap["agents"] if row["role"] == "master")
    master_orchestrator.request_cancel(master_id)
    release.set()
    thread.join(5)

    assert box["result"]["output"] == "CANCELLED"
    assert box["result"]["concurrency"]["ceiling"] == 1


def test_p3_all_failed_return_path_reports_concurrency(monkeypatch):
    monkeypatch.setattr(master_orchestrator, "parallel_worker_slots", lambda requested: 2)

    def worker(prompt):
        raise ValueError("permanent")

    result = master_orchestrator.run_delegated(
        "fan out", worker_fn=worker, audit_fn=lambda p: "merged", agents=2,
    )

    assert result["output"].startswith("ERROR")
    assert result["concurrency"]["ceiling"] == 2


def test_p3_kill_switch_unknown_value_is_logged_and_ignored(monkeypatch, caplog):
    monkeypatch.setenv("SONDER_FLEET_ADAPTIVE_CONCURRENCY", "maybe")
    with caplog.at_level("WARNING"):
        assert master_orchestrator.adaptive_concurrency_enabled() is True
    assert "SONDER_FLEET_ADAPTIVE_CONCURRENCY" in caplog.text


@pytest.mark.parametrize("value", ["0", "false", "no", "off", " OFF "])
def test_p3_kill_switch_documented_off_values(monkeypatch, value, caplog):
    monkeypatch.setenv("SONDER_FLEET_ADAPTIVE_CONCURRENCY", value)
    with caplog.at_level("WARNING"):
        assert master_orchestrator.adaptive_concurrency_enabled() is False
    assert caplog.text == ""


# --- re-review fixes (PR #552 @ 2943664d): each failed before its fix -------


def test_rr1_lane_admitted_but_never_submitted_is_abandoned(monkeypatch):
    real_pool = master_orchestrator.ThreadPoolExecutor

    class BrokenPool(real_pool):
        submits = 0

        def submit(self, *args, **kwargs):
            BrokenPool.submits += 1
            if BrokenPool.submits == 2:
                raise RuntimeError("cannot schedule new futures after shutdown")
            return super().submit(*args, **kwargs)

    monkeypatch.setattr(master_orchestrator, "ThreadPoolExecutor", BrokenPool)
    scheduler = _scheduler([LaneClaim(name) for name in ("a", "b", "c")], 2)
    abandoned = []

    with pytest.raises(RuntimeError, match="cannot schedule"):
        master_orchestrator.dispatch_lanes(
            scheduler, 2, lambda lane, sink: lane, lambda lane, result: None,
            lambda lane, exc: None, on_abandon=abandoned.append,
        )

    # b was admitted (marked running) but never got a future; c never started.
    assert abandoned == ["b", "c"]


def test_rr2_error_handler_failure_is_logged_counted_and_lane_closed(caplog):
    scheduler = _scheduler([LaneClaim("a"), LaneClaim("b")], 1)
    abandoned = []

    def run_lane(lane_id, _sink):
        if lane_id == "a":
            raise RuntimeError("worker boom")
        return "ok"

    def on_error(_lane, _exc):
        raise OSError("fleet store down: database is locked\n" + "x" * 5000)

    with caplog.at_level("WARNING"):
        master_orchestrator.dispatch_lanes(
            scheduler, 1, run_lane, lambda lane, result: None, on_error,
            on_abandon=abandoned.append,
        )

    assert abandoned == ["a"]
    assert scheduler.summary()["handler_failures"] == 1
    record = next(r for r in caplog.records if "fleet lane a" in r.getMessage())
    message = record.getMessage()
    assert "fleet store down: database is locked" in message
    assert "\n" not in message and len(message) < 400


def test_rr2_collect_failure_is_counted_but_worker_closed_row_is_not_reclosed(caplog):
    scheduler = _scheduler([LaneClaim("a")], 1)
    abandoned = []

    def collect(_lane, _result):
        raise ValueError("bad readiness record")

    with caplog.at_level("WARNING"):
        master_orchestrator.dispatch_lanes(
            scheduler, 1, lambda lane, sink: "ok", collect,
            lambda lane, exc: None, on_abandon=abandoned.append,
        )

    assert abandoned == []
    assert scheduler.summary()["handler_failures"] == 1
    assert "bad readiness record" in caplog.text


def test_rr2_run_delegated_releases_reserved_slot_when_store_stays_down(monkeypatch):
    monkeypatch.setattr(master_orchestrator, "parallel_worker_slots", lambda requested: 1)
    real_finish = master_orchestrator._finish
    doomed = {}

    def worker(prompt):
        if "subagent 1/" in prompt:
            raise ValueError("permanent worker failure")
        return "ok"

    def finish(agent_id, *args, **kwargs):
        if kwargs.get("error") and not doomed:
            doomed["id"] = agent_id
        if doomed.get("id") == agent_id:
            raise OSError("fleet store down")
        return real_finish(agent_id, *args, **kwargs)

    monkeypatch.setattr(master_orchestrator, "_finish", finish)
    before = master_orchestrator.reserved_slot_count()
    result = master_orchestrator.run_delegated(
        "fan out", worker_fn=worker, audit_fn=lambda prompt: "merged", agents=3,
    )

    assert result["output"] == "merged"
    assert result["concurrency"]["handler_failures"] == 1
    assert master_orchestrator.reserved_slot_count() == before
