"""The nightly stage that actually changes Sonder's own source.

Everything else in the nightly cycle exercises the model and grooms the stores:
it records outcomes, refreshes embeddings, prunes duplicates, queues proposals.
None of it edits a source file, by design. So after seven nights the learning
store had grown by 4213 interactions and the code had not changed by one line.

Meanwhile the machinery to change it already existed and was simply never
called. `selfmod` creates a detached Git worktree, applies a candidate there,
runs real test commands against it, and on deploy copies the files back and
records `git commit -m "selfmod: <objective>"`, with an immutable backup and a
rollback path. Five runs had been created by hand; none ever deployed. One of
them -- "permission_rules.load silently degrades to default rules" -- named a
real defect that a human later fixed independently, which is the clearest
possible evidence the loop was finding things and dropping them on the floor.

This module closes that gap: it drives one full lifecycle per night.

WHAT IT WILL NOT DO
  - It never bypasses the configured mode. Under `propose` (the default) a
    candidate stops at `reviewing` and waits for a human `/selfmod approve`.
    Only `auto-low-risk` lets this stage approve and deploy unattended, and
    that is the operator's switch to flip, not this script's.
  - It refuses to start on a dirty tree. selfmod's own deploy path declines to
    commit when the run began with uncommitted changes, so a run started dirty
    could only ever produce an uncommitted edit -- worse than nothing, because
    it mutates source with no commit to review or revert to.
  - It never touches a protected path, never merges, and never pushes.

WHY THE TEST COMMAND IS THE WHOLE SUITE
  Tonight's evidence: an agent lane produced four plausible fixes whose tests
  had never been executed, and running them revealed one broke an architecture
  rule. A candidate that changes source unattended has to clear the same bar
  the humans do, and a targeted subset cannot show what a change broke
  elsewhere.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import selfmod  # noqa: E402

# Files the nightly stage is allowed to propose changes to. Deliberately narrow
# and deliberately NOT server.py: a 14k-line module that every lane touches is
# the worst possible place for an unattended edit, and the point of the first
# night is to prove the loop works, not to maximise its reach.
CANDIDATE_FILES = (
    "reflection.py",
    "memory_quality.py",
    "reward.py",
    "promotion_eval.py",
    "context_policy.py",
    "summarizer.py",
)

_FENCE = re.compile(r"^\s*```[a-zA-Z0-9_+-]*\s*$", re.M)


def _ask(server, prompt, num_predict=1200):
    reply = server.ensemble_answer(
        prompt, tiers="code", num_predict=num_predict, mode="code")
    text = (reply or "").strip()
    # ensemble_answer reports failure by RETURNING prose rather than raising.
    # Splicing that into a source file is how a dead backend becomes a code
    # change, so it is an abort condition, not a candidate.
    if text.startswith("ERROR:") or "no model produced an answer" in text[:200]:
        raise RuntimeError("model unavailable: %s" % text[:160])
    return _FENCE.sub("", text).strip()


def propose_objective(server, log) -> tuple[str, str] | None:
    """Ask the local model for ONE small, concrete improvement.

    Grounded in a real file's real contents, never from memory: asked to
    improve code it cannot see, this model class invents plausible defects in
    functions that do not exist. The file is chosen by rotation rather than by
    the model, so a single unlucky answer cannot steer every future night at
    the same target.
    """
    import random

    for name in random.sample(CANDIDATE_FILES, k=len(CANDIDATE_FILES)):
        path = REPO / name
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        if len(source) > 60_000:
            continue
        answer = _ask(server, (
            "Here is one Python module from a local AI runtime.\n\n"
            "Find ONE small, concrete defect or clear improvement in it. Good\n"
            "candidates: a guard that silently swallows a failure, a count that\n"
            "is reported as a total when it is really a bounded window, a\n"
            "docstring that contradicts the code, a missing edge case.\n\n"
            "Reply with exactly two lines and nothing else:\n"
            "OBJECTIVE: <one sentence, imperative>\n"
            "WHY: <one sentence naming the concrete wrong behaviour>\n\n"
            "If the module has no such defect, reply exactly: NONE\n\n"
            "=== %s ===\n%s" % (name, source[:60_000])
        ), num_predict=300)
        if answer.strip().upper().startswith("NONE"):
            log("  %s: model reports no defect" % name)
            continue
        objective = why = ""
        for line in answer.splitlines():
            if line.upper().startswith("OBJECTIVE:"):
                objective = line.split(":", 1)[1].strip()
            elif line.upper().startswith("WHY:"):
                why = line.split(":", 1)[1].strip()
        if objective:
            return name, "%s (%s)" % (objective, why or "no rationale given")
    return None


def run(server, log, *, test_timeout=1800, branch=True):
    """Drive one selfmod lifecycle.

    branch=True commits a verified candidate to its own selfmod/<run-id>
    branch from inside the worktree and never writes the main tree; that
    is the continuous-run mode. branch=False follows the configured
    selfmod mode instead, which under auto-low-risk deploys into the
    working tree.
    """
    settings = selfmod.settings()
    if not settings.get("enabled"):
        return "selfmod disabled in settings"
    mode = settings.get("mode") or "propose"
    if mode == "observe":
        return "mode=observe (observation only, no candidate created)"

    # A dirty tree cannot produce a committed improvement -- selfmod's deploy
    # refuses to commit when the run started with uncommitted changes -- so
    # starting one would at best mutate source with nothing to review.
    ok, _commit, status = selfmod._git_info(REPO)
    if not ok:
        return "not a Git work tree; refusing to self-modify without a rollback point"
    if status.strip():
        return ("working tree dirty (%d path(s)); a run started here could not "
                "be committed, so none was started" % len(status.splitlines()))

    proposed = propose_objective(server, log)
    if not proposed:
        return "no objective proposed"
    target, objective = proposed
    log("  objective: %s" % objective[:160])

    run_id = selfmod.create_plan(
        objective, str(REPO),
        problem=objective,
        # create_plan refuses a proposal with no evidence, and rightly:
        # an objective with nothing behind it is exactly the plausible-
        # sounding invention this model class produces when asked to
        # improve code from memory. The evidence is the model's own
        # rationale plus the file it was actually shown.
        evidence=[
            objective,
            "proposed by the local model against the current contents of %s"
            % target,
        ],
        files=[target],
        criteria=["the full test suite passes", "ruff is clean"],
        risk="low",
        expected_benefit="nightly autonomous improvement",
        rollback_plan="selfmod rollback restores the immutable backup",
    )
    run_id = run_id["id"] if isinstance(run_id, dict) else run_id
    log("  run: %s  target: %s" % (run_id, target))

    # The lifecycle refuses a workspace without a verified backup, which is
    # the whole basis of rollback: no restore point, no isolated edit.
    selfmod.create_backup(run_id)
    selfmod.verify_backup(run_id)
    selfmod.prepare_workspace(run_id)
    workspace = selfmod.candidate_path(run_id)
    original = (workspace / target).read_text(encoding="utf-8", errors="replace")

    edited = _ask(server, (
        "Rewrite this Python module to accomplish exactly this objective, and\n"
        "nothing else:\n\n    %s\n\n"
        "Rules:\n"
        "- Output the COMPLETE file. No markdown fence, no commentary.\n"
        "- Change as little as possible. Do not reformat untouched code.\n"
        "- Keep every public name and signature.\n"
        "- Add a brief comment where you changed something, saying WHY.\n\n"
        "=== %s ===\n%s" % (objective, target, original)
    ), num_predict=8000)

    # The shrink floor, for the same reason it exists in codegen_loop: an
    # unguarded repair converges on deletion, because removing the offending
    # code is always a valid way to satisfy a checker. Measured elsewhere in
    # this repo at 44% and 13% of a file returned to fix two typos.
    if len(edited) < 0.75 * len(original):
        selfmod.cancel(run_id)
        return ("candidate rejected: returned %d%% of the original file "
                "(deletion guard)" % (100 * len(edited) // max(1, len(original))))

    selfmod.apply_candidate_changes(run_id, {target: edited})
    diff = selfmod.inspect_diff(run_id)
    if not diff.get("changed_files"):
        selfmod.cancel(run_id)
        return "candidate made no change"

    selfmod.begin_testing(run_id)
    py = str(REPO / "venv" / "Scripts" / "python.exe")
    results = []
    for kind, command in (
        ("lint", [py, "-m", "ruff", "check", target]),
        ("unit", [py, "-m", "pytest", "-q"]),
    ):
        outcome = selfmod.record_test(
            run_id, kind, command, cwd=str(workspace), timeout=test_timeout)
        passed = bool(outcome.get("passed")) if isinstance(outcome, dict) else bool(outcome)
        results.append((kind, passed))
        log("  %s: %s" % (kind, "pass" if passed else "FAIL"))
        if not passed:
            selfmod.reject(run_id, reason="%s failed" % kind)
            return "candidate rejected: %s failed (run %s kept for inspection)" % (kind, run_id)

    selfmod.review(run_id)

    if branch:
        # Commit INSIDE the worktree, onto its own branch. Strictly safer than
        # deploying: the main working tree is never written, so a live session
        # keeps its checkout and its uncommitted work, and every candidate is
        # an independent branch off HEAD that can be read, cherry-picked or
        # deleted without touching anything. `deploy` remains the path that
        # actually installs a change; this path only makes one reviewable.
        name = "selfmod/%s" % run_id
        code, out = selfmod._git(workspace, "checkout", "-B", name)
        if code:
            return "candidate tested but branch checkout failed: %s" % out[:160]
        code, out = selfmod._git(workspace, "add", "--", *diff["changed_files"])
        if code:
            return "candidate tested but staging failed: %s" % out[:160]
        code, out = selfmod._git(
            workspace, "commit", "-m", "selfmod: %s" % objective[:100])
        if code:
            return "candidate tested but commit failed: %s" % out[:160]
        _, sha = selfmod._git(workspace, "rev-parse", "--short", "HEAD")
        return "COMMITTED %s to %s (%s) -- review: git log -p %s" % (
            sha.strip(), name, target, name)

    if mode != "auto-low-risk":
        return ("candidate READY for review: %s -- %s | approve with "
                "/selfmod approve %s" % (run_id, target, run_id))

    selfmod.approve(run_id, approver="nightly")
    selfmod.deploy(run_id)
    run = selfmod.get_run(run_id)
    return "DEPLOYED %s to %s (commit %s)" % (
        run_id, target, (run.get("deployed_commit") or "")[:10] or "none")


def main() -> int:
    import server
    def log(message):
        print(message, flush=True)
    log(json.dumps(selfmod.settings()))
    log(run(server, log))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
