"""self_eval_harness — regression-tracking eval log.

Runs a fixed set of held-out tasks through an INJECTED `solve_fn` (so the
caller decides how "solving" happens: a live model, solver.solve, a stub for
tests, whatever), computes a pass-rate, and appends one JSONL record per run
to a log file so pass-rate can be tracked over time (e.g. across sonder
fine-tunes, prompt tweaks, or verifier changes) to catch regressions.

Deliberately dependency-free: no model/GPU calls happen here, and `ts` is
always passed in by the caller rather than read from the clock, so runs are
reproducible and easy to unit test.

Typical usage:

    import self_eval_harness as seh
    import training_tasks

    tasks = training_tasks.sample(20)
    record = seh.run_and_log(tasks, my_solve_fn, ts=time.time(),
                              log_path="eval_log.jsonl")
    print(record["pass_rate"])

    history = seh.load_log("eval_log.jsonl")
    if seh.regressed(history):
        print("pass-rate dropped vs best-so-far -- investigate before shipping")
"""
import json
import os


def run_eval(tasks, solve_fn):
    """Run each task in `tasks` through solve_fn(task) -> bool and tally results.

    `tasks` is any iterable of task-like objects (e.g. the dicts in
    training_tasks.TASKS); this module never inspects their fields itself --
    solve_fn owns that. `solve_fn` may raise; a raising task counts as a fail
    rather than aborting the whole run, so one broken task can't blank out the
    rest of the eval.

    Returns a dict:
        {"results": [{"name": <str>, "passed": <bool>}, ...],
         "passed": <int>, "total": <int>, "pass_rate": <float in [0,1]>}
    """
    results = []
    passed = 0
    for i, task in enumerate(tasks):
        name = task.get("name", str(i)) if isinstance(task, dict) else str(i)
        try:
            ok = bool(solve_fn(task))
        except Exception:
            ok = False
        results.append({"name": name, "passed": ok})
        passed += 1 if ok else 0

    total = len(results)
    return {
        "results": results,
        "passed": passed,
        "total": total,
        "pass_rate": (passed / total) if total else 0.0,
    }


def append_log(log_path, ts, passed, total):
    """Append one {"ts", "passed", "total", "pass_rate"} record as a JSONL line.

    Creates the parent directory if needed. `ts` is caller-supplied (e.g.
    time.time() or a fixed value in tests) -- this function never touches the
    clock, so callers fully control what gets recorded.
    """
    record = {
        "ts": ts,
        "passed": passed,
        "total": total,
        "pass_rate": (passed / total) if total else 0.0,
    }
    d = os.path.dirname(log_path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def run_and_log(tasks, solve_fn, ts, log_path):
    """Run `tasks` through `solve_fn`, append a summary record to `log_path`,
    and return that record (with "results" attached for immediate inspection).
    """
    summary = run_eval(tasks, solve_fn)
    record = append_log(log_path, ts, summary["passed"], summary["total"])
    record["results"] = summary["results"]
    return record


def load_log(log_path):
    """Return every record previously appended to `log_path`, oldest first.

    Missing file -> []. Blank lines are skipped; this never raises on an
    empty or not-yet-created log.
    """
    if not os.path.exists(log_path):
        return []
    records = []
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def regressed(history, latest=None, tolerance=0.0):
    """True if the most recent pass_rate dropped below the best pass_rate seen
    among the earlier records (by more than `tolerance`).

    `history` is the list returned by load_log(), in chronological order. If
    `latest` is given it's compared against the best of `history` instead of
    treating history[-1] as the latest run (useful right after run_and_log,
    before re-reading the file). Fewer than 2 data points (nothing to compare
    the newest run against) -> never a regression.
    """
    if latest is None:
        if len(history) < 2:
            return False
        prior, latest_rate = history[:-1], history[-1]["pass_rate"]
    else:
        if not history:
            return False
        prior, latest_rate = history, latest["pass_rate"]

    best_prior = max(r["pass_rate"] for r in prior)
    return latest_rate < (best_prior - tolerance)
