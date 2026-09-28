You are inventing ONE new Python coding practice task for a training curriculum.

Output a single JSON object with exactly these keys:
- "name": a snake_case Python function name for the task (e.g. "reverse_words").
- "prompt": a self-contained task description asking for that Python function, ending with the exact sentence "Return ONLY the function in one python code block."
- "check": Python code with 2 or more `assert` statements that exercise the function by name (referring to it as defined by "name") and would fail on a wrong or missing implementation.
- "reference": a correct Python implementation of the function described in "prompt".

Rules:
- The task must be genuinely different from common textbook exercises already seen (reverse a string, factorial, fizzbuzz, is_prime, etc.) — invent something novel.
- "reference" must actually satisfy "check" when run together.
- Output ONLY the JSON object. No prose, no markdown fences, no explanation before or after.

