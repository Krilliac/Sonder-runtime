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
* A verification that never produced a verdict is not evidence either. ``ok``
  alone cannot tell "the build failed" from "there was no build system", and
  both used to arrive as signal ``failed`` -- reward -1.0, the harshest in the
  table -- against work nothing ever examined. See
  ``evaluation_infrastructure_error``.

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
    "artifact_verify": ("accepted", "rejected"),
    "ground_artifact": ("accepted", "rejected"),
    "artifact_ground": ("accepted", "rejected"),
}

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
    run_id: str = ""
    judged: set = field(default_factory=set)   # verification kinds already applied


_PENDING: list[_Pending] = []
_STATS = {"noted": 0, "attributed": 0, "expired": 0, "unlinked": 0, "unmeasured": 0}

# Where ``_format_run_result``'s header block ends and the child's own output
# begins. Everything after one of these lines is text the verified program
# chose, so nothing after them may be read as a report about the verifier.
_RENDERED_OUTPUT_HEADERS = ("stdout:", "stderr:")

# Every verdict line this codebase actually emits, keyed by the field name and
# mapping its two rendered values to a pass/fail. Derived by reading the
# renderers, not recalled -- and the count is the point: ``rendered_verdict``
# originally read the first two only, so the other four rendered a FAIL that
# it answered ``None`` for, and the caller kept its inverted ``ok=True``.
# Measured, one failing result per renderer::
#
#   server._format_run_result          "  ok: False"                    -> False
#   code_runner.format_result          "status: failed"                 -> False
#   code_runner.format_project_result  "project status: failed"         -> None
#   artifact_grounding.format_result   "artifact grounding: FAIL"       -> None
#   server.artifact_verify (inline)    "artifact verification: FAIL"    -> None
#   isolated_runner.format_result      "isolated status: failed"        -> None
#
# The four unread shapes belong to VERIFIERS -- ``run_project``,
# ``ground_artifact``/``artifact_ground``, ``artifact_verify`` and
# ``isolated_run`` -- so a failing verification by any of them was still being
# attributed as a success. ``test_every_renderer_shape_is_readable`` renders a
# failing result through each renderer and asserts this reads it, so a sixth
# renderer cannot be added without either being covered or failing that test.
_RENDERED_VERDICT_FIELDS = {
    "ok": {"true": True, "false": False},
    "status": {"ok": True, "failed": False},
    "project status": {"ok": True, "failed": False},
    "isolated status": {"ok": True, "failed": False},
    "artifact grounding": {"pass": True, "fail": False},
    "artifact verification": {"pass": True, "fail": False},
}


