"""Cross-instance and hierarchical child reservation regressions."""

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from multiprocessing import get_context
from threading import Barrier, Event

import pytest

from sonder_runtime.adapters.persistence.durable_continuation import (
    SQLiteDurableContinuationRepository,
)
from sonder_runtime.adapters.persistence.postgres_continuation import (
    _apply as postgres_apply,
)
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.continuation_records import (
    ChildSessionLineage,
    DurableChildSession,
)
from sonder_runtime.application.ports.subagents import (
    InvalidSubagentRequest,
    SubagentBudget,
    SubagentProtocolError,
    SubagentRequest,
    SubagentResult,
    SubagentStatus,
    SubagentUsage,
)
from sonder_runtime.application.ports.worker_registry import (
    WorkerExecutionContract,
    WorkerLaunch,
)
from sonder_runtime.application.subagents.continuable import ContinuableCheckpoint
from sonder_runtime.application.subagents.durable_continuation import (
    DurableContinuationService,
)
from sonder_runtime.application.worker_registry.continuation import (
    ContinuationWorkerRegistry,
    _request_for,
)


def _root_budget(*, width=1, steps=100, tokens=100, wall=100, children=10, depth=3):
    return SubagentBudget(
        max_children=children, max_depth=depth, max_concurrency=width,
        max_steps=steps, max_output_tokens=tokens, max_wall_seconds=wall,
    )


def _child(child_id, *, parent="root", width=1, steps=5, tokens=5, wall=5, children=5):
    return SubagentRequest(
        parent, "bounded child", _root_budget(
            width=width, steps=steps, tokens=tokens, wall=wall, children=children,
        ), child_id,
    )


def _context(name):
    return local_owner_context(correlation_id=name)


def _reserve_in_process(path, barrier, results, index):
    repository = SQLiteDurableContinuationRepository(path)
    try:
        barrier.wait(timeout=10)
        repository.create(DurableChildSession(
            _child(f"process-{index}"), ChildSessionLineage("root"),
        ))
    except InvalidSubagentRequest as error:
        results.put((index, "rejected", str(error)))
    else:
        results.put((index, "admitted", ""))


def _speculative_request(index, lane_id, hypothesis):
    return SubagentRequest(
        "root", "one hypothesis", _root_budget(width=2, steps=5, tokens=5, wall=5),
        f"speculative-{index}", (
            ("execution_task_scope", "shared-question"),
            ("execution_speculative_lane", "true"),
            ("speculative_lane_id", lane_id),
            ("hypothesis_digest", sha256(hypothesis.encode()).hexdigest()),
        ),
    )


def _reserve_speculative_in_process(path, barrier, results, index, lane_id, hypothesis):
    repository = SQLiteDurableContinuationRepository(path)
    barrier.wait(timeout=10)
    try:
        repository.create(DurableChildSession(
            _speculative_request(index, lane_id, hypothesis), ChildSessionLineage("root"),
        ))
    except InvalidSubagentRequest as error:
        results.put((index, "rejected", str(error)))
    else:
        results.put((index, "admitted", ""))


def _reserve_owned_root_in_process(path, barrier, results, index):
    repository = SQLiteDurableContinuationRepository(path)
    barrier.wait(timeout=10)
    try:
        repository.create(DurableChildSession(
            _child(f"host-child-{index}", parent=f"operation-{index}"),
            ChildSessionLineage(f"operation-{index}"),
        ))
    except InvalidSubagentRequest as error:
        results.put((index, "rejected", str(error)))
    else:
        results.put((index, "admitted", ""))


