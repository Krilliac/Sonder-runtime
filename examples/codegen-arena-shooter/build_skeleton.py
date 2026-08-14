"""Fill harness-owned C# skeletons one method body at a time.

The v1 harness (`build_with_sonder.py`) asked the model for whole files and
measured 85 errors, of which every dominant class -- CS1061, CS0103, CS0272,
CS0117, CS1503 -- was two files disagreeing about an API. That is the failure a
7B-class model cannot be prompted out of: it writes correct functions and
cannot hold a system. See `skeleton.py` for the measurements.

So this driver never asks for a system. `skeleton.py` owns every declaration,
so the project compiles to ZERO errors before any model output exists, and the
model is asked only for one method body at a time with every fact it needs in
the prompt -- the transformation shape it is good at.

Two consequences worth stating, because they change what the number means:

  * A body that breaks the build is reverted to its placeholder and the run
    continues. One bad body can no longer poison a file, and the v1 failure
    mode where 7 of 12 regenerations were rejected as parse-broken cannot
    happen: the model does not write the structure.
  * The headline metric is therefore NOT an error count. It is "how many of
    the N bodies were implemented without breaking the build". An error count
    would sit near zero by construction and would say nothing.

Usage:
    python build_skeleton.py                      # all bodies
    python build_skeleton.py --only GameMap.cs    # one file
    python build_skeleton.py --limit 5            # first 5 bodies (smoke)
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import os
import re
import subprocess
import sys
import time

SONDER = os.environ.get(
    "SONDER_RUNTIME",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."),
)
HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.join(HERE, "FpsGame_Skeleton")

# The example owns small helper modules (``skeleton``, ``bodynotes`` and the
# project scaffold). Keep its directory ahead of the runtime root: otherwise
# a same-named runtime helper can silently shadow an example contract.
sys.path.insert(0, SONDER)
sys.path.insert(0, HERE)

import skeleton  # noqa: E402
import specs  # noqa: E402
import bodynotes  # noqa: E402
import codegen_loop  # noqa: E402
import server  # noqa: E402

# ``server``/``codegen_loop`` may legitimately import the runtime's own
# project_scaffold module. Load this example-local manifest helper under a
# distinct module identity so that existing ``sys.modules`` entries cannot
# redirect a generated game to unrelated runtime scaffolding.
_SCAFFOLD_SPEC = importlib.util.spec_from_file_location(
    "arena_shooter_project_scaffold", os.path.join(HERE, "project_scaffold.py"),
)
if _SCAFFOLD_SPEC is None or _SCAFFOLD_SPEC.loader is None:
    raise RuntimeError("arena shooter project scaffold is unavailable")
_SCAFFOLD_MODULE = importlib.util.module_from_spec(_SCAFFOLD_SPEC)
_SCAFFOLD_SPEC.loader.exec_module(_SCAFFOLD_MODULE)
ensure_project_file = _SCAFFOLD_MODULE.ensure_project_file

FENCE = re.compile(r"^\s*```[a-zA-Z0-9_+-]*\s*$", re.M)

# Compilation alone cannot see two Raylib lifecycle bugs observed in the
# generated entrypoint: a match can start without a local player, and screen
# handlers can incorrectly nest BeginDrawing/EndDrawing inside Main's frame.
# These narrow body-local checks are derived from the published skeleton
# contract. They do not attempt to grade gameplay or replace the real build.
_REQUIRED_BODY_TEXT = {
    ("Program.cs", "StartMatch"): ("_match.AddLocalPlayer(",),
}
_FORBIDDEN_BODY_TEXT = {
    ("Program.cs", "DoLobby"): ("Raylib.BeginDrawing", "Raylib.EndDrawing"),
    ("Program.cs", "DoScoreboard"): ("Raylib.BeginDrawing", "Raylib.EndDrawing"),
}


def body_contract_issues(name: str, body_name: str, body: str) -> list[str]:
    """Return deterministic, body-local contract violations before compiling."""
    source = str(body or "")
    key = (name, body_name)
    issues = []
    for required in _REQUIRED_BODY_TEXT.get(key, ()):
        if required not in source:
            issues.append("must contain %s" % required)
    for forbidden in _FORBIDDEN_BODY_TEXT.get(key, ()):
        if forbidden in source:
            issues.append("must not contain %s" % forbidden)
    return issues


def build_errors():
    """Distinct compiler error lines, and whether the build actually ran.

    Both are needed: a build that never launched prints no errors, which reads
    as success. codegen_loop.build_ran exists because that exact confusion
    scored a candidate whose build was killed as the best in a run.
    """
    try:
        proc = subprocess.run(
            ["dotnet", "build", "-c", "Release", "--nologo"],
            cwd=PROJECT, capture_output=True, text=True, timeout=600,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:
        return ["error: build could not run: %s" % exc], False
    errors = sorted({ln.strip() for ln in out.splitlines() if "error CS" in ln})
    return errors, codegen_loop.build_ran(out)


def compiler_error_preview(errors: list[str], *, limit: int = 3) -> str:
    """Render a bounded diagnostic for a rejected body attempt.

    A count tells the runner that a candidate is unsafe to keep, but it does
    not tell the operator whether the model ignored the body-only contract,
    recalled a wrong API, or hit a harness issue.  Preserve the count as the
    acceptance authority and show only a small compiler excerpt for diagnosis.
    """
    rows = [str(error).strip() for error in (errors or []) if str(error).strip()]
    if not rows:
        return "(compiler reported no captured error lines)"
    shown = rows[:max(1, int(limit))]
    suffix = "\n      ... and %d more" % (len(rows) - len(shown)) if len(rows) > len(shown) else ""
    return "\n      " + "\n      ".join(row[:240] for row in shown) + suffix


def strip_fences(text: str) -> str:
    return FENCE.sub("", text or "").strip()


def wrapped_body_issue(text: str, signature: str) -> str:
    """Reject an incomplete method wrapper before it can poison the skeleton.

    The model is asked for bare statements but sometimes returns a complete
    method.  A balanced wrapper can be recovered by :func:`extract_body`; an
    unclosed wrapper cannot.  Compiling it only repeats a predictable parser
    failure, so keep the incumbent untouched and give the next attempt a
    precise host observation instead.
    """
    cleaned = strip_fences(text)
    head = signature.split("(")[0].strip().split()
    name = head[-1] if head else ""
    prefix, marker, tail = cleaned.partition("{")
    if not name or not marker or name not in prefix:
        return ""
    depth = 1
    for char in tail:
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return ""
    return "returned an unbalanced method wrapper; output statements only"


def body_brace_issue(body: str) -> str:
    """Return a parser-free C# brace error, ignoring ordinary strings/comments."""
    text = str(body or "")
    depth, index, state = 0, 0, "normal"
    while index < len(text):
        char = text[index]
        nxt = text[index + 1] if index + 1 < len(text) else ""
        if state == "line-comment":
            if char in "\r\n":
                state = "normal"
        elif state == "block-comment":
            if char == "*" and nxt == "/":
                state, index = "normal", index + 1
        elif state == "string":
            if char == "\\":
                index += 1
            elif char == '"':
                state = "normal"
        elif state == "verbatim-string":
            if char == '"' and nxt == '"':
                index += 1
            elif char == '"':
                state = "normal"
        elif state == "char":
            if char == "\\":
                index += 1
            elif char == "'":
                state = "normal"
        elif char == "/" and nxt == "/":
            state, index = "line-comment", index + 1
        elif char == "/" and nxt == "*":
            state, index = "block-comment", index + 1
        elif char == "@" and nxt == '"':
            state, index = "verbatim-string", index + 1
        elif char == '"':
            state = "string"
        elif char == "'":
            state = "char"
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                return "returned an unexpected closing brace in body statements"
        index += 1
    if depth:
        return "returned unbalanced braces in body statements"
    return ""