def evaluation_infrastructure_error(evidence) -> str:
    """Why this verification measured nothing, or "" when it really ran.

    ``harness_tools`` already knows the difference and the call site threw it
    away. Measured returns from a project with no build system::

        no build system    {"ok": False, "error": "no recognized build system..."}
        unknown framework  {"ok": False, "error": "unknown framework: nosuchfw"}
        command not found  {"ok": False, "returncode": -1, "stderr": "command not found: ..."}
        timeout            {"ok": False, "returncode": -1, "timed_out": True}
        real build failure {"ok": False, "returncode": 1, ...}

    Only the last is a statement about the work. The first four reached the
    store as ``failed`` at reward -1.0, and -- worse -- consumed the pending
    entry, so the later genuine verdict for that generation had nothing left to
    attach to.

    ``returncode: -1`` is the harness's own marker for "no process exited":
    ``_run`` uses it for both ``TimeoutExpired`` and ``FileNotFoundError`` and
    nowhere else, because a real child's status is whatever it exited with.

    Silence when there is nothing to read. A verifier that runs no process (an
    artifact validator) passes no dict, and must keep attributing exactly as
    before; reading absence as breakage would silently switch off every
    verifier this lane did not wire.
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
        return stderr[0] if stderr else "the verifier could not be run"
    return ""


def rendered_infrastructure_error(observation) -> str:
    """The same question, asked of ``_format_run_result``'s rendered text.

    The agent and autopilot lanes are the paths that actually run, and they
    never see the result dict: ``server._agent_dispatch`` returns a formatted
    string and the outcome feed receives only that. So the fix has to be
    answerable from text, or it does not reach production at all.

    Read strictly, and only from the header block. ``_format_run_result``
    emits its ``ok:``/``returncode:``/``timed_out:``/``error:`` lines first and
    then the child's own ``stdout:``/``stderr:`` blocks, and a failing test
    suite prints whatever it likes -- including lines that look exactly like an
    infrastructure report. Reading past the first header would let a genuine
    failure disguise itself as "nothing was measured", and a lost negative is
    the worse mistake here: the caller-judged population is short of failures
    already. So parsing stops at the first output header.

    ``test_the_two_predicates_agree`` pins this against
    ``evaluation_infrastructure_error`` over the same measured result dicts, so
    the two cannot drift.

    One grammar, not two. A tool that raised never produced a verdict about
    anything, and ``server._agent_dispatch`` renders that as a first line whose
    header key is ``error``. This function used to test that line against the
    literal ``ERROR:`` wire marker, which put knowledge of the server's error
    protocol inside a module whose whole design is to have no dependency on the
    server (``record_fn`` is injected for exactly that reason, and this module
    is stdlib-only). It is also a second protocol bolted beside the header
    grammar the rest of this function already reads. So the leading line is now
    read as what it is -- an ``error:`` header -- which matches every refusal
    the dispatcher emits *and* ``_format_run_result``'s own ``error:`` line,
    without naming the marker. Measured over the dispatcher's real refusal
    shapes, the verdicts are identical to the literal test, including a refusal
    whose detail is empty (``"ERROR:"``), which the header loop below cannot
    see because it reads the value and that value is blank. That case is the
    reason this is a distinct read rather than a deletion:
    ``server.isolated_run`` renders ``"ERROR: %s" % exc``, and an exception
    whose ``str()`` is empty would otherwise be attributed a verdict it never
    produced.
    """
    text = str(observation or "")
    if not text.strip():
        return ""
    leading = next(
        (line.strip() for line in text.splitlines() if line.strip()), "",
    )
    leading_key, leading_sep, _ = leading.partition(":")
    if leading_sep and leading_key.strip().casefold() == "error":
        return leading
    fields = {}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.casefold() in _RENDERED_OUTPUT_HEADERS:
            break
        key, sep, value = stripped.partition(":")
        if sep and key:
            fields.setdefault(key.strip().casefold(), value.strip())
    if fields.get("timed_out", "").casefold() == "true":
        return "the verification timed out before producing a verdict"
    if fields.get("error"):
        return fields["error"]
    if fields.get("returncode") in ("-1", "None"):
        return "the verifier could not be run"
    return ""


def rendered_verdict(observation):
    """The verifier's own pass/fail from its rendered text, or None.

    The agent path derives ``ok`` as ``not observation.startswith("ERROR:")``
    (``server._agent_dispatch_observed``), and that is not a verdict about the
    work -- measured, a pytest suite with a failing test renders
    ``"test run (pytest)\\n  ok: False\\n  returncode: 1"``, which does not
    start with ``ERROR:``, so a FAILING suite arrived here as ``ok=True`` and
    would have been filed ``tests_passed`` at +1.0. A false pass is worse than
    the false failure this lane was opened for: the whole point of this store
    is a number that can be trusted, and the caller-judged population sits near
    52%, so a manufactured success is indistinguishable from real progress.

    The renderers and their verdict lines are enumerated in
    ``_RENDERED_VERDICT_FIELDS``; all six are measured, and reading only the
    first two of them is what let a failing ``run_project``,
    ``ground_artifact`` or ``artifact_verify`` keep being attributed as a pass
    after this function already existed.

    Returns None when no such field is present, so a verifier rendered some
    other way keeps whatever the caller decided and nothing changes for it.
    """
    text = str(observation or "")
    if not text.strip():
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.casefold() in _RENDERED_OUTPUT_HEADERS:
            break
        key, sep, value = stripped.partition(":")
        if not sep:
            continue
        values = _RENDERED_VERDICT_FIELDS.get(key.strip().casefold())
        if values is None:
            continue
        verdict = values.get(value.strip().casefold())
        if verdict is not None:
            return verdict
    return None


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


def note_generation(interaction_id: str, tool: str, project: str = "", run_id: str = "") -> bool:
    """Record that `tool` produced work that a later verification can judge.

    Returns False when there is nothing judgeable -- no interaction id means no
    row to attach an outcome to.

    `run_id` is the caller's run/span identity (e.g. an agent loop's
    activity_tracker response id) when one is available. It is a stronger
    link than project + time window alone: two concurrent runs (autopilot
    spawns one thread per run) can share a project -- or both leave it blank
    -- and without a run identity the newest pending generation from either
    run looks like a valid candidate for the other's verification.
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
        _PENDING.append(
            _Pending(ident, name, str(project or ""), _now(), str(run_id or ""))
        )
        _STATS["noted"] += 1
    return True


def _candidate(project: str, kind: str, run_id: str = ""):
    """Newest pending generation eligible for this verification, or None."""
    wanted = str(project or "")
    wanted_run = str(run_id or "")
    with _LOCK:
        for pending in reversed(_PENDING):
            if kind in pending.judged:
                continue
            # An empty project on either side means "unscoped" and is allowed
            # to match; two *different* named projects never match. Run
            # identity follows the same rule: it only ever narrows a match,
            # never widens one, so a caller with no run identity available
            # (the direct-call path) behaves exactly as before.
            if wanted and pending.project and wanted != pending.project:
                continue
            if wanted_run and pending.run_id and wanted_run != pending.run_id:
                continue
            return pending
    return None


def attribute(tool: str, ok: bool, project: str = "", record_fn=None,
              run_id: str = "", evidence=None) -> dict:
    """Attribute a verification result to the generation it most likely judges.

    `record_fn(interaction_id, signal)` performs the write; injected so this
    module has no import cycle with the server and is trivially testable.
    `evidence` is the verifier's own result dict, or the rendered observation
    the agent path has instead of one, read only to tell a verdict from the
    absence of one. Returns a report describing what happened, including when
    nothing did.
    """
    name = str(tool or "").strip().lstrip("/")
    if name not in VERIFIERS:
        return {"attributed": False, "reason": "%s is not execution-grounded evidence" % name}
    # Asked BEFORE a pending generation is claimed. One verifier kind judges a
    # generation once, so a run that measured nothing must not consume the one
    # chance that generation had to be judged for real -- measured, an
    # infrastructure blip permanently displaced the later genuine verdict.
    infrastructure_error = (
        evaluation_infrastructure_error(evidence)
        if isinstance(evidence, dict)
        else rendered_infrastructure_error(evidence)
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
    pending = _candidate(project, name, run_id)
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
