"""Guarded single-function code improvement.

The productised form of what the self-modification work learned: a model is
reliable at TRANSFORMING one function when every fact it needs is in the prompt,
and unreliable at everything else. So this module asks for exactly one function,
splices only that function back, and refuses the specific failure shapes that
were each measured slipping through a green test suite.

The guards live here, as one source of truth, so the nightly self-mod loop and
the interactive `/refactor` command cannot drift apart -- they did once, and a
bug fixed in one copy survived in the other.

Nothing here writes a file or calls a model directly. `improve_function` takes
an injected `ask_fn(prompt, tier) -> str` seam so it is testable without a
model, and returns a result dict for the caller to apply or reject.
"""
from __future__ import annotations

import difflib
import ast
import io
import re
import tokenize

# A rewrite that returns less than this fraction of the original is treated as a
# deletion, not an edit. An unguarded repair converges on deletion because
# removing the offending code is always a valid way to satisfy a checker;
# measured at 44% and 13% of a file returned to fix a two-character typo.
SHRINK_FLOOR = 0.75

_DEF_RE = re.compile(r"^(?P<indent>[ \t]*)def\s+(?P<name>\w+)\s*\(", re.M)
_NUMERIC_GATE = re.compile(r"[<>]=?\s*-?\d|\d\s*[<>]=?")
_DEFAULTED_GET = re.compile(r"\.get\(\s*[^,()]+,\s*[^)]+\)")
_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_+-]*\s*$", re.M)


def list_functions(source: str) -> list[str]:
    """Top-level (column-0) function names in source order."""
    try:
        tree = ast.parse(source or "")
    except SyntaxError:
        return []
    return [node.name for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _block_bounds(lines: list, name: str):
    """(first, last) line indices of a top-level `def name` block, or None.

    `last` is the first line after the block: the next column-0 non-blank line.
    Balances the signature's parentheses first, because a multi-line signature
    closes with `):` at column 0 and a naive scan would stop there and leave
    half a def behind (which then does not parse).
    """
    try:
        tree = ast.parse("".join(lines))
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            starts = [node.lineno] + [item.lineno for item in node.decorator_list]
            return min(starts) - 1, node.end_lineno
    return None


def extract_function(source: str, name: str) -> str | None:
    """The full text of one top-level function, or None if it is not defined."""
    lines = source.splitlines(keepends=True)
    bounds = _block_bounds(lines, name)
    if bounds is None:
        return None
    first, last = bounds
    return "".join(lines[first:last])


def strip_fences(text: str) -> str:
    """Drop markdown code fences a model wraps its reply in."""
    value = (text or "").strip()
    lines = value.splitlines()
    if len(lines) >= 2 and _FENCE_RE.fullmatch(lines[0]) and _FENCE_RE.fullmatch(lines[-1]):
        return "\n".join(lines[1:-1]).strip()
    return value


def splice_function(original: str, reply: str) -> str | None:
    """Replace one top-level function in `original` with the model's version.

    Returns the new module text, or None when the reply is not a single function
    that already exists at module level -- a reply naming a function the file
    does not have is an invention, and inserting it would add code nobody asked
    for rather than change the code that was targeted.
    """
    text = strip_fences(reply)
    try:
        reply_tree = ast.parse(text)
    except SyntaxError:
        return None
    if len(reply_tree.body) != 1 or not isinstance(
        reply_tree.body[0], (ast.FunctionDef, ast.AsyncFunctionDef)
    ):
        return None
    replacement = reply_tree.body[0]
    name = replacement.name

    lines = original.splitlines(keepends=True)
    bounds = _block_bounds(lines, name)
    if bounds is None:
        return None
    first, last = bounds
    try:
        original_tree = ast.parse(original)
    except SyntaxError:
        return None
    current = next(
        (node for node in original_tree.body
         if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name),
        None,
    )
    if current is None or type(current) is not type(replacement):
        return None
    if ast.dump(current.args, include_attributes=False) != ast.dump(
        replacement.args, include_attributes=False
    ):
        return None
    if [ast.dump(item, include_attributes=False) for item in current.decorator_list] != [
        ast.dump(item, include_attributes=False) for item in replacement.decorator_list
    ]:
        return None
    newline = "\r\n" if "\r\n" in original else "\n"
    body = text.rstrip().replace("\r\n", "\n").replace("\n", newline) + newline
    candidate = "".join(lines[:first]) + body + newline + "".join(lines[last:])
    try:
        ast.parse(candidate)
    except SyntaxError:
        return None
    return candidate


def shrink_rejected(previous: str, candidate: str, floor: float = SHRINK_FLOOR) -> bool:
    """Whether `candidate` deleted too much of `previous` to be a fix."""
    if not previous:
        return False
    return len(candidate) < floor * len(previous)


def _code_only(text: str) -> list:
    """Statements with comments and blank lines removed.

    Docstrings survive on purpose: a docstring is the function's stated contract
    and improving one is real work, while `# added check` appended to a line
    that already existed is not.
    """
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text or "").readline)
        kept = [tok for tok in tokens if tok.type != tokenize.COMMENT]
        value = tokenize.untokenize(kept)
    except (tokenize.TokenError, IndentationError):
        value = text or ""
    return [line.rstrip() for line in value.splitlines() if line.strip()]


