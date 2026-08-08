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
  Measured 2026-08-08: an agent lane produced four plausible fixes whose tests
  had never been executed, and running them revealed that one broke an
  architecture rule. A candidate that changes source unattended has to clear the same bar
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

# Files the nightly stage may propose changes to. Every entry is a module
# with its own test file, small enough that one function is a meaningful
# fraction of it, and none is a hot path every lane touches -- server.py is
# deliberately absent for that reason.
#
# The list grew from six after the duplicate-objective guard started working:
# with repeats blocked, the model ran out of distinct proposals for six files
# in five passes and every later pass reported "no objective proposed". That
# is the correct outcome for an exhausted file set, and the answer is more
# material rather than a laxer guard.
CANDIDATE_FILES = (
    "reflection.py",
    "memory_quality.py",
    "reward.py",
    "promotion_eval.py",
    "context_policy.py",
    "summarizer.py",
    "ruff_verifier.py",
    "node_verifier.py",
    "json_schema_verifier.py",
    "sql_verifier.py",
    "domain_grounding.py",
    "preference_learning.py",
    "recall.py",
    "seed_merge.py",
    "store_integrity.py",
    "creative_router.py",
    "embed_cache.py",
    "ollama_endpoint.py",
    "live_reload.py",
    "self_curriculum.py",
    "eval_retrieval.py",
    "feedback.py",
    "emotion_vectors.py",
    "lesson_decay.py",
    "lesson_pruner.py",
    "goal_store.py",
    "intents.py",
    "import_autofix.py",
    "command_registry.py",
    "contribute.py",
    "game_ladder.py",
    "orchestrator.py",
    "embeddings.py",
    "codegen_loop.py",
    "grounding.py",
    "ollama_lifecycle.py",
    "reloadable_mcp.py",
    "autopilot_store.py",
    "bootstrap_engine.py",
    "media_assets.py",
    "engine_bundle.py",
    "game_forge.py",
    "pull_community.py",
    "tune_min_sim.py",
    "ooxml_assets.py",
    "qlora_train.py",
    "retriever.py",
    "runtime_policy.py",
    "safe_update.py",
    "self_heal.py",
    "solver.py",
    "sonder_doctor.py",
    "system_profile.py",
    "training_data.py",
    "training_tasks.py",
    "verifiers.py",
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


def _splice_function(original: str, reply: str):
    """Replace one top-level function in `original` with the model's version.

    Returns the new module text, or None when the reply is not a single
    function that already exists at module level -- a reply naming a function
    the file does not have is an invention, and inserting it would add code
    nobody asked for rather than change the code that was targeted.

    Indentation-based rather than AST-based on purpose: the reply frequently
    does not parse on its own (a method body arrives dedented, a decorator is
    dropped), and this only needs to find where the old block ends.
    """
    match = re.search(r"^(\s*)def\s+(\w+)\s*\(", reply, re.M)
    if not match:
        return None
    name = match.group(2)
    body = reply[match.start():].rstrip() + "\n"

    lines = original.splitlines(keepends=True)
    first = None
    for index, line in enumerate(lines):
        if re.match(r"^def\s+%s\s*\(" % re.escape(name), line):
            first = index
            break
    if first is None:
        return None

    # Consume the signature before looking for the end of the block. A
    # multi-line signature closes with `):` at COLUMN 0, so a naive "first
    # later line at column 0" scan stops on the signature's own closing paren
    # and leaves half a def behind -- which then does not parse. Balance the
    # parentheses first, then look for the next top-level line.
    depth, index = 0, first
    while index < len(lines):
        depth += lines[index].count("(") - lines[index].count(")")
        index += 1
        if depth <= 0:
            break

    last = len(lines)
    for probe in range(index, len(lines)):
        line = lines[probe]
        if line.strip() and not line[:1].isspace():
            last = probe
            break
    return "".join(lines[:first]) + body + "\n" + "".join(lines[last:])


def _diff_objection(original: str, edited: str):
    """Why this candidate should be thrown away, or None to keep it.

    Judged on the DIFF rather than the objective, because the objective is the
    model's own words and it grades itself generously against them. Measured
    over the first 13 committed candidates, only 2 were worth keeping.

    Two rules, both from a specific failure in that batch:

    1. A change that only adds comments did not do the work. Three candidates
       appended a trailing comment to a line that already existed and stopped
       there -- and one of those comments was wrong, claiming to check `value`
       on a line testing `text`.

    2. A change that REWRITES an existing return or raise is a contract change,
       not a check. Two candidates turned `return int(default)` into
       `return None` and into `raise ValueError(...)`, either of which breaks
       every caller expecting an int. Both passed the full regression suite,
       so the tests cannot be relied on to catch this class; adding a guard is
       welcome, altering what a function already promised is not.
    """
    import difflib

    def code_only(text):
        """Statements with comments and blank lines removed.

        Docstrings survive, deliberately: a docstring is the function's stated
        contract and improving one is real work, while `# Added check` appended
        to a line that already existed is not.
        """
        out = []
        for line in text.splitlines():
            body = line.split("#", 1)[0].rstrip()
            if body.strip():
                out.append(body)
        return out

    before, after = code_only(original), code_only(edited)
    if before == after:
        # Asking "was any statement added" is not enough: three candidates
        # appended a trailing comment to a line that already existed, so the
        # added line still CONTAINED a statement -- the same one. Comparing the
        # comment-stripped files is what actually detects "nothing happened".
        return "comment-only change (the code is unchanged)"

    # Strip the diff marker BEFORE stripping whitespace: a removed line arrives
    # as "-        return int(default)", so matching on the raw string never
    # sees `return` at the start and the check silently passes everything.
    removed = []
    for line in difflib.unified_diff(before, after, lineterm="", n=0):
        if line.startswith("---"):
            continue
        if line.startswith("-"):
            removed.append(line[1:].strip())
    added = []
    for line in difflib.unified_diff(before, after, lineterm="", n=0):
        if line.startswith("+++"):
            continue
        if line.startswith("+"):
            added.append(line[1:].strip())

    # A net-new print() is rejected. This is server/library code with a real
    # logging path; a print goes nowhere useful and in an MCP server can corrupt
    # a stdio protocol stream. It is also the tell for the fail-open handler
    # that slipped through once: a candidate wrapped a DB call in
    # `try/except Exception as e: print(...); return []`, swallowing a real
    # error and reporting nothing. Rejecting the print rejects that whole shape.
    if any("print(" in ln for ln in added) and not any("print(" in ln for ln in removed):
        return "adds a print() -- library code logs, it does not print"

    for line in removed:
        if re.match(r"^(return|raise)\b", line):
            return ("rewrites an existing `%s` -- that is a contract change, "
                    "not a check" % line.split()[0])

    # An ADDED raise whose condition tests a hardcoded numeric range is an
    # invented restriction, not a check. Two candidates added
    # `if not (0.01 <= abs(step) <= 0.25): raise` and `if assert_count < 2:
    # raise`, both restricting previously-valid inputs to a threshold with no
    # basis in the code. The existing return/raise guard misses these because
    # the raise is ADDED, not a rewrite. A raise on truthiness (`if not x:`)
    # is spared -- that is ordinary None/empty defence -- but a raise gated by
    # a numeric comparison against a literal is rejected.
    _NUMERIC_GATE = re.compile(r"[<>]=?\s*-?\d|\d\s*[<>]=?")
    added_raise = any(re.match(r"^raise\b", ln) for ln in added)
    if added_raise:
        gated_by_number = any(
            _NUMERIC_GATE.search(ln) and re.search(r"\b(if|while|assert)\b", ln)
            for ln in added
        )
        if gated_by_number:
            return ("adds a raise gated by a hardcoded numeric threshold -- "
                    "an invented restriction on previously-valid input, not a check")

    # A defaulted lookup turned strict is the SAME class as a rewritten return,
    # just spelled differently, and it slipped past the check above. A run
    # rewrote `schema.get("type", "any")` -- a documented permissive default --
    # into `if "type" not in schema: error; schema["type"]`, changing a
    # type-less schema from "accept anything" to a hard failure. Tests passed.
    # If a removed line accessed a key WITH a default and no added line keeps a
    # defaulted access, the permissive path was deleted; reject it. Over-
    # rejecting a legitimate refactor is the tolerable direction here.
    _DEFAULTED_GET = re.compile(r"\.get\(\s*[^,()]+,\s*[^)]+\)")
    if any(_DEFAULTED_GET.search(line) for line in removed):
        if not any(_DEFAULTED_GET.search(line) for line in added):
            return ("removes a defaulted lookup (.get(k, default)) -- that "
                    "turns a permissive default into a strict path, a contract "
                    "change, not a check")
    return None


def _recent_objectives(limit=40):
    """Objectives from recent runs, for de-duplication.

    The proposer re-derives the same handful every pass because nothing tells
    it what has already been tried: of 13 commits, three attacked
    `exact_duplicate_plan`, four the same empty-string check and two the same
    docstring.
    """
    out = []
    try:
        for run in (selfmod.list_runs(limit) or []):
            text = (run.get("objective") or "").strip().lower()
            if text:
                out.append(text)
    except Exception:
        pass
    return out


def _objective_key(text) -> str:
    """The comparable part of an objective.

    Stored objectives carry the model's rationale appended as "OBJ (WHY...)",
    while a fresh proposal is compared before that suffix exists. Left
    unnormalised, SequenceMatcher divides by TOTAL length, so an exact restated
    objective scored about 0.59 against its own stored form and every duplicate
    sailed through -- the loop committed the same change twice in consecutive
    passes with the guard active.
    """
    head = str(text or "").split(" (", 1)[0]
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", head.lower()).split())


# Words shared by so many objectives that overlap on them alone means nothing.
# Two proposals that both "add a guard to catch and report exceptions" are the
# same SHAPE, not the same work -- what distinguishes them is the identifier
# they name. Comparing on distinctive tokens stops the guard collapsing every
# exception-guard objective into one and skipping real work on files it has not
# touched.
_OBJECTIVE_STOPWORDS = frozenset("""
a an the to in of on for and or that this it its is are be add ensure handle
check guard catch report raise return value function method case where when
does not no non empty string text input correctly properly current
implementation code line so which can could lead unexpected behaviour behavior
""".split())


def _distinctive(text) -> set:
    return {w for w in _objective_key(text).split()
            if w not in _OBJECTIVE_STOPWORDS and len(w) > 2}


def _too_similar(objective, previous) -> bool:
    """Whether this objective restates one already attempted.

    Compares on DISTINCTIVE tokens (identifiers, not boilerplate). An objective
    that shares no distinctive token with any prior one is new work even if its
    verb phrase is identical -- "guard exceptions in check_bad_embeddings" and
    "guard exceptions in run_ruff" are different, and an earlier version wrongly
    merged them because the shared "add a guard to catch and report exceptions
    in" dominated a raw token overlap.
    """
    import difflib

    candidate = _objective_key(objective)
    if not candidate:
        return False
    cand_tokens = _distinctive(objective)
    for earlier in previous:
        key = _objective_key(earlier)
        if not key:
            continue
        # A near-identical string is a repeat regardless of tokens.
        if candidate in key or key in candidate:
            return True
        if difflib.SequenceMatcher(None, candidate, key).ratio() >= 0.85:
            return True
        # Otherwise it is a repeat only when the DISTINCTIVE tokens overlap --
        # same identifiers, not merely the same boilerplate verb.
        prev_tokens = _distinctive(earlier)
        if cand_tokens and prev_tokens:
            overlap = len(cand_tokens & prev_tokens) / float(len(cand_tokens | prev_tokens))
            if overlap >= 0.60:
                return True
    return False


def reclaim_orphans(log) -> int:
    """Reclaim runs stuck in an active phase by a previously killed loop.

    A run this loop starts is never claim()ed, so its owner_id is NULL, and
    selfmod.reconcile_interrupted only touches rows WHERE owner_id IS NOT NULL;
    prune_workspaces skips active phases entirely. So a loop killed mid-pass --
    which is how every run of this loop has ended so far -- leaves its run in
    editing/testing, its ~100 MB worktree, and its branch behind forever,
    invisible to both cleanup paths and inflating the 'active' count.

    Called once at loop start: any active-phase run with no owner predates this
    process and cannot belong to a live loop (only one runs at a time), so it
    is safe to cancel and discard.
    """
    reclaimed = 0
    try:
        runs = selfmod.list_runs(200) or []
    except Exception:
        return 0
    for run in runs:
        if run.get("phase") not in ("editing", "testing", "backed_up", "proposed"):
            continue
        if run.get("owner_id"):
            continue
        rid = run.get("id")
        try:
            selfmod.cancel(rid)
        except Exception:
            pass
        _discard_workspace(rid)
        reclaimed += 1
    if reclaimed:
        log("  reclaimed %d orphaned run(s) from a previous killed loop" % reclaimed)
    return reclaimed


def _discard_workspace(run_id) -> None:
    """Remove a rejected run's worktree and branch.

    Neither cancel() nor reject() reclaims them, and prune_workspaces() is
    retention-based (30 days by default) -- fine for a handful of human-driven
    runs, useless for a loop that starts one every ninety seconds. Measured:
    two killed loops left 26 worktrees and 26 branches behind, 237 MB, and a
    branch list that no longer fit on a screen.

    Only ever called for a run that did NOT produce a keeper. Best-effort by
    design: failing to reclaim a workspace must not fail the pass.
    """
    try:
        workspace = selfmod.candidate_path(run_id)
        if workspace.exists():
            selfmod._git(REPO, "worktree", "remove", "--force", str(workspace))
        selfmod._git(REPO, "worktree", "prune")
        selfmod._git(REPO, "branch", "-D", "selfmod/%s" % run_id)
    except Exception:
        pass


def propose_objective(server, log) -> tuple[str, str] | None:
    """Ask the local model for ONE small, concrete improvement.

    Grounded in a real file's real contents, never from memory: asked to
    improve code it cannot see, this model class invents plausible defects in
    functions that do not exist. The file is chosen by rotation rather than by
    the model, so a single unlucky answer cannot steer every future night at
    the same target.
    """
    import random

    seen_before = _recent_objectives()

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
            if _too_similar(objective, seen_before):
                log("  %s: objective restates a previous run, skipping" % name)
                continue
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
        "Rewrite ONE function from this Python module so that it accomplishes\n"
        "exactly this objective, and nothing else:\n\n    %s\n\n"
        "Rules:\n"
        "- Output ONLY that single function, complete, from its `def` line to\n"
        "  its last line. Nothing before it, nothing after it, no fence.\n"
        "- Keep its name, signature and indentation exactly as they are.\n"
        "- Add a brief comment where you changed something, saying WHY.\n\n"
        "=== %s ===\n%s" % (objective, target, original)
    ), num_predict=2000)

    # Splice one function back rather than accepting a whole-file rewrite.
    #
    # Asking for the whole module converges on deletion: the model reproduces
    # the parts it is thinking about and drops the rest. Measured on this loop
    # at 49% and 50% of the original file returned on consecutive passes with a
    # 14B, and previously at 44% and 13% with a 7B -- the deletion guard caught
    # every one, which means every pass was wasted.
    #
    # The fix is the one the codegen work arrived at independently: never ask a
    # model to reproduce structure it does not need to touch. It writes one
    # function; the harness owns the file.
    edited = _splice_function(original, edited)
    if edited is None:
        selfmod.cancel(run_id)
        _discard_workspace(run_id)
        return "candidate rejected: reply was not a single replaceable function"

    # The shrink floor still applies to the SPLICED file, not the reply. A
    # function-level edit that shrinks the module by a quarter has deleted
    # something it was not asked to touch.
    if len(edited) < 0.75 * len(original):
        selfmod.cancel(run_id)
        _discard_workspace(run_id)
        return ("candidate rejected: returned %d%% of the original file "
                "(deletion guard)" % (100 * len(edited) // max(1, len(original))))

    objection = _diff_objection(original, edited)
    if objection:
        selfmod.cancel(run_id)
        _discard_workspace(run_id)
        return "candidate rejected: %s" % objection

    selfmod.apply_candidate_changes(run_id, {target: edited})
    diff = selfmod.inspect_diff(run_id)
    if not diff.get("changed_files"):
        selfmod.cancel(run_id)
        _discard_workspace(run_id)
        return "candidate made no change"

    selfmod.begin_testing(run_id)
    py = str(REPO / "venv" / "Scripts" / "python.exe")
    results = []
    # The kind NAMES matter: review() checks recorded kinds against a required
    # set ("reproducer_before", "syntax", "targeted", "regression", "smoke"),
    # so recording a passing test under an unrecognised name leaves the
    # requirement unmet and review fails on a candidate that did everything
    # asked of it. "lint"/"unit" did exactly that -- the loop could not have
    # produced a commit even from a perfect candidate.
    for kind, command in (
        ("syntax", [py, "-m", "ruff", "check", target]),
        ("regression", [py, "-m", "pytest", "-q"]),
    ):
        # cwd is deliberately NOT passed. record_test computes
        # `(workspace / _rel(workspace, cwd)).parent` for an explicit cwd,
        # which for the workspace itself resolves to its PARENT -- so every
        # command ran one directory above the checkout and ruff reported
        # E902 file-not-found, which read as a lint failure of the
        # candidate. The default already is the workspace.
        outcome = selfmod.record_test(
            run_id, kind, command, timeout=test_timeout)
        passed = bool(outcome.get("passed")) if isinstance(outcome, dict) else bool(outcome)
        results.append((kind, passed))
        log("  %s: %s" % (kind, "pass" if passed else "FAIL"))
        if not passed:
            selfmod.reject(run_id, reason="%s failed" % kind)
            _discard_workspace(run_id)
            return "candidate rejected: %s failed (run %s kept for inspection)" % (kind, run_id)

    # Only the kinds this stage actually records; the default set includes
    # reproducer/targeted/smoke phases that belong to a human-driven run.
    #
    # HONOUR THE VERDICT. review() reports rejection by moving the run to
    # `rejected` and returning that row -- it does not raise. The first version
    # discarded the return and committed regardless, so every branch was
    # advertised "COMMITTED ... -- review: git log -p" while its ledger row read
    # phase=rejected: the scope-escape, protected-file and weakened-tests checks
    # were dead code for the branch deliverable. (They fired on nothing real
    # only because two of review's checks were also structurally unsatisfiable,
    # both fixed in selfmod.py alongside this.)
    reviewed = selfmod.review(run_id, require_kinds={"syntax", "regression"})
    # A PASS lands on reviewing and may auto-advance to approved under
    # auto-low-risk; a FAIL lands on rejected/restored with last_error set.
    # Key on the failure states, not one success phase -- an earlier version
    # checked `phase == "reviewing"` and would have discarded a passing run
    # that auto-advanced.
    phase = reviewed.get("phase") if isinstance(reviewed, dict) else None
    if phase in (None, "rejected", "restored"):
        reason = (reviewed or {}).get("last_error") if isinstance(reviewed, dict) else None
        _discard_workspace(run_id)
        return "candidate rejected by review: %s" % (reason or "unspecified")[:160]

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
