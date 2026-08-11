"""benchmark_moat — a reproducible "prove-the-moat" harness.

The README makes a claim the rest of the project has to earn: *the moat isn't
the weights, it's your private, grounded data.* This turns that sentence into a
measured number. It runs a small, fixed suite of tasks three ways against the
SAME model and compares the scorecards:

  (1) bare        — the task prompt alone, no runtime augmentation at all.
  (2) runtime_cold — the runtime's facts/lessons retrieval path is available but
                     the store is empty (nothing to inject yet). This is the
                     do-no-harm baseline: turning the runtime on before it has
                     learned anything must not make the model *worse*.
  (3) runtime_warm — the same retrieval path, but with the lessons/facts that a
                     real run would have accumulated already warmed in.

The interesting quantities are the deltas: warm-vs-bare is the headline "moat"
number (what accumulated data buys you), and cold-vs-bare is the honesty check
(the runtime scaffolding, empty, should cost nothing).

WHAT THIS DOES NOT PROVE. It measures the *lift from retrieval augmentation on a
fixed model*, using deterministic graders on a bounded task set. It is not a
general capability benchmark, it says nothing about a model you did not test, and
a fake `model_fn` (as the tests use) measures the harness, not any model. The
warm lessons here are authored, not distilled from real outcomes; on real data
the lift is whatever your history actually earns. See
``docs/wiki/17-benchmarking.md`` for the full, honest methodology.

Design constraints that make this trustworthy:
  * The model call is injectable: pass ``model_fn(prompt) -> str`` and the whole
    harness runs offline with a scripted fake. The real default is built lazily
    (``default_model_fn``) and only touches ``server`` when actually called, so
    importing this module needs no model, no network, and no GPU.
  * Every task carries a deterministic grader (substring / regex / parse checks).
    Given a fixed ``model_fn`` the entire scorecard is reproducible.
  * The suite size is bounded (see ``DEFAULT_SUITE``).

Usage (repo root, runtime venv, Ollama serving — for a REAL run):

    python scripts/benchmark_moat.py                 # markdown to stdout
    python scripts/benchmark_moat.py --json out.json --markdown card.md

Importable API:

    from scripts import benchmark_moat as bm
    result = bm.benchmark(bm.DEFAULT_SUITE, my_model_fn)
    print(bm.render_scorecard(result))
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---------------------------------------------------------------------------
# Arm identifiers. Kept as plain constants so JSON keys are stable and greppable.
# ---------------------------------------------------------------------------
ARM_BARE = "bare"
ARM_RUNTIME_COLD = "runtime_cold"
ARM_RUNTIME_WARM = "runtime_warm"
ARMS = (ARM_BARE, ARM_RUNTIME_COLD, ARM_RUNTIME_WARM)

# Human labels for the scorecard header, in scorecard order.
_ARM_LABELS = {
    ARM_BARE: "bare",
    ARM_RUNTIME_COLD: "+runtime (cold)",
    ARM_RUNTIME_WARM: "+runtime (warmed)",
}


# ---------------------------------------------------------------------------
# Task model
# ---------------------------------------------------------------------------
@dataclass
class MoatTask:
    """One benchmark task with a deterministic grader and its warmable context.

    ``grade`` maps a model response to a score in [0, 1]; ``threshold`` is the
    score at or above which the task counts as passed (partial-credit graders
    can therefore contribute to the mean score without flipping the pass bit).
    ``lessons``/``facts`` are the context the *warm* arm injects — what a real
    run would have distilled and stored for a task like this. The cold arm sees
    the retrieval path but an empty store, so it never sees these.
    """

    name: str
    prompt: str
    grade: Callable[[str], float]
    lessons: List[str] = field(default_factory=list)
    facts: List[str] = field(default_factory=list)
    threshold: float = 1.0


# ---------------------------------------------------------------------------
# Deterministic grader factories. Each returns a Callable[[str], float] in [0,1].
# ---------------------------------------------------------------------------
def grade_contains(needle: str, ignore_case: bool = True) -> Callable[[str], float]:
    """1.0 if ``needle`` appears in the response, else 0.0."""
    target = needle.lower() if ignore_case else needle

    def _grade(response: str) -> float:
        text = (response or "")
        hay = text.lower() if ignore_case else text
        return 1.0 if target in hay else 0.0

    return _grade


def grade_all_of(needles, ignore_case: bool = True) -> Callable[[str], float]:
    """Partial credit: fraction of ``needles`` present in the response.

    Useful when a task has several independent requirements and you want the
    mean score to reflect "got 2 of 3" rather than a hard pass/fail.
    """
    needles = list(needles)

    def _grade(response: str) -> float:
        if not needles:
            return 0.0
        text = (response or "")
        hay = text.lower() if ignore_case else text
        hits = 0
        for n in needles:
            probe = n.lower() if ignore_case else n
            if probe in hay:
                hits += 1
        return hits / len(needles)

    return _grade


def grade_regex(pattern: str, flags: int = re.IGNORECASE) -> Callable[[str], float]:
    """1.0 if ``pattern`` matches anywhere in the response, else 0.0."""
    compiled = re.compile(pattern, flags)

    def _grade(response: str) -> float:
        return 1.0 if compiled.search(response or "") else 0.0

    return _grade


def grade_parses_int(expected: int) -> Callable[[str], float]:
    """1.0 if the first integer parsed from the response equals ``expected``.

    Tolerant of surrounding prose ("The answer is 42.") but strict on value.
    """

    def _grade(response: str) -> float:
        m = re.search(r"-?\d+", response or "")
        if not m:
            return 0.0
        try:
            return 1.0 if int(m.group(0)) == expected else 0.0
        except ValueError:
            return 0.0

    return _grade


# ---------------------------------------------------------------------------
# Prompt augmentation. Deliberately small and self-contained (a slimmed echo of
# orchestrator.build_prompt) so the harness has no import-time dependency on the
# live retrieval stack — the real lift comes from the same *shape* of context.
#
# Known divergence (2026-08-10): this still renders the older facts shape. The
# runtime (orchestrator.build_prompt) now fences each fact, labels the block as
# non-instruction reference material, bounds it with disclosure, and appends
# orchestrator.TASK_DIRECTIVE. Adopting that here would move the measured moat
# numbers for a reason unrelated to retrieval quality, so the divergence is
# recorded rather than silently inherited; sync it only with a re-baselined run.
# ---------------------------------------------------------------------------
FACTS_HEADER = "# Project facts (always true here):"
LESSONS_HEADER = "# Relevant lessons from past work (may help):"


def build_augmented_prompt(task_prompt: str, lessons=None, facts=None) -> str:
    """Prepend facts/lessons blocks to a task prompt, in the pre-2026-08-10
    runtime shape (see the divergence note above — this no longer mirrors
    orchestrator.build_prompt and is pinned deliberately).

    With no lessons and no facts this returns the task prompt unchanged — which
    is exactly why the cold arm (empty store) reduces to the bare prompt and the
    do-no-harm check is meaningful.
    """
    blocks = []
    if facts:
        blocks.append("%s\n%s" % (FACTS_HEADER, "\n".join("- %s" % f for f in facts)))
    if lessons:
        blocks.append(
            "%s\n%s" % (LESSONS_HEADER, "\n".join("- %s" % lesson for lesson in lessons))
        )
    if not blocks:
        return task_prompt
    return "%s\n\n# Task:\n%s" % ("\n\n".join(blocks), task_prompt)


# ---------------------------------------------------------------------------
# Retrieval injection. Isolated behind a callable so tests can substitute a fake
# store and so the three arms differ ONLY in what context reaches the model.
# ---------------------------------------------------------------------------
def default_retrieve(task: MoatTask, warm: bool) -> Tuple[List[str], List[str]]:
    """Return (lessons, facts) for a task: the task's own context when warm,
    empty when cold. Signature matches any ``retrieve_fn`` the harness accepts.
    """
    if warm:
        return list(task.lessons), list(task.facts)
    return [], []


def _prompt_for_arm(task: MoatTask, arm: str, retrieve_fn) -> str:
    if arm == ARM_BARE:
        # No runtime at all: the model sees the raw task and nothing else.
        return task.prompt
    warm = arm == ARM_RUNTIME_WARM
    lessons, facts = retrieve_fn(task, warm)
    return build_augmented_prompt(task.prompt, lessons, facts)


def default_scorer(task: MoatTask, response: str) -> float:
    """Default scorer: run the task's own grader and clamp to [0, 1]."""
    score = float(task.grade(response))
    if score < 0.0:
        return 0.0
    if score > 1.0:
        return 1.0
    return score


