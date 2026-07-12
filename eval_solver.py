"""eval_solver — measure the reasoning lift from execution-grounded self-repair.

For each hard task, runs solver.solve() on the SAME model selected by Sonder Runtime and reports:
  * pass@1     — did the FIRST single-shot attempt already pass (baseline)?
  * pass@repair — did the generate->run->repair loop eventually pass?
The gap is the lift the verifier buys at test time. Uses grounding.run_code for
real execution. Chunk-resumable like eval_retrieval.

Usage: python eval_solver.py [max_attempts]   (default 3)
"""
import sys

import grounding
import server
import solver
import training_tasks

# Harder tasks where a 7B often misses single-shot -> most room for repair to help.
HARD_NAMES = [
    "eval_expr", "base64_encode_manual", "topological_sort",
    "levenshtein_distance", "int_to_roman", "LRUCache", "merge_intervals",
]


def _gen_fn():
    """Escalating-temperature generator: each successive attempt samples hotter so
    self-repair actually explores instead of re-emitting the same buggy answer."""
    model = server.resolve_sonder_model(False)
    temps = [0.2, 0.6, 0.9, 1.1, 1.2]
    state = {"i": 0}

    def gen(prompt, history=None):
        t = temps[min(state["i"], len(temps) - 1)]
        state["i"] += 1
        return server._make_generate(model, "", t, 1024, 4096)(prompt)

    return gen


def main(argv):
    max_attempts = int(argv[1]) if len(argv) > 1 else 3
    tasks = [t for t in training_tasks.TASKS if t["name"] in HARD_NAMES]

    p1 = pr = 0
    for t in tasks:
        gen = _gen_fn()  # fresh temperature schedule per task
        res = solver.solve(t["prompt"], t["check"], gen,
                           run_code_fn=grounding.run_code, max_attempts=max_attempts)
        first_ok = bool(res["transcript"]) and res["transcript"][0]["ok"]
        p1 += 1 if first_ok else 0
        pr += 1 if res["passed"] else 0
        tag = "pass@1" if first_ok else ("repaired@%d" % res["attempts"] if res["passed"] else "FAIL")
        print("%-22s %s (attempts=%d)" % (t["name"], tag, res["attempts"]))

    n = len(tasks)
    print("\nEVAL solver: pass@1 %d/%d  ->  pass@repair %d/%d  (max_attempts=%d)"
          % (p1, n, pr, n, max_attempts))


if __name__ == "__main__":
    main(sys.argv)
