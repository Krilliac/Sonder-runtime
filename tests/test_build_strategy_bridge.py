"""The build-fix strategy bridge over the real ``StrategyController`` (F11).

The loop keeps its best candidate by the lexicographic ``progress_key``; the
controller judges the same measurement by dominance over
``to_strategy_metrics``. Both readings are recorded, and the controller's
action is mapped onto the fix's small action set.
"""
from __future__ import annotations

import hashlib

import pytest

pytest.importorskip("sonder_runtime.domain.build.repair", reason="needs lane A-domain-build")

from sonder_runtime.application.build.fix_ports import FixDecision  # noqa: E402
from sonder_runtime.application.build.strategy_bridge import (  # noqa: E402
    StrategyFixAdapter,
    lexicographic_assessment,
    strategy_vector,
)
from sonder_runtime.domain.build.repair import BuildProgress, FixAttemptRecord  # noqa: E402
from sonder_runtime.domain.strategy.models import StrategyError  # noqa: E402

pytestmark = pytest.mark.unit

OBJECTIVE = hashlib.sha256(b"fix core").hexdigest()


def progress(errors, *, failed=1, warnings=0, ran=True, complete=True, focus=0, link=0):
    return BuildProgress(build_ran=ran, complete=complete, errors_total=errors, focus_errors=focus,
                         failed_units=failed, link_errors=link, new_warnings=warnings)


def attempt(n):
    return FixAttemptRecord(n=n, files=("src/core/math.cpp",), outcome="x", progress=None)


def adapter(**kwargs):
    item = StrategyFixAdapter(**kwargs)
    item.begin("build-fix-" + "c" * 32, OBJECTIVE, attempts=4, max_model_calls=12)
    return item


def h(n):
    return hashlib.sha256(b"hypothesis-%d" % n).hexdigest()


def test_dominance_regressed_even_when_the_key_improved_and_both_are_recorded():
    bridge = adapter()
    before = progress(3)
    after = progress(1, warnings=1)  # errors fixed, a warning appeared
    decision = bridge.observe(attempt(1), before=before, after=after, failure="BUILD_FAILURE",
                              hypothesis_digest=h(1), focus="src/core/math.cpp", model_calls=1,
                              verifier_calls=2)
    assert isinstance(decision, FixDecision)
    assert decision.dominance == "regressed" and decision.lexicographic == "improved"
    assert decision.controller_action == "rollback" and decision.action == "rollback"
    record = bridge.records[-1]
    assert (record.dominance, record.lexicographic) == ("regressed", "improved")


def test_plain_improvement_keeps_repairing():
    bridge = adapter()
    decision = bridge.observe(attempt(1), before=progress(3), after=progress(1),
                              failure="BUILD_FAILURE", hypothesis_digest=h(1), model_calls=1)
    assert (decision.dominance, decision.lexicographic) == ("improved", "improved")
    assert decision.action == "repair"


def test_a_rejected_hypothesis_asks_for_inspection():
    bridge = adapter()
    decision = bridge.observe(attempt(1), before=progress(2), after=progress(2),
                              failure="HYPOTHESIS_REJECTED", hypothesis_digest=h(1), model_calls=1)
    assert decision.action == "inspect"


def test_no_progress_is_a_stall_that_asks_for_a_critic_or_another_model():
    bridge = adapter()
    decision = bridge.observe(attempt(1), before=progress(2), after=progress(2),
                              failure="NO_PROGRESS", hypothesis_digest=h(1), model_calls=1)
    assert decision.action == "critic"
    switch = adapter(critic_available=False, switch_model_available=True)
    decision = switch.observe(attempt(1), before=progress(2), after=progress(2),
                              failure="NO_PROGRESS", hypothesis_digest=h(1), model_calls=1)
    assert decision.action == "switch_model"


@pytest.mark.parametrize("failure,controller", [
    ("PERMISSION_DENIED", "pause"),
    ("UNCERTAIN_SIDE_EFFECT", "reconcile"),
])
def test_pause_and_reconcile_become_fail_with_their_reason(failure, controller):
    bridge = adapter()
    decision = bridge.observe(attempt(1), before=progress(2), after=progress(2), failure=failure,
                              hypothesis_digest=h(1), model_calls=1)
    assert decision.controller_action == controller
    assert decision.action == "fail" and decision.reason.startswith(controller + ":")


def test_an_exhausted_budget_fails():
    bridge = StrategyFixAdapter()
    bridge.begin("build-fix-" + "d" * 32, OBJECTIVE, attempts=4, max_model_calls=2)
    for n in (1, 2):
        decision = bridge.observe(attempt(n), before=progress(4 - n), after=progress(3 - n),
                                  failure="BUILD_FAILURE", hypothesis_digest=h(n), model_calls=1)
    decision = bridge.observe(attempt(3), before=progress(1), after=progress(1),
                              failure="BUILD_FAILURE", hypothesis_digest=h(3), model_calls=1)
    assert decision.action == "fail" and "budget" in decision.reason


def test_incomplete_or_absent_measurements_are_incomparable():
    assert strategy_vector(progress(1, ran=False), OBJECTIVE) is None
    vector = strategy_vector(progress(1, complete=False), OBJECTIVE)
    assert vector is not None and not vector.complete
    assert lexicographic_assessment(None, progress(1)) == "incomparable"
    bridge = adapter()
    decision = bridge.observe(attempt(1), before=progress(2), after=progress(1, complete=False),
                              failure="BUILD_FAILURE", hypothesis_digest=h(1), model_calls=1)
    assert decision.dominance == "incomparable" and decision.lexicographic == "regressed"


def test_misuse_is_refused():
    bridge = StrategyFixAdapter()
    with pytest.raises(StrategyError):
        bridge.observe(attempt(1), before=None, after=None, failure="BUILD_FAILURE")
    bridge.begin("build-fix-" + "e" * 32, OBJECTIVE)
    with pytest.raises(StrategyError):
        bridge.observe(attempt(1), before=None, after=None, failure="SOMETHING_ELSE")
    with pytest.raises(ValueError):
        FixDecision(action="pause")


def test_begin_resets_history():
    bridge = adapter()
    bridge.observe(attempt(1), before=progress(2), after=progress(2), failure="NO_PROGRESS",
                   hypothesis_digest=h(1), model_calls=1)
    bridge.begin("build-fix-" + "f" * 32, OBJECTIVE)
    assert bridge.records == ()