def test_distinct_operation_roots_share_one_owner_parallelism_ceiling(tmp_path):
    path = tmp_path / "owner-wide.sqlite"
    service = DurableContinuationService(SQLiteDurableContinuationRepository(path))
    for index in range(2):
        service.register_root(f"operation-{index}", _root_budget(width=1), owner_id="host-owner")
    multiprocessing = get_context("spawn")
    barrier, results = multiprocessing.Barrier(2), multiprocessing.Queue()
    processes = [
        multiprocessing.Process(
            target=_reserve_owned_root_in_process, args=(path, barrier, results, index),
        ) for index in range(2)
    ]
    for process in processes:
        process.start()
    try:
        for process in processes:
            process.join(timeout=15)
        assert all(process.exitcode == 0 for process in processes)
        outcomes = [results.get(timeout=2) for _ in processes]
        assert sorted(status for _i, status, _detail in outcomes) == ["admitted", "rejected"]
        assert "host concurrency" in next(detail for _i, status, detail in outcomes if status == "rejected")
        admitted_index = next(index for index, status, _detail in outcomes if status == "admitted")
        rejected_index = 1 - admitted_index
        repository = SQLiteDurableContinuationRepository(path)
        assert repository.request_cancel(
            f"host-child-{admitted_index}", reason="release owner slot",
            expected_revision=0, unstarted_only=True,
        )
        assert repository.create(DurableChildSession(
            _child(f"host-child-{rejected_index}", parent=f"operation-{rejected_index}"),
            ChildSessionLineage(f"operation-{rejected_index}"),
        )).status is SubagentStatus.CREATED
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)


def test_owner_root_width_changes_after_old_run_quiesces(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "owner-width-change.sqlite")
    service = DurableContinuationService(repository)
    service.register_root("old-run", _root_budget(width=1), owner_id="host-owner")
    repository.create(DurableChildSession(_child("old-child", parent="old-run"),
                                          ChildSessionLineage("old-run")))
    service.register_root("new-run", _root_budget(width=2), owner_id="host-owner")
    with pytest.raises(InvalidSubagentRequest, match="host concurrency"):
        repository.create(DurableChildSession(
            _child("new-child-1", parent="new-run"), ChildSessionLineage("new-run"),
        ))
    assert repository.request_cancel(
        "old-child", reason="old run finished", expected_revision=0, unstarted_only=True,
    )
    for index in range(2):
        assert repository.create(DurableChildSession(
            _child(f"new-child-{index}", parent="new-run", width=2),
            ChildSessionLineage("new-run"),
        )).status is SubagentStatus.CREATED


def test_paused_old_root_keeps_its_smaller_owner_width_until_claim_released(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "paused-old-owner.sqlite")
    service = DurableContinuationService(repository)
    service.register_root("old-run", _root_budget(width=1), owner_id="host-owner")
    repository.create(DurableChildSession(_child("paused-child", parent="old-run"),
                                          ChildSessionLineage("old-run")))
    assert repository.update("paused-child", status=SubagentStatus.FAILED,
                             recovery_required=True) is not None
    service.register_root("new-run", _root_budget(width=2), owner_id="host-owner")
    repository.create(DurableChildSession(
        _child("one-new", parent="new-run", width=2), ChildSessionLineage("new-run"),
    ))
    with pytest.raises(InvalidSubagentRequest, match="host concurrency"):
        repository.create(DurableChildSession(
            _child("two-new", parent="new-run", width=2), ChildSessionLineage("new-run"),
        ))
    assert repository.update("paused-child", status=SubagentStatus.FAILED,
                             recovery_required=False) is not None
    assert repository.create(DurableChildSession(
        _child("two-new", parent="new-run", width=2), ChildSessionLineage("new-run"),
    )).status is SubagentStatus.CREATED


def test_host_child_limit_remains_per_root_and_other_owner_has_own_width(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "owner-limits.sqlite")
    service = DurableContinuationService(repository)
    for root in ("run-a", "run-b"):
        service.register_root(root, _root_budget(width=5, children=2), owner_id="owner-a")
        for index in range(2):
            repository.create(DurableChildSession(
                _child(f"{root}-child-{index}", parent=root, width=2, children=2),
                ChildSessionLineage(root),
            ))
    with pytest.raises(InvalidSubagentRequest, match="child-count"):
        repository.create(DurableChildSession(
            _child("run-a-excess", parent="run-a", width=2, children=2),
            ChildSessionLineage("run-a"),
        ))
    service.register_root("run-c", _root_budget(width=1), owner_id="owner-b")
    assert repository.create(DurableChildSession(
        _child("other-owner", parent="run-c"), ChildSessionLineage("run-c"),
    )).status is SubagentStatus.CREATED


