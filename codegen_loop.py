"""Generate code, compile it, and repair it -- with the guards that make that safe.

A naive "ask the model, feed the compiler errors back" loop does not converge on
working code. It converges on *deleted* code, because removing the offending
lines is always a valid way to make an error go away. Every guard here exists
because the unguarded version was measured doing the wrong thing while driving a
local ensemble to write an 8-file C# game (2026-08-06):

  * Asked only to insert two missing '.' characters, a whole-file repair came
    back at 44% of the original length. Asked to add one missing type name, 13%.
    Both parse cleanly -- an unguarded loop reports success on a gutted program.
    -> SHRINK_FLOOR rejects a "repair" that is mostly deletion.

  * Regenerating a file that already compiled cleanly produced one with six new
    errors. A loop that only compares its own attempts will replace working code
    with worse.  -> the incumbent file is seeded as the baseline and a clean file
    is never regenerated.

  * Scoring a file by its OWN error count picks the wrong winner: one rewrite
    took a file from 10 errors to 14 while taking the project from 106 to 14,
    because everything downstream could finally resolve against it.
    -> scoring is on TOTAL project errors.

  * Missing imports are the single largest error class and are pure bookkeeping.
    Fixing them by lookup table took one project from 50 errors to 2 in a single
    pass, with no deletion risk.  -> mechanical fixes are done here, in code,
    never by asking the model.

  * A model asked to fix a line with a missing '.' echoed the broken line back
    verbatim, twice. Character-level slips are close to invisible to it.
    -> known slips are rewritten deterministically.

  * The model cannot hold a contract spanning files even when that contract is
    in every prompt. -> dependents are handed the API *extracted from the
    generated source*, not the API someone intended.

The loop is language-agnostic: it takes a build argv and a regex for error
lines. Writes go through file_ops and the build goes through workbench, so the
host's file-root and approval gates apply exactly as they do elsewhere.
"""
from __future__ import annotations

import json
import os
import re

SHRINK_FLOOR = 0.75
DEFAULT_ERROR_RE = r"(?i)\b(?:error|fatal)\b"

# A compiler that cannot PARSE a file stops before BINDING, so every semantic
# error behind it goes unreported: a file with two syntax errors can be masking
# ninety. A falling total is therefore not progress while any parse error
# remains -- measured, a run that read as "2 errors" was really 99 once it
# parsed. Matched on message shape rather than error-code range because ranges
# leak (C# syntax errors are mostly CS1xxx, but CS8180 and CS8124 are the parser
# too), and shape generalises across toolchains.
DEFAULT_PARSE_ERROR_RE = (
    r"(?i)(?:expected|unexpected token|invalid expression|invalid token|"
    r"unterminated|parse error|syntax error|unexpected end of|"
    r"tuple must contain|unclosed|missing closing)"
)

# Declaration-ish lines worth showing a dependent file. Deliberately shallow:
# the input is often not parseable (that is why it is in this loop), so a real
# parser would refuse it exactly when a signature is most needed.
_DECL_RE = re.compile(
    r"^\s*(?:public|export|pub|def |fn |func |class |interface |enum |struct |type )"
)


def extract_api(text: str, max_lines: int = 60) -> str:
    """Pull declaration-looking lines out of a source file."""
    out = []
    for line in text.split("\n"):
        if len(out) >= max_lines:
            break
        stripped = line.strip()
        if not stripped or stripped.startswith(("//", "#", "*", "/*")):
            continue
        if not _DECL_RE.match(line):
            continue
        # Cut bodies and initialisers; keep the signature.
        for token in ("=>", " {", " ="):
            if token in stripped:
                stripped = stripped.split(token, 1)[0]
                break
        out.append("    " + stripped.rstrip("{ ").rstrip())
    return "\n".join(out)


def dependency_brief(sources: dict) -> str:
    """Describe the real API of already-written sibling files."""
    chunks = []
    for name, text in sources.items():
        api = extract_api(text)
        if api.strip():
            chunks.append(f"  // --- {name} (ACTUAL current API) ---\n{api}")
    if not chunks:
        return ""
    return (
        "THESE FILES ALREADY EXIST. Their real API is below, read directly from\n"
        "the source. Call these EXACTLY as written. Do not redefine them and do\n"
        "not call a member that is not listed.\n\n" + "\n\n".join(chunks) + "\n"
    )


