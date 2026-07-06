"""solver — execution-grounded self-repair (reflexion) + best-of-N for the coder.

trilobite's model is frozen, so its single-shot answer is a fixed guess. But the
loop already owns a VERIFIER (grounding.run_code), which lets us spend test-time
compute to reason harder: generate a candidate, run it against the task's check,
and if it fails, feed the exact traceback back to the model and ask for a fix.
Iterated, this converges on code that provably passes rather than code that
merely looks right — the standard, highest-ROI way to lift a fixed model's
effective reasoning on tasks that have a checker.

Pure and dependency-injected: generate_fn/run_code_fn/extract_fn are passed in,
so the whole loop is unit-testable without a GPU. server wires the real ones.
"""
import grounding

# Lead with the failing code + error so the model attends to the correction
# instead of regenerating its canonical (still-buggy) answer from the task text.
REPAIR_TEMPLATE = (
    "The Python code below has a BUG — it failed when actually executed.\n\n"
    "```python\n{code}\n```\n\n"
    "Running it against the tests produced:\n{error}\n\n"
    "The task this code must satisfy:\n{original}\n\n"
    "Find the SPECIFIC line(s) causing the failure and return a corrected, COMPLETE "
    "solution in ONE python code block. Change your approach if the same idea keeps "
    "failing — do NOT resubmit identical code. No prose outside the code block."
)

NO_CODE_HINT = "Your reply contained no ```python code block. Return the full solution in one python code block."


def _repair_prompt(original, code, error):
    return REPAIR_TEMPLATE.format(original=original, code=code or "", error=(error or "").strip()[:1500])


def solve(prompt, check, generate_fn, run_code_fn=grounding.run_code,
          extract_fn=grounding.extract_code_block, max_attempts=3):
    """Generate -> run -> repair loop.

    generate_fn(prompt) -> response text expected to contain a fenced code block.
    run_code_fn(code, check) -> (ok, output). check is the assert-based verifier.
    Returns a dict: {passed, code, attempts, transcript} where transcript is a
    list of {attempt, code, ok, output} for every try (audit trail + lesson source).
    Never raises: a generate/run error is captured as a failed attempt and fed back.
    """
    transcript = []
    cur_prompt = prompt
    last_code = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = generate_fn(cur_prompt)
        except Exception as e:  # a dead model/round-trip is just a failed attempt
            transcript.append({"attempt": attempt, "code": None, "ok": False,
                               "output": "generate error: %r" % (e,)})
            cur_prompt = _repair_prompt(prompt, last_code, "generation failed; try again")
            continue
        code = extract_fn(resp)
        if code is None:
            transcript.append({"attempt": attempt, "code": None, "ok": False,
                               "output": "no code block"})
            cur_prompt = _repair_prompt(prompt, last_code, NO_CODE_HINT)
            continue
        last_code = code
        try:
            ok, out = run_code_fn(code, check)
        except Exception as e:
            ok, out = False, "run_code error: %r" % (e,)
        transcript.append({"attempt": attempt, "code": code, "ok": ok, "output": out})
        if ok:
            return {"passed": True, "code": code, "attempts": attempt, "transcript": transcript}
        cur_prompt = _repair_prompt(prompt, code, out)
    return {"passed": False, "code": last_code, "attempts": max_attempts, "transcript": transcript}


def best_of_n(prompt, generate_fn, check="", run_code_fn=grounding.run_code,
              extract_fn=grounding.extract_code_block, n=3):
    """Sample n independent candidates; return the first that runs green.

    Complements solve(): repair chains one lineage deeper, best_of_n widens the
    search across independent samples (use a temperature-varying generate_fn).
    With a `check`, "green" means it passes the check; without one, "green" means
    the code executes without raising. Returns {passed, code, candidates, transcript};
    falls back to the last candidate's code if none pass.
    """
    transcript = []
    last_code = None
    for i in range(1, n + 1):
        try:
            resp = generate_fn(prompt)
        except Exception as e:
            transcript.append({"candidate": i, "code": None, "ok": False,
                               "output": "generate error: %r" % (e,)})
            continue
        code = extract_fn(resp)
        if code is None:
            transcript.append({"candidate": i, "code": None, "ok": False, "output": "no code block"})
            continue
        last_code = code
        try:
            ok, out = run_code_fn(code, check)
        except Exception as e:
            ok, out = False, "run_code error: %r" % (e,)
        transcript.append({"candidate": i, "code": code, "ok": ok, "output": out})
        if ok:
            return {"passed": True, "code": code, "candidates": i, "transcript": transcript}
    return {"passed": False, "code": last_code, "candidates": n, "transcript": transcript}
