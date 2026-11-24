"""Pure, bounded, stdlib-only regex tester, substitution previewer, and ReDoS analyzer.

Eliminates regex off-by-one, anchoring, capture-group, and catastrophic
backtracking errors by testing patterns against positive/negative samples and
inspecting the compiled ``re._parser``/token structure statically.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

MAX_PATTERN_CHARS = 2_000
MAX_SAMPLE_CHARS = 32_000
MAX_MEDIUM_HAZARD_SAMPLE_CHARS = 1_024
MAX_HIGH_HAZARD_SAMPLE_CHARS = 256
MAX_SAMPLES = 50
MAX_MATCHES_PER_SAMPLE = 50

_FLAG_MAP: dict[str, int] = {
    "i": re.IGNORECASE,
    "m": re.MULTILINE,
    "s": re.DOTALL,
    "x": re.VERBOSE,
    "a": re.ASCII,
}

# Static ReDoS hazard heuristics (fallback structural checks).
_NESTED_QUANTIFIER_RE = re.compile(
    r"\((?:[^()\\]|\\.)*[*+}](?:[^()\\]|\\.)*\)\s*(?:[*+]|\{\d+,\s*\d*\})"
)
_OVERLAPPING_ALT_RE = re.compile(
    r"\((?:[^()|\\]|\\.)*\|(?:[^()|\\]|\\.)*\)\s*(?:[*+]|\{\d+,\s*\d*\})"
)
_ADJACENT_WILDCARDS_RE = re.compile(r"(?:\.\*|\.\+)(?:[^()\\]|\\.){0,6}(?:\.\*|\.\+)")

try:
    import re._parser as _re_parser  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - Python <3.11 fallback
    _re_parser = None  # type: ignore[assignment]


_ASCII_DIGITS = frozenset(range(ord("0"), ord("9") + 1))
_ASCII_SPACE = frozenset(ord(ch) for ch in " \t\n\r\f\v")
_ASCII_WORD = frozenset(
    list(range(ord("0"), ord("9") + 1))
    + list(range(ord("A"), ord("Z") + 1))
    + list(range(ord("a"), ord("z") + 1))
    + [ord("_")]
)


def _category_chars(cat: Any) -> frozenset[int] | None:
    name = str(cat).upper()
    if "NOT_" in name:
        return None
    if name.endswith("DIGIT"):
        return _ASCII_DIGITS
    if name.endswith("SPACE"):
        return _ASCII_SPACE
    if name.endswith("WORD"):
        return _ASCII_WORD
    return None


def _item_first_chars(op: Any, av: Any, p: Any, *, depth: int = 0) -> tuple[bool, set[int] | None]:
    """Return ``(can_match_empty, leading_char_ordinals_or_None_for_wildcard)``."""
    if depth > 16:
        return True, None
    if op == p.LITERAL:
        return False, {int(av)}
    if op in (p.ANY, p.NOT_LITERAL):
        return False, None
    if op == p.IN:
        if av and av[0][0] == p.NEGATE:
            return False, None
        chars: set[int] = set()
        for sub_op, sub_av in av:
            if sub_op == p.LITERAL:
                chars.add(int(sub_av))
            elif sub_op == p.RANGE:
                lo, hi = int(sub_av[0]), int(sub_av[1])
                if hi - lo > 256:
                    return False, None
                chars.update(range(lo, hi + 1))
            elif sub_op == p.CATEGORY:
                cat_set = _category_chars(sub_av)
                if cat_set is None:
                    return False, None
                chars.update(cat_set)
            else:
                return False, None
        return False, chars
    if op == p.CATEGORY:
        cat_set = _category_chars(av)
        return False, (set(cat_set) if cat_set is not None else None)
    if op == p.SUBPATTERN:
        return _seq_first_chars(av[3], p, depth=depth + 1)
    if op == p.BRANCH:
        can_empty = False
        union: set[int] | None = set()
        for branch in av[1]:
            b_empty, b_chars = _seq_first_chars(branch, p, depth=depth + 1)
            can_empty = can_empty or b_empty
            if b_chars is None:
                union = None
            elif union is not None:
                union.update(b_chars)
        return can_empty, union
    if op in (p.MAX_REPEAT, p.MIN_REPEAT, getattr(p, "POSSESSIVE_REPEAT", None)):
        min_r, _max_r, body = av
        b_empty, b_chars = _seq_first_chars(body, p, depth=depth + 1)
        return (min_r == 0 or b_empty), b_chars
    if op in (p.AT, p.ASSERT, p.ASSERT_NOT):
        return True, set()
    return True, None


def _seq_first_chars(seq: Any, p: Any, *, depth: int = 0) -> tuple[bool, set[int] | None]:
    """Leading character set for a parsed regex sequence."""
    if depth > 16:
        return True, None
    union: set[int] | None = set()
    for op, av in seq:
        item_empty, item_chars = _item_first_chars(op, av, p, depth=depth + 1)
        if item_chars is None:
            union = None
        elif union is not None:
            union.update(item_chars)
        if not item_empty:
            return False, union
    return True, union


def _branches_overlap(branches: Sequence[Any], p: Any) -> bool:
    """True when two alternation branches can overlap or any branch matches empty."""
    seen_sets: list[set[int] | None] = []
    for branch in branches:
        b_empty, b_chars = _seq_first_chars(branch, p)
        if b_empty:
            return True
        for prev in seen_sets:
            if prev is None or b_chars is None or (prev & b_chars):
                return True
        seen_sets.append(b_chars)
    return False


def _ast_analyze_redos(pattern: str, flag_bits: int = 0) -> list[dict[str, str]]:
    """Inspect Python's parsed regex AST for catastrophic backtracking structures."""
    if _re_parser is None:
        return []
    p = _re_parser
    try:
        parsed = p.parse(pattern, flag_bits)
    except Exception:
        return []

    found: dict[str, dict[str, str]] = {}
    possessive_op = getattr(p, "POSSESSIVE_REPEAT", None)

    def _add(kind: str, severity: str, detail: str) -> None:
        prev = found.get(kind)
        if prev is None or (prev["severity"] != "high" and severity == "high"):
            found[kind] = {"kind": kind, "severity": severity, "detail": detail}

    def _is_unbounded_wildcard_repeat(op: Any, av: Any) -> bool:
        if op not in (p.MAX_REPEAT, p.MIN_REPEAT):
            return False
        _lo, hi, body = av
        if hi <= 1:
            return False
        items = list(body)
        return len(items) == 1 and items[0][0] in (p.ANY, p.NOT_LITERAL)

    def _walk(seq: Any, *, in_repeat: bool, depth: int = 0) -> None:
        if depth > 64:
            _add(
                "excessive_nesting",
                "high",
                "Regex nesting depth exceeds safe ReDoS analysis ceiling.",
            )
            return
        wildcard_runs = 0
        for op, av in seq:
            if _is_unbounded_wildcard_repeat(op, av):
                wildcard_runs += 1
                if wildcard_runs >= 3:
                    _add(
                        "polynomial_backtracking",
                        "high",
                        "Three or more chained unbounded wildcards (e.g. .*.*.*) risk severe polynomial backtracking.",
                    )
            if op in (p.MAX_REPEAT, p.MIN_REPEAT):
                lo, hi, body = av
                is_variable_or_multi = (hi > 1) or (lo != hi)
                if in_repeat and is_variable_or_multi:
                    _add(
                        "nested_quantifier",
                        "high",
                        "Quantified group contains an inner quantifier (e.g. (a+)+), risking exponential backtracking.",
                    )
                _walk(body, in_repeat=(in_repeat or hi > 1), depth=depth + 1)
            elif possessive_op is not None and op == possessive_op:
                _lo, _hi, body = av
                _walk(body, in_repeat=False, depth=depth + 1)
            elif op == p.SUBPATTERN:
                _walk(av[3], in_repeat=in_repeat, depth=depth + 1)
            elif op == p.BRANCH:
                branches = av[1]
                if in_repeat:
                    if _branches_overlap(branches, p):
                        _add(
                            "overlapping_alternation",
                            "high",
                            "Quantified alternation has overlapping or nullable branches (e.g. (a|aa)+), risking exponential backtracking.",
                        )
                    else:
                        _add(
                            "quantified_alternation",
                            "medium",
                            "Quantified alternation group can cause super-linear backtracking if branches overlap.",
                        )
                for branch in branches:
                    _walk(branch, in_repeat=in_repeat, depth=depth + 1)
            elif op in (p.ASSERT, p.ASSERT_NOT):
                _walk(av[1], in_repeat=in_repeat, depth=depth + 1)
            elif op == p.GROUPREF and in_repeat:
                _add(
                    "repeated_backreference",
                    "high",
                    "Backreference inside a quantified group risks exponential backtracking.",
                )

    _walk(parsed, in_repeat=False)
    return list(found.values())