def test_cancelled_root_fences_new_descendants_but_prior_worker_can_settle(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "ancestor-cancel.sqlite")
    service = DurableContinuationService(repository)
    service.register_root("root", _root_budget(width=2), owner_id="host-owner")
    started, release = Event(), Event()

    def running(*_):
        started.set()
        assert release.wait(3)
        return "already admitted"

    handle = service.spawn(
        _child("parent", width=2, steps=8, tokens=8, wall=8), _context("parent"), running,
    )
    try:
        assert started.wait(2)
        assert service.cancel("root", reason="host operation cancelled")
        with pytest.raises(InvalidSubagentRequest, match="cancelled ancestor"):
            repository.create(DurableChildSession(
                _child("grandchild", parent="parent", tokens=5),
                ChildSessionLineage("parent", ("root",)),
            ))
    finally:
        release.set()
    assert handle.result(3).status is SubagentStatus.SUCCEEDED


@pytest.mark.parametrize("identities", [
    (("same-lane", "hypothesis-a"), ("same-lane", "hypothesis-b")),
    (("lane-a", "same-hypothesis"), ("lane-b", "same-hypothesis")),
])
def test_independent_processes_cannot_duplicate_one_speculative_identity(tmp_path, identities):
    path = tmp_path / "speculative-race.sqlite"
    DurableContinuationService(SQLiteDurableContinuationRepository(path)).register_root(
        "root", _root_budget(width=3),
    )
    multiprocessing = get_context("spawn")
    barrier, results = multiprocessing.Barrier(2), multiprocessing.Queue()
    processes = [
        multiprocessing.Process(
            target=_reserve_speculative_in_process,
            args=(path, barrier, results, index, *identity),
        ) for index, identity in enumerate(identities)
    ]
    for process in processes:
        process.start()
    try:
        for process in processes:
            process.join(timeout=15)
        assert all(process.exitcode == 0 for process in processes)
        outcomes = [results.get(timeout=2) for _ in processes]
        assert sorted(status for _index, status, _error in outcomes) == ["admitted", "rejected"]
        assert "speculative hypothesis" in next(
            error for _i, status, error in outcomes if status == "rejected"
        )
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)


def test_distinct_speculative_lanes_under_one_question_can_both_run(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "distinct-lanes.sqlite")
    DurableContinuationService(repository).register_root("root", _root_budget(width=2))
    for index, lane in enumerate(("lane-a", "lane-b")):
        assert repository.create(DurableChildSession(
            _speculative_request(index, lane, f"hypothesis-{index}"),
            ChildSessionLineage("root"),
        )).status is SubagentStatus.CREATED


def test_independent_processes_share_one_sqlite_admission_transaction(tmp_path):
    path = tmp_path / "process-race.sqlite"
    repository = SQLiteDurableContinuationRepository(path)
    DurableContinuationService(repository).register_root("root", _root_budget(width=1))
    context = get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(target=_reserve_in_process, args=(path, barrier, results, index))
        for index in range(2)
    ]
    for process in processes:
        process.start()
    try:
        for process in processes:
            process.join(timeout=15)
        assert all(process.exitcode == 0 for process in processes)
        outcomes = [results.get(timeout=2) for _ in processes]
        assert sorted(status for _index, status, _message in outcomes) == ["admitted", "rejected"]
        assert "concurrency" in next(message for _i, status, message in outcomes if status == "rejected")
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)


def test_two_independent_services_cannot_both_claim_one_root_slot(tmp_path):
    path = tmp_path / "shared.sqlite"
    services = [DurableContinuationService(SQLiteDurableContinuationRepository(path)) for _ in range(2)]
    services[0].register_root("root", _root_budget(width=1))
    preflight = Barrier(2)
    release = Event()

    def runner(*_):
        assert release.wait(3)
        return "done"

    for service in services:
        admit = service._admit

        def synchronized(*args, _admit=admit, **kwargs):
            _admit(*args, **kwargs)
            preflight.wait(3)

        service._admit = synchronized

    def spawn(index):
        try:
            return services[index].spawn(_child(f"child-{index}"), _context(str(index)), runner)
        except InvalidSubagentRequest as error:
            assert "concurrency" in str(error)
            return None

    try:
        with ThreadPoolExecutor(2) as pool:
            handles = list(pool.map(spawn, range(2)))
        assert sum(handle is not None for handle in handles) == 1
        assert len(SQLiteDurableContinuationRepository(path).list_active()) == 2  # root + child
    finally:
        release.set()
    assert all(handle.result(3).status is SubagentStatus.SUCCEEDED for handle in handles if handle)


