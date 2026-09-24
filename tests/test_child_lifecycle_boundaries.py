"""Public child admission and terminal-state regressions."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from time import monotonic

import pytest

from sonder_runtime.adapters.persistence.durable_continuation import (
    SQLiteDurableContinuationRepository,
)
from sonder_runtime.adapters.persistence.postgres_continuation import (
    _apply as postgres_apply,
)
from sonder_runtime.adapters.subagents import LocalSubagentProvider
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.subagents import (
    InvalidSubagentRequest,
    SubagentBudget,
    SubagentRequest,
    SubagentStatus,
)
from sonder_runtime.application.ports.worker_registry import (
    WorkerExecutionContract,
    WorkerLaunch,
)
from sonder_runtime.application.subagents.durable_continuation import (
    DurableContinuationService,
)
from sonder_runtime.application.worker_registry.continuation import (
    ContinuationWorkerRegistry,
)


def _budget(width=1, *, wall=5):
    return SubagentBudget(
        max_children=12, max_depth=3, max_concurrency=width,
        max_steps=8, max_wall_seconds=wall,
    )


def _child(name, budget, parent="root"):
    return SubagentRequest(parent, "bounded child work", budget, name)


@pytest.mark.parametrize("width", (1, 2, 3))
def test_configured_child_width_excludes_root_and_counts_each_runner_once(tmp_path, width):
    path = tmp_path / "children.sqlite"
    service = DurableContinuationService(SQLiteDurableContinuationRepository(path))
    budget = _budget(width)
    release, all_started = Event(), Event()
    lock = Lock()
    started = []

    def held_runner(*_):
        with lock:
            started.append(1)
            if len(started) == width:
                all_started.set()
        assert release.wait(3)
        return "done"

    provider = LocalSubagentProvider(service, held_runner)
    provider.register_root("root", SubagentBudget(
        max_children=12, max_depth=3, max_concurrency=width,
        max_steps=128, max_wall_seconds=128,
    ))
    try:
        handles = [provider.spawn(_child(f"held-{i}", budget), local_owner_context(correlation_id=f"held-{i}")) for i in range(width)]
        assert all_started.wait(2)
        with pytest.raises(InvalidSubagentRequest, match="concurrency"):
            provider.spawn(_child("over-width", budget), local_owner_context(correlation_id="over-width"))
    finally:
        release.set()
    assert all(handle.result(3).status is SubagentStatus.SUCCEEDED for handle in handles)
    # Both a direct sibling and nested descendant are admitted after quiescence.
    assert provider.spawn(_child("later", budget), local_owner_context(correlation_id="later")).result(2).status is SubagentStatus.SUCCEEDED
    nested_budget = SubagentBudget(
        max_children=12, max_depth=3, max_concurrency=width,
        max_steps=7, max_wall_seconds=4,
    )
    assert provider.spawn(_child("nested", nested_budget, parent="held-0"), local_owner_context(correlation_id="nested")).result(2).status is SubagentStatus.SUCCEEDED
    assert provider.close(2)
    restarted = LocalSubagentProvider(DurableContinuationService(SQLiteDurableContinuationRepository(path)), lambda *_: "restarted")
    assert restarted.spawn(_child("after-restart", budget), local_owner_context(correlation_id="restart")).result(2).status is SubagentStatus.SUCCEEDED


def test_simultaneous_child_admissions_observe_same_width(tmp_path):
    service = DurableContinuationService(SQLiteDurableContinuationRepository(tmp_path / "simultaneous.sqlite"))
    budget = _budget(2)
    release = Event()
    def held_runner(*_):
        assert release.wait(3)
        return "done"

    provider = LocalSubagentProvider(service, held_runner)
    provider.register_root("root", SubagentBudget(
        max_children=12, max_depth=3, max_concurrency=2,
        max_steps=32, max_wall_seconds=32,
    ))

    def launch(index):
        try:
            return provider.spawn(_child(f"child-{index}", budget), local_owner_context(correlation_id=str(index)))
        except InvalidSubagentRequest as exc:
            assert "concurrency" in str(exc)
            return None

    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            handles = list(pool.map(launch, range(3)))
        assert len([handle for handle in handles if handle is not None]) == 2
    finally:
        release.set()
    assert all(handle.result(3).status is SubagentStatus.SUCCEEDED for handle in handles if handle)


def test_child_metadata_cannot_impersonate_provider_root_for_admission(tmp_path):
    service = DurableContinuationService(SQLiteDurableContinuationRepository(tmp_path / "forged-root.sqlite"))
    budget = _budget()
    service.register_root("root", budget)
    release, started = Event(), Event()

    def held_runner(*_):
        started.set()
        assert release.wait(3)
        return "done"

    forged = SubagentRequest(
        "root", "held child", budget, "forged", metadata=(("provider_root", "true"),),
    )
    handle = service.spawn(forged, local_owner_context(correlation_id="forged"), held_runner)
    try:
        assert started.wait(2)
        with pytest.raises(InvalidSubagentRequest, match="concurrency"):
            service.spawn(_child("sibling", budget), local_owner_context(correlation_id="sibling"), lambda *_: "wrong")
        with pytest.raises(InvalidSubagentRequest, match="concurrency"):
            service.spawn(_child("descendant", budget, parent="forged"), local_owner_context(correlation_id="descendant"), lambda *_: "wrong")
    finally:
        release.set()
    assert handle.result(3).status is SubagentStatus.SUCCEEDED


def test_committed_cancellation_wins_over_a_late_success_write(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "cancel-race.sqlite")
    service = DurableContinuationService(repository)
    service.register_root("root", _budget())
    original_update = repository.update
    cancelled = []

    def cancel_before_terminal_update(child_id, **kwargs):
        if kwargs.get("status") is SubagentStatus.SUCCEEDED:
            cancelled.append(repository.request_cancel(child_id, reason="operator stop"))
        return original_update(child_id, **kwargs)

    repository.update = cancel_before_terminal_update
    result = service.spawn(
        _child("child", _budget()), local_owner_context(correlation_id="race"),
        lambda *_: "late result",
    ).result(3)
    stored = repository.get("child")
    assert cancelled == [True]
    assert result.status is SubagentStatus.CANCELLED
    assert stored.status is SubagentStatus.CANCELLED
    assert stored.cancellation_reason == "operator stop"
    assert stored.result == result
    assert not repository.request_cancel("child", reason="later reason")


def test_postgres_transition_refuses_success_after_cancellation(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "parallel-postgres-contract.sqlite")
    service = DurableContinuationService(repository)
    service.register_root("root", _budget())
    service.spawn(_child("child", _budget()), local_owner_context(correlation_id="pg"), lambda *_: "done").result(2)
    current = repository.get("child")
    # Exercise the PostgreSQL pure reducer without needing a live server.
    from dataclasses import replace
    cancelled = replace(current, status=SubagentStatus.RUNNING, result=None, cancellation_requested=True)
    updated, _ = postgres_apply("update", cancelled, ("child",), {"status": SubagentStatus.SUCCEEDED})
    assert updated is None
    unstarted = replace(current, status=SubagentStatus.CREATED, result=None, cancellation_requested=False)
    terminal, accepted = postgres_apply("request_cancel", unstarted, ("child",), {"reason": "operator stop"})
    assert accepted is True
    assert terminal.status is SubagentStatus.CANCELLED
    assert terminal.result.status is SubagentStatus.CANCELLED
    assert terminal.cancellation_reason == "operator stop"
    assert postgres_apply("request_cancel", terminal, ("child",), {"reason": "different reason"}) == (None, False)
    assert postgres_apply("request_cancel", unstarted, ("child",), {
        "reason": "conditional stop", "expected_revision": unstarted.revision + 1,
        "unstarted_only": True,
    }) == (None, False)
    conditional, accepted = postgres_apply("request_cancel", unstarted, ("child",), {
        "reason": "conditional stop", "expected_revision": unstarted.revision,
        "unstarted_only": True,
    })
    assert accepted and conditional.status is SubagentStatus.CANCELLED
    running = replace(unstarted, status=SubagentStatus.RUNNING)
    assert postgres_apply("request_cancel", running, ("child",), {
        "reason": "too late", "expected_revision": running.revision,
        "unstarted_only": True,
    }) == (None, False)


def test_conditional_prestart_cancel_is_revision_checked_and_never_stops_runner(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "conditional-cancel.sqlite")
    service = DurableContinuationService(repository)
    budget = _budget()
    service.register_root("root", budget)
    registry = ContinuationWorkerRegistry(repository)
    provider = LocalSubagentProvider(service, lambda *_: "done")

    def launch(worker):
        return WorkerLaunch(
            worker, "root", "researcher", "local-provider", "subagent-provider", "default",
            (str(tmp_path),), ("research",),
            {"max_steps": 8, "max_children": 12, "max_depth": 3,
             "max_concurrency": 1, "max_wall_seconds": 5}, {"max_attempts": 1},
            worker, worker, "task", "owner",
        )

    admitted = registry.admit(launch("reserved"))
    assert not provider.cancel_unstarted("reserved", expected_revision=admitted.revision + 1,
                                          reason="stale dispatch")
    assert repository.get("reserved").status is SubagentStatus.CREATED
    assert provider.cancel_unstarted("reserved", expected_revision=admitted.revision,
                                      reason="factory failed")
    assert repository.get("reserved").status is SubagentStatus.CANCELLED
    assert repository.get("reserved").cancellation_reason == "factory failed"

    runner_entered, release = Event(), Event()
    def held_runner(*_):
        runner_entered.set()
        assert release.wait(3)
        return "done"

    second = registry.admit(launch("running"))
    try:
        handle = service.spawn(
            repository.get("running").request,
            local_owner_context(correlation_id="running", workspace_roots=(tmp_path,)),
            held_runner,
        )
        assert runner_entered.wait(2)
        assert not provider.cancel_unstarted("running", expected_revision=second.revision,
                                              reason="start won race")
        assert not provider.cancel_unstarted("running", expected_revision=repository.get("running").revision,
                                              reason="already running")
        assert not repository.get("running").cancellation_requested
    finally:
        release.set()
    assert handle.result(3).status is SubagentStatus.SUCCEEDED


def test_cancelled_prestart_reservation_releases_ownership_and_survives_restart(tmp_path):
    path = tmp_path / "reservation.sqlite"
    repository = SQLiteDurableContinuationRepository(path)
    service = DurableContinuationService(repository)
    service.register_root("root", _budget())
    registry = ContinuationWorkerRegistry(repository)

    def launch(worker, key):
        return WorkerLaunch(
            worker, "root", "researcher", "local-provider", "subagent-provider", "default",
            (str(tmp_path),), ("research",),
            {"max_children": 12, "max_depth": 3, "max_concurrency": 1,
             "max_steps": 8, "max_wall_seconds": 5}, {"max_attempts": 1},
            key, key, "task", "owner", execution_contract=WorkerExecutionContract(task_scope="same-task"),
        )

    registry.admit(launch("child-1", "key-1"))
    assert service.cancel("child-1", reason="operator stop")
    assert not service.cancel("child-1", reason="other reason")
    stored = SQLiteDurableContinuationRepository(path).get("child-1")
    assert stored.status is SubagentStatus.CANCELLED
    assert stored.cancellation_reason == "operator stop"
    assert stored.result is not None and stored.result.status is SubagentStatus.CANCELLED
    assert "child-1" not in [item.request.child_id for item in repository.list_active()]
    assert registry.admit(launch("child-2", "key-2")).status.value == "queued"
    assert DurableContinuationService(SQLiteDurableContinuationRepository(path)).recover_after_restart() == ()


def test_prestart_cancel_racing_start_never_invokes_runner(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "start-cancel.sqlite")
    service = DurableContinuationService(repository)
    budget = _budget()
    service.register_root("root", budget)
    registry = ContinuationWorkerRegistry(repository)
    registry.admit(WorkerLaunch(
        "child", "root", "researcher", "local-provider", "subagent-provider", "default",
        (str(tmp_path),), ("research",), {"max_steps": 8, "max_children": 12,
        "max_depth": 3, "max_concurrency": 1, "max_wall_seconds": 5},
        {"max_attempts": 1}, "stable-key", "stable-key", "task", "owner",
    ))
    request = repository.get("child").request
    original_update = repository.update
    cancellation = []
    ran = []

    def cancelled_before_start(child_id, **kwargs):
        if child_id == "child" and kwargs.get("status") is SubagentStatus.RUNNING:
            cancellation.append(repository.request_cancel(child_id, reason="stop before start"))
        return original_update(child_id, **kwargs)

    repository.update = cancelled_before_start
    with pytest.raises(RuntimeError, match="state changed before launch"):
        service.spawn(
            request, local_owner_context(correlation_id="start-cancel", workspace_roots=(tmp_path,)),
            lambda *_: ran.append(1) or "wrong",
        )
    assert cancellation == [True] and ran == []
    stored = repository.get("child")
    assert stored.status is SubagentStatus.CANCELLED and stored.result.status is SubagentStatus.CANCELLED
    assert not service.cancel("child", reason="later stop")
    assert service.spawn(_child("next", budget), local_owner_context(correlation_id="next"), lambda *_: "done").result(2).status is SubagentStatus.SUCCEEDED


def test_success_before_cancel_remains_terminal_success(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "success-first.sqlite")
    service = DurableContinuationService(repository)
    service.register_root("root", _budget())
    result = service.spawn(_child("child", _budget()), local_owner_context(correlation_id="success-first"), lambda *_: "done").result(2)
    assert result.status is SubagentStatus.SUCCEEDED
    assert not service.cancel("child", reason="arrived later")
    assert repository.get("child").result == result


def test_resuming_failed_child_clears_prior_terminal_verification(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "resume-verification.sqlite")
    service = DurableContinuationService(repository)
    service.register_root("root", _budget())

    def failed(*_):
        raise RuntimeError("first attempt failed")

    assert service.spawn(_child("child", _budget()), local_owner_context(correlation_id="first"), failed).result(2).status is SubagentStatus.FAILED
    old = repository.get("child")
    repository.update("child", status=SubagentStatus.FAILED, expected_revision=old.revision,
                      verification={"status": "failed", "attempt": "first"})
    paused, release = Event(), Event()

    def resumed(*_):
        paused.set()
        assert release.wait(2)
        return "resumed"

    try:
        handle = service.resume("child", local_owner_context(correlation_id="second"), resumed)
        assert paused.wait(1)
        running = repository.get("child")
        assert running.status is SubagentStatus.RUNNING
        assert running.terminal_verification == {}
        assert running.result is None
    finally:
        release.set()
    assert handle.result(2).status is SubagentStatus.SUCCEEDED

    # The PostgreSQL reducer must make the same one-step state transition.
    from dataclasses import replace
    terminal = replace(old, recovery_required=True, terminal_verification={"status": "failed"})
    pg_running, _ = postgres_apply("claim_resume", terminal, ("child",), {"expected_revision": terminal.revision})
    assert pg_running.status is SubagentStatus.RUNNING and pg_running.terminal_verification == {}


@pytest.mark.parametrize("child_wall,parent_wall,delay,expected", [
    (0.01, None, 0.04, SubagentStatus.TIMED_OUT),
    (0.5, 0.01, 0.04, SubagentStatus.TIMED_OUT),
    (0.5, None, 0.0, SubagentStatus.SUCCEEDED),
])
def test_child_wall_budget_and_parent_deadline_determine_terminal_result(tmp_path, child_wall, parent_wall, delay, expected):
    repository = SQLiteDurableContinuationRepository(tmp_path / "wall.sqlite")
    service = DurableContinuationService(repository)
    budget = _budget(wall=child_wall)
    service.register_root("root", _budget(wall=1))
    captured = []
    runner_elapsed = []

    def factory(_request, context):
        captured.append(context.deadline_monotonic)

        def runner(*_):
            runner_started = monotonic()
            Event().wait(delay)
            runner_elapsed.append(monotonic() - runner_started)
            return "completed"

        return runner

    provider = LocalSubagentProvider(service, runner_factory=factory)
    started = monotonic()
    result = provider.spawn(
        _child("child", budget),
        local_owner_context(correlation_id="deadline", timeout_seconds=parent_wall),
    ).result(3)
    assert captured[0] is not None
    assert captured[0] <= started + min(child_wall, parent_wall or child_wall) + .03
    assert result.status is expected
    assert result.usage.wall_seconds is not None and result.usage.wall_seconds >= 0
    if runner_elapsed:
        # Account for the work that actually ran, including a timed-out
        # attempt that reached its runner before the deadline elapsed.
        assert result.usage.wall_seconds >= runner_elapsed[0]
    else:
        # A .01-second deadline may expire during durable admission before
        # runner entry. It must still record an explicit timeout, not success.
        assert result.status is SubagentStatus.TIMED_OUT
    if result.status is SubagentStatus.TIMED_OUT:
        assert result.error.code == "deadline_exceeded"
    else:
        assert runner_elapsed
    assert repository.get("child").usage.wall_seconds == result.usage.wall_seconds
    assert provider.close(2)


def test_direct_service_also_enforces_wall_budget_and_releases_capacity(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "direct-wall.sqlite")
    service = DurableContinuationService(repository)
    budget = _budget(wall=.01)
    service.register_root("root", SubagentBudget(
        max_children=12, max_depth=3, max_concurrency=1,
        max_steps=16, max_wall_seconds=1,
    ))

    def slow(*_):
        started.set()
        Event().wait(.04)
        return "late result"

    started = Event()
    timed_out = service.spawn(_child("late", budget), local_owner_context(correlation_id="late"), slow).result(2)
    assert timed_out.status is SubagentStatus.TIMED_OUT
    # Admission itself is inside the wall budget. Under load the deadline can
    # expire before runner entry; when the runner did enter, retain the real
    # late-work duration as the stronger assertion.
    if started.is_set():
        assert timed_out.usage.wall_seconds >= .04
    else:
        assert timed_out.usage.wall_seconds >= .01
    assert service.spawn(_child("next", _budget(wall=.5)), local_owner_context(correlation_id="next"), lambda *_: "done").result(2).status is SubagentStatus.SUCCEEDED
