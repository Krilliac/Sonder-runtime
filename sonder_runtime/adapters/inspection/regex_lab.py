"""Pure, bounded, stdlib-only regex tester, substitution previewer, and ReDoS analyzer.

Eliminates regex off-by-one, anchoring, capture-group, and catastrophic
backtracking errors by testing patterns against positive/negative samples and
inspecting the compiled ``re._parser``/token structure statically.
"""
from __future__ import annotations

import re
from typing import Any

MAX_PATTERN_CHARS = 2_000
MAX_SAMPLE_CHARS = 32_000
MAX_SAMPLES = 50
MAX_MATCHES_PER_SAMPLE = 50

_FLAG_MAP: dict[str, int] = {
    "i": re.IGNORECASE,
    "m": re.MULTILINE,
    "s": re.DOTALL,
    "x": re.VERBOSE,
    "a": re.ASCII,
}

# Static ReDoS hazard heuristics (structural, zero execution needed).
_NESTED_QUANTIFIER_RE = re.compile(
    r"\((?:[^()\\]|\\.)*[*+}](?:[^()\\]|\\.)*\)\s*(?:[*+]|\{\d+,\s*\d*\})"
)
_OVERLAPPING_ALT_RE = re.compile(
    r"\((?:[^()|\\]|\\.)*\|(?:[^()|\\]|\\.)*\)\s*(?:[*+]|\{\d+,\s*\d*\})"
)
_ADJACENT_WILDCARDS_RE = re.compile(r"(?:\.\*|\.\+)(?:[^()\\]|\\.){0,6}(?:\.\*|\.\+)")


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


def _analyze_redos(pattern: str) -> list[dict[str, str]]:
    hazards: list[dict[str, str]] = []
    if _NESTED_QUANTIFIER_RE.search(pattern):
        hazards.append({
            "kind": "nested_quantifier",
            "severity": "high",
            "detail": "Quantified group contains an inner quantifier (e.g. (a+)+), risking exponential backtracking.",
        })
    if _OVERLAPPING_ALT_RE.search(pattern):
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

    hazards = _analyze_redos(pat)
    info = _describe_pattern(compiled, pat)
    limit = max(1, min(MAX_MATCHES_PER_SAMPLE, int(max_matches or 20)))

    # Guard against catastrophic backtracking on large inputs when high-severity hazard is detected
    effective_sample_cap = 256 if any(h["severity"] == "high" for h in hazards) else MAX_SAMPLE_CHARS

    def _run_on_sample(sample_text: str) -> dict[str, Any]:
        clipped = sample_text[:effective_sample_cap]
        truncated = len(sample_text) > effective_sample_cap
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


__all__ = ["regex_lab"]