def test_root_registration_cannot_grandfather_external_reservations(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "late-root.sqlite")
    repository.create(DurableChildSession(
        _child("external"), ChildSessionLineage("root"),
    ))
    with pytest.raises(InvalidSubagentRequest, match="before admitting children"):
        DurableContinuationService(repository).register_root("root", _root_budget(width=1))
    assert repository.get("root") is None


def test_registered_root_identity_is_idempotent_only_for_same_owner_and_budget(tmp_path):
    path = tmp_path / "owned-root.sqlite"
    budget = _root_budget(width=2)
    first = DurableContinuationService(SQLiteDurableContinuationRepository(path))
    second = DurableContinuationService(SQLiteDurableContinuationRepository(path))
    registered = first.register_root("root", budget, owner_id="session-owner")
    assert second.register_root("root", budget, owner_id="session-owner") == registered
    assert dict(registered.request.metadata)["owner_id"] == "session-owner"
    with pytest.raises(InvalidSubagentRequest, match="owner or budget differs"):
        second.register_root("root", budget, owner_id="different-owner")
    with pytest.raises(InvalidSubagentRequest, match="owner or budget differs"):
        second.register_root("root", _root_budget(width=1), owner_id="session-owner")


@pytest.mark.parametrize("field,first,second", [
    ("max_steps", 6, 5),
    ("max_output_tokens", 6, 5),
    ("max_wall_seconds", 6, 5),
])
def test_parent_pool_reserves_children_across_instances(tmp_path, field, first, second):
    path = tmp_path / "resource.sqlite"
    root = _root_budget(width=2, steps=10, tokens=10, wall=10)
    services = [DurableContinuationService(SQLiteDurableContinuationRepository(path)) for _ in range(2)]
    services[0].register_root("root", root)
    def request(name, value):
        limits = {"width": 2, "steps": 1, "tokens": 1, "wall": 1}
        limits[{"max_steps": "steps", "max_output_tokens": "tokens", "max_wall_seconds": "wall"}[field]] = value
        return _child(name, **limits)

    first_record = services[0]._repository.create(
        DurableChildSession(request("first", first), ChildSessionLineage("root"))
    )
    assert first_record.status is SubagentStatus.CREATED
    with pytest.raises(InvalidSubagentRequest, match=field):
        services[1].spawn(request("second", second), _context("second"), lambda *_: "done")
    assert services[0].cancel("first", reason="release unused reservation")
    assert services[1].spawn(request("second", second), _context("second-after-release"), lambda *_: "done").result(3).status is SubagentStatus.SUCCEEDED


def test_registry_reservation_occupies_one_slot_and_provider_consumes_it_once(tmp_path):
    path = tmp_path / "registry.sqlite"
    repository = SQLiteDurableContinuationRepository(path)
    first = DurableContinuationService(repository)
    second = DurableContinuationService(SQLiteDurableContinuationRepository(path))
    first.register_root("root", _root_budget(width=1))
    registry = ContinuationWorkerRegistry(repository)
    launch = WorkerLaunch(
        "reserved", "root", "researcher", "local-provider", "subagent-provider", "default",
        (str(tmp_path),), ("research",),
        {"max_children": 5, "max_depth": 3, "max_concurrency": 1,
         "max_steps": 5, "max_output_tokens": 5, "max_wall_seconds": 5},
        {"max_attempts": 1}, "reserved-key", "reserved-key", "task", "owner",
    )
    admitted = registry.admit(launch)
    with pytest.raises(InvalidSubagentRequest, match="concurrency"):
        second.spawn(_child("other"), _context("other"), lambda *_: "wrong")
    reserved = repository.get(admitted.launch.worker_id)
    context = _context("consume")
    object.__setattr__(context, "principal_id", "owner")
    assert first.spawn(reserved.request, context, lambda *_: "done").result(3).status is SubagentStatus.SUCCEEDED
    assert second.spawn(_child("later"), _context("later"), lambda *_: "done").result(3).status is SubagentStatus.SUCCEEDED


