"""prompt_clarifier — detect an underspecified coding prompt and ask about it.

The base model selected by Sonder Runtime is frozen: it will happily generate a confident, wrong
answer to a vague task ("write something that processes the data") because
nothing upstream stops it and asks what's actually meant. That guess then
burns a solver.solve() repair loop, or worse, produces plausible-looking code
that silently does the wrong thing because the spec never nailed down the
types or an expected input/output pair.

This module checks a prompt's TEXT (no execution, no model call) for three
concrete gaps that make a coding task hard to solve correctly on the first
try:
  - no worked input/output example  (no ">>>", "e.g.", "for example",
    "Example:", or a call like `foo(3, "x") -> 7` with a literal argument)
  - no function/method name or signature (no `def`, no backtick-quoted
    `name(...)`, no "function/method named/called X", no "class X")
  - ambiguous parameter/return types (vague nouns like "data", "input",
    "items", "numbers" with no concrete type — int/str/list of X/dict of X/
    etc. — anywhere nearby, and no Python-style type hints in the prompt
    at all)

clarify(prompt) returns a list of specific clarifying questions (empty if the
prompt already covers all three). Pure and stdlib-only: one function of a
string in, one list of strings out. No GPU, no network, no subprocess.
"""
import re

# --- signal 1: worked input/output example -----------------------------------

_IO_EXAMPLE_PATTERNS = (
    re.compile(r">>>"),                                   # doctest-style
    re.compile(r"\be\.g\.,?", re.I),
    re.compile(r"\bfor example\b", re.I),
    re.compile(r"\bexamples?\s*:", re.I),
    re.compile(r"\binput\s*:.*?\boutput\s*:", re.I | re.S),
    # a call with a literal argument (digit or quoted string, so it reads as
    # a worked value, not a type name) followed by an arrow/return word:
    # e.g. `is_palindrome("racecar") -> True` or `add(2, 3) returns 5`.
    re.compile(r"[a-zA-Z_]\w*\(\s*[^()]*[\d\"'][^()]*\)\s*(?:->|=>|should\s+return\b|returns?\b)", re.I),
)


def has_io_example(text):
    """True if `text` contains at least one concrete input/output example."""
    text = text or ""
    return any(pat.search(text) for pat in _IO_EXAMPLE_PATTERNS)


# --- signal 2: function/method name or signature -----------------------------

_SIGNATURE_PATTERNS = (
    re.compile(r"\bdef\s+[a-zA-Z_]\w*\s*\("),                       # literal `def foo(`
    re.compile(r"`[a-zA-Z_]\w*\s*\([^`]*\)`"),                      # backtick-quoted `foo(...)`
    re.compile(r"\b(function|method)\s+(called|named|titled)\s+[`'\"]?[a-zA-Z_]\w*", re.I),
    re.compile(r"\b(function|method)\s+[`'\"]?[a-zA-Z_]\w*\s*\(", re.I),  # "write a function foo("
    re.compile(r"\bclass\s+[a-zA-Z_]\w*\b"),
)


def has_function_signature(text):
    """True if `text` names the function/method (or class) to be written."""
    text = text or ""
    return any(pat.search(text) for pat in _SIGNATURE_PATTERNS)


# --- signal 3: ambiguous parameter/return types ------------------------------

# Words that describe "some data" without saying what kind. Flagged only when
# no concrete type qualifier appears nearby (see _has_type_qualifier_near).
_VAGUE_TYPE_TERMS = re.compile(
    r"\b(numbers?|values?|items?|elements?|data|things?|stuff|inputs?|"
    r"arguments?|params?|args?|arrays?|lists?|collections?|sequences?|"
    r"objects?|results?)\b",
    re.I,
)

# Concrete type words/phrases that resolve a vague term into a real type.
_CONCRETE_TYPE_QUALIFIER = re.compile(
    r"\b(int(eger)?s?|float(ing point)?s?|str(ing)?s?|bool(ean)?s?|chars?|"
    r"character\w*|double\w*|long\w*|bytes?|json|dataframe|matrix|graph|tree|"
    r"node\w*|dict(ionary)?(?:ies)?|tuple\w*|set\w*|(list|array|dict|tuple|set)s?\s+of\s+\w+)\b",
    re.I,
)

# Explicit Python-style type hints anywhere in the prompt (`x: int`,
# `-> bool`, `List[int]`, `Optional[str]`, ...). Their presence means the
# author already pinned down types, so we don't nag about vague nouns too.
_TYPE_HINT_PATTERN = re.compile(
    r"->\s*(int|str|float|bool|list|dict|tuple|set|List|Dict|Tuple|Optional|Any|None)\b"
    r"|:\s*(int|str|float|bool|List\[|Dict\[|Tuple\[|Optional\[|Set\[)"
    r"|\b(List|Dict|Tuple|Optional|Set)\[",
)


def has_type_hints(text):
    """True if `text` already contains Python-style type-hint syntax."""
    return bool(_TYPE_HINT_PATTERN.search(text or ""))


def _has_type_qualifier_near(text, span, window=30):
    start = max(0, span[0] - window)
    end = min(len(text), span[1] + window)
    return bool(_CONCRETE_TYPE_QUALIFIER.search(text[start:end]))


def ambiguous_type_terms(text):
    """Return the vague-type words in `text` that have no nearby concrete
    type qualifier (e.g. "numbers" with no "integers"/"floats" close by).
    Case-normalized to lowercase; may contain duplicates (callers dedupe).
    """
    text = text or ""
    return [m.group(0).lower() for m in _VAGUE_TYPE_TERMS.finditer(text)
            if not _has_type_qualifier_near(text, m.span())]


# --- public API ---------------------------------------------------------------

_MAX_VAGUE_TERMS_SHOWN = 5


def clarify(prompt):
    """Return a list of clarifying questions for an underspecified coding
    prompt. Empty list means the prompt already looks well-specified.

    Checks, in order:
      1. Is there a worked input/output example?
      2. Is the function/method (or class) name and call shape given?
      3. Are parameter/return types concrete, or at least type-hinted?
    """
    text = (prompt or "").strip()
    if not text:
        return ["The prompt is empty — describe what the function should do, "
                "give it a name and parameter list, and include at least one "
                "input/output example."]

    questions = []

    if not has_io_example(text):
        questions.append(
            "What should the function return for a specific example input? "
            "Add at least one worked case, e.g. 'for input X the output should be Y'."
        )

    if not has_function_signature(text):
        questions.append(
            "What should the function (or class/method) be named, and what "
            "are its exact parameters? e.g. 'def solve(items: list[int]) -> int:'."
        )

    if not has_type_hints(text):
        vague = sorted(set(ambiguous_type_terms(text)))
        if vague:
            shown = vague[:_MAX_VAGUE_TERMS_SHOWN]
            questions.append(
                "What are the exact types for: %s? (e.g. 'a list of positive "
                "integers' instead of 'numbers', or 'a string' instead of "
                "'input')." % ", ".join(shown)
            )

    return questions


def is_well_specified(prompt):
    """Convenience: True iff clarify(prompt) has nothing to ask about."""
    return not clarify(prompt)