# ---------------------------------------------------------------------------
# Core runners
# ---------------------------------------------------------------------------
def run_arm(
    tasks: List[MoatTask],
    model_fn: Callable[[str], str],
    arm: str,
    scorer: Optional[Callable[[MoatTask, str], float]] = None,
    retrieve_fn=None,
    capture_responses: bool = False,
) -> Dict:
    """Run every task once under a single arm and aggregate the scores.

    Never raises on a bad task: a grader/model exception is recorded as a 0.0
    score with a ``detail`` note, so one broken task cannot void the run.
    """
    if arm not in ARMS:
        raise ValueError("unknown arm: %r" % (arm,))
    scorer = scorer or default_scorer
    retrieve_fn = retrieve_fn or default_retrieve

    per_task = []
    total_score = 0.0
    passed = 0
    for task in tasks:
        detail = ""
        try:
            prompt = _prompt_for_arm(task, arm, retrieve_fn)
            response = model_fn(prompt)
            score = float(scorer(task, response))
        except Exception as exc:  # defense in depth; keep the run alive
            response, score, detail = "", 0.0, "harness error: %r" % (exc,)
        score = max(0.0, min(1.0, score))
        is_pass = score >= task.threshold
        total_score += score
        if is_pass:
            passed += 1
        row = {"name": task.name, "score": score, "passed": is_pass}
        if detail:
            row["detail"] = detail
        if capture_responses:
            row["response"] = response
        per_task.append(row)

    n = len(tasks)
    return {
        "arm": arm,
        "tasks": per_task,
        "total": n,
        "passed": passed,
        "pass_rate": (passed / n) if n else 0.0,
        "mean_score": (total_score / n) if n else 0.0,
    }


