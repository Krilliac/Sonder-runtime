# self_eval_harness — regression-tracking eval log

Runs a held-out task set through an injected `solve_fn` (any callable `task -> bool`), tallies a
pass-rate, and appends a compact `{ts, passed, total, pass_rate}` record to a JSONL log — a
lightweight, dependency-free regression tracker for Sonder Runtime's self-improvement loop. `regressed()`
compares the newest run against the best pass-rate on record so a fine-tune, prompt change, or
verifier tweak that quietly makes things worse gets caught instead of silently shipped.

It's valuable because Sonder Runtime currently has one-off eval scripts (`eval_models.py`,
`eval_solver.py`) that print a score and forget it — there's no persisted history to answer "did the
last change help or hurt?" This module is the missing memory layer for those scripts: give it
`training_tasks.sample(n)` and a `solve_fn` that wraps `solver.solve` + `grounding.run_code`, and
every run appends to `eval_log.jsonl` for free.

To integrate: after `qlora_train.py`/`curriculum_run.py` finish (or on a schedule), call
`self_eval_harness.run_and_log(training_tasks.sample(N), solve_fn, ts=time.time(), log_path="eval_log.jsonl")`
where `solve_fn(task)` wraps `solver.solve(task["prompt"], task["check"], generate_fn, grounding.run_code)`
and returns `res["passed"]`; then gate promotion/deployment on `not self_eval_harness.regressed(self_eval_harness.load_log(...))`.
