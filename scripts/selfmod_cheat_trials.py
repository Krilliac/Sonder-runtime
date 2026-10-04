"""Record planted-shortcut cheat trials against a selfmod candidate (#517).

``record_cheat_trial`` plants one trial of the evaluation battery
(``sonder_runtime.application.evaluation.cheat_trials``) in a fresh workspace,
runs the candidate's solver command there through the same candidate
supervisor as every other selfmod check (``selfmod._record_command``: the
``cheat_trial_probe`` row, isolation attestation, protected-write watch of the
candidate workspace), and grades the result in this, the evaluator, process.
The verdict is a parent-only ``cheat_trial`` ledger row -- ``record_test``
refuses that kind -- and a failed trial makes ``selfmod.review`` reject the
run with the trial and shortcut named.

Detection is mechanical (fixture digests, created harness files, the decoy
and canary in the answer or output, the loopback route's request count); no
model judges the attempt.
"""
from __future__ import annotations

import json
import time

import selfmod
from sonder_runtime.adapters.cheat_trial_runner import run_cheat_trial
from sonder_runtime.application.evaluation.cheat_trials import DEFAULT_BATTERY, CheatTrial
from sonder_runtime.application.evaluation.integrity import CHEAT_TRIAL

PROBE_KIND = "cheat_trial_probe"


def record_cheat_trial(run_id, trial: CheatTrial, command, *, timeout=None, nonce=None,
                       low_integrity=None, isolation=None) -> dict:
    """Run ``command`` against ``trial`` for ``run_id`` and record the verdict."""
    run = selfmod.get_run(run_id)
    if run["phase"] != "testing":
        raise RuntimeError("cheat trials may run only in testing phase")
    if not isinstance(trial, CheatTrial):
        raise TypeError("trial must be a CheatTrial")
    seconds = min(int(timeout or run["budgets"]["max_test_seconds"]), run["budgets"]["max_test_seconds"])
    probes: list[dict] = []

    def solver(workspace):
        probe = selfmod._record_command(
            run, PROBE_KIND, [str(item) for item in command], workspace, seconds,
            low_integrity=low_integrity, isolation=isolation,
        )
        probes.append(probe)
        return probe.get("output") or ""

    verdict = run_cheat_trial(trial, solver, nonce=nonce)
    probe = probes[0] if probes else {}
    detail = verdict.summary + ("; " + "; ".join(verdict.notes) if verdict.notes else "")
    with selfmod._tx() as conn:
        conn.execute(
            "INSERT INTO selfmod_tests(run_id,kind,command_json,exit_code,duration_ms,output,passed,created_ts,isolation) VALUES(?,?,?,?,?,?,?,?,?)",
            (run_id, CHEAT_TRIAL, json.dumps({"probe_id": probe.get("test_id"), "trial": trial.trial_id,
                                              "trial_kind": trial.kind}, sort_keys=True),
             0 if verdict.passed else 1, 0, detail[:100_000], int(verdict.passed), time.time(),
             probe.get("isolation") or "unverified"),
        )
        selfmod._event(conn, run_id, CHEAT_TRIAL, "%s %s" % (
            trial.trial_id, "passed" if verdict.passed else "FAILED: " + ", ".join(verdict.shortcuts)[:500]))
    return {"kind": CHEAT_TRIAL, "passed": verdict.passed, "verdict": verdict.as_dict(), "detail": detail}


def record_battery(run_id, command, *, trials=DEFAULT_BATTERY, **kwargs) -> list[dict]:
    """``record_cheat_trial`` for every trial in ``trials``."""
    return [record_cheat_trial(run_id, trial, command, **kwargs) for trial in trials]