def _delta(a: Dict, b: Dict) -> Dict:
    """a minus b for the two headline metrics."""
    return {
        "pass_rate": a["pass_rate"] - b["pass_rate"],
        "mean_score": a["mean_score"] - b["mean_score"],
    }


def benchmark(
    tasks: List[MoatTask],
    model_fn: Callable[[str], str],
    scorer: Optional[Callable[[MoatTask, str], float]] = None,
    retrieve_fn=None,
    suite_name: str = "moat",
    capture_responses: bool = False,
) -> Dict:
    """Run all three arms and compute the comparison scorecard.

    The result is a plain dict (JSON-serialisable) so callers can persist it,
    diff it across runs, or render it with ``render_scorecard``.
    """
    arms = {
        arm: run_arm(
            tasks, model_fn, arm,
            scorer=scorer, retrieve_fn=retrieve_fn,
            capture_responses=capture_responses,
        )
        for arm in ARMS
    }
    return {
        "suite": suite_name,
        "n_tasks": len(tasks),
        "arms": arms,
        "deltas": {
            # The moat: what warmed data buys over the raw model.
            "warm_vs_bare": _delta(arms[ARM_RUNTIME_WARM], arms[ARM_BARE]),
            # Honesty check: empty runtime should not cost anything.
            "cold_vs_bare": _delta(arms[ARM_RUNTIME_COLD], arms[ARM_BARE]),
            # Isolates the warming step from the scaffolding.
            "warm_vs_cold": _delta(arms[ARM_RUNTIME_WARM], arms[ARM_RUNTIME_COLD]),
        },
    }


# ---------------------------------------------------------------------------
# Markdown scorecard
# ---------------------------------------------------------------------------
def _pct(x: float) -> str:
    return "%.0f%%" % (100.0 * x)


def render_scorecard(result: Dict) -> str:
    """Render a benchmark result as a Markdown scorecard (bare vs +runtime vs
    +warmed: pass-rate, mean score) plus the deltas that carry the argument.
    """
    arms = result["arms"]
    lines = []
    lines.append("# Moat benchmark scorecard")
    lines.append("")
    lines.append("Suite: `%s` (%d tasks)" % (result["suite"], result["n_tasks"]))
    lines.append("")
    lines.append("| Arm | Pass rate | Mean score |")
    lines.append("| --- | --- | --- |")
    for arm in ARMS:
        a = arms[arm]
        lines.append(
            "| %s | %s (%d/%d) | %.2f |"
            % (_ARM_LABELS[arm], _pct(a["pass_rate"]), a["passed"], a["total"], a["mean_score"])
        )
    lines.append("")
    d = result["deltas"]
    lines.append("| Delta | Pass rate | Mean score |")
    lines.append("| --- | --- | --- |")
    lines.append(
        "| warmed - bare (**the moat**) | %+.0f%% | %+.2f |"
        % (100.0 * d["warm_vs_bare"]["pass_rate"], d["warm_vs_bare"]["mean_score"])
    )
    lines.append(
        "| cold - bare (do-no-harm) | %+.0f%% | %+.2f |"
        % (100.0 * d["cold_vs_bare"]["pass_rate"], d["cold_vs_bare"]["mean_score"])
    )
    lines.append(
        "| warmed - cold (warming step) | %+.0f%% | %+.2f |"
        % (100.0 * d["warm_vs_cold"]["pass_rate"], d["warm_vs_cold"]["mean_score"])
    )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Default suite. Bounded on purpose. Each task pairs a deterministic grader with
