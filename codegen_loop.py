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

  * A build reported "1 error" when the true count was 109. The one error was a
    duplicate member (C# CS0111): the type parsed fine but could not be
    declared, so the compiler never bound a single method body and every
    downstream error stayed invisible. A falling total read as near-success.
    -> an error count is only trusted when nothing in it stopped the compiler
    short of binding; see count_unreliable().

The loop is language-agnostic: it takes a build argv and a regex for error
lines. Writes go through file_ops and the build goes through workbench, so the
host's file-root and approval gates apply exactly as they do elsewhere.
"""
from __future__ import annotations

import json
import re

SHRINK_FLOOR = 0.75
DEFAULT_ERROR_RE = r"(?i)\b(?:error|fatal)\b"

# --- Masking errors: the ones that make the error COUNT itself a lie ----------
#
# A compiler runs in phases -- parse, declare, bind bodies -- and an error in an
# early phase stops it before the later ones report anything. The total it
# prints is then not "how broken the project is", it is "how far it got". Two
# such phases have been measured masking a real count here; both are matched on
# message SHAPE rather than error-code range, because ranges leak across
# toolchains (C# syntax errors are mostly CS1xxx, but CS8180 and CS8124 are the
# parser too) while shapes generalise.

# Phase 1, PARSE. A file the compiler cannot read stops before binding, so every
# semantic error behind it goes unreported: a file with two syntax errors can be
# masking ninety. Measured -- a run that read as "2 errors" was really 99 once
# it parsed.
DEFAULT_PARSE_ERROR_RE = (
    r"(?i)(?:expected|unexpected token|invalid expression|invalid token|"
    r"unterminated|parse error|syntax error|unexpected end of|"
    r"tuple must contain|unclosed|missing closing)"
)

# Phase 0, and the one this harness itself got wrong. Two ways a build reports a
# number that is not a count of anything:
#
#   * The compiler gave up at its own error ceiling. clang stops at 20 BY
#     DEFAULT; MSVC at 100. The remainder is never emitted, so the total is a
#     cap. The marker line itself matches DEFAULT_ERROR_RE, so it read as one
#     more ordinary error -- the same 1-vs-109 failure as the parse and declare
#     phases, one notch further along, and missed when those were added.
#
#   * The build never ran, or was killed. This harness's own
#     "error: build could not run: ..." string matches DEFAULT_ERROR_RE and
#     counts as exactly ONE error in the TRUSTWORTHY tier -- so a candidate
#     whose build never launched outscored an honest one with thirty real
#     errors, and every later attempt was compared against that fiction. A build
#     killed by a timeout before printing any error line counted as ZERO, which
#     format_report renders as BUILD SUCCEEDED.
DEFAULT_TRUNCATED_ERROR_RE = (
    r"(?i)(?:too many errors|error count exceeds|maximum number of errors|"
    r"stopping compilation|-ferror-limit|"
    r"build could not run|build timed out|timed out after|"
    r"build did not run)"
)

# Phase 2, DECLARE. A type that parses but cannot be *defined* -- a duplicate
# member, a redefinition, a conflicting declaration -- leaves the compiler with
# no usable symbol for it, so it stops before binding method bodies and every
# dependent file's errors go unreported. Measured -- a build reported "1 error"
# (CS0111, a duplicated member) when the true count was 109.
#
# Every alternative below names the DECLARATION being duplicated, which is what
# keeps this from swallowing ordinary semantic errors:
#   * "already defines a member"      C# CS0111
#   * "already contains a definition" C# CS0101 (namespace) / CS0102 (type)
#   * "already defined in <kind>"     javac "m() is already defined in class F"
#     -- the trailing kind word is load-bearing: C# CS0128 says "already defined
#     in this scope" for a duplicate LOCAL, which is a method-body error that
#     masks nothing, and must not match.
#   * "defined multiple times"        Rust E0428
#   * "redefinition of"               clang / gcc
#   * "duplicate <decl kind>"         javac "duplicate class", TS2300
#     "Duplicate identifier", linker "duplicate symbol"
#   * "conflicting types/declaration" gcc
#
# Deliberately NOT here: "does not contain a definition for" (C# CS0117/CS1061)
# and "could not be found" (CS0246). Those are the loop's normal progress
# signal -- they mean a call site is wrong, the binder ran fine, and the count
# is honest. See the module notes on why a namespace mismatch is not detectable
# from message shape.
DEFAULT_DEFINITION_ERROR_RE = (
    r"(?i)(?:already defines a member|already contains a definition|"
    r"already defined in (?:class|struct|interface|enum|namespace|module|"
    r"package|type|record|trait|object)\b|"
    r"defined multiple times|redefinition of|"
    r"duplicate (?:definition|declaration|symbol|member|identifier|class|"
    r"type|method|field)|"
    r"conflicting (?:declaration|declarations|definition|types))"
)

# NOT guarded, on purpose: a namespace mismatch -- a type that is defined but
# invisible to its dependents. It has no shape of its own. The compiler reports
# it as the same "could not be found" / "does not exist in the namespace" line
# it emits when the type was simply never written, which is the loop's single
# most common HONEST error and the one whose falling count is real progress.
# Matching it would mark almost every mid-run build unreliable and flatten the
# score tier that makes the loop converge, to catch a failure that does not even
# under-report: a type the dependents cannot see produces one error per use
# site, so a namespace mismatch INFLATES the count rather than masking it.
# (The one variant that would genuinely mask -- an unresolvable name in a base
# type list, which fails in the declare phase -- is spelled identically to the
# same name failing inside a method body, so message shape cannot separate them.
# Detecting it needs the symbol table, not the log.)

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


def definition_blocked(
    errors: list, definition_regex: str = DEFAULT_DEFINITION_ERROR_RE
) -> int:
    """How many errors are a type that parsed but could not be declared."""
    pattern = re.compile(definition_regex)
    return sum(1 for e in errors if pattern.search(e))


def count_unreliable(
    errors: list,
    parse_regex: str = DEFAULT_PARSE_ERROR_RE,
    definition_regex: str = DEFAULT_DEFINITION_ERROR_RE,
    truncated_regex: str = DEFAULT_TRUNCATED_ERROR_RE,
) -> int:
    """How many errors stop the compiler before it reports the rest.

    While any of these stand, len(errors) is a floor, not a total: it says how
    far the compiler got, not how broken the project is.
    """
    patterns = [re.compile(r) for r in (parse_regex, definition_regex, truncated_regex)]
    return sum(1 for e in errors if any(p.search(e) for p in patterns))


def build_ran(output: str, truncated_regex: str = DEFAULT_TRUNCATED_ERROR_RE) -> bool:
    """Whether the build actually executed and reported on the code.

    A build that could not launch, or was killed, tells you nothing about the
    source -- and an empty error list from one is NOT a pass. Callers must check
    this before reading success from a zero count, because zero errors from a
    build that never ran renders as BUILD SUCCEEDED.
    """
    return not re.search(truncated_regex, output or "")


def score(
    errors: list,
    parse_regex: str = DEFAULT_PARSE_ERROR_RE,
    definition_regex: str = DEFAULT_DEFINITION_ERROR_RE,
) -> tuple:
    """Rank a candidate as (masked?, blockers, total). Lower is better.

    A version with 50 real errors is strictly better than one showing 2 that
    the compiler never got past parsing or declaring, because the 2 is fiction.

    Within the masked tier, ordering is by how many blockers remain, NOT by
    total: a masked total is a floor, and a file that collapses earlier reports
    a smaller one, so ranking by total actively prefers the more broken file.
    """
    masked = count_unreliable(errors, parse_regex, definition_regex)
    if not masked:
        return (0, 0, len(errors))
    # Both candidates masked: their totals are FLOORS, and a file that fails
    # EARLIER produces a SMALLER one. Ordering by total therefore prefers the
    # more broken file -- measured, a candidate with 43 errors (35 masking) beat
    # the incumbent's 109 (36 masking) purely by collapsing sooner. Rank by how
    # much masking is left instead: fewer blockers is genuinely closer to a
    # build that can be read at all. Total is only a tie-break.
    return (1, masked, len(errors))


def truncated_blocked(errors: list, truncated_regex: str = DEFAULT_TRUNCATED_ERROR_RE) -> int:
    """How many errors say the report itself was capped, or never produced."""
    pattern = re.compile(truncated_regex)
    return sum(1 for e in errors if pattern.search(e))


def describe_total(
    errors: list,
    parse_regex: str = DEFAULT_PARSE_ERROR_RE,
    definition_regex: str = DEFAULT_DEFINITION_ERROR_RE,
    truncated_regex: str = DEFAULT_TRUNCATED_ERROR_RE,
) -> str:
    """Report a total, flagging when it cannot be trusted, and why."""
    reasons = []
    blocked = parse_blocked(errors, parse_regex)
    if blocked:
        reasons.append("%d parse error(s)" % blocked)
    undeclared = definition_blocked(errors, definition_regex)
    if undeclared:
        reasons.append("%d failed definition(s)" % undeclared)
    capped = truncated_blocked(errors, truncated_regex)
    if capped:
        reasons.append("%d capped-or-unrun build(s)" % capped)
    if not reasons:
        return "%d total" % len(errors)
    return ("%d total (UNRELIABLE: %s are masking the semantic count)"
            % (len(errors), " and ".join(reasons)))


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


def format_report(rows: list, final_errors: list, ok: bool, ran: bool = True) -> str:
    """Human-readable outcome. Honest about what is unproven.

    ``ran`` says the build actually executed. It defaults True for callers that
    cannot tell, but passing it matters: a build killed by a timeout before it
    printed an error line yields an EMPTY error list, and an empty list is
    indistinguishable from a clean compile unless someone says so.
    """
    lines = ["=== codegen build loop ==="]
    for row in rows:
        lines.append(
            "  %-24s %s" % (row["name"], row["note"])
        )
    lines.append("")
    if not ran:
        lines.append("BUILD DID NOT RUN")
        lines.append(
            "The build could not be launched or was killed, so nothing here "
            "says anything about the code. This is NOT a pass, and an empty "
            "error list from an unrun build is not a clean compile."
        )
        for e in final_errors[:5]:
            lines.append("  " + e[:200])
        return "\n".join(lines)
    if ok:
        lines.append("BUILD SUCCEEDED")
        lines.append(
            "NOTE: a green build is not proof the program works. A field that is "
            "declared and never assigned is not a compile error. Run the "
            "program's own tests before believing it."
        )
    else:
        lines.append("BUILD FAILED: %d distinct error line(s)" % len(final_errors))
        masked = count_unreliable(final_errors)
        if masked:
            lines.append(
                "WARNING: %d of these stop the compiler before it binds the rest "
                "of the project, so the count above is a FLOOR, not a total -- "
                "measured, a build reporting 1 really had 109. Fix these first "
                "and re-run before reading any other number here." % masked
            )
        for e in final_errors[:15]:
            lines.append("  " + e[:200])
        if len(final_errors) > 15:
            lines.append("  ... and %d more" % (len(final_errors) - 15))
    return "\n".join(lines)


# --- Skeleton fill: the harness owns the declarations ------------------------
#
# The whole-file loop above asks a model to write a file, which means writing
# its public surface. Measured over an 8-file C# project, that is the thing a
# 7B-class model cannot do: the final error mix was CS1061 member-not-found
# (28), CS0103 name-not-found (21), CS0272 assign-to-readonly (16), CS0117
# no-such-definition (15), CS1503 argument-mismatch (13) -- every dominant
# class two files disagreeing about an API rather than bad code inside a
# method. Handing each file the REAL extracted signatures of its dependencies
# did not fix it; one file was given another's actual surface and still called
# four members that existed in neither the contract nor the extraction.
#
# So this mode stops asking. The caller supplies a skeleton whose declarations
# are already correct, marked with body slots, and the model is asked for one
# body at a time. Three things change:
#
#   * The project compiles to ZERO errors before any model output exists, so
#     the baseline is trustworthy and every later error belongs to exactly one
#     body.
#   * A bad body reverts to its placeholder instead of poisoning the file. The
#     whole-file mode's dominant failure -- 7 of 12 regenerations rejected as
#     parse-broken, each reporting a LOWER masked count than the incumbent --
#     cannot occur, because the model never writes the structure.
#   * The metric stops being an error count, which would sit near zero by
#     construction and say nothing, and becomes "bodies implemented / N".
#
# Deliberately language-agnostic: a slot is a comment marker naming the slot
# followed by exactly one placeholder line. That shape holds for `// BODY:x` +
# `throw new NotImplementedException();` in C-family languages and `# BODY:x` +
# `raise NotImplementedError` in Python, which is why the marker is matched on
# comment syntax rather than on any one language's grammar.
BODY_MARKER = re.compile(
    r"[ \t]*(?://|#|--|;)[ \t]*BODY:(?P<name>\w+)[ \t]*\n(?P<placeholder>[ \t]*\S[^\n]*)"
)


def body_slots(text: str) -> list:
    """Every body slot in a skeleton, in source order."""
    return [match.group("name") for match in BODY_MARKER.finditer(text or "")]


def splice_body(text: str, name: str, body: str) -> str:
    """Replace one slot's marker+placeholder with generated code.

    Returns the text UNCHANGED when the slot is absent, so a caller cannot
    silently believe it installed a body it did not -- compare the result
    against the input to detect that.
    """
    def replace(match):
        if match.group("name") != name:
            return match.group(0)
        indent = re.match(r"[ \t]*", match.group("placeholder")).group(0)
        lines = [line.rstrip() for line in (body or "").strip().splitlines()]
        if not lines:
            return match.group(0)
        return "\n".join(indent + line if line else "" for line in lines)

    return BODY_MARKER.sub(replace, text or "")


def body_signature(text: str, name: str) -> str:
    """The declaration line owning a slot, for the prompt.

    Walks back over blank and brace-only lines so the caller gets the
    signature rather than the `{` that follows it.
    """
    lines = (text or "").splitlines()
    for index, line in enumerate(lines):
        if ("BODY:" + name) in line:
            for back in range(index - 1, max(-1, index - 8), -1):
                candidate = lines[back].strip()
                if candidate and candidate not in ("{", "}", "(", ")"):
                    return candidate
            break
    return name


def collapse_bodies(text: str, filler: str = "...") -> str:
    """The skeleton with every body collapsed -- a dependency brief.

    This is the same text that compiles, so a brief handed to a dependent
    cannot drift from the declarations it describes. That drift is what the
    prose contract in the whole-file mode suffered from.
    """
    def replace(match):
        indent = re.match(r"[ \t]*", match.group("placeholder")).group(0)
        return indent + filler

    return BODY_MARKER.sub(replace, text or "")
