"""eval_retrieval — measures whether Sonder Runtime's learned-lesson retrieval actually
improves grounded pass-rate on HELD-OUT tasks (names disjoint from training_tasks.TASKS).

Compares two conditions on the SAME held-out task, both served by the SAME
model selected by Sonder Runtime:
  * retrieval ON  -> server.sonder(prompt)            (real loop: lessons injected + captured)
  * baseline OFF  -> baseline_generate(prompt)            (same model, no lessons, no capture)

Grounded pass/fail: extract the model's fenced python code block (grounding.extract_code_block)
and actually execute it plus the task's assert-based check (grounding.run_code) in a subprocess.

This module makes NO model calls at import time -- it only calls Ollama when main() runs a
chunk of HELDOUT. Safe to import from tests without a GPU.

Usage (chunk-resumable, so the controller can run it in <10-min foreground pieces):
    python eval_retrieval.py [start] [count]

Prints one PASS/FAIL line per task, then a summary line:
    EVAL chunk: retrieval P/N, baseline Q/N
"""
import sys

import grounding
import server
import training_tasks

# ---------------------------------------------------------------------------
# Held-out tasks. Medium-difficulty string/list/dict/number ops adjacent to the
# training pool's domains, so lessons distilled from training plausibly transfer
# -- but none of these `name`s appear in training_tasks.TASKS (see
# tests/test_eval_retrieval.py, which asserts the pools are disjoint).
# ---------------------------------------------------------------------------
HELDOUT = [
    {"name": "swap_case",
     "prompt": "Write a Python function named `swap_case(s)` that swaps the case of every letter in s (uppercase becomes lowercase and vice versa), leaving non-letters unchanged. Return ONLY the function in one python code block.",
     "check": "assert swap_case('Hello World') == 'hELLO wORLD'\nassert swap_case('') == ''\nassert swap_case('123abc') == '123ABC'"},

    {"name": "count_substring",
     "prompt": "Write a Python function named `count_substring(s, sub)` that returns the number of non-overlapping occurrences of sub in s (same semantics as str.count). Return ONLY the function in one python code block.",
     "check": "assert count_substring('ababab', 'ab') == 3\nassert count_substring('aaaa', 'aa') == 2\nassert count_substring('abc', 'x') == 0"},

    {"name": "is_pangram",
     "prompt": "Write a Python function named `is_pangram(s)` that returns True if s contains every letter of the English alphabet at least once (case-insensitive), False otherwise. Return ONLY the function in one python code block.",
     "check": "assert is_pangram('The quick brown fox jumps over the lazy dog') is True\nassert is_pangram('Hello World') is False"},

    {"name": "sum_digits_until_single",
     "prompt": "Write a Python function named `sum_digits_until_single(n)` that repeatedly sums the digits of a non-negative integer n until a single digit remains (the digital root), and returns that digit. Return ONLY the function in one python code block.",
     "check": "assert sum_digits_until_single(9875) == 2\nassert sum_digits_until_single(0) == 0\nassert sum_digits_until_single(5) == 5"},

    {"name": "unique_chars",
     "prompt": "Write a Python function named `unique_chars(s)` that returns a list of the distinct characters in s, preserving the order of their first occurrence. Return ONLY the function in one python code block.",
     "check": "assert unique_chars('aabbcc') == ['a','b','c']\nassert unique_chars('') == []\nassert unique_chars('abcabc') == ['a','b','c']"},

    {"name": "nth_largest",
     "prompt": "Write a Python function named `nth_largest(nums, n)` that returns the n-th largest value in nums (1-indexed) when sorted in descending order, counting duplicate values separately. Return ONLY the function in one python code block.",
     "check": "assert nth_largest([5,1,9,3,7], 1) == 9\nassert nth_largest([5,1,9,3,7], 3) == 5\nassert nth_largest([4,4,4], 2) == 4"},

    {"name": "interleave_lists",
     "prompt": "Write a Python function named `interleave_lists(a, b)` that interleaves two lists elementwise starting with a's first element, appending any leftover tail from the longer list at the end. Return ONLY the function in one python code block.",
     "check": "assert interleave_lists([1,3,5], [2,4,6]) == [1,2,3,4,5,6]\nassert interleave_lists([1,2], [3,4,5,6]) == [1,3,2,4,5,6]\nassert interleave_lists([], [1,2]) == [1,2]"},

    {"name": "hamming_distance",
     "prompt": "Write a Python function named `hamming_distance(a, b)` that takes two equal-length strings and returns the number of positions at which the corresponding characters differ. Return ONLY the function in one python code block.",
     "check": "assert hamming_distance('karolin', 'kathrin') == 3\nassert hamming_distance('abc', 'abc') == 0"},

    {"name": "compress_spaces",
     "prompt": "Write a Python function named `compress_spaces(s)` that collapses every run of whitespace in s into a single space and strips leading/trailing whitespace. Return ONLY the function in one python code block.",
     "check": "assert compress_spaces('  hello   world  ') == 'hello world'\nassert compress_spaces('a  b') == 'a b'\nassert compress_spaces('') == ''"},

    {"name": "titlecase_sentence",
     "prompt": "Write a Python function named `titlecase_sentence(s)` that returns s with only its very first letter capitalized and every other letter lowercased (sentence case, not per-word). Return ONLY the function in one python code block.",
     "check": "assert titlecase_sentence('hello world') == 'Hello world'\nassert titlecase_sentence('THE CAT SAT') == 'The cat sat'\nassert titlecase_sentence('') == ''"},

    {"name": "first_non_repeating_char",
     "prompt": "Write a Python function named `first_non_repeating_char(s)` that returns the first character in s that occurs exactly once, or None if every character repeats (or s is empty). Return ONLY the function in one python code block.",
     "check": "assert first_non_repeating_char('swiss') == 'w'\nassert first_non_repeating_char('aabbcc') is None\nassert first_non_repeating_char('') is None"},

    {"name": "remove_vowels",
     "prompt": "Write a Python function named `remove_vowels(s)` that returns s with all vowels (a, e, i, o, u, both cases) removed, leaving everything else unchanged. Return ONLY the function in one python code block.",
     "check": "assert remove_vowels('Hello World') == 'Hll Wrld'\nassert remove_vowels('') == ''\nassert remove_vowels('AEIOUaeiou') == ''"},
]


