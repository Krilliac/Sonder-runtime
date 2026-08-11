"""Did constraining the decoder with a schema actually make the work better?

Plan 03 added two things: ``offload(schema=...)``, which hands a JSON Schema to
Ollama's ``format`` and re-checks the reply here, and ``extract_grounded``,
which additionally demands that every extracted field cite a span present in the
source by literal substring. Both are worth having only if a number moves, and a
claim that a number moved is exactly the claim this codebase distrusts.

So this runs one fixed set of prompts twice -- once with the schema supplied to
the decoder, once without -- judges both arms by *the same* post-hoc checks, and
reports the result through ``calibration``'s caller-judged population.

Three things that are never the same thing
------------------------------------------
The failure this harness is built against is recorded in the operator's own
notes: a count reported as an improvement four times over, when each fall in the
count actually meant the toolchain had stopped earlier. ``benchmark_moat`` in
this repository still has the shape -- ``run_arm`` scores a model call that
never happened as ``0.0``, its arm aggregate carries no count of those, and
``render_scorecard`` prints only a rate and ``passed/total``, so a single outage
during a three-arm run swings the headline by up to a hundred points and looks
like a result.

Every outcome here is therefore one of five, and the first three are never
merged:

``valid``     the call ran and the reply passed every check
``rejected``  the call ran and the reply was refused -- schema violation, or a
              quote that is not in the source
``unusable``  the call ran and returned text with no JSON object in it at all
``wrong``     the reply was well-formed and grounded, but a value disagrees with
              the fixture -- the silent pass that grounding cannot catch
``not_run``   the call never happened: model unavailable, timeout, load failure

``not_run`` enters neither side of any rate. It is counted, printed, and it makes
:func:`compare_arms` refuse to compare when the two arms did not reach the same
stage. An arm with fewer completions is not a better arm, however good the
surviving rate looks.

What this measures, and what it does not
----------------------------------------
It measures whether a caller who asked for a specific structured answer *got*
one, judged against fixtures whose correct values were fixed before any model
ran. It does not measure whether the model is good at the underlying task in
general, and it is not a causal claim about anything outside this case set.

Both arms are judged by the same two checks -- ``json_schema_verifier.validate``
against the same schema, then ``grounded_extraction.verify_grounding`` against
the same source. The only variable between the arms is whether the schema was
also given to the decoder. Where a judgement could go either way it is made in
favour of the *unconstrained* arm: prose around the JSON is parsed rather than
failed, and top-level keys outside the schema are ignored rather than penalised.
Leniency in that direction cannot manufacture an improvement.

Why a rejection happened
------------------------
``verify_grounding`` normalises nothing at all, deliberately, so an honest quote
that was re-wrapped while copying is rejected exactly like an invented one.
Task 2 left the size of that cost unmeasured and it is measured here:
:func:`classify_span` sorts every failing span into ``reflow`` (differs only in
whitespace), ``cosmetic`` (differs only in case or punctuation shape once
whitespace is collapsed), and ``not_in_source`` (absent even then). This is
measurement only -- nothing here relaxes the production check, and a test pins
that a reflowed quote is still rejected.

``not_in_source`` is still not proof of invention: a paraphrase, an elision, or
two spans joined into one all land there. It is the upper bound on invention,
and the two softer buckets are the false-rejection rate that was previously
unknown.

The hazard in reporting through the population you write to
-----------------------------------------------------------
``--record`` files this run's verdicts into the same caller-judged population
the report then quotes. That is what the population is for -- a caller reviewed
delegated work -- but it means **running this benchmark moves the number it
reports**, and a benchmark with easy fixtures run repeatedly would raise the
caller-judged rate without any work getting better.

The disclosure that used to sit here was not a mitigation. It lived in the
writer's own output, while the damage lands on a *reader* who never sees it:
``calibration.measure`` aggregates by signal name and there is no provenance
column, so ``calibration_status`` reports one unmarked number, and
``calibration.should_verify`` gates runtime behaviour off that same population
-- meaning the instrument moves the control loop it is measuring. A convention
imposed on a stranger is not a control.

So ``--record`` now **refuses** while ``PROVENANCE_AVAILABLE`` is False. What
remains true and load-bearing: ``--live`` without ``--record`` is unaffected and
measures everything this task set out to measure; ``accepted`` is filed only for
agreement with a fixture fixed before the model ran, never for a reply that
merely matched a shape; and the report still prints the population before and
after, so a recorded run's contribution is visible as a delta rather than
absorbed into a headline.

Stdlib plus this repository's own modules; ``server`` is imported only on the
live path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import grounded_extraction
import json_schema_verifier


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------
VALID = "valid"
REJECTED = "rejected"
UNUSABLE = "unusable"
WRONG = "wrong"
NOT_RUN = "not_run"

#: Outcomes of calls that actually happened. The denominator of every rate.
RAN = (VALID, REJECTED, UNUSABLE, WRONG)
OUTCOMES = RAN + (NOT_RUN,)

ARM_NO_SCHEMA = "no_schema"
ARM_SCHEMA = "schema"
ARMS = (ARM_NO_SCHEMA, ARM_SCHEMA)

#: Why a cited span is not a literal substring of the source.
GROUNDED = "grounded"
REFLOW = "reflow"
COSMETIC = "cosmetic"
NOT_IN_SOURCE = "not_in_source"
SPAN_CLASSES = (GROUNDED, REFLOW, COSMETIC, NOT_IN_SOURCE)

#: ``ModelCallError`` kinds that mean the exchange happened and its *content*
#: was refused. ``server`` classifies a schema violation and a reply that is not
#: JSON as ``protocol``; every other kind -- timeout, request, cancelled,
#: configuration -- means no answer was ever produced, which is not a data
#: point.
RAN_ERROR_KINDS = frozenset({"protocol"})

#: Mirrors ``server.FOOTER_PREFIX``. Pinned by a drift test rather than imported,
#: so the pure judging path does not need ``server``.
FOOTER_PREFIX = "\n\n[interaction_id: "

_ACCEPTED, _REJECTED_SIGNAL = "accepted", "rejected"

#: Whether an outcome row can carry where it came from. It cannot: the store
#: has no provenance column and ``calibration.measure`` aggregates by signal
#: name alone, so a benchmark-authored row is indistinguishable from a row a
#: human filed after reviewing real delegated work. Until that changes,
#: ``--record`` refuses.
PROVENANCE_AVAILABLE = False

_PROVENANCE_REFUSAL = (
    "recording is disabled: an outcome row cannot yet carry its provenance. "
    "calibration.measure aggregates by signal name alone, so rows this "
    "benchmark writes are indistinguishable from a caller's verdict on real "
    "delegated work -- and calibration.should_verify gates runtime behaviour "
    "off that same population, so recording here moves the control loop this "
    "harness is supposed to be measuring. A disclosure in the writer's own "
    "output does not reach the stranger who later quotes the number. Run "
    "--live without --record, or add a provenance column and set "
    "PROVENANCE_AVAILABLE once a benchmark-authored row can be excluded on "
    "request."
)

# Curly quotes, dashes and non-breaking spaces: the characters a model
# "tidies" while copying. Folding them is how a cosmetic difference is told
# apart from an invention -- it is never applied to the production check.
_COSMETIC_FOLD = {
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "−": "-", " ": " ",
    "…": "...",
}


class SchemaBenchmarkError(ValueError):
    """The comparison cannot be run or cannot be trusted as set up."""


# ---------------------------------------------------------------------------
# The fixed case set
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Case:
    """One extraction with a correct answer fixed before any model ran.

    Every expected value appears verbatim in ``source``, so a failure is always
    the model's and never the fixture's. Each source also carries a plausible
    *wrong* value nearby -- a second year, a second service -- because a case
    whose answer is the only number present measures very little.
    """

    name: str
    source: str
    fields: dict
    expected: dict


CASES = (
    Case(
        name="release",
        source=(
            "Release 4.2.0 shipped on 2026-03-14 after a two week code freeze. "
            "The previous release, 4.1.7, shipped on 2026-01-30."
        ),
        fields={"version": {"type": "string"}, "date": {"type": "string"}},
        expected={"version": "4.2.0", "date": "2026-03-14"},
    ),
    Case(
        name="person",
        source=(
            "Ada Lovelace was born in London on 10 December 1815, and she died "
            "in 1852 at the age of thirty-six."
        ),
        fields={"name": {"type": "string"}, "birth_year": {"type": "integer"}},
        expected={"name": "Ada Lovelace", "birth_year": 1815},
    ),
    Case(
        name="invoice",
        source=(
            "Invoice INV-8841 was settled in full on the day it was issued; the "
            "total came to 1299 EUR, of which 216 EUR was tax."
        ),
        fields={
            "invoice_id": {"type": "string"},
            "total": {"type": "integer"},
            "currency": {"type": "string"},
        },
        expected={"invoice_id": "INV-8841", "total": 1299, "currency": "EUR"},
    ),
    Case(
        name="incident",
        source=(
            "Incident SEV-2 took the checkout service down at 03:11 UTC for "
            "nineteen minutes. The payments service was never affected."
        ),
        fields={
            "severity": {"type": "string"},
            "affected_service": {"type": "string"},
        },
        expected={"severity": "SEV-2", "affected_service": "checkout"},
    ),
    Case(
        name="config",
        source=(
            "In production the daemon binds to db.internal on port 5433. The "
            "staging host, db.staging, still listens on 5432."
        ),
        fields={"host": {"type": "string"}, "port": {"type": "integer"}},
        expected={"host": "db.internal", "port": 5433},
    ),
    Case(
        name="changelog",
        source=(
            "Commit 9f3c1ab was authored by Mira Okonkwo and reverted the cache "
            "change introduced in 2b7de40 by Tomas Halvorsen."
        ),
        fields={"commit": {"type": "string"}, "author": {"type": "string"}},
        expected={"commit": "9f3c1ab", "author": "Mira Okonkwo"},
    ),
    # The one hard-wrapped source, and the reason it is here: every case above
    # is a single unwrapped paragraph, which is the easiest possible condition
    # for copying a span character for character. A zero-normalisation check
    # only costs anything when the text the model is copying has line breaks in
    # it, so a case set without one would measure the false-rejection rate of
    # zero normalisation as zero and report that as though it were free.
    Case(
        name="postmortem",
        source=(
            "Postmortem 2026-04-02\n"
            "The outage began when the primary replica in region eu-west-2 was\n"
            "promoted twice inside a single minute, and it lasted 47 minutes\n"
            "before the on-call engineer rolled the change back.\n"
        ),
        fields={
            "region": {"type": "string"},
            "duration_minutes": {"type": "integer"},
        },
        expected={"region": "eu-west-2", "duration_minutes": 47},
    ),
)


def _type_name(subschema):
    kind = subschema.get("type") if isinstance(subschema, dict) else None
    return str(kind) if isinstance(kind, str) else "value"


def field_instruction(case):
    """The field list, worded identically for both arms.

    The unconstrained arm has no schema to read the field names off, so they go
    in the prompt -- and therefore they go in *both* prompts, or the arms would
    differ by more than the one variable under test.
    """
    listed = ", ".join(
        "%s (%s)" % (name, _type_name(sub)) for name, sub in case.fields.items()
    )
    return (
        "Extract these fields: %s. Reply with ONE JSON object and nothing else. "
        "Its keys are exactly those field names; each key maps to an object with "
        "two keys, \"value\" and \"quote\"." % listed
    )


def case_prompt(case):
    """The user turn, identical in both arms."""
    return grounded_extraction.extraction_prompt(case.source, field_instruction(case))


def case_schema(case):
    """The grounded schema: given to the decoder in one arm, used to judge both."""
    return grounded_extraction.grounded_schema(
        {"properties": dict(case.fields), "required": list(case.fields)}
    )


def case_set_digest(cases=CASES):
    """Identity of the prompt set, so two runs of different fixtures cannot be
    compared to each other by accident."""
    payload = [
        {"name": c.name, "source": c.source, "fields": c.fields,
         "expected": c.expected}
        for c in cases
    ]
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# ---------------------------------------------------------------------------
# Reading one reply
# ---------------------------------------------------------------------------
def interaction_id(text):
    """The id ``server.with_footer`` appended, read by the footer's delimiters.

    Not a hex regex: the day an id gains a hyphen, a regex stops matching and
    outcomes silently stop being filed -- in the flattering direction, since a
    dropped row is a row that never lowered anything.
    """
    body = (text or "").rstrip()
    start = body.rfind(FOOTER_PREFIX)
    if start < 0 or not body.endswith("]"):
        return None
    return body[start + len(FOOTER_PREFIX):-1].strip() or None


def extract_json_object(text):
    """The JSON object in a reply, and how much digging it took.

    Returns ``(data, parse_mode)`` with ``parse_mode`` one of ``leading``,
    ``fenced``, ``scanned`` or ``""`` when there is no object at all.

    The digging is deliberate generosity toward the unconstrained arm, which is
    the arm that wraps its answer in "Sure! Here you go". Being strict here
    would hand the schema arm a win on presentation rather than on content, and
    a win won that way is the kind of number this whole task exists to avoid.
    """
    body = (text or "").strip()
    if not body:
        return None, ""
    decoder = json.JSONDecoder()
    try:
        data, _end = decoder.raw_decode(body)
        if isinstance(data, dict):
            return data, "leading"
    except ValueError:
        pass
    for fence in ("```json", "```"):
        start = body.find(fence)
        while start >= 0:
            inner = body[start + len(fence):]
            end = inner.find("```")
            candidate = (inner if end < 0 else inner[:end]).strip()
            try:
                data, _end = decoder.raw_decode(candidate)
                if isinstance(data, dict):
                    return data, "fenced"
            except ValueError:
                pass
            start = body.find(fence, start + len(fence))
    index = body.find("{")
    while index >= 0:
        try:
            data, _end = decoder.raw_decode(body[index:])
            if isinstance(data, dict):
                return data, "scanned"
        except ValueError:
            pass
        index = body.find("{", index + 1)
    return None, ""


def _collapse(text):
    return " ".join(str(text).split())


def _fold(text):
    folded = "".join(_COSMETIC_FOLD.get(ch, ch) for ch in str(text))
    return _collapse(folded).casefold()


def classify_span(quote, source):
    """Why a cited span is, or is not, a literal substring of the source.

    ``reflow`` and ``cosmetic`` are false rejections: the model was copying
    honestly and tidied the text on the way. ``not_in_source`` is the upper
    bound on invention -- a paraphrase or two joined spans land there too, so it
    is not by itself proof that anything was made up.

    Nothing here is applied to the production check. It exists so that a
    rejection count cannot be read as an invention count.
    """
    if not isinstance(quote, str) or not quote.strip():
        return NOT_IN_SOURCE
    if quote in source:
        return GROUNDED
    if _collapse(quote) in _collapse(source):
        return REFLOW
    if _fold(quote) in _fold(source):
        return COSMETIC
    return NOT_IN_SOURCE


def _span_mix(data, source):
    mix = {}
    if not isinstance(data, dict):
        return mix
    for name, entry in data.items():
        if isinstance(entry, dict) and isinstance(entry.get(grounded_extraction.QUOTE_KEY), str):
            mix[name] = classify_span(entry[grounded_extraction.QUOTE_KEY], source)
    return mix


def judge_text(case, text):
    """Judge one reply. Identical for both arms; that is the whole point.

    Order matters and is the same order the runtime uses: is there an object at
    all, does it match the schema, is every quote really in the source, and only
    then -- does it say the right thing. The last step is the one grounding
    cannot do: a real span with a fabricated value hung off it passes every
    check above and is caught here only because the fixture knows the answer.
    """
    schema = case_schema(case)
    row = {
        "case": case.name,
        "interaction_id": interaction_id(text),
        "span_mix": {},
        "not_run_kind": "",
        "detail": "",
        "parse_mode": "",
        "already_filed": False,
        "ignored_keys": [],
    }
    data, parse_mode = extract_json_object(text)
    row["parse_mode"] = parse_mode
    if data is None:
        row["outcome"] = UNUSABLE
        row["detail"] = "no JSON object anywhere in the reply"
        return row
    # Keys outside the schema are dropped BEFORE anything judges them. Only the
    # unconstrained arm can emit one -- the decoder forbids it on the other --
    # so penalising it would be a penalty applied to one arm alone, and it would
    # run in the one direction this whole design exists to exclude: making the
    # schema arm look better than it is. `verify_grounding` rejects any
    # top-level key that is not a {value, quote} pair (grounded_extraction.py
    # :171-177), so an "explanation" field alongside the real ones would sink an
    # otherwise valid reply. Stating the leniency in a comment was not enough;
    # it has to be executed, which is what this line does.
    declared = {name: value for name, value in data.items() if name in case.fields}
    row["ignored_keys"] = sorted(set(data) - set(declared))
    row["span_mix"] = _span_mix(declared, case.source)
    errors = json_schema_verifier.validate(declared, schema)
    if errors:
        row["outcome"] = REJECTED
        row["detail"] = "schema violation: %s" % "; ".join(str(e) for e in errors[:3])
        return row
    try:
        fields = grounded_extraction.verify_grounding(declared, case.source)
    except grounded_extraction.GroundingError as exc:
        row["outcome"] = REJECTED
        row["detail"] = str(exc)
        return row
    disagreements = []
    for name, expected in case.expected.items():
        actual = fields.get(name, {}).get(grounded_extraction.VALUE_KEY)
        if actual != expected:
            disagreements.append("%s=%r (expected %r)" % (name, actual, expected))
    if disagreements:
        row["outcome"] = WRONG
        row["detail"] = "; ".join(disagreements)
        return row
    row["outcome"] = VALID
    return row


# ---------------------------------------------------------------------------
# Running one case, one arm
# ---------------------------------------------------------------------------
def run_case(case, *, with_schema, call_fn):
    """One call, one verdict.

    ``call_fn(prompt=, system=, schema=)`` returns the reply text or raises. A
    raise is classified before anything else, because that is where the
    dangerous collapse lives: a timeout and a refused answer look equally like
    "no result" and are not remotely the same evidence.
    """
    schema = case_schema(case) if with_schema else None
    try:
        text = call_fn(
            prompt=case_prompt(case),
            system=grounded_extraction.EXTRACTION_SYSTEM,
            schema=schema,
        )
    except Exception as exc:  # classified, never scored
        kind = str(getattr(exc, "kind", "") or "").strip()
        row = {
            "case": case.name, "interaction_id": None, "span_mix": {},
            "not_run_kind": "", "parse_mode": "", "already_filed": False,
            "detail": "%s: %s" % (type(exc).__name__, exc),
        }
        if kind in RAN_ERROR_KINDS:
            row["outcome"] = REJECTED
            # ``server._file_schema_rejection`` fires on the learning path
            # whenever a schema was supplied, so on the schema arm the store
            # already has this row and filing it again would double-count the
            # only signal the store is starved of.
            row["already_filed"] = bool(with_schema)
            return row
        row["outcome"] = NOT_RUN
        row["not_run_kind"] = kind or "harness_error"
        return row
    return judge_text(case, text)


def run_arm(cases, arm, call_fn):
    """Every case once under one arm."""
    if arm not in ARMS:
        raise SchemaBenchmarkError("unknown arm: %r" % (arm,))
    rows = [
        run_case(case, with_schema=(arm == ARM_SCHEMA), call_fn=call_fn)
        for case in cases
    ]
    return aggregate(arm, rows)


def aggregate(arm, rows):
    """Count the rows without ever merging the three states that matter.

    ``success_rate`` is ``None`` and not ``0.0`` when nothing completed. Zero is
    a measurement; this is the absence of one, and the two must not print the
    same.
    """
    rows = list(rows)
    counts = {name: 0 for name in OUTCOMES}
    for row in rows:
        outcome = row.get("outcome")
        if outcome not in counts:
            raise SchemaBenchmarkError(
                "unknown outcome %r in arm %s" % (outcome, arm)
            )
        counts[outcome] += 1
    completed = sum(counts[name] for name in RAN)
    not_run = counts[NOT_RUN]
    attempted = len(rows)
    if completed + not_run != attempted:
        raise SchemaBenchmarkError("arm %s does not account for every case" % arm)
    mix = Counter()
    for row in rows:
        for span_class in (row.get("span_mix") or {}).values():
            if span_class != GROUNDED:
                mix[span_class] += 1
    kinds = Counter(
        row.get("not_run_kind") or "unknown"
        for row in rows if row.get("outcome") == NOT_RUN
    )
    return {
        "arm": arm,
        "rows": rows,
        "counts": counts,
        "completed": completed,
        "not_run": not_run,
        "attempted": attempted,
        "success_rate": (counts[VALID] / completed) if completed else None,
        "rejection_mix": dict(mix),
        "not_run_kinds": dict(kinds),
    }


# ---------------------------------------------------------------------------
# Comparing the arms
# ---------------------------------------------------------------------------
def _digest_for(names):
    """The digest of the cases a run actually covered, or empty if unknowable.

    Stamping the module's current case set onto a comparison would be a label
    that does not describe what was measured -- the same family of defect as a
    rate that does not describe what completed. If a run covered cases this
    module does not define, the honest answer is no digest at all.
    """
    covered = set(names)
    if not covered <= {case.name for case in CASES}:
        return ""
    return case_set_digest(tuple(c for c in CASES if c.name in covered))


def compare_arms(baseline, treatment):
    """Compare, or refuse to.

    The plan's rule, verbatim: *do not report an improvement unless both arms
    reached the same stage.* An arm that completed fewer calls did not score
    better, it got less far, and there is no honest way to put those two rates
    beside each other. When the completion counts differ the deltas are ``None``
    -- not merely flagged -- so there is no number for anyone to quote.
    """
    baseline_cases = [row["case"] for row in baseline["rows"]]
    treatment_cases = [row["case"] for row in treatment["rows"]]
    if sorted(baseline_cases) != sorted(treatment_cases):
        # Same failure as a truncated arm, wearing different clothes: two rates
        # over different work. This one is a setup error rather than a result,
        # so it raises instead of returning a verdict nobody should read.
        raise SchemaBenchmarkError(
            "the arms did not run the same cases: %s vs %s"
            % (sorted(baseline_cases), sorted(treatment_cases))
        )
    result = {
        "case_set_digest": _digest_for(baseline_cases),
        "arms": {baseline["arm"]: baseline, treatment["arm"]: treatment},
        "baseline_arm": baseline["arm"],
        "treatment_arm": treatment["arm"],
        "comparable": False,
        "verdict": "unmeasured",
        "reason": "",
        "valid_delta": None,
        "success_rate_delta": None,
    }
    if not baseline["completed"] or not treatment["completed"]:
        result["reason"] = (
            "nothing to measure: %s completed %d of %d calls and %s completed "
            "%d of %d. A rate over zero completions is not a zero score."
            % (baseline["arm"], baseline["completed"], baseline["attempted"],
               treatment["arm"], treatment["completed"], treatment["attempted"])
        )
        return result
    if baseline["completed"] != treatment["completed"]:
        result["verdict"] = "incomparable"
        result["reason"] = (
            "the arms did not reach the same stage: %s completed %d of %d "
            "calls, %s completed %d of %d (%d never ran). A lower count is not "
            "a better score, so no delta is reported."
            % (baseline["arm"], baseline["completed"], baseline["attempted"],
               treatment["arm"], treatment["completed"], treatment["attempted"],
               baseline["not_run"] + treatment["not_run"])
        )
        return result
    delta = treatment["counts"][VALID] - baseline["counts"][VALID]
    result["comparable"] = True
    result["valid_delta"] = delta
    result["success_rate_delta"] = treatment["success_rate"] - baseline["success_rate"]
    result["verdict"] = (
        "improved" if delta > 0 else ("regressed" if delta < 0 else "unchanged")
    )
    result["reason"] = (
        "both arms completed %d of %d calls, so the rates describe the same "
        "work." % (baseline["completed"], baseline["attempted"])
    )
    return result


# ---------------------------------------------------------------------------
# What reaches the outcome store
# ---------------------------------------------------------------------------
def record_outcomes(arm_result, record_fn):
    """File this run's caller verdicts, and nothing else.

    A caller reviewed the work against an answer fixed before the model ran;
    that is what the caller-judged population is for. Three things are
    deliberately *not* filed:

    * a call that never ran -- it produced no work to judge, and letting an
      outage write a row would put infrastructure noise into a quality figure;
    * a rejection the runtime already filed itself, which would double-count the
      one signal the store is short of;
    * anything with no interaction id to attach to -- counted as
      ``unrecorded_no_id`` rather than dropped quietly.

    Conformance alone is never filed as ``accepted``; only agreement with the
    fixture is. Filing ``accepted`` for a reply that merely matched a shape is
    precisely how a pass rate rises without the work getting better.
    """
    written = []
    summary = {
        "written": written, "skipped_not_run": 0,
        "skipped_already_filed": 0, "unrecorded_no_id": 0,
    }
    for row in arm_result["rows"]:
        outcome = row.get("outcome")
        if outcome == NOT_RUN:
            summary["skipped_not_run"] += 1
            continue
        if outcome == REJECTED and row.get("already_filed"):
            summary["skipped_already_filed"] += 1
            continue
        identifier = row.get("interaction_id")
        if not identifier:
            summary["unrecorded_no_id"] += 1
            continue
        signal = _ACCEPTED if outcome == VALID else _REJECTED_SIGNAL
        record_fn(identifier, signal)
        written.append((identifier, signal))
    return summary


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _rate_cell(arm):
    if arm["success_rate"] is None:
        return "no completions - unmeasured"
    return "%.1f%% of %d completed" % (arm["success_rate"] * 100, arm["completed"])


def _mix_line(arm):
    mix = arm["rejection_mix"]
    if not mix:
        return "%s: no failing spans." % arm["arm"]
    return "%s: %s." % (
        arm["arm"],
        ", ".join("%s %d" % (name, mix[name]) for name in sorted(mix)),
    )


def render_report(comparison, *, caller_before=None, caller_after=None):
    """The scorecard, with every rate chaperoned by the count behind it."""
    baseline = comparison["arms"][comparison["baseline_arm"]]
    treatment = comparison["arms"][comparison["treatment_arm"]]
    lines = [
        "# Schema-constrained offload: with schema vs without",
        "",
        "Case set `%s...` (%d cases). Both arms send the identical prompt and "
        "system turn and are judged by the identical checks; the only variable "
        "is whether the schema was also given to the decoder."
        % (comparison["case_set_digest"][:12], baseline["attempted"]),
        "",
        "| Arm | Valid | Rejected | Unusable | Wrong | Completed | Not run | Attempted | Success rate |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for arm in (baseline, treatment):
        counts = arm["counts"]
        lines.append(
            "| %s | %d | %d | %d | %d | %d | %d | %d | %s |" % (
                arm["arm"], counts[VALID], counts[REJECTED], counts[UNUSABLE],
                counts[WRONG], arm["completed"], arm["not_run"],
                arm["attempted"], _rate_cell(arm),
            )
        )
    lines += [
        "",
        "Verdict: **%s**" % comparison["verdict"],
        "",
        comparison["reason"],
        "",
        "A call that never ran is counted under **Not run** and is excluded "
        "from both sides of the success rate. It is not a failure and it is not "
        "a success; it is the absence of evidence, and a rate that quietly "
        "absorbs one is the defect this harness was written against.",
        "",
    ]
    for arm in (baseline, treatment):
        if arm["not_run_kinds"]:
            lines.append(
                "%s did not run: %s." % (
                    arm["arm"],
                    ", ".join(
                        "%s %d" % (kind, count)
                        for kind, count in sorted(arm["not_run_kinds"].items())
                    ),
                )
            )
    lines += [
        "",
        "## Why spans were rejected",
        "",
        "`reflow` and `cosmetic` are honest quotes that were tidied while being "
        "copied -- false rejections, the cost of normalising nothing. "
        "`not_in_source` is the upper bound on invention, not a proof of it.",
        "",
        _mix_line(baseline),
        "",
        _mix_line(treatment),
        "",
        "## Caller-judged outcomes",
        "",
        "This run files its verdicts into `calibration`'s caller-judged "
        "population. The self-graded curriculum population is measured "
        "separately and is never averaged with it: the curriculum population is "
        "far larger and far higher, so one figure over both would read like "
        "accuracy and would not be one.",
        "",
        "Before: %s" % (caller_before or "not measured in this run"),
        "After:  %s" % (caller_after or "not measured in this run"),
        "",
        "The two lines differ by exactly the rows this run filed. Reporting "
        "through a population you also write to means the benchmark moves the "
        "number it quotes, so the delta is printed rather than folded away: "
        "anyone citing the figure afterwards has to subtract this run or say it "
        "is included.",
        "",
    ]
    return "\n".join(lines)


def render_case_set(cases=CASES):
    lines = [
        "schema-offload comparison: %d cases, digest %s"
        % (len(cases), case_set_digest(cases)),
        "",
    ]
    for case in cases:
        lines.append(
            "  %-10s fields: %s" % (case.name, ", ".join(sorted(case.fields)))
        )
    lines += [
        "",
        "  not yet run. Pass --live to run both arms against the local model,",
        "  and --record to file the caller verdicts into the outcome store.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The live path
# ---------------------------------------------------------------------------
def build_live_call(tier="fast", *, temperature=0.0, num_predict=768,
                    timeout=180):
    """A ``call_fn`` bound to the real local model.

    Refuses a hosted tier outright: the cases carry whole source documents, and
    the same rule ``extract_grounded`` enforces applies to anything that sends
    them.

    Learning is not a parameter, deliberately. It is what mints the interaction
    id a caller verdict attaches to, and it is also what makes the runtime file
    its own rejection -- which is why :func:`run_case` skips re-filing one on the
    schema arm. Turning it off would silently break both halves of that at once:
    every row would become unrecordable, and a rejection the runtime never filed
    would be skipped as though it had been. The second of those errs in the
    flattering direction, so the option is not offered.
    """
    import server

    if server._is_cloud_tier(tier):
        raise SchemaBenchmarkError(
            "the comparison runs on local tiers only (%s); '%s' is hosted and "
            "these cases carry whole source documents"
            % (", ".join(server.LOCAL_TIERS), tier)
        )

    def call_fn(prompt, system, schema):
        return server._offload_impl(
            prompt=prompt, tier=tier, system=system, temperature=temperature,
            num_predict=num_predict, learn=True, timeout=timeout, schema=schema,
        )

    return call_fn


def _caller_line():
    import calibration
    import server

    conn = server._open_db()
    try:
        return str(calibration.measure(conn, "caller"))
    finally:
        conn.close()


def _live_recorder():
    import server

    def record_fn(identifier, signal):
        server._record_outcome_signal(identifier, signal)

    return record_fn


def run_live(tier="fast", cases=CASES, *, record=False, timeout=180):
    """Both arms against the real model, in a fixed order, once each.

    The arms run as two consecutive blocks, and that is a known confound rather
    than an oversight. ``orchestrator._run`` prepends retrieved lessons to the
    prompt *below* the layer this module controls, so the parity test at the
    ``call_fn`` boundary cannot see them: if the lesson store changed mid-run,
    the change would be perfectly correlated with the arm. Nothing on this path
    writes a lesson row, so retrieval is static within a run and the confound is
    currently inert -- but it is inert by circumstance, not by construction.
    Interleaving the arms case by case would remove it, at the cost of making
    every past run incomparable to every future one.
    """
    if record and not PROVENANCE_AVAILABLE:
        raise SchemaBenchmarkError(_PROVENANCE_REFUSAL)
    call_fn = build_live_call(tier, timeout=timeout)
    baseline = run_arm(cases, ARM_NO_SCHEMA, call_fn)
    treatment = run_arm(cases, ARM_SCHEMA, call_fn)
    comparison = compare_arms(baseline, treatment)
    comparison["tier"] = tier
    caller_before = _caller_line()
    if record:
        record_fn = _live_recorder()
        comparison["recorded"] = {
            arm["arm"]: record_outcomes(arm, record_fn)
            for arm in (baseline, treatment)
        }
    caller_after = _caller_line() if record else caller_before
    comparison["caller_before"] = caller_before
    comparison["caller_after"] = caller_after
    return comparison


def _serialisable(comparison):
    out = dict(comparison)
    out["arms"] = {
        name: {key: value for key, value in arm.items() if key != "rows"}
        for name, arm in comparison["arms"].items()
    }
    out["rows"] = {
        name: comparison["arms"][name]["rows"] for name in comparison["arms"]
    }
    recorded = out.get("recorded")
    if recorded:
        out["recorded"] = {
            arm: {key: (list(value) if key == "written" else value)
                  for key, value in summary.items()}
            for arm, summary in recorded.items()
        }
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description="with-schema vs without-schema")
    parser.add_argument("--live", action="store_true",
                        help="make real calls against the local model")
    parser.add_argument("--record", action="store_true",
                        help="file the caller verdicts into the outcome store")
    parser.add_argument("--tier", default="fast")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--json", default="")
    parser.add_argument("--markdown", default="")
    args = parser.parse_args(argv)
    if not args.live:
        print(render_case_set())
        return 0
    try:
        comparison = run_live(
            args.tier, record=args.record, timeout=args.timeout,
        )
    except SchemaBenchmarkError as exc:
        print("schema offload benchmark: %s" % exc, file=sys.stderr)
        return 2
    text = render_report(
        comparison,
        caller_before=comparison.get("caller_before"),
        caller_after=comparison.get("caller_after"),
    )
    if args.json:
        Path(args.json).write_text(
            json.dumps(_serialisable(comparison), indent=2, sort_keys=True,
                       ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    if args.markdown:
        Path(args.markdown).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