def detect_redos_hazards(pattern: str, flag_bits: int = 0) -> list[dict[str, str]]:
    """Detect static ReDoS hazards via AST inspection plus structural heuristics."""
    hazards = _ast_analyze_redos(pattern, flag_bits)
    kinds = {h["kind"] for h in hazards}
    if "nested_quantifier" not in kinds and _NESTED_QUANTIFIER_RE.search(pattern):
        hazards.append({
            "kind": "nested_quantifier",
            "severity": "high",
            "detail": "Quantified group contains an inner quantifier (e.g. (a+)+), risking exponential backtracking.",
        })
        kinds.add("nested_quantifier")
    if (
        "overlapping_alternation" not in kinds
        and "quantified_alternation" not in kinds
        and _OVERLAPPING_ALT_RE.search(pattern)
    ):
        hazards.append({
            "kind": "quantified_alternation",
            "severity": "medium",
            "detail": "Quantified alternation group (e.g. (a|aa)+) can cause super-linear backtracking if branches overlap.",
        })
    if _ADJACENT_WILDCARDS_RE.search(pattern):
        hazards.append({
            "kind": "adjacent_wildcards",
            "severity": "medium",
            "detail": "Multiple greedy/lazy '.*' or '.+' spans in sequence can cause quadratic backtracking on non-matching lines.",
        })
    return hazards