def extract_body(text: str, signature: str) -> str:
    """Pull just the statements out of whatever shape the model returned.

    It is asked for a bare body and frequently returns the whole method, or the
    whole class, anyway. Taking the outermost balanced brace block turns those
    into the body instead of discarding an otherwise usable answer -- and a
    body spliced into a skeleton that already has the braces would be a
    guaranteed syntax error if left alone.
    """
    text = strip_fences(text)
    if not text:
        return ""
    head = signature.split("(")[0].strip().split()
    name = head[-1] if head else ""
    # C# permits ``T Method(...) => expression;``.  It is a complete method
    # wrapper just like the brace form, but has no opening brace for the
    # recovery logic below to find.  Normalize it into the statements that
    # belong inside the harness-owned braces instead of splicing a second
    # signature into the skeleton.
    arrow = text.find("=>")
    if name and arrow >= 0 and name in text[:arrow]:
        expression = text[arrow + 2:].strip()
        if not expression:
            return ""
        if not expression.endswith(";"):
            expression += ";"
        return expression if len(head) >= 2 and head[-2] == "void" else "return " + expression
    looks_wrapped = name and name in text.split("{")[0]
    if not looks_wrapped:
        return text
    start = text.find("{")
    if start < 0:
        return text
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                inner = text[start + 1:index].strip()
                return inner or text
    # An incomplete wrapper is not a body.  Its caller reports the precise
    # shape error and leaves the last known-good skeleton in place.
    return ""


