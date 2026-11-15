"""Precision/recall scoring and the paired A/B comparison (pure).

The positive class is a CONFIRMED verdict on a CONFIRMED label. Rules that keep the numbers honest:
- a label UNCERTAIN is excluded from precision and recall and counted;
- a model UNCERTAIN, UNPARSED, MODEL_ERROR, SOURCE_ERROR or CONTEXT_ERROR item is a non-positive
  prediction: it is counted in its own bucket and never dropped, so a missed positive still lowers
  recall;
- precision and recall are ``None`` (never 0.0 or 1.0) when their denominator is zero.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import comb

from .records import HypothesisRecord
from .verdict import ParsedHypothesis

OUTCOMES = ("SCORED", "UNPARSED", "MODEL_ERROR", "SOURCE_ERROR", "CONTEXT_ERROR")
_ERROR_FIELD = {"UNPARSED": "unparsed", "MODEL_ERROR": "model_error",
                "SOURCE_ERROR": "source_error", "CONTEXT_ERROR": "context_error"}


@dataclass(frozen=True)
class ScoreRow:
    hid: str
    arm: str
    category: str
    stage: str
    label: str
    predicted: str | None
    outcome: str


@dataclass
class Confusion:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0
    label_uncertain: int = 0
    model_uncertain: int = 0
    unparsed: int = 0
    model_error: int = 0
    source_error: int = 0
    context_error: int = 0
    n: int = 0

    @property
    def precision(self) -> float | None:
        predicted = self.tp + self.fp
        return self.tp / predicted if predicted else None

    @property
    def recall(self) -> float | None:
        actual = self.tp + self.fn
        return self.tp / actual if actual else None

    def add(self, row: ScoreRow) -> None:
        self.n += 1
        if row.outcome in _ERROR_FIELD:
            setattr(self, _ERROR_FIELD[row.outcome], getattr(self, _ERROR_FIELD[row.outcome]) + 1)
        elif row.predicted == "UNCERTAIN":
            self.model_uncertain += 1
        if row.label == "UNCERTAIN":
            self.label_uncertain += 1
            return
        predicted_positive = row.outcome == "SCORED" and row.predicted == "CONFIRMED"
        actual_positive = row.label == "CONFIRMED"
        if predicted_positive and actual_positive:
            self.tp += 1
        elif predicted_positive:
            self.fp += 1
        elif actual_positive:
            self.fn += 1
        else:
            self.tn += 1

    def as_dict(self) -> dict:
        return {"n": self.n, "tp": self.tp, "fp": self.fp, "fn": self.fn, "tn": self.tn,
                "precision": self.precision, "recall": self.recall,
                "label_uncertain": self.label_uncertain, "model_uncertain": self.model_uncertain,
                "unparsed": self.unparsed, "model_error": self.model_error,
                "source_error": self.source_error, "context_error": self.context_error}


@dataclass(frozen=True)
class ScoreReport:
    """``overall[arm]`` and ``groups[field][value][arm]`` are ``Confusion`` values."""

    overall: dict = field(default_factory=dict)
    groups: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "overall": {arm: conf.as_dict() for arm, conf in sorted(self.overall.items())},
            "groups": {name: {value: {arm: conf.as_dict() for arm, conf in sorted(arms.items())}
                              for value, arms in sorted(values.items())}
                       for name, values in sorted(self.groups.items())},
        }


def score(rows, group_by=("category", "stage")) -> ScoreReport:
    """Score ``rows`` per arm, and per arm within each ``group_by`` field."""
    overall: dict[str, Confusion] = {}
    groups: dict[str, dict[str, dict[str, Confusion]]] = {name: {} for name in group_by}
    for row in rows:
        overall.setdefault(row.arm, Confusion()).add(row)
        for name in group_by:
            value = str(getattr(row, name))
            groups[name].setdefault(value, {}).setdefault(row.arm, Confusion()).add(row)
    return ScoreReport(overall=overall, groups=groups)


@dataclass(frozen=True)
class PairedComparison:
    arm_a: str
    arm_b: str
    n_pairs: int
    b: int
    c: int
    p_value: float
    excluded: int

    def as_dict(self) -> dict:
        return {"arm_a": self.arm_a, "arm_b": self.arm_b, "n_pairs": self.n_pairs, "b": self.b,
                "c": self.c, "p_value": self.p_value, "excluded": self.excluded,
                "note": "b: arm_a right and arm_b wrong; c: arm_a wrong and arm_b right; exact "
                        "two-sided McNemar over items scored in both arms with a CONFIRMED or "
                        "REFUTED label"}


def _correct(row: ScoreRow) -> bool:
    return row.outcome == "SCORED" and row.predicted == row.label


def paired_comparison(rows, arm_a: str = "none", arm_b: str = "retrieved") -> PairedComparison:
    """Exact McNemar test of ``arm_a`` against ``arm_b`` over items both arms scored."""
    by_arm: dict[str, dict[str, ScoreRow]] = {arm_a: {}, arm_b: {}}
    for row in rows:
        if row.arm in by_arm:
            by_arm[row.arm][row.hid] = row
    hids = sorted(set(by_arm[arm_a]) | set(by_arm[arm_b]))
    b = c = pairs = 0
    for hid in hids:
        first, second = by_arm[arm_a].get(hid), by_arm[arm_b].get(hid)
        if first is None or second is None or first.label == "UNCERTAIN":
            continue
        if first.outcome != "SCORED" or second.outcome != "SCORED":
            continue
        pairs += 1
        right_a, right_b = _correct(first), _correct(second)
        if right_a and not right_b:
            b += 1
        elif right_b and not right_a:
            c += 1
    discordant = b + c
    if discordant == 0:
        p_value = 1.0
    else:
        tail = sum(comb(discordant, i) for i in range(0, min(b, c) + 1))
        p_value = min(1.0, 2 * tail / (2 ** discordant))
    return PairedComparison(arm_a, arm_b, pairs, b, c, p_value, len(hids) - pairs)


@dataclass(frozen=True)
class ChunkOutcome:
    """One source chunk judged in one arm.

    ``generated`` findings were produced; ``matched_labels`` holds the label of each distinct
    labelled record a finding matched; ``scope_confirmed`` counts the CONFIRMED records located in
    the chunk (the recall denominator, kept even when the chunk failed).
    """

    arm: str
    generated: int
    matched_labels: tuple[str, ...]
    scope_confirmed: int
    outcome: str


@dataclass
class ChunkScore:
    generated: int = 0
    matched_confirmed: int = 0
    matched_refuted: int = 0
    matched_uncertain: int = 0
    scope_confirmed: int = 0
    chunks: int = 0
    failed_chunks: int = 0

    @property
    def precision_floor(self) -> float | None:
        """Matched CONFIRMED over all generated findings; unlabelled findings count against it."""
        return self.matched_confirmed / self.generated if self.generated else None

    @property
    def precision_labelled(self) -> float | None:
        judged = self.matched_confirmed + self.matched_refuted
        return self.matched_confirmed / judged if judged else None

    @property
    def recall(self) -> float | None:
        return self.matched_confirmed / self.scope_confirmed if self.scope_confirmed else None

    def as_dict(self) -> dict:
        return {"chunks": self.chunks, "failed_chunks": self.failed_chunks,
                "generated": self.generated, "matched_confirmed": self.matched_confirmed,
                "matched_refuted": self.matched_refuted, "matched_uncertain": self.matched_uncertain,
                "scope_confirmed": self.scope_confirmed,
                "precision_floor": self.precision_floor,
                "precision_labelled": self.precision_labelled, "recall": self.recall,
                "note": "precision_floor counts unlabelled findings as wrong, so it is a lower bound"}


def score_chunks(outcomes) -> dict:
    """Aggregate chunk outcomes per arm."""
    result: dict[str, ChunkScore] = {}
    for item in outcomes:
        total = result.setdefault(item.arm, ChunkScore())
        total.chunks += 1
        total.scope_confirmed += item.scope_confirmed
        if item.outcome != "SCORED":
            total.failed_chunks += 1
            continue
        total.generated += item.generated
        for label in item.matched_labels:
            if label == "CONFIRMED":
                total.matched_confirmed += 1
            elif label == "REFUTED":
                total.matched_refuted += 1
            else:
                total.matched_uncertain += 1
    return result


def _overlap(first_start: int, first_end: int, second_start: int, second_end: int) -> int:
    return min(first_end, second_end) - max(first_start, second_start) + 1


def match_findings(findings, records, tolerance: int) -> tuple[HypothesisRecord, ...]:
    """Match each located finding to at most one labelled record, each record at most once.

    A finding matches a record whose line range, widened by ``tolerance`` lines, overlaps the
    finding's range. Among candidates the largest overlap wins, then the nearest start, then the
    lower ``hid``. A finding with no line range matches nothing (it still counts as generated).
    """
    taken: set[str] = set()
    matched: list[HypothesisRecord] = []
    for finding in findings:
        if not isinstance(finding, ParsedHypothesis) or finding.lines is None:
            continue
        best = None
        for record in records:
            if record.hid in taken:
                continue
            size = _overlap(finding.lines.start, finding.lines.end,
                            record.lines.start - tolerance, record.lines.end + tolerance)
            if size < 1:
                continue
            rank = (-size, abs(finding.lines.start - record.lines.start), record.hid)
            if best is None or rank < best[0]:
                best = (rank, record)
        if best is not None:
            taken.add(best[1].hid)
            matched.append(best[1])
    return tuple(matched)