def test_child_local_width_does_not_reduce_root_sibling_capacity(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "nested-width.sqlite")
    service = DurableContinuationService(repository)
    service.register_root("root", _root_budget(width=2))
    first = repository.create(DurableChildSession(_child("first", width=1), ChildSessionLineage("root")))
    second = repository.create(DurableChildSession(_child("second", width=1), ChildSessionLineage("root")))
    assert first.status is second.status is SubagentStatus.CREATED
    with pytest.raises(InvalidSubagentRequest, match="concurrency"):
        repository.create(DurableChildSession(_child("third", width=1), ChildSessionLineage("root")))


def test_root_total_descendant_cap_includes_grandchildren(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "descendants.sqlite")
    service = DurableContinuationService(repository)
    service.register_root("root", _root_budget(width=3, children=2))
    repository.create(DurableChildSession(
        _child("parent", width=2, steps=8, tokens=8, wall=8, children=2),
        ChildSessionLineage("root"),
    ))
    repository.create(DurableChildSession(
        _child("grandchild", parent="parent", steps=5, tokens=5, wall=5, children=1),
        ChildSessionLineage("parent", ("root",)),
    ))
    with pytest.raises(InvalidSubagentRequest, match="child-count"):
        repository.create(DurableChildSession(
            _child("sibling", children=2), ChildSessionLineage("root"),
        ))


def test_settled_usage_debits_parent_pool_and_cannot_be_reduced(tmp_path):
    path = tmp_path / "spent.sqlite"
    repository = SQLiteDurableContinuationRepository(path)
    service = DurableContinuationService(repository)
    service.register_root("root", _root_budget(width=2, steps=10, tokens=10, wall=10))

    def runner(_state, save, _cancel):
        for step in range(4):
            save({"step": step})
        return "x" * 20

    result = service.spawn(_child("first", steps=6, tokens=6, wall=6), _context("first"), runner).result(3)
    assert result.status is SubagentStatus.SUCCEEDED
    assert result.usage.steps == 4 and result.usage.output_tokens == 5
    assert result.usage.wall_seconds > 0
    prior = repository.get("first")
    assert repository.update(
        "first", status=SubagentStatus.SUCCEEDED, expected_revision=prior.revision,
        usage=SubagentUsage(steps=0, output_tokens=0, wall_seconds=0),
    ) is None
    extra_usage = SubagentUsage(
        steps=result.usage.steps + 1, output_tokens=result.usage.output_tokens,
        wall_seconds=result.usage.wall_seconds,
    )
    assert repository.update("first", status=SubagentStatus.SUCCEEDED,
                             usage=extra_usage) is None
    assert repository.get("first").usage == result.usage
    assert postgres_apply("update", prior, ("first",), {
        "status": SubagentStatus.SUCCEEDED,
        "usage": SubagentUsage(steps=0, output_tokens=0, wall_seconds=0),
    }) == (None, None)
    assert postgres_apply("update", prior, ("first",), {
        "status": SubagentStatus.SUCCEEDED, "usage": extra_usage,
    }) == (None, None)
    with pytest.raises(InvalidSubagentRequest, match="max_steps"):
        service.spawn(_child("too-many-steps", steps=7, tokens=1, wall=1), _context("steps"), lambda *_: "x")
    with pytest.raises(InvalidSubagentRequest, match="max_output_tokens"):
        service.spawn(_child("too-many-tokens", steps=1, tokens=6, wall=1), _context("tokens"), lambda *_: "x")
    with pytest.raises(SubagentProtocolError):
        SubagentUsage(steps=-1, output_tokens=0, wall_seconds=0)


def test_parallel_worker_seconds_are_additive_after_terminal_result(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "wall-use.sqlite")
    service = DurableContinuationService(repository)
    service.register_root("root", _root_budget(width=2, wall=.3))
    def runner(*_):
        Event().wait(.03)
        return "done"

    result = service.spawn(_child("first", wall=.2), _context("first"), runner).result(3)
    assert result.status is SubagentStatus.SUCCEEDED and result.usage.wall_seconds >= .03
    with pytest.raises(InvalidSubagentRequest, match="max_wall_seconds"):
        service.spawn(_child("second", wall=.28), _context("second"), lambda *_: "done")


