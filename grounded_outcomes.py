"""Attribute verification results back to the work that produced them.

The outcome store is not short of data -- it holds ~9,200 rows -- it is short of
*honest* data. 8,883 of those are ``tests_passed`` from the self-graded
curriculum. The population that actually measures delegated work, the
caller-judged one, is 101 good against 91 rejected: 192 rows, 52.6%. Averaging
the two produces a number that reads like accuracy and is not one, which is why
``record_outcome``'s own docstring keeps them apart.

The reason the honest population is thin is that recording an outcome is a
manual step performed by whoever called the tool, and people record success far
more readily than failure. Asking harder does not fix a reporting bias; removing
the human from the report does.

Sonder already runs tools that know the truth. ``test_run`` knows whether tests
passed. ``build_run`` knows whether it compiled. ``lint_run``, ``typecheck_run``
and ``run_code`` all return a verdict. When one of those runs shortly after a
generation, its result *is* the outcome of that generation -- execution-grounded
evidence, which the reward table already weights highest, obtained without
anyone remembering to file it.

This module keeps a short-lived ledger of generations awaiting judgement and
attributes the next relevant verification to them.

Deliberate limits
-----------------
* Attribution needs a *plausible* link, not a certain one, so it is bounded by
  time, by project, and to one signal per generation per verification kind.
  A wrong attribution is worse than a missing one -- it poisons the very
  population this exists to clean up -- so every rule here fails toward
  recording nothing.
* Only genuinely execution-grounded tools may attribute. A tool whose "success"
  means "it ran", not "the work was good", is not evidence.
* A verification that never produced a verdict is not evidence either. A
  timeout, a missing toolchain, or an unrecognised build system is a fact about
  this machine, not about the work, and it is recorded as neither good nor bad
  -- the third state is *unmeasured*. ``promotion_eval`` already refuses a
  promotion decision on the same grounds and calls it
  ``evaluation_infrastructure_error``; this is that idea, one layer down.
  Reading that state off a result dict is per-runner work, not one shared rule:
  ``code_runner`` says ``error`` for a compiler it could not find *and* for a
  compilation that genuinely failed, so a predicate tuned to ``harness_tools``
  would drop the second as unmeasured. Dropping a real failure is the more
  expensive mistake -- failures are the scarce half of the honest population --
  so each runner gets a predicate that reads its own shape.

Stdlib only.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

# How long a generation stays eligible for judgement. Long enough for a build
# or test cycle, short enough that an unrelated later run cannot claim it.
ATTRIBUTION_WINDOW_SECONDS = 900.0

# Never let the ledger grow without bound in a long-lived server.
MAX_PENDING = 64

# Tools whose verdict is real evidence about generated work, and what their
# pass/fail means in the existing signal vocabulary. `compiled` (+0.70) sits
# below the good threshold of 0.71 on purpose: building is not passing.
VERIFIERS = {
    "test_run": ("tests_passed", "failed"),
    "build_run": ("compiled", "failed"),
    "typecheck_run": ("compiled", "failed"),
    "lint_run": ("compiled", "failed"),
    "run_code": ("compiled", "failed"),
    "run_project": ("compiled", "failed"),
    "isolated_run": ("compiled", "failed"),
    "codegen_build_loop": ("compiled", "failed"),
    # File-format validators. Their verdict is a program's, so it belongs in the
    # execution-grounded vocabulary like every other entry here. It was
    # ("accepted", "rejected") -- signals `calibration.CALLER_JUDGED` counts --
    # which quietly filed a machine's opinion of a .docx into the population
    # that answers "did a caller accept the delegated work", moving the
    # `should_verify` and `_status` gates with no human having judged anything.
    # `compiled` is the honest fit: well-formed is not the same as right, which
    # is why that signal sits below the good threshold.
    "artifact_verify": ("compiled", "failed"),
    "ground_artifact": ("compiled", "failed"),
    "artifact_ground": ("compiled", "failed"),
}

# Verifiers backed by ``code_runner``, whose result dict overloads ``error``.
# ``_run_rust`` stamps "rust compilation failed" onto a result whose process
# really did run and really did reject the code (code_runner.py:460), so the
# ``error``-first reading that is correct for ``harness_tools`` would file a
# genuine compile failure as unmeasured -- discarding the negative evidence
# this store is measurably short of. These use their own predicate.
#
# ``isolated_run`` is deliberately NOT here: ``isolated_runner._run_bounded``
# puts the container's own exit status in ``returncode`` and reserves ``error``
# for the reasons the runner itself stopped the run (no engine, output cap,
# time cap, unverified cleanup), so the ``error``-first predicate is right for
# it -- and is the only correct one, because a container killed by the time cap
# still carries the killed process's integer returncode.
CODE_RUNNER_VERIFIERS = frozenset({"run_code", "run_project"})

# Tools that produce work worth judging later.
GENERATORS = frozenset({
    "sonder", "offload", "agent", "workbench_agent", "improve_function",
    "codegen_build_loop", "artifact_generate", "scaffold_project",
    "game_generate_and_test", "ensemble_answer", "consult",
    "apply_learned", "file_write", "file_batch_write", "file_edit",
    "text_patch", "json_patch", "apply_patch", "rename_symbol",
})

_LOCK = threading.RLock()


@dataclass
class _Pending:
    interaction_id: str
    tool: str
    project: str
    created: float
    judged: set = field(default_factory=set)   # verification kinds already applied


_PENDING: list[_Pending] = []
_STATS = {"noted": 0, "attributed": 0, "expired": 0, "unlinked": 0, "unmeasured": 0}


def evaluation_infrastructure_error(evidence) -> str:
    """Why this verification measured nothing, or "" when it really ran.

    ``harness_tools`` already knows the difference and throws it away at the
    call site. A timeout and a missing binary both come back as
    ``returncode: -1`` (``MAX_TIMEOUT`` is a hard 120s clamp, so any suite
    slower than two minutes always times out), and the build-system detector
    returns an ``error`` without spawning anything at all. Each of those used to
    reach the store as signal ``failed`` -- reward -1.0, the harshest in the
    table -- against work that was never examined.

    Silence when there is no evidence to read: a verifier that runs no process
    (an artifact validator, say) passes none, and must keep attributing.
    """
    if not isinstance(evidence, dict):
        return ""
    if evidence.get("timed_out"):
        return "the verification timed out before producing a verdict"
    error = evidence.get("error")
    if error:
        return str(error)
    if evidence.get("returncode") == -1:
        stderr = str(evidence.get("stderr") or "").strip().splitlines()
        detail = stderr[0] if stderr else "the verifier could not be run"
        return detail
    return ""


def code_runner_infrastructure_error(evidence) -> str:
    """Why ``run_code``/``run_project`` measured nothing, or "" when code ran.

    ``code_runner`` uses ``error`` for two opposite things, which is the whole
    difficulty here. It is the runner reporting that it could not start -- no
    interpreter, no compiler, no time left -- and it is also, once,
    ``_run_rust`` labelling a compilation that genuinely failed. Reading
    ``error`` first would turn that real verdict into "nothing was measured",
    and a lost failure is worse than a lost success: the caller-judged
    population is 101 good to 91 rejected only because failures are already the
    hard half to collect.

    ``returncode`` separates them honestly, because ``code_runner`` only ever
    reports one when a process exited. ``_error_result`` and ``_timeout_result`` --
    every missing-toolchain path, every timeout, ``/runwindow`` off Windows --
    carry ``returncode: None``, because no process ever produced one.
    ``_completed_result`` carries the integer a process exited with, and the
    rust overload is written onto one of those. So an ``error`` beside an
    integer returncode is the code's verdict; an ``error`` without one is the
    runner's.

    Known limit: a toolchain that starts and then fails for its own reasons --
    a broken vcvars where ``cl`` is missing inside the batch file, a dotnet SDK
    that cannot restore -- exits nonzero like a rejected program and is
    recorded as ``failed``. That is the direction to be wrong in; the opposite
    mistake would silently drop real negative evidence.
    """
    if not isinstance(evidence, dict):
        return ""
    steps = evidence.get("steps")
    if isinstance(steps, list):
        return _project_infrastructure_error(evidence, steps)
    if evidence.get("timed_out"):
        return "the run timed out before producing a verdict"
    error = str(evidence.get("error") or "").strip()
    if not error:
        return ""
    returncode = evidence.get("returncode")
    if isinstance(returncode, int) and not isinstance(returncode, bool):
        # A process ran and exited. Whatever `error` says, this is a verdict.
        return ""
    return error


def _project_infrastructure_error(evidence, steps) -> str:
    """``run_project``'s composite result hides its evidence one level down.

    The top level is ``{ok, files, steps, timeout}`` -- no ``error``, no
    ``returncode`` -- so a predicate reading only the top level sees a clean
    result no matter what happened. ``run_project`` stops at the first step
    that fails, so the step that stopped it is the one holding the evidence.
    """
    if evidence.get("ok"):
        return ""
    for step in reversed(steps):
        result = step.get("result") if isinstance(step, dict) else None
        if isinstance(result, dict) and not result.get("ok"):
            return code_runner_infrastructure_error(result)
    return "the project run produced no step result to judge"


def _now() -> float:
    return time.monotonic()


def _prune(now: float | None = None) -> None:
    now = _now() if now is None else now
    with _LOCK:
        before = len(_PENDING)
        _PENDING[:] = [
            p for p in _PENDING
            if now - p.created <= ATTRIBUTION_WINDOW_SECONDS
        ]
        _STATS["expired"] += before - len(_PENDING)
        if len(_PENDING) > MAX_PENDING:
            dropped = len(_PENDING) - MAX_PENDING
            del _PENDING[:dropped]
            _STATS["expired"] += dropped


def note_generation(interaction_id: str, tool: str, project: str = "") -> bool:
    """Record that `tool` produced work that a later verification can judge.

    Returns False when there is nothing judgeable -- no interaction id means no
    row to attach an outcome to.
    """
    ident = str(interaction_id or "").strip()
    name = str(tool or "").strip().lstrip("/")
    if not ident or name not in GENERATORS:
        return False
    _prune()
    with _LOCK:
        # A repeat generation for the same interaction replaces the old entry
        # rather than stacking, so one id cannot collect several verdicts.
        _PENDING[:] = [p for p in _PENDING if p.interaction_id != ident]
        _PENDING.append(_Pending(ident, name, str(project or ""), _now()))
        _STATS["noted"] += 1
    return True


def _candidate(project: str, kind: str):
    """Newest pending generation eligible for this verification, or None."""
    wanted = str(project or "")
    with _LOCK:
        for pending in reversed(_PENDING):
            if kind in pending.judged:
                continue
            # An empty project on either side means "unscoped" and is allowed
            # to match; two *different* named projects never match.
            if wanted and pending.project and wanted != pending.project:
                continue
            return pending
    return None


def attribute(tool: str, ok: bool, project: str = "", record_fn=None,
              evidence=None) -> dict:
    """Attribute a verification result to the generation it most likely judges.

    `record_fn(interaction_id, signal)` performs the write; injected so this
    module has no import cycle with the server and is trivially testable.
    `evidence` is the verifier's own result dict, read only to tell a verdict
    from the absence of one. Returns a report describing what happened,
    including when nothing did.
    """
    name = str(tool or "").strip().lstrip("/")
    if name not in VERIFIERS:
        return {"attributed": False, "reason": "%s is not execution-grounded evidence" % name}
    # Checked before a pending generation is claimed: an unmeasured run must not
    # consume the one chance that generation had to be judged for real.
    infrastructure_error = (
        code_runner_infrastructure_error(evidence)
        if name in CODE_RUNNER_VERIFIERS
        else evaluation_infrastructure_error(evidence)
    )
    if infrastructure_error:
        with _LOCK:
            _STATS["unmeasured"] += 1
        return {
            "attributed": False,
            "reason": "%s produced no verdict, so there is nothing to attribute" % name,
            "evaluation_infrastructure_error": infrastructure_error,
        }
    _prune()
    pending = _candidate(project, name)
    if pending is None:
        with _LOCK:
            _STATS["unlinked"] += 1
        return {"attributed": False, "reason": "no recent generation to judge"}

    good_signal, bad_signal = VERIFIERS[name]
    signal = good_signal if ok else bad_signal
    with _LOCK:
        pending.judged.add(name)
        _STATS["attributed"] += 1
    report = {
        "attributed": True,
        "interaction_id": pending.interaction_id,
        "signal": signal,
        "verifier": name,
        "generator": pending.tool,
        "age_seconds": round(_now() - pending.created, 1),
    }
    if record_fn is not None:
        try:
            record_fn(pending.interaction_id, signal)
            report["recorded"] = True
        except Exception as exc:            # a failed write must not break the run
            report["recorded"] = False
            report["error"] = str(exc)
    return report


def pending_count() -> int:
    _prune()
    with _LOCK:
        return len(_PENDING)


def stats() -> dict:
    with _LOCK:
        return dict(_STATS)


def reset() -> None:
    """Clear all state. For tests and for a fresh session."""
    with _LOCK:
        _PENDING.clear()
        for key in _STATS:
            _STATS[key] = 0
