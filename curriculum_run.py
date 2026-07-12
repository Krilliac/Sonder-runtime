"""curriculum_run — the LIVE controller for Sonder Runtime's self-curriculum loop.

Builds a real model-backed gen_fn, harvests up to N valid+novel self-invented
tasks (self_curriculum.harvest), persists the accepted ones (curriculum_store),
then runs grounded practice through Sonder Runtime: ask -> check the answer against the task's own
check (grounding) -> record_outcome. Needs a live Ollama/GPU — this module is
written but intentionally NOT executed by the agent that wrote it.

Usage:
    python curriculum_run.py [N]        # N defaults to 5
"""
import sys

import curriculum_store
import grounding
import self_curriculum
import server


def build_gen_fn():
    """Wire a gen_fn() -> raw model text closure to Sonder Runtime's selected model,
    prompted with GEN_PROMPT at a higher temperature for task-generation variety.
    """
    model = server.resolve_sonder_model(False)
    raw_gen = server._make_generate(model, "", 0.7, 512, 4096)
    return lambda: raw_gen(self_curriculum.GEN_PROMPT)


def practice_on(tasks):
    """Practice each accepted task through Sonder Runtime: ask -> ground -> record.

    Wrapped per-task in try/except so one bad task can't abort the whole run.
    Returns (passed, new_lessons).
    """
    passed = 0
    new_lessons = 0
    for t in tasks:
        name = t.get("name", "?")
        try:
            print("  practice: %s ..." % name)
            resp = server.sonder(t["prompt"])
            iid = server.parse_interaction_id(resp)
            code = grounding.extract_code_block(resp)
            ok = False
            if code:
                ok, _out = grounding.run_code(code, t["check"])
            signal = "tests_passed" if ok else "failed"
            if ok:
                passed += 1
            if iid:
                msg = server.record_outcome(iid, signal)
                if "Distilled lesson" in msg:
                    new_lessons += 1
                print("    -> %s  %s" % ("PASS" if ok else "FAIL", msg))
            else:
                print("    -> %s (no interaction id)" % ("PASS" if ok else "FAIL"))
        except Exception as e:
            print("    -> ERROR practicing '%s': %r" % (name, e))
    return passed, new_lessons


def main():
    n = 5
    if len(sys.argv) > 1:
        try:
            n = int(sys.argv[1])
        except ValueError:
            print("usage: python curriculum_run.py [N]")
            return
    n = max(1, n)

    print("CURRICULUM: resolving the local model selected by Sonder Runtime ...")
    gen_fn = build_gen_fn()

    print("--- lessons before ---")
    print(server.sonder_stats())

    existing_names = curriculum_store.names()
    print("CURRICULUM: harvesting up to %d valid+novel task(s) ..." % n)
    accepted = self_curriculum.harvest(n, gen_fn, existing_names)
    print("CURRICULUM: accepted %d/%d requested" % (len(accepted), n))
    for t in accepted:
        print("  + %s" % t.get("name", "?"))

    curriculum_store.append(accepted)

    print("CURRICULUM: practicing %d accepted task(s) ..." % len(accepted))
    passed, new_lessons = practice_on(accepted)

    print("--- lessons after ---")
    print(server.sonder_stats())

    print("CURRICULUM done: harvested=%d practiced=%d passed=%d new_lessons=%d" % (
        len(accepted), len(accepted), passed, new_lessons))


if __name__ == "__main__":
    main()