def diff_objection(original: str, edited: str) -> str | None:
    """Why this change should be thrown away, or None to keep it.

    Each rule caught a candidate that passed a full test suite while doing
    something other than improving the code. Over-rejecting a borderline
    refactor is the tolerable direction: the cost is a skipped suggestion, not a
    broken function.
    """
    before, after = _code_only(original), _code_only(edited)
    if before == after:
        # A trailing comment on a line that already existed leaves the same
        # statement in place; comparing the comment-stripped code is what
        # actually detects "nothing happened".
        return "comment-only change (the code is unchanged)"

    removed, added = [], []
    for line in difflib.unified_diff(before, after, lineterm="", n=0):
        if line.startswith("---"):
            continue
        if line.startswith("-"):
            removed.append(line[1:].strip())
    for line in difflib.unified_diff(before, after, lineterm="", n=0):
        if line.startswith("+++"):
            continue
        if line.startswith("+"):
            added.append(line[1:].strip())

    # A net-new print() -- library code logs, it does not print, and a print in
    # an MCP server can corrupt a stdio protocol stream. It is also the tell for
    # a fail-open handler (`except: print(...); return []`).
    if any("print(" in ln for ln in added) and not any("print(" in ln for ln in removed):
        return "adds a print() -- library code logs, it does not print"

    # A rewritten return/raise changes the function's contract; callers break
    # while the tests, which do not cover the path, stay green.
    for line in removed:
        if re.match(r"^(return|raise)\b", line):
            return ("rewrites an existing `%s` -- that is a contract change, "
                    "not a check" % line.split()[0])

    # An added raise gated by a hardcoded numeric threshold is an invented
    # restriction on previously-valid input. A raise on plain truthiness
    # (`if not x:`) is spared -- that is ordinary None/empty defence.
    if any(re.match(r"^raise\b", ln) for ln in added):
        if any(_NUMERIC_GATE.search(ln) and re.search(r"\b(if|while|assert)\b", ln)
               for ln in added):
            return ("adds a raise gated by a hardcoded numeric threshold -- "
                    "an invented restriction on previously-valid input, not a check")

    # A defaulted lookup turned strict is a contract change spelled differently:
    # `schema.get("type", "any")` (a documented permissive default) rewritten to
    # a hard failure on a missing key.
    if any(_DEFAULTED_GET.search(line) for line in removed):
        if not any(_DEFAULTED_GET.search(line) for line in added):
            return ("removes a defaulted lookup (.get(k, default)) -- that turns "
                    "a permissive default into a strict path, a contract change")
    return None


def unified_diff(original: str, edited: str, path: str = "file") -> str:
    """A readable unified diff between two module texts."""
    return "".join(difflib.unified_diff(
        original.splitlines(keepends=True),
        edited.splitlines(keepends=True),
        fromfile="a/%s" % path, tofile="b/%s" % path,
    ))


def _reply_failed(reply: str) -> str:
    """The runtime's error string when a call produced no answer.

    ensemble_answer reports failure by RETURNING prose, not by raising. Splicing
    that into a source file is how a dead backend becomes a code change.
    """
    head = (reply or "").strip()[:200]
    if head.startswith("ERROR:") or "no model produced an answer" in head:
        return head
    if not head:
        return "empty reply from the model"
    return ""


def improve_function(source: str, name: str, ask_fn, *, tier: str = "code",
                     objective: str = "") -> dict:
    """Propose a guarded improvement to one function. Never writes a file.

    ask_fn(prompt, tier) -> str is injected so this is testable without a model.
    Returns a dict:
        ok         -- True when a usable, guarded candidate was produced
        reason     -- why it was rejected, when ok is False
        function   -- the target name
        original   -- the extracted original function text
        edited     -- the full module text with the one function replaced
        diff       -- a unified diff of the change (empty when rejected)
        objective  -- the objective handed to the model
    """
    result = {"ok": False, "reason": "", "function": name, "original": None,
              "edited": None, "diff": "", "objective": objective}

    original_fn = extract_function(source, name)
    if original_fn is None:
        result["reason"] = "no top-level function %r in the source" % name
        return result
    result["original"] = original_fn

    ask = (
        "Improve this ONE Python function. %s\n\n"
        "Rules:\n"
        "- Output ONLY the function, complete, from its `def` line to its last "
        "line. No prose, no fence, no surrounding code.\n"
        "- Keep its name, signature and indentation exactly.\n"
        "- Change as little as possible; do not reformat untouched lines.\n"
        "- Prefer a real fix (a missing guard, a wrong edge case, an inaccurate "
        "docstring) over cosmetic edits. If nothing is worth changing, reply "
        "with the function unchanged.\n\n%s"
        % (objective or "Fix a real defect or add a missing safety check.",
           original_fn)
    )
    reply = ask_fn(ask, tier)
    failure = _reply_failed(reply)
    if failure:
        result["reason"] = "model unavailable: %s" % failure
        return result

    edited_module = splice_function(source, reply)
    if edited_module is None:
        result["reason"] = "reply was not a single replaceable function"
        return result
    if edited_module == source:
        result["reason"] = "no change proposed"
        return result

    edited_fn = extract_function(edited_module, name) or ""
    if shrink_rejected(original_fn, edited_fn):
        result["reason"] = ("candidate returned %d%% of the original function "
                            "(deletion guard)"
                            % (100 * len(edited_fn) // max(1, len(original_fn))))
        return result

    objection = diff_objection(original_fn, edited_fn)
    if objection:
        result["reason"] = objection
        return result

    result["ok"] = True
    result["edited"] = edited_module
    result["diff"] = unified_diff(source, edited_module)
    return result