def test_unknown_terminal_output_usage_keeps_its_original_reservation(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "unknown-usage.sqlite")
    DurableContinuationService(repository).register_root(
        "root", _root_budget(width=2, tokens=10),
    )
    unknown = SubagentUsage(steps=1, wall_seconds=.01)
    repository.create(DurableChildSession(
        _child("legacy", tokens=6), ChildSessionLineage("root"),
        status=SubagentStatus.SUCCEEDED, usage=unknown,
        result=SubagentResult(
            "legacy", "root", SubagentStatus.SUCCEEDED, output="done", usage=unknown,
        ),
    ))
    with pytest.raises(InvalidSubagentRequest, match="max_output_tokens"):
        repository.create(DurableChildSession(
            _child("next", tokens=5), ChildSessionLineage("root"),
        ))


def test_short_text_cannot_refund_unmetered_model_tokens(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "underestimated-tokens.sqlite")
    service = DurableContinuationService(repository)
    service.register_root("root", _root_budget(width=2, tokens=10))
    finished = service.spawn(
        _child("unmetered", tokens=9), _context("token-estimate"), lambda *_: "x",
    ).result(3)
    assert finished.status is SubagentStatus.SUCCEEDED
    assert finished.usage.output_tokens == 1  # Text size does not prove model use.
    with pytest.raises(InvalidSubagentRequest, match="max_output_tokens"):
        repository.create(DurableChildSession(
            _child("next", tokens=2), ChildSessionLineage("root"),
        ))


def test_nested_token_reservation_stays_inside_parent_block_only_once(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "nested-tokens.sqlite")
    service = DurableContinuationService(repository)
    service.register_root("root", _root_budget(width=3, tokens=10))
    assert service.spawn(
        _child("parent", tokens=8, wall=8, steps=8),
        _context("parent"), lambda *_: "p",
    ).result(3).status is SubagentStatus.SUCCEEDED
    assert service.spawn(
        _child("grandchild", parent="parent", tokens=6),
        _context("grandchild"), lambda *_: "c",
    ).result(3).status is SubagentStatus.SUCCEEDED
    assert service.spawn(
        _child("sibling", tokens=2), _context("sibling"), lambda *_: "s",
    ).result(3).status is SubagentStatus.SUCCEEDED
    with pytest.raises(InvalidSubagentRequest, match="max_output_tokens"):
        service.spawn(_child("excess", tokens=1), _context("excess"), lambda *_: "e")


def test_terminal_status_without_result_keeps_full_unknown_step_reservation(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "missing-receipt.sqlite")
    DurableContinuationService(repository).register_root(
        "root", _root_budget(width=2, steps=10),
    )
    repository.create(DurableChildSession(
        _child("legacy", steps=6), ChildSessionLineage("root"),
        status=SubagentStatus.SUCCEEDED,
    ))
    with pytest.raises(InvalidSubagentRequest, match="max_steps"):
        repository.create(DurableChildSession(
            _child("next", steps=5), ChildSessionLineage("root"),
        ))


def test_recoverable_child_cannot_resume_over_a_live_root_slot(tmp_path):
    path = tmp_path / "resume-slot.sqlite"
    first = DurableContinuationService(SQLiteDurableContinuationRepository(path))
    second = DurableContinuationService(SQLiteDurableContinuationRepository(path))
    first.register_root("root", _root_budget(width=1))
    def fails(*_):
        raise RuntimeError("recoverable failure")

    assert first.spawn(_child("failed"), _context("failure"), fails).result(3).status is SubagentStatus.FAILED
    entered, release = Event(), Event()
    def held(*_):
        entered.set()
        assert release.wait(3)
        return "done"

    handle = second.spawn(_child("running"), _context("running"), held)
    try:
        assert entered.wait(2)
        with pytest.raises(InvalidSubagentRequest, match="concurrency"):
            first.resume("failed", _context("resume"), lambda *_: "wrong")
        assert first._repository.get("failed").status is SubagentStatus.FAILED
    finally:
        release.set()
    assert handle.result(3).status is SubagentStatus.SUCCEEDED
    assert first.resume("failed", _context("resume-after"), lambda *_: "done").result(3).status is SubagentStatus.SUCCEEDED


