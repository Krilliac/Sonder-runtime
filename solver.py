"""solver — execution-grounded self-repair (reflexion) + best-of-N for the coder.

The base model selected by Sonder Runtime is frozen, so its single-shot answer
is a fixed guess. But the
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
import import_autofix

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
        if not ok:
            # cheap mechanical recovery before spending a model round-trip: if the
            # failure is just a forgotten stdlib import, patch it and re-run once.
            fixed = import_autofix.fix_missing_imports(code, out)
            if fixed != code:
                try:
                    ok2, out2 = run_code_fn(fixed, check)
                except Exception as e:
                    ok2, out2 = False, "run_code error: %r" % (e,)
                if ok2:
                    code, ok, out = fixed, ok2, out2
        transcript.append({"attempt": attempt, "code": code, "ok": ok, "output": out})
        if ok:
            return {"passed": True, "code": code, "attempts": attempt, "transcript": transcript}
        cur_prompt = _repair_prompt(prompt, code, out)
    return {"passed": False, "code": last_code, "attempts": max_attempts, "transcript": transcript}


# --- cross-model critique: a DIFFERENT model reviews the failing code -----------
# A single frozen model tends to re-emit its own buggy answer (it can't see its
# blind spot). An independent critic model supplies the diversity same-model
# repair lacks: it diagnoses the root cause, and that diagnosis steers the fix.
CRITIQUE_TEMPLATE = (
    "You are a strict code reviewer. The task:\n{original}\n\n"
    "This candidate solution FAILED its tests:\n```python\n{code}\n```\n\n"
    "Failing output:\n{error}\n\n"
    "In 1-3 sentences, state the SPECIFIC root-cause bug and the concrete fix. "
    "Diagnose precisely; do not rewrite the whole solution."
)
REPAIR_WITH_CRITIQUE = (
    "{original}\n\n"
    "Your previous attempt:\n```python\n{code}\n```\n"
    "failed its tests. An independent reviewer diagnosed the bug:\n{critique}\n\n"
    "Apply that fix and return a corrected, COMPLETE solution in ONE python code "
    "block. No prose outside the code block."
)


def critique_prompt(original, code, error):
    return CRITIQUE_TEMPLATE.format(original=original, code=code or "",
                                    error=(error or "").strip()[:1500])


def _repair_with_critique(original, code, critique):
    return REPAIR_WITH_CRITIQUE.format(original=original, code=code or "",
                                       critique=(critique or "").strip()[:1500])


def _try(fn, *a):
    try:
        return fn(*a), None
    except Exception as e:  # a dead model round-trip is a failed step, not a crash
        return None, "%r" % (e,)


def solve_with_critic(prompt, check, gen_fn, critic_fn, run_code_fn=grounding.run_code,
                      extract_fn=grounding.extract_code_block, max_attempts=3):
    """Generator+critic loop: gen_fn writes code; on a failed check, critic_fn (a
    DIFFERENT model) diagnoses the root cause, and that critique steers gen_fn's
    next attempt. Returns {passed, code, attempts, transcript}; each transcript
    entry also carries the `critique` that produced the following attempt.
    """
    transcript = []
    cur_prompt = prompt
    last_code = None
    for attempt in range(1, max_attempts + 1):
        resp, gerr = _try(gen_fn, cur_prompt)
        code = extract_fn(resp) if resp is not None else None
        if code is None:
            transcript.append({"attempt": attempt, "code": None, "ok": False,
                               "output": gerr or "no code block", "critique": None})
            cur_prompt = _repair_prompt(prompt, last_code, NO_CODE_HINT)
            continue
        last_code = code
        ok, out = run_code_fn(code, check)
        entry = {"attempt": attempt, "code": code, "ok": ok, "output": out, "critique": None}
        if ok:
            transcript.append(entry)
            return {"passed": True, "code": code, "attempts": attempt, "transcript": transcript}
        crit, _ = _try(critic_fn, critique_prompt(prompt, code, out))
        entry["critique"] = crit or ""
        transcript.append(entry)
        cur_prompt = _repair_with_critique(prompt, code, crit or out)
    return {"passed": False, "code": last_code, "attempts": max_attempts, "transcript": transcript}


def rotate_solve(prompt, check, gen_fns, run_code_fn=grounding.run_code,
                 extract_fn=grounding.extract_code_block, max_attempts=None):
    """Like solve(), but ROTATES through gen_fns (one model per attempt), each
    model seeing the prior one's failing code+error. Cross-model repair without a
    separate critic role: whichever model is up next brings a fresh distribution.
    max_attempts defaults to len(gen_fns) (one shot per model).
    """
    n = len(gen_fns)
    if n == 0:
        raise ValueError("rotate_solve needs at least one generator")
    max_attempts = max_attempts if max_attempts is not None else n
    transcript = []
    cur_prompt = prompt
    last_code = None
    for attempt in range(1, max_attempts + 1):
        g = gen_fns[(attempt - 1) % n]
        resp, gerr = _try(g, cur_prompt)
        code = extract_fn(resp) if resp is not None else None
        if code is None:
            transcript.append({"attempt": attempt, "model": (attempt - 1) % n,
                               "code": None, "ok": False, "output": gerr or "no code block"})
            cur_prompt = _repair_prompt(prompt, last_code, NO_CODE_HINT)
            continue
        last_code = code
        ok, out = run_code_fn(code, check)
        transcript.append({"attempt": attempt, "model": (attempt - 1) % n,
                           "code": code, "ok": ok, "output": out})
        if ok:
            return {"passed": True, "code": code, "attempts": attempt, "transcript": transcript}
        cur_prompt = _repair_prompt(prompt, code, out)
    return {"passed": False, "code": last_code, "attempts": max_attempts, "transcript": transcript}


def solve_verified(prompt, gen_fn, verifier, spec=None,
                   extract_fn=grounding.extract_code_block, max_attempts=3, verify_fn=None):
    """Execution-grounded self-repair driven by a NAMED verifier from the registry.

    Generalizes solve() beyond run_code_fn: `verifier` is a key like 'python_exec',
    'program_run', 'cpp_compile', 'pytest_run', or 'llm_judge', and the repair loop
    uses that backend's Verdict.detail (full traceback / compiler output) to steer
    the next attempt. verify_fn is injectable for tests; by default it dispatches
    through verifiers.verify(verifier, code, spec). Returns {passed, code, attempts,
    transcript}. Never raises for an ordinary failure; a VerifierUnavailable from the
    backend propagates (that means "couldn't judge", the caller should decide).
    """
    if verify_fn is None:
        import verifiers
        def verify_fn(code):
            return verifiers.verify(verifier, code, spec)
    transcript = []
    cur_prompt = prompt
    last_code = None
    for attempt in range(1, max_attempts + 1):
        code = extract_fn(gen_fn(cur_prompt))
        if code is None:
            transcript.append({"attempt": attempt, "code": None, "ok": False, "output": "no code block"})
            cur_prompt = _repair_prompt(prompt, last_code, NO_CODE_HINT)
            continue
        last_code = code
        v = verify_fn(code)
        detail = v.detail or v.reason
        transcript.append({"attempt": attempt, "code": code, "ok": v.passed, "output": detail})
        if v.passed:
            return {"passed": True, "code": code, "attempts": attempt, "transcript": transcript}
        cur_prompt = _repair_prompt(prompt, code, detail)
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