def baseline_generate(prompt):
    """Same model selected by Sonder Runtime, but NO lesson injection and NO capture."""
    model = server.resolve_sonder_model(False)
    gen = server._make_generate(model, "", 0.2, 1024, 4096)
    return gen(prompt)


def _run_condition(prompt, check, generate_fn):
    """Call generate_fn(prompt), extract its code block, ground it against check.
    Returns (passed: bool, detail: str) and never raises -- callers rely on this.
    """
    try:
        response = generate_fn(prompt)
    except Exception as e:
        return False, "generate error: %r" % (e,)
    code = grounding.extract_code_block(response)
    if code is None:
        return False, "no fenced python code block in response"
    try:
        ok, out = grounding.run_code(code, check)
    except Exception as e:
        return False, "run_code error: %r" % (e,)
    return ok, out


def run_task(task):
    """Run one held-out task under both conditions. Never raises."""
    name, prompt, check = task["name"], task["prompt"], task["check"]
    retrieval_ok, retrieval_detail = _run_condition(
        prompt, check, lambda p: server.sonder(p))
    baseline_ok, baseline_detail = _run_condition(
        prompt, check, baseline_generate)
    return {
        "name": name,
        "retrieval": retrieval_ok,
        "retrieval_detail": retrieval_detail,
        "baseline": baseline_ok,
        "baseline_detail": baseline_detail,
    }


def main(argv):
    start = int(argv[1]) if len(argv) > 1 else 0
    count = int(argv[2]) if len(argv) > 2 else len(HELDOUT)
    chunk = HELDOUT[start:start + count]

    if not chunk:
        print("EVAL chunk: no tasks in range [%d:%d) (pool size %d)" %
              (start, start + count, len(HELDOUT)))
        return

    retrieval_pass = 0
    baseline_pass = 0
    for task in chunk:
        try:
            result = run_task(task)
        except Exception as e:
            # Defense in depth: run_task itself shouldn't raise, but one bad task
            # must never kill the rest of the chunk.
            print("%s: retrieval=FAIL baseline=FAIL (harness error: %r)" % (task["name"], e))
            continue

        r_status = "PASS" if result["retrieval"] else "FAIL"
        b_status = "PASS" if result["baseline"] else "FAIL"
        print("%s: retrieval=%s baseline=%s" % (task["name"], r_status, b_status))
        if not result["retrieval"]:
            print("    retrieval detail: %s" % (result["retrieval_detail"][:300]))
        if not result["baseline"]:
            print("    baseline detail:  %s" % (result["baseline_detail"][:300]))

        if result["retrieval"]:
            retrieval_pass += 1
        if result["baseline"]:
            baseline_pass += 1

    n = len(chunk)
    print("EVAL chunk: retrieval %d/%d, baseline %d/%d" % (retrieval_pass, n, baseline_pass, n))


if __name__ == "__main__":
    # Fail fast and loudly if the hold-out pool ever regresses into the training pool.
    _training_names = {t["name"] for t in training_tasks.TASKS}
    _overlap = {t["name"] for t in HELDOUT} & _training_names
    if _overlap:
        print("ERROR: HELDOUT overlaps training_tasks.TASKS: %s" % sorted(_overlap))
        sys.exit(1)
    main(sys.argv)
