"""Honestly measure whether the fine-tune helped: base 1.5B versus the
``sonder-tuned`` Ollama alias (the same 1.5B with a QLoRA adapter) on HELD-OUT
coding tasks, graded by real
execution (grounding.run_code), not the model's say-so.

Fair comparison: same 1.5B family, same system prompt, same params, tasks that
are NOT in training_data.jsonl. Run: ./venv/Scripts/python.exe eval_models.py
"""
import io
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(__file__))
import grounding  # noqa

OLLAMA = "http://127.0.0.1:11434/api/chat"
SYSTEM = "You are a helpful coding assistant. Return only the requested Python function in a single ```python code block."

# Held-out tasks (fresh, not in training_data.jsonl), each with an execution check.
TASKS = [
    ("Write a Python function second_largest(nums) returning the second largest DISTINCT value in a list.",
     "assert second_largest([3,1,4,1,5,9,2,6])==6\nassert second_largest([5,5,4])==4"),
    ("Write count_vowels(s) returning the number of vowels (aeiou, case-insensitive).",
     "assert count_vowels('Hello World')==3\nassert count_vowels('xyz')==0"),
    ("Write is_leap_year(y) returning True iff y is a Gregorian leap year.",
     "assert is_leap_year(2000) and not is_leap_year(1900) and is_leap_year(2024) and not is_leap_year(2023)"),
    ("Write flatten(nested) that flattens a list of lists one level deep.",
     "assert flatten([[1,2],[3],[4,5]])==[1,2,3,4,5]"),
    ("Write gcd(a,b) returning the greatest common divisor.",
     "assert gcd(48,18)==6 and gcd(7,13)==1"),
    ("Write run_length_encode(s) returning a list of (char,count) tuples.",
     "assert run_length_encode('aaabbc')==[('a',3),('b',2),('c',1)]"),
    ("Write is_balanced(s) checking balanced brackets among ()[]{}.",
     "assert is_balanced('([]{})') and not is_balanced('([)]')"),
    ("Write title_case(s) capitalizing the first letter of each word.",
     "assert title_case('hello world')=='Hello World'"),
    ("Write nth_fibonacci(n), 0-indexed with fib(0)=0, fib(1)=1.",
     "assert nth_fibonacci(10)==55 and nth_fibonacci(0)==0"),
    ("Write dedupe(lst) returning the list with duplicates removed, order preserved.",
     "assert dedupe([1,2,2,3,1,4])==[1,2,3,4]"),
    # --- harder, discriminating algorithmic tasks ---
    ("Write lcs_length(a,b) returning the length of the longest common subsequence.",
     "assert lcs_length('ABCBDAB','BDCAB')==4 and lcs_length('abc','')==0"),
    ("Write merge_intervals(intervals) merging overlapping [start,end] intervals, sorted ascending.",
     "assert merge_intervals([[1,3],[2,6],[8,10],[15,18]])==[[1,6],[8,10],[15,18]]"),
    ("Write edit_distance(a,b) returning the Levenshtein edit distance.",
     "assert edit_distance('kitten','sitting')==3 and edit_distance('','abc')==3"),
    ("Write coin_change(coins, amount) returning the min number of coins to make amount, or -1 if impossible.",
     "assert coin_change([1,2,5],11)==3 and coin_change([2],3)==-1"),
    ("Write max_subarray(nums) returning the maximum contiguous subarray sum (Kadane).",
     "assert max_subarray([-2,1,-3,4,-1,2,1,-5,4])==6 and max_subarray([-3,-1,-2])==-1"),
    ("Write rotate90(matrix) returning a square matrix rotated 90 degrees clockwise.",
     "assert rotate90([[1,2],[3,4]])==[[3,1],[4,2]]"),
]


def generate(model, prompt):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": 400},
    }).encode()
    req = urllib.request.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=120).read())["message"]["content"]


def evaluate(model):
    passed = 0
    for prompt, check in TASKS:
        try:
            out = generate(model, prompt)
            code = grounding.extract_code_block(out)
            ok = bool(code) and grounding.run_code(code, extra=check, timeout=8)[0]
        except Exception:
            ok = False
        passed += 1 if ok else 0
        print("  [%s] %s" % ("PASS" if ok else "FAIL", prompt[:52]))
    return passed


def main():
    models = sys.argv[1:] or ["qwen2.5-coder:1.5b", "sonder-tuned"]
    results = {}
    for m in models:
        print("=== %s ===" % m)
        results[m] = evaluate(m)
    print("\n=== RESULTS (pass@1 over %d held-out tasks) ===" % len(TASKS))
    for m in models:
        print("  %-24s %d/%d" % (m, results[m], len(TASKS)))


if __name__ == "__main__":
    main()