def strip_code(text: str) -> str:
    """Recover a bare source file from a model response."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    fenced = re.findall(r"```[a-zA-Z#+]*\s*\n(.*?)```", text, flags=re.S)
    if fenced:
        text = max(fenced, key=len)
    return text.strip() + "\n"


def count_errors(output: str, error_regex: str = DEFAULT_ERROR_RE) -> list:
    """Distinct error lines in build output, order preserved."""
    pattern = re.compile(error_regex)
    seen, out = set(), []
    for line in output.split("\n"):
        line = line.strip()
        if not line or not pattern.search(line):
            continue
        if line not in seen:
            seen.add(line)
            out.append(line)
    return out


def parse_blocked(errors: list, parse_regex: str = DEFAULT_PARSE_ERROR_RE) -> int:
    """How many errors are the parser refusing to read the source."""
    pattern = re.compile(parse_regex)
    return sum(1 for e in errors if pattern.search(e))


def score(errors: list, parse_regex: str = DEFAULT_PARSE_ERROR_RE) -> tuple:
    """Rank a candidate. Lower is better; parse-clean always beats parse-broken.

    A parse-clean version with 50 errors is strictly better than a parse-broken
    one showing 2, because the 2 is fiction -- the binder never ran.
    """
    return (1 if parse_blocked(errors, parse_regex) else 0, len(errors))


def describe_total(errors: list, parse_regex: str = DEFAULT_PARSE_ERROR_RE) -> str:
    """Report a total, flagging when it cannot be trusted."""
    blocked = parse_blocked(errors, parse_regex)
    if blocked:
        return ("%d total (UNRELIABLE: %d parse error(s) are masking the "
                "semantic count)" % (len(errors), blocked))
    return "%d total" % len(errors)


def apply_slips(text: str, slips) -> tuple:
    """Rewrite known wrong-library calls. Returns (text, count)."""
    hits = 0
    for pattern, repl in slips:
        text, n = re.subn(pattern, repl, text)
        hits += n
    return text, hits


def parse_slips(slips_json: str):
    """Read a [[pattern, replacement], ...] table."""
    if not str(slips_json or "").strip():
        return []
    try:
        rows = json.loads(slips_json)
    except ValueError as exc:
        raise ValueError("slips_json must be JSON: %s" % exc)
    out = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            raise ValueError("each slip must be [pattern, replacement]")
        try:
            re.compile(row[0])
        except re.error as exc:
            raise ValueError("bad slip pattern %r: %s" % (row[0], exc))
        out.append((row[0], row[1]))
    return out


def parse_files(files_json: str) -> list:
    """Read {"name": "spec"} or [{"name":..., "spec":...}] into ordered pairs."""
    try:
        data = json.loads(files_json)
    except ValueError as exc:
        raise ValueError("files_json must be JSON: %s" % exc)
    if isinstance(data, dict):
        return list(data.items())
    out = []
    for row in data:
        if not isinstance(row, dict) or "name" not in row:
            raise ValueError("each file entry needs a 'name'")
        out.append((row["name"], row.get("spec", "")))
    return out


def shrink_rejected(previous: str, candidate: str, floor: float = SHRINK_FLOOR) -> bool:
    """True when a replacement is short enough to be a deletion, not a fix."""
    if not previous:
        return False
    if len(candidate.strip()) < 40:
        return True
    return (len(candidate) / max(1, len(previous))) < floor


def format_report(rows: list, final_errors: list, ok: bool) -> str:
    """Human-readable outcome. Honest about what is unproven."""
    lines = ["=== codegen build loop ==="]
    for row in rows:
        lines.append(
            "  %-24s %s" % (row["name"], row["note"])
        )
    lines.append("")
    if ok:
        lines.append("BUILD SUCCEEDED")
        lines.append(
            "NOTE: a green build is not proof the program works. A field that is "
            "declared and never assigned is not a compile error. Run the "
            "program's own tests before believing it."
        )
    else:
        lines.append("BUILD FAILED: %d distinct error line(s)" % len(final_errors))
        for e in final_errors[:15]:
            lines.append("  " + e[:200])
        if len(final_errors) > 15:
            lines.append("  ... and %d more" % (len(final_errors) - 15))
    return "\n".join(lines)
