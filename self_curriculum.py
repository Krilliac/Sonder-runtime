"""self_curriculum — sonder invents its own grounded practice tasks.

The model is asked to emit a JSON object describing ONE new coding task
(name/prompt/check/reference). We parse it, validate it by actually
*running* the reference solution against its own check (grounding.run_code),
and require it be novel against the existing task pools. Everything here is
pure/injectable so it can be tested without a live model or GPU — the
controller (curriculum_run.py) wires in the real model.
"""
import json

import grounding

GEN_PROMPT = """You are inventing ONE new Python coding practice task for a training curriculum.

Output a single JSON object with exactly these keys:
- "name": a snake_case Python function name for the task (e.g. "reverse_words").
- "prompt": a self-contained task description asking for that Python function, \
ending with the exact sentence "Return ONLY the function in one python code block."
- "check": Python code with 2 or more `assert` statements that exercise the \
function by name (referring to it as defined by "name") and would fail on a \
wrong or missing implementation.
- "reference": a correct Python implementation of the function described in "prompt".

Rules:
- The task must be genuinely different from common textbook exercises already seen \
(reverse a string, factorial, fizzbuzz, is_prime, etc.) — invent something novel.
- "reference" must actually satisfy "check" when run together.
- Output ONLY the JSON object. No prose, no markdown fences, no explanation before or after.
"""


def parse_task(text):
    """Extract the first balanced {...} JSON object from text and parse it.

    Returns the dict, or None if no balanced object is found, it isn't valid
    JSON, it isn't an object, or it's missing any of the 4 required keys.
    """
    if not text:
        return None
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape = False
    end = None
    for i in range(start, len(text)):
        c = text[i]
        if in_string:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end is None:
        return None

    blob = text[start:end + 1]
    try:
        obj = json.loads(blob)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None

    required = ("name", "prompt", "check", "reference")
    if not all(k in obj for k in required):
        return None
    return obj


def is_valid(task, run_code_fn=grounding.run_code):
    """A generated task is valid iff:
    (a) all 4 keys are present and non-empty strings;
    (b) `check` really tests something (contains 'assert');
    (c) `reference` actually passes its own `check` when executed.
    """
    if not task:
        return False
    required = ("name", "prompt", "check", "reference")
    for k in required:
        v = task.get(k)
        if not isinstance(v, str) or not v.strip():
            return False

    if "assert" not in task["check"]:
        return False

    try:
        ok, _out = run_code_fn(task["reference"] + "\n" + task["check"])
    except Exception:
        return False
    return bool(ok)


def is_novel(task, existing_names):
    """True iff task's name isn't already in existing_names."""
    return task.get("name") not in existing_names


def generate_one(gen_fn):
    """Call gen_fn() (no-arg, returns raw model text) and parse the task out of it."""
    return parse_task(gen_fn())


def harvest(n, gen_fn, existing_names, run_code_fn=grounding.run_code, max_attempts=None):
    """Attempt to collect up to n tasks that are valid AND novel.

    Dedupes by name both against existing_names and within this batch. Caps
    attempts at max_attempts (default n*4) so a bad gen_fn can't loop forever.
    Returns the list of accepted task dicts (may be shorter than n).
    """
    max_attempts = max_attempts if max_attempts is not None else n * 4
    accepted = []
    seen_in_batch = set()
    attempts = 0
    while len(accepted) < n and attempts < max_attempts:
        attempts += 1
        task = generate_one(gen_fn)
        if not task:
            continue
        if not is_valid(task, run_code_fn):
            continue
        name = task.get("name")
        if name in seen_in_batch or not is_novel(task, existing_names):
            continue
        seen_in_batch.add(name)
        accepted.append(task)
    return accepted