def _analyze_redos(pattern: str, flag_bits: int = 0) -> list[dict[str, str]]:
    return detect_redos_hazards(pattern, flag_bits)


def _parse_flags(flags: str) -> tuple[int, list[str]]:
    bits = 0
    names: list[str] = []
    for ch in str(flags or "").lower():
        if ch in _FLAG_MAP:
            bits |= _FLAG_MAP[ch]
            if ch not in names:
                names.append(ch)
        elif ch.strip():
            raise ValueError(f"unknown regex flag {ch!r} (valid: i, m, s, x, a)")
    return bits, names


def _describe_pattern(compiled: re.Pattern[str], pattern: str) -> dict[str, Any]:
    return {
        "group_count": compiled.groups,
        "named_groups": dict(sorted(compiled.groupindex.items(), key=lambda kv: kv[1])),
        "anchored_start": pattern.startswith("^") or pattern.startswith(r"\A"),
        "anchored_end": pattern.endswith("$") or pattern.endswith(r"\Z"),
        "length": len(pattern),
    }


def regex_lab(
    pattern: str,
    *,
    text: str = "",
    samples: list[str] | None = None,
    should_match: list[str] | None = None,
    should_not_match: list[str] | None = None,
    replacement: str | None = None,
    flags: str = "",
    mode: str = "search",
    max_matches: int = 20,
) -> dict[str, Any]:
    """Compile, analyze for ReDoS hazards, and test a regular expression against text/cases."""
    pat = str(pattern or "")
    if not pat:
        return {"ok": False, "error": "pattern is required"}
    if len(pat) > MAX_PATTERN_CHARS:
        return {"ok": False, "error": f"pattern exceeds {MAX_PATTERN_CHARS} characters"}

    mode_lc = str(mode or "search").strip().lower() or "search"
    if mode_lc not in {"search", "match", "fullmatch", "findall"}:
        return {"ok": False, "error": f"unknown mode {mode_lc!r} (use search, match, fullmatch, or findall)"}

    try:
        flag_bits, flag_names = _parse_flags(flags)
        compiled = re.compile(pat, flag_bits)
    except (re.error, ValueError) as exc:
        return {"ok": False, "error": f"invalid regex: {exc}"}

    hazards = _analyze_redos(pat, flag_bits)
    info = _describe_pattern(compiled, pat)
    limit = max(1, min(MAX_MATCHES_PER_SAMPLE, int(max_matches or 20)))

    has_high_redos = any(h["severity"] == "high" for h in hazards)
    has_medium_redos = any(h["severity"] == "medium" for h in hazards)
    # Refuse execution when a high-severity catastrophic backtracking hazard is detected,
    # and tighten input caps on medium-severity hazards.
    if has_high_redos:
        effective_sample_cap = MAX_HIGH_HAZARD_SAMPLE_CHARS
    elif has_medium_redos:
        effective_sample_cap = MAX_MEDIUM_HAZARD_SAMPLE_CHARS
    else:
        effective_sample_cap = MAX_SAMPLE_CHARS

    def _run_on_sample(sample_text: str) -> dict[str, Any]:
        clipped = sample_text[:effective_sample_cap]
        truncated = len(sample_text) > effective_sample_cap
        if has_high_redos:
            return {
                "matched": False,
                "match_count": 0,
                "matches": [],
                "truncated": truncated,
                "execution_blocked": True,
                "blocked_reason": "regex execution refused due to high-severity ReDoS hazard",
            }
        matches: list[dict[str, Any]] = []
        if mode_lc == "fullmatch":
            m = compiled.fullmatch(clipped)
            if m:
                matches.append({
                    "match": m.group(0),
                    "span": [m.start(), m.end()],
                    "groups": list(m.groups()),
                    "named_groups": m.groupdict(),
                })
        elif mode_lc == "match":
            m = compiled.match(clipped)
            if m:
                matches.append({
                    "match": m.group(0),
                    "span": [m.start(), m.end()],
                    "groups": list(m.groups()),
                    "named_groups": m.groupdict(),
                })
        elif mode_lc == "search":
            m = compiled.search(clipped)
            if m:
                matches.append({
                    "match": m.group(0),
                    "span": [m.start(), m.end()],
                    "groups": list(m.groups()),
                    "named_groups": m.groupdict(),
                })
        else:
            for idx, m in enumerate(compiled.finditer(clipped)):
                if idx >= limit:
                    break
                matches.append({
                    "match": m.group(0),
                    "span": [m.start(), m.end()],
                    "groups": list(m.groups()),
                    "named_groups": m.groupdict(),
                })
        entry: dict[str, Any] = {
            "matched": bool(matches),
            "match_count": len(matches),
            "matches": matches,
            "truncated": truncated,
        }
        if replacement is not None:
            try:
                replaced, count = compiled.subn(str(replacement), clipped, count=limit)
                entry["substituted"] = replaced
                entry["substitutions"] = count
            except re.error as exc:
                entry["substitution_error"] = str(exc)
        return entry

    result: dict[str, Any] = {
        "ok": True,
        "pattern": pat,
        "flags": flag_names,
        "mode": mode_lc,
        "info": info,
        "redos_hazards": hazards,
    }

    if text:
        result["text_result"] = _run_on_sample(str(text))

    if samples:
        bounded = list(samples)[:MAX_SAMPLES]
        result["samples"] = [
            {"input": str(s)[:200], **_run_on_sample(str(s))}
            for s in bounded
        ]

    assertion_failures: list[dict[str, Any]] = []
    if should_match:
        for s in list(should_match)[:MAX_SAMPLES]:
            r = _run_on_sample(str(s))
            if not r["matched"]:
                assertion_failures.append({"expected": "match", "input": str(s)[:200], "actual": "no_match"})
    if should_not_match:
        for s in list(should_not_match)[:MAX_SAMPLES]:
            r = _run_on_sample(str(s))
            if r["matched"]:
                assertion_failures.append({
                    "expected": "no_match",
                    "input": str(s)[:200],
                    "actual": r["matches"][0]["match"],
                })
    if should_match or should_not_match:
        result["assertions_passed"] = len(assertion_failures) == 0
        result["assertion_failures"] = assertion_failures

    return result


__all__ = ["detect_redos_hazards", "regex_lab"]
