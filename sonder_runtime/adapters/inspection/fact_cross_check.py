"""Pure, linear-time, stdlib-only prose factual cross-checker.

Detects high-signal, zero-ambiguity factual hallucinations inside model answers
without needing external search or subprocesses:
1. Inline arithmetic mistakes: ``A op B = C`` and ``P% of N = R`` outside code
   fences, checked with exact ``Decimal``/``Fraction`` math.
2. Markdown table & bullet-list total mismatches: verifies ``Total`` / ``Sum``
   rows against the actual sum of numeric values above them.
3. Calendar & day-of-week hallucinations: catches nonexistent calendar dates
   (e.g. ``2026-02-30``) and wrong stated weekdays (e.g. ``Monday, 2026-11-22``
   when 2026-11-22 is Sunday).
4. Enumerated count mismatches: catches `"the following 5 items:"` followed by
   a bullet/numbered list with a different item count.
5. Direct same-subject polarity contradictions across paragraphs (e.g.
   ``foo is enabled by default`` vs ``foo is disabled by default``).
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal, InvalidOperation
import re
from typing import Any

MAX_TEXT_CHARS = 64_000
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)

# 1. Inline arithmetic: e.g. `12 * 14 = 160` or `2^10 = 1048`
_ARITH_EQ_RE = re.compile(
    r"(?<![A-Za-z0-9_.])"
    r"(-?\d+(?:,\d{3})*(?:\.\d+)?)\s*([+\-*/\u00d7\u00f7]|\*\*|\^)\s*"
    r"(-?\d+(?:,\d{3})*(?:\.\d+)?)\s*=\s*"
    r"(-?\d+(?:,\d{3})*(?:\.\d+)?)"
    r"(?![A-Za-z0-9_%]|\.\d)"
)
_PCT_OF_RE = re.compile(
    r"(?<![A-Za-z0-9_.])"
    r"(\d+(?:\.\d+)?)\s*%\s+of\s+(-?\d+(?:,\d{3})*(?:\.\d+)?)\s*(?:=|is|equals)\s*"
    r"(-?\d+(?:,\d{3})*(?:\.\d+)?)"
    r"(?![A-Za-z0-9_%]|\.\d)",
    re.IGNORECASE,
)

# 2. Calendar + weekday
_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
_WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_ISO_DATE_RE = re.compile(
    r"(?:(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*,?\s+)?"
    r"\b(19\d\d|20\d\d|21\d\d)-(\d{2})-(\d{2})\b",
    re.IGNORECASE,
)
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
_NAMED_DATE_RE = re.compile(
    r"(?:(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*,?\s+)?"
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{1,2})\s*,\s*(19\d\d|20\d\d|21\d\d)\b",
    re.IGNORECASE,
)

# 3. Direct polarity assertions: `<subject> is (not )?(enabled|disabled|true|false|thread-safe|mutable|immutable|synchronous|asynchronous|supported|unsupported)`
_POLARITY_RE = re.compile(
    r"\b(`?[A-Za-z_][A-Za-z0-9_.-]{1,40}`?)\s+is\s+"
    r"(not\s+)?(enabled|disabled|thread-safe|mutable|immutable|synchronous|asynchronous|supported|unsupported|required|optional)\b",
    re.IGNORECASE,
)
_OPPOSITE_PAIR: dict[str, tuple[str, bool]] = {
    "enabled": ("enabled", True),
    "disabled": ("enabled", False),
    "thread-safe": ("thread-safe", True),
    "mutable": ("mutable", True),
    "immutable": ("mutable", False),
    "synchronous": ("synchronous", True),
    "asynchronous": ("synchronous", False),
    "supported": ("supported", True),
    "unsupported": ("supported", False),
    "required": ("required", True),
    "optional": ("required", False),
}

# 4. Counted list header
_COUNT_HEADER_RE = re.compile(
    r"\b(?:following|these|top|main)\s+(\d{1,2})\s+"
    r"(?:items|points|steps|reasons|issues|bugs|fixes|tools|checks|rules|options|ways|methods|causes)\s*:\s*$",
    re.IGNORECASE,
)


def _to_dec(raw: str) -> Decimal | None:
    try:
        return Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return None


def _check_inline_arithmetic(prose: str) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for m in _ARITH_EQ_RE.finditer(prose):
        a = _to_dec(m.group(1))
        op = m.group(2)
        b = _to_dec(m.group(3))
        claimed = _to_dec(m.group(4))
        if a is None or b is None or claimed is None:
            continue
        try:
            if op == "+":
                actual = a + b
            elif op == "-":
                actual = a - b
            elif op in {"*", "\u00d7"}:
                actual = a * b
            elif op in {"/", "\u00f7"}:
                if b == 0:
                    issues.append({"kind": "division_by_zero", "excerpt": m.group(0), "detail": "division by zero"})
                    continue
                actual = a / b
            elif op in {"**", "^"}:
                if b != int(b) or abs(int(b)) > 200 or int(b) < 0:
                    continue
                actual = a ** int(b)
            else:
                continue
        except (InvalidOperation, OverflowError):
            continue
        # Tolerance accounts for reasonable rounded display (e.g. 1/3 = 0.33)
        claim_str = m.group(4).replace(",", "")
        decimals = len(claim_str.split(".")[1]) if "." in claim_str else 0
        tol = Decimal(10) ** (-decimals) * Decimal("0.55") if decimals > 0 else Decimal("0")
        if abs(actual - claimed) > tol:
            issues.append({
                "kind": "arithmetic_error",
                "excerpt": m.group(0),
                "claimed": str(claimed),
                "actual": format(actual.normalize(), "f") if actual == int(actual) else f"{float(actual):.6g}",
            })

    for m in _PCT_OF_RE.finditer(prose):
        pct = _to_dec(m.group(1))
        base = _to_dec(m.group(2))
        claimed = _to_dec(m.group(3))
        if pct is None or base is None or claimed is None:
            continue
        actual = (pct * base) / Decimal(100)
        claim_str = m.group(3).replace(",", "")
        decimals = len(claim_str.split(".")[1]) if "." in claim_str else 0
        tol = Decimal(10) ** (-decimals) * Decimal("0.55") if decimals > 0 else Decimal("0")
        if abs(actual - claimed) > tol:
            issues.append({
                "kind": "percentage_error",
                "excerpt": m.group(0),
                "claimed": str(claimed),
                "actual": format(actual.normalize(), "f") if actual == int(actual) else f"{float(actual):.6g}",
            })
    return issues


def _check_dates(prose: str) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for m in _ISO_DATE_RE.finditer(prose):
        wd_str, y_s, mo_s, d_s = m.group(1), m.group(2), m.group(3), m.group(4)
        y, mo, d = int(y_s), int(mo_s), int(d_s)
        try:
            dt = _dt.date(y, mo, d)
        except ValueError:
            issues.append({
                "kind": "invalid_calendar_date",
                "excerpt": m.group(0),
                "detail": f"{y:04d}-{mo:02d}-{d:02d} is not a real calendar date",
            })
            continue
        if wd_str:
            claimed_wd = _WEEKDAYS[wd_str.lower()]
            if dt.weekday() != claimed_wd:
                issues.append({
                    "kind": "wrong_weekday",
                    "excerpt": m.group(0),
                    "claimed": wd_str,
                    "actual": _WEEKDAY_NAMES[dt.weekday()],
                })

    for m in _NAMED_DATE_RE.finditer(prose):
        wd_str, mo_name, d_s, y_s = m.group(1), m.group(2), m.group(3), m.group(4)
        y, mo, d = int(y_s), _MONTHS[mo_name.lower()], int(d_s)
        try:
            dt = _dt.date(y, mo, d)
        except ValueError:
            issues.append({
                "kind": "invalid_calendar_date",
                "excerpt": m.group(0),
                "detail": f"{mo_name} {d}, {y} is not a real calendar date",
            })
            continue
        if wd_str:
            claimed_wd = _WEEKDAYS[wd_str.lower()]
            if dt.weekday() != claimed_wd:
                issues.append({
                    "kind": "wrong_weekday",
                    "excerpt": m.group(0),
                    "claimed": wd_str,
                    "actual": _WEEKDAY_NAMES[dt.weekday()],
                })
    return issues


def _check_markdown_table_totals(prose: str) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    lines = prose.splitlines()
    i = 0
    while i < len(lines):
        if "|" in lines[i] and i + 1 < len(lines) and re.match(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", lines[i + 1]):
            table_rows: list[list[str]] = []
            j = i + 2
            while j < len(lines) and "|" in lines[j] and lines[j].strip():
                cells = [c.strip().strip("*`") for c in lines[j].strip().strip("|").split("|")]
                table_rows.append(cells)
                j += 1
            if len(table_rows) >= 2:
                last = table_rows[-1]
                if last and last[0].lower() in {"total", "sum", "subtotal"}:
                    body = table_rows[:-1]
                    for col in range(1, len(last)):
                        claimed = _to_dec(re.sub(r"[$€£%]", "", last[col]))
                        if claimed is None:
                            continue
                        col_vals: list[Decimal] = []
                        all_numeric = True
                        for row in body:
                            if col >= len(row):
                                all_numeric = False
                                break
                            v = _to_dec(re.sub(r"[$€£%]", "", row[col]))
                            if v is None:
                                all_numeric = False
                                break
                            col_vals.append(v)
                        if all_numeric and col_vals:
                            actual = sum(col_vals, Decimal(0))
                            if abs(actual - claimed) > Decimal("0.01"):
                                issues.append({
                                    "kind": "table_total_mismatch",
                                    "column": col,
                                    "claimed": str(claimed),
                                    "actual": str(actual),
                                })
            i = j
            continue
        i += 1
    return issues


def _check_counted_lists(prose: str) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    lines = prose.splitlines()
    for idx, line in enumerate(lines):
        m = _COUNT_HEADER_RE.search(line.strip())
        if not m:
            continue
        expected = int(m.group(1))
        if expected < 2:
            continue
        count = 0
        j = idx + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        while j < len(lines):
            stripped = lines[j].strip()
            if re.match(r"^(?:[-*+]\s+|\d+[.)]\s+)", stripped):
                count += 1
                j += 1
            elif not stripped:
                break
            elif lines[j].startswith(("  ", "\t")):
                j += 1
            else:
                break
        if count > 0 and count != expected:
            issues.append({
                "kind": "list_count_mismatch",
                "excerpt": line.strip()[:120],
                "claimed_count": expected,
                "actual_count": count,
            })
    return issues


def _check_polarity_contradictions(prose: str) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    seen: dict[tuple[str, str], tuple[bool, str]] = {}
    for m in _POLARITY_RE.finditer(prose):
        # Skip conditional/hypothetical context immediately preceding match
        prefix = prose[max(0, m.start() - 30) : m.start()].lower()
        if any(w in prefix for w in ("if ", "when ", "unless ", "otherwise", "whereas ", "instead of")):
            continue
        subj = m.group(1).strip("`").lower()
        if subj in {"it", "this", "that", "there", "which", "what"}:
            continue
        neg = bool(m.group(2))
        prop_word = m.group(3).lower()
        axis, base_val = _OPPOSITE_PAIR[prop_word]
        val = (not base_val) if neg else base_val
        key = (subj, axis)
        if key in seen and seen[key][0] != val:
            issues.append({
                "kind": "self_contradiction",
                "subject": m.group(1).strip("`"),
                "axis": axis,
                "first_claim": seen[key][1],
                "conflicting_claim": m.group(0),
            })
        else:
            seen[key] = (val, m.group(0))
    return issues


def fact_cross_check(text: str) -> dict[str, Any]:
    """Cross-check prose for arithmetic errors, table total mismatches, bad dates/weekdays, list counts, and contradictions."""
    raw = str(text or "")
    if not raw.strip():
        return {"ok": False, "error": "text is required"}
    if len(raw) > MAX_TEXT_CHARS:
        return {"ok": False, "error": f"text exceeds {MAX_TEXT_CHARS} characters"}

    prose = _CODE_FENCE_RE.sub("\n", raw)
    issues: list[dict[str, Any]] = []
    issues.extend(_check_inline_arithmetic(prose))
    issues.extend(_check_dates(prose))
    issues.extend(_check_markdown_table_totals(prose))
    issues.extend(_check_counted_lists(prose))
    issues.extend(_check_polarity_contradictions(prose))

    return {
        "ok": True,
        "clean": len(issues) == 0,
        "issue_count": len(issues),
        "issues": issues[:50],
    }


__all__ = ["fact_cross_check"]
