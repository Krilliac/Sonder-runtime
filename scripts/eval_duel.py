"""eval_duel — compare single-model vs CROSS-MODEL reasoning strategies on hard
tasks, grounded by real execution. Tests the hypothesis that a second, different
model (rotated generator, or a dedicated critic) converts failures that a single
model's self-repair cannot.

Strategies compared (all served locally, execution-verified):
  * coder pass@1            baseline single-shot (qwen2.5-coder:7b)
  * coder self-repair x3    same model repairs itself (the loop that showed no lift)
  * r1 self-repair x3       is a reasoning model alone enough?
  * rotate coder<->r1 x4    rotate the generator across two models each attempt
  * gen coder / critic r1   coder writes, r1 diagnoses failures
  * gen r1 / critic coder   r1 writes, coder diagnoses failures

NOTE: on 6GB VRAM only one 7B is resident at a time, so cross-model strategies
pay a model-swap reload between alternating calls — slow but correct. Run in the
background. Usage: python eval_duel.py [n_tasks]
"""
import sys

import grounding
import server
import solver
import training_tasks

HARD = [
    "eval_expr", "base64_encode_manual", "topological_sort",
    "levenshtein_distance", "int_to_roman", "merge_intervals",
    "matrix_multiply", "lcs_length", "is_balanced", "kth_smallest",
    "max_subarray_sum", "two_sum",
]

CODER = "qwen2.5-coder:7b"
R1 = "deepseek-r1:7b"   # reasoning heavyweight; spills ~16% to CPU on 6GB (slow)
Q3 = "qwen3:4b"          # reasoning model that fits fully on GPU (fast)


def mk(model, temp=0.3):
    # Keep a 7B FULLY GPU-resident on 6GB. Even 4096 context spilled r1 ~18% to
    # CPU; 2048 fits it entirely (short functions + <think> trace stay well under).
    # 1024 predict caps runaway reasoning so a truncated attempt fails fast.
    return server._make_generate(model, "", temp, 1024, 2048)


def main(argv):
    n = int(argv[1]) if len(argv) > 1 else len(HARD)
    tasks = [t for t in training_tasks.TASKS if t["name"] in HARD][:n]
    gc, gr, gq = mk(CODER), mk(R1), mk(Q3)

    # qwen3:4b is the fast cross-model partner (fits GPU); r1 appears in one
    # strategy so we get its solo data point without it dominating wall-clock.
    strategies = [
        ("coder pass@1",          lambda t: solver.solve(t["prompt"], t["check"], gc, max_attempts=1)),
        ("coder self-repair x3",  lambda t: solver.solve(t["prompt"], t["check"], gc, max_attempts=3)),
        ("q3 self-repair x3",     lambda t: solver.solve(t["prompt"], t["check"], gq, max_attempts=3)),
        ("r1 self-repair x3",     lambda t: solver.solve(t["prompt"], t["check"], gr, max_attempts=3)),
        ("rotate coder<->q3 x4",  lambda t: solver.rotate_solve(t["prompt"], t["check"], [gc, gq], max_attempts=4)),
        ("gen coder/critic q3",   lambda t: solver.solve_with_critic(t["prompt"], t["check"], gc, gq, max_attempts=3)),
    ]

    tally = {name: 0 for name, _ in strategies}
    for t in tasks:
        print("\n# %s" % t["name"], flush=True)
        for name, fn in strategies:
            try:
                ok = bool(fn(t)["passed"])
            except Exception as e:
                ok = False
                print("  ! %s: %r" % (name, e), flush=True)
            tally[name] += 1 if ok else 0
            print("   %-24s %s" % (name, "PASS" if ok else "fail"), flush=True)

    m = len(tasks)
    print("\n=== pass-rate on %d hard tasks ===" % m)
    for name, _ in strategies:
        print("  %-24s %d/%d" % (name, tally[name], m))


if __name__ == "__main__":
    main(sys.argv)