def test_nested_budget_is_charged_within_parent_envelope_and_checkpoint_guarded(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "nested.sqlite")
    service = DurableContinuationService(repository)
    service.register_root("root", _root_budget(width=3, steps=10, tokens=10, wall=10))
    parent = repository.create(DurableChildSession(
        _child("parent", steps=8, tokens=8, wall=8), ChildSessionLineage("root"),
    ))
    child = repository.create(DurableChildSession(
        _child("grandchild", parent="parent", steps=5, tokens=5, wall=5),
        ChildSessionLineage("parent", ("root",)),
    ))
    assert child.status is SubagentStatus.CREATED
    assert parent.status is SubagentStatus.CREATED
    with pytest.raises(InvalidSubagentRequest, match="max_steps"):
        repository.create(DurableChildSession(_child("sibling", steps=3), ChildSessionLineage("root")))
    for step in range(3):
        assert repository.save_checkpoint(
            ContinuableCheckpoint("parent", step, {"step": step}), expected_sequence=step - 1,
        ) is not None
    assert repository.save_checkpoint(
        ContinuableCheckpoint("parent", 3, {"step": 3}), expected_sequence=2,
    ) is None
    assert repository.get("parent").checkpoint.sequence == 2


def test_parent_cannot_report_success_after_its_child_reserves_remaining_wall(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "active-parent-wall.sqlite")
    service = DurableContinuationService(repository)
    service.register_root("root", _root_budget(width=3, wall=20))
    started, release = Event(), Event()
    def parent_runner(*_):
        started.set()
        assert release.wait(3)
        Event().wait(.04)
        return "done"

    parent = service.spawn(
        _child("parent", width=2, wall=.15, steps=8, tokens=8), _context("parent"), parent_runner,
    )
    try:
        assert started.wait(2)
        repository.create(DurableChildSession(
            _child("grandchild", parent="parent", wall=.14, steps=1, tokens=1),
            ChildSessionLineage("parent", ("root",)),
        ))
    finally:
        release.set()
    result = parent.result(3)
    assert result.status is SubagentStatus.TIMED_OUT
    assert result.error.code == "budget_exhausted"
    assert result.usage.wall_seconds >= .04
    with pytest.raises(InvalidSubagentRequest, match="max_wall_seconds"):
        repository.create(DurableChildSession(
            _child("later-grandchild", parent="parent", wall=.1, steps=1, tokens=1),
            ChildSessionLineage("parent", ("root",)),
        ))


@pytest.mark.parametrize("owned,task", [(True, ""), (False, "same-task")])
def test_two_registry_instances_cannot_doublebook_one_ownership_scope(tmp_path, owned, task):
    path = tmp_path / "ownership.sqlite"
    repository = SQLiteDurableContinuationRepository(path)
    DurableContinuationService(repository).register_root("root", _root_budget(width=2))
    repositories = [SQLiteDurableContinuationRepository(path) for _ in range(2)]

    def launch(index):
        key = f"key-{index}"
        return WorkerLaunch(
            f"worker-{index}", "root", "researcher", "local-provider", "subagent-provider", "default",
            (str(tmp_path),), ("research",),
            {"max_children": 5, "max_depth": 3, "max_concurrency": 2,
             "max_steps": 5, "max_output_tokens": 5, "max_wall_seconds": 5},
            {"max_attempts": 1}, key, key, "work", "owner",
            execution_contract=WorkerExecutionContract(
                owned_files=(str(tmp_path / "src" / "shared.py"),) if owned else (),
                task_scope=task,
            ),
        )

    rejected = []
    def reserve(index):
        try:
            return repositories[index].create(DurableChildSession(
                _request_for(launch(index)), ChildSessionLineage("root"),
            ))
        except InvalidSubagentRequest as error:
            rejected.append(str(error))
            return None

    with ThreadPoolExecutor(2) as pool:
        records = list(pool.map(reserve, range(2)))
    assert sum(record is not None for record in records) == 1, rejected