# the lesson/fact a real run would have accumulated for a task like it — so the
# warm arm can plausibly clear a bar the bare model misses. The graders never
# depend on the augmentation text (they check the *answer*), so a model that
# already knows the answer passes every arm and the moat delta is honestly 0.
# ---------------------------------------------------------------------------
DEFAULT_SUITE: List[MoatTask] = [
    MoatTask(
        name="project_db_path",
        prompt="Where does this project store its conversation memory? Answer with the filename.",
        grade=grade_contains("memory.db"),
        facts=["All local state lives under SONDER_HOME; conversation memory is in memory.db."],
        lessons=["When asked where memory lives, name the SQLite file memory.db."],
    ),
    MoatTask(
        name="model_alias",
        prompt="What is the Ollama alias for this project's active serving model?",
        grade=grade_contains("sonder:latest"),
        facts=["The active serving model is the Ollama alias sonder:latest."],
    ),
    MoatTask(
        name="run_fence_rule",
        prompt="Write a Python snippet the /run tool can execute. State the input rule it must obey.",
        grade=grade_all_of(["input(", "def "]),
        lessons=[
            "Code for /run must never call input(); it runs non-interactively.",
            "Return exactly one fenced python block with a complete, runnable function.",
        ],
    ),
    MoatTask(
        name="consent_gate",
        prompt="Name one capability in this runtime that is default-off and requires an explicit opt-in.",
        grade=grade_regex(r"cloud|web tool|remote ollama|location"),
        facts=["Cloud models, web tools, remote Ollama, and location are consent-gated (default off)."],
    ),
    MoatTask(
        name="digital_root",
        prompt="Compute the digital root of 9875 (repeatedly sum digits until one digit). Give the number.",
        grade=grade_parses_int(2),
        lessons=["Digital root of a positive n is 1 + (n - 1) % 9; for 9875 that is 2."],
    ),
    MoatTask(
        name="lesson_definition",
        prompt="In this project, what is a 'lesson' and when is one created?",
        grade=grade_all_of(["distilled", "successful"]),
        facts=["A lesson is a short takeaway distilled from a successful interaction, retrieved later."],
    ),
]


# ---------------------------------------------------------------------------
# Real default model_fn. Lazily built so importing this module needs no model.
# ---------------------------------------------------------------------------
def default_model_fn(temperature: float = 0.2) -> Callable[[str], str]:
    """Build the real model callable used for a live run.

    This is the ONLY path that touches the runtime, and it does so lazily: the
    import and model resolution happen when this function is *called*, never at
    module import. It mirrors ``eval_retrieval.baseline_generate`` — the same
    model Sonder Runtime would select, driven through the orchestrator's offload
    surface — so every arm is served by one model and only the prompt differs.
    """
    import server  # local import: no model/network needed to import this module

    model = server.resolve_sonder_model(False)
    gen = server._make_generate(model, "", temperature, 1024, 4096)

    def _model_fn(prompt: str) -> str:
        return gen(prompt)

    return _model_fn


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the moat benchmark and emit a scorecard.")
    parser.add_argument("--json", default="", help="write the full JSON result to this path")
    parser.add_argument("--markdown", default="", help="write the Markdown scorecard to this path")
    parser.add_argument(
        "--temperature", type=float, default=0.2,
        help="sampling temperature for the real model (default 0.2)",
    )
    args = parser.parse_args(argv)

    # A live run needs the runtime; build it lazily and fail with a clear note if
    # the model surface is unavailable rather than a cryptic import error.
    try:
        model_fn = default_model_fn(args.temperature)
    except Exception as exc:
        print("cannot build the runtime model_fn (need Ollama serving): %r" % (exc,))
        return 2

    result = benchmark(DEFAULT_SUITE, model_fn)
    card = render_scorecard(result)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2, sort_keys=True)
    if args.markdown:
        with open(args.markdown, "w", encoding="utf-8") as fh:
            fh.write(card + "\n")

    print(card)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