def reply_failed(reply: str) -> str:
    """The runtime's error string, when the call did not produce an answer.

    ensemble_answer reports failure by RETURNING prose, not by raising. The
    first version of this driver passed that prose to extract_body, spliced the
    English into the C# file, and scored the resulting syntax errors as if the
    model had written bad code.

    Measured: with Ollama down, a full 38-slot run reported "0 / 38 bodies
    implemented" and exited 0. Every slot showed the SAME 8 masking errors,
    because every slot was compiling the same error message. Nothing about the
    model was measured, and the report did not say so -- it looked exactly like
    a capability result.

    A constant error count across different inputs is the tell. Treat it as an
    aborting condition rather than a body: continuing produces a number that
    means nothing, which is worse than stopping.
    """
    head = (reply or "").strip()[:200]
    if head.startswith("ERROR:") or "no model produced an answer" in head:
        return head
    if not head:
        return "empty reply from the runtime"
    return ""


def dependency_brief(done: list) -> str:
    if not done:
        return ""
    return (
        "THESE TYPES ALREADY EXIST WITH EXACTLY THIS SURFACE. Use them as "
        "written; never redefine or invent members:\n\n"
        + "\n\n".join(done)
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--tiers", default="code,reasoning")
    parser.add_argument("--num-predict", type=int, default=1400)
    parser.add_argument("--attempts", type=int, default=2,
                        help="tries per body; a failed one is shown its own "
                             "compiler errors before the next try")
    parser.add_argument("--reset", action="store_true",
                        help="rewrite every skeleton before filling")
    parser.add_argument("--prepare-only", action="store_true",
                        help="write the deterministic project/skeleton and verify its baseline build; never call a model")
    parser.add_argument("--model", default="",
                        help="override the ollama model behind --tiers, so two "
                             "models can be compared on identical slots")
    args = parser.parse_args()

    if args.model:
        # A/B on the SAME slots is the only comparison worth making here: the
        # remaining bodies are the ones a smaller model already failed, so
        # scoring a different model on a different set would measure the set.
        for tier in [t.strip() for t in args.tiers.split(",") if t.strip()]:
            server.TIERS[tier] = args.model
        print("model override: %s -> %s" % (args.tiers, args.model), flush=True)

    os.makedirs(PROJECT, exist_ok=True)
    project_file = ensure_project_file(PROJECT)
    print("project scaffold: %s" % project_file, flush=True)
    # v1 per-file contracts are deliberately NOT used: they describe a design
    # this skeleton replaced, and carrying both put two contradictory contracts
    # in one prompt. bodynotes.py is the single source now.
    current = {}

    for name, skel in skeleton.FILES:
        path = os.path.join(PROJECT, name)
        if args.reset or not os.path.exists(path):
            with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(skel)
            current[name] = skel
        else:
            with io.open(path, "r", encoding="utf-8") as handle:
                current[name] = handle.read()

    errors, ran = build_errors()
    if not ran:
        print("ABORT: the baseline build did not run. Nothing measured.")
        return 1
    print("baseline: %d error(s) across %d skeleton file(s)\n"
          % (len(errors), len(skeleton.FILES)))
    baseline = len(errors)
    if args.prepare_only:
        if errors:
            print("ABORT: harness-owned baseline must compile before model work.")
            return 1
        print("baseline build succeeded; no model bodies were requested.")
        return 0

    slots = []
    for name, skel in skeleton.FILES:
        if args.only and name != args.only:
            continue
        # Only slots still OPEN in the file on disk. Without this a resume
        # spends its budget re-attempting bodies that are already implemented:
        # the splice refuses (their marker is gone), every one counts as a
        # failure, and the resumed run reports a WORSE result than the run it
        # resumed from.
        open_slots = set(skeleton.body_names(current[name]))
        for body in skeleton.body_names(skel):
            if body in open_slots:
                slots.append((name, body))
    if args.limit:
        slots = slots[:args.limit]

    kept, reverted, results = 0, 0, []
    started = time.time()

    for index, (name, body_name) in enumerate(slots, 1):
        path = os.path.join(PROJECT, name)
        signature = skeleton.signature_of(skeleton.by_name(name), body_name)
        before = current[name]

        done = [skeleton.declarations(s) for n, s in skeleton.FILES if n != name]
        base_prompt = "\n\n".join([
            specs.API_BRIEF,
            dependency_brief(done),
            "THIS IS THE FILE YOU ARE WRITING INTO. Every declaration below "
            "already exists exactly as shown:\n\n" + skeleton.declarations(
                skeleton.by_name(name)),
            bodynotes.note(name, body_name),
            "Write ONLY the statements inside the body of:\n    %s\n\n"
            "No signature, no braces around the whole thing, no class, no "
            "usings, no explanation. Statements only." % signature,
        ])

        # Retry a failed body, showing it its own compiler errors.
        #
        # Measured attribution, because the obvious credit is the wrong one.
        # Over 21 previously-failed bodies with --attempts 2:
        #
        #     KEPT on attempt 1 (a fresh sample)      8
        #     KEPT on attempt 2 (the error feedback)  1
        #
        # So nearly all of the gain is RE-SAMPLING, not repair. That also
        # retires the justification this was added under: an earlier eval
        # scored "self-repair x3" at 5/6 against "pass@1" at 4/6 and was read
        # as repair working, but three attempts with feedback versus one
        # attempt without conflates two variables. The control for repair is
        # N samples WITHOUT feedback, which was never run.
        #
        # Keep the retry -- it converted 9 of 21 either way. Do not claim the
        # feedback is what does it.
        status, feedback = "no attempt made", ""
        for attempt in range(1, max(1, int(args.attempts)) + 1):
            prompt = base_prompt + feedback
            reply = server.ensemble_answer(
                prompt, tiers=args.tiers, num_predict=args.num_predict, mode="code")
            failure = reply_failed(reply)
            if failure:
                # Not a body. Stop the whole run: every remaining slot would
                # compile the same error text and the tally would read as a
                # capability result.
                print("\nABORT: the runtime returned an error, not code:\n  %s"
                      % failure, flush=True)
                print("Nothing was measured. Fix the runtime and re-run.", flush=True)
                return 2
            shape_issue = wrapped_body_issue(reply, signature)
            if shape_issue:
                status = "attempt %d rejected: host contract (%s)" % (attempt, shape_issue)
                feedback = (
                    "\n\nYOUR PREVIOUS ATTEMPT VIOLATED A HOST-OBSERVED OUTPUT "
                    "CONTRACT: %s. Do not write a method signature or braces; "
                    "return statements only." % shape_issue
                )
                continue
            body = extract_body(reply, signature)
            if not body:
                status = "attempt %d: empty reply" % attempt
                continue

            brace_issue = body_brace_issue(body)
            if brace_issue:
                status = "attempt %d rejected: host contract (%s)" % (attempt, brace_issue)
                feedback = (
                    "\n\nYOUR PREVIOUS ATTEMPT VIOLATED A HOST-OBSERVED OUTPUT "
                    "CONTRACT: %s. Balance every block before returning only "
                    "body statements." % brace_issue
                )
                continue

            contract_issues = body_contract_issues(name, body_name, body)
            if contract_issues:
                status = "attempt %d rejected: host contract (%s)" % (
                    attempt, "; ".join(contract_issues))
                feedback = (
                    "\n\nYOUR PREVIOUS ATTEMPT VIOLATED A HOST-OBSERVED "
                    "METHOD CONTRACT: %s. Rewrite only this body; do not "
                    "start or end a drawing frame here."
                    % "; ".join(contract_issues)
                )
                continue

            candidate = skeleton.splice(before, body_name, body)
            if candidate == before:
                status = "attempt %d: splice refused" % attempt
                continue

            with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(candidate)
            errors, ran = build_errors()

            if not ran:
                # An infrastructure failure is not evidence about the body, so
                # retrying would just burn attempts against a build that cannot
                # answer. Put the file back and move on.
                with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(before)
                status = "build did not run"
                break

            masked = codegen_loop.count_unreliable(errors)
            if masked:
                status = "attempt %d rejected: %d masking error(s)" % (attempt, masked)
            elif len(errors) > baseline:
                status = "attempt %d rejected: +%d error(s)" % (
                    attempt, len(errors) - baseline)
            else:
                current[name] = candidate
                kept += 1
                status = "KEPT (attempt %d)" % attempt
                break

            print("      compiler:" + compiler_error_preview(errors), flush=True)

            with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(before)
            # Prefer the errors naming this file; a masked build often names
            # none, and showing an empty list would read as "it compiled".
            mine = [e for e in errors if name in e] or errors
            feedback = (
                "\n\nYOUR PREVIOUS ATTEMPT DID NOT COMPILE. It was:\n\n%s\n\n"
                "The compiler reported:\n%s\n\n"
                "Rewrite the body so those errors do not occur. Same rules: "
                "statements only." % ("\n".join(body.splitlines()[:40]),
                                      "\n".join(e[:200] for e in mine[:6]))
            )

        if not status.startswith("KEPT"):
            reverted += 1

        results.append((name, body_name, status))
        print("[%2d/%2d] %-16s %-16s %s" % (index, len(slots), name, body_name, status),
              flush=True)  # a 45-minute run must be watchable; block buffering hid it

    errors, ran = build_errors()
    print("\n%s" % ("-" * 60))
    print("bodies implemented : %d / %d" % (kept, len(slots)))
    print("reverted           : %d" % reverted)
    print("final errors       : %s" % (codegen_loop.describe_total(errors) if ran
                                       else "BUILD DID NOT RUN"))
    print("elapsed            : %.1f min" % ((time.time() - started) / 60.0))
    print("\nNOTE: a green build is not proof the game works. Every unfilled "
          "body still throws NotImplementedException at runtime.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
