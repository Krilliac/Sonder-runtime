"""Deterministic, I/O-free checks of a model's code claim against source text.

A claim cites a file, a line (range), named symbols and, optionally, the guard
it says is missing and an entry point. Each check returns a typed result
(pass, fail or not-checkable) with evidence. A wrong citation rejects the
claim; a guard shape found nearby only flags it (a hint, never a verdict that
the claim is false); anything else is kept for a human or model to judge.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

_MAX_TEXT = 160


class ClaimError(ValueError):
    """The claim itself is malformed (no file, impossible line range)."""


class CheckStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    NOT_CHECKABLE = "not_checkable"


class Verdict(str, Enum):
    REJECT = "reject"
    FLAG = "flag"
    KEEP = "keep"


@dataclass(frozen=True)
class Claim:
    file: str
    line_start: int | None = None
    line_end: int | None = None
    symbols: tuple[str, ...] = ()
    category: str = ""
    guard: str = ""
    entry_point: str = ""
    claim_id: str = ""

    def __post_init__(self):
        if not isinstance(self.file, str) or not self.file.strip():
            raise ClaimError("claim needs a file")
        if self.line_start is None and self.line_end is not None:
            raise ClaimError("line_end needs line_start")
        if self.line_start is not None:
            if type(self.line_start) is not int or self.line_start < 1:
                raise ClaimError("line_start must be an integer >= 1")
            if self.line_end is None:
                object.__setattr__(self, "line_end", self.line_start)
            elif type(self.line_end) is not int or self.line_end < self.line_start:
                raise ClaimError("line_end must be an integer >= line_start")
        object.__setattr__(self, "symbols", tuple(self.symbols))


@dataclass(frozen=True)
class Evidence:
    path: str
    line: int
    text: str


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: CheckStatus
    detail: str
    evidence: tuple[Evidence, ...] = ()


@dataclass(frozen=True)
class GuardHit:
    path: str
    line: int
    kind: str
    text: str
    where: str  # "near" the cited line, in a "caller", or at the "entry" point


@dataclass(frozen=True)
class ClaimReport:
    claim: Claim
    verdict: Verdict
    checks: tuple[CheckResult, ...]
    guards: tuple[GuardHit, ...]
    duplicate_of: str = ""

    def to_dict(self) -> dict:
        claim = self.claim
        return {
            "claim_id": claim.claim_id,
            "file": claim.file,
            "line_start": claim.line_start,
            "line_end": claim.line_end,
            "verdict": self.verdict.value,
            "duplicate_of": self.duplicate_of,
            "checks": [
                {"name": c.name, "status": c.status.value, "detail": c.detail,
                 "evidence": [{"path": e.path, "line": e.line, "text": e.text}
                              for e in c.evidence]}
                for c in self.checks
            ],
            "guards": [
                {"path": g.path, "line": g.line, "kind": g.kind, "text": g.text,
                 "where": g.where}
                for g in self.guards
            ],
        }


def _clip(text: str) -> str:
    text = text.strip()
    return text if len(text) <= _MAX_TEXT else text[:_MAX_TEXT - 3] + "..."


def check_line_in_range(claim: Claim, line_count: int) -> CheckResult:
    if claim.line_start is None:
        return CheckResult("line_in_range", CheckStatus.NOT_CHECKABLE, "claim cites no line")
    if claim.line_end > line_count:
        return CheckResult(
            "line_in_range", CheckStatus.FAIL,
            f"cited line {claim.line_end} is past the end of the file ({line_count} lines)")
    return CheckResult(
        "line_in_range", CheckStatus.PASS,
        f"lines {claim.line_start}-{claim.line_end} exist ({line_count} lines)")


def _word(symbol: str) -> re.Pattern:
    return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(symbol) + r"(?![A-Za-z0-9_])")


def _symbol_needles(symbol: str) -> list[str]:
    parts = [p for p in re.split(r"\.|::|->|#", symbol) if p]
    needles = [symbol.strip()]
    if len(parts) > 1:
        needles.append(parts[-1])
    return [n for n in needles if n]


def check_symbols_near(claim: Claim, lines: list[str], window: int,
                       require: str = "all") -> CheckResult:
    if claim.line_start is None:
        return CheckResult("symbols_near", CheckStatus.NOT_CHECKABLE, "claim cites no line")
    if not claim.symbols:
        return CheckResult("symbols_near", CheckStatus.NOT_CHECKABLE, "claim names no symbols")
    first = max(1, claim.line_start - window)
    last = min(len(lines), claim.line_end + window)
    evidence: list[Evidence] = []
    missing: list[str] = []
    for symbol in claim.symbols:
        patterns = [_word(n) for n in _symbol_needles(symbol)]
        found = None
        for number in range(first, last + 1):
            text = lines[number - 1]
            if any(p.search(text) for p in patterns):
                found = number
                break
        if found is None:
            missing.append(symbol)
        else:
            evidence.append(Evidence("", found, _clip(lines[found - 1])))
    ok = (not missing) if require == "all" else bool(evidence)
    if ok:
        return CheckResult("symbols_near", CheckStatus.PASS,
                           f"{len(evidence)} of {len(claim.symbols)} symbols within "
                           f"{window} lines of the citation", tuple(evidence))
    return CheckResult(
        "symbols_near", CheckStatus.FAIL,
        f"not within {window} lines of {claim.line_start}-{claim.line_end}: "
        + ", ".join(missing), tuple(evidence))


# --- guard shapes -----------------------------------------------------------

_GUARD_PATTERNS: dict[str, tuple[re.Pattern, ...]] = {
    "bounds": tuple(re.compile(p) for p in (
        r"\b(?:Math\.|MathF\.|MathHelper\.|std::)?(?:Min|Clamp|min|clamp)\s*\(",
        r"(?:\.Length|\.Count|\.Size|\.size\(\)|\blen\(|\bsizeof\b)[^;{]*(?:<=|>=|<|>)",
        r"(?:<=|>=|<|>)\s*[\w.]*(?:Max|MAX|Limit|LIMIT|Cap|Capacity)\w*",
        r"\b[\w.]*(?:Max|MAX|Limit|LIMIT)\w*\s*(?:<=|>=|<|>)",
        r"ArgumentOutOfRange|ThrowIf(?:Greater|Less|Negative)\w*|IndexOutOfRange",
    )),
    "null": tuple(re.compile(p) for p in (
        r"(?:==|!=)\s*null\b",
        r"\bis\s+(?:not\s+)?(?:null|None)\b",
        r"ThrowIfNull|ArgumentNullException|IsNullOrEmpty|IsNullOrWhiteSpace",
        r"\?\?\s*throw|\bnullptr\b",
        r"\bif\s*\(\s*!\s*\w+\s*\)",
    )),
    "validation": tuple(re.compile(p) for p in (
        r"\bValidate\w*\s*\(|\bTryParse\w*\s*\(|\bIsValid\w*|\bVerify\w*\s*\(",
        r"\bthrow\s+new\s+(?:Argument|Invalid|Format|Security|Unauthorized|Protocol|Overflow)\w*",
        r"\braise\s+(?:ValueError|PermissionError|TypeError)",
    )),
    "auth": tuple(re.compile(p) for p in (
        r"\bAuthori[sz]\w*|\bAuthenticat\w*|\bIsAdmin\b|\bHasPermission\w*|\bCheckPermission\w*",
        r"\bRequire(?:Role|Auth|Permission)\w*|\bCanAccess\w*|\bIsAllowed\w*",
        r"VerifySignature|ConstantTimeEquals|FixedTimeEquals|\bHmac\w*|Handshake",
    )),
    "path": tuple(re.compile(p) for p in (
        r"GetFullPath|IsPathRooted|GetRelativePath|ResolveLinkTarget|is_relative_to|realpath",
        r"StartsWith\s*\([^)]*(?:root|Root|base|Base)",
        r"Contains\s*\(\s*\"\.\.\"",
    )),
    "rate": tuple(re.compile(p) for p in (
        r"RateLimit\w*|Throttl\w*|\bSemaphore\w*|Cooldown|TokenBucket|Debounce",
    )),
    "timeout": tuple(re.compile(p) for p in (
        r"Timeout|CancelAfter|CancellationTokenSource\s*\(",
    )),
    "concurrency": tuple(re.compile(p) for p in (
        r"\block\s*\(|Interlocked|Transaction|\bMutex\b|SemaphoreSlim|ConcurrentDictionary|\bTryAdd\b",
    )),
    "sanitize": tuple(re.compile(p) for p in (
        r"HtmlEncode|\bEscape\w*|\bSanitiz\w*|WebUtility|\bEncode\w*\s*\(",
    )),
}

_CATEGORY_FAMILIES = (
    (re.compile(r"dos|crash|client input", re.I), ("bounds", "null", "validation", "rate", "timeout")),
    (re.compile(r"anti-?cheat|cluster|auth", re.I), ("auth", "validation", "null")),
    (re.compile(r"path|file", re.I), ("path", "validation", "null")),
    (re.compile(r"leak", re.I), ("auth", "validation")),
    (re.compile(r"dupe|economy|race", re.I), ("concurrency", "validation", "bounds")),
    (re.compile(r"xss|inject", re.I), ("sanitize", "validation")),
)
_GUARD_TEXT_FAMILIES = (
    (re.compile(r"length|size|count|bound|cap\b|limit|max|clamp", re.I), "bounds"),
    (re.compile(r"null|none\b", re.I), "null"),
    (re.compile(r"auth|permission|signature|secret|token|identity", re.I), "auth"),
    (re.compile(r"path|travers|symlink", re.I), "path"),
    (re.compile(r"rate|throttl|flood", re.I), "rate"),
    (re.compile(r"timeout|deadline", re.I), "timeout"),
    (re.compile(r"lock|race|atomic|transaction|concurren", re.I), "concurrency"),
    (re.compile(r"escap|sanitiz|encod", re.I), "sanitize"),
    (re.compile(r"valid|verif|check", re.I), "validation"),
)
_DEFAULT_FAMILIES = ("bounds", "null", "validation")
_COMMENT = re.compile(r"^\s*(?://|#|\*|/\*|--)")


def guard_families(category: str, guard_text: str) -> tuple[str, ...]:
    """Guard families for a claim: its category's, plus any the guard text names."""
    chosen: list[str] = []
    for pattern, families in _CATEGORY_FAMILIES:
        if pattern.search(category or ""):
            chosen.extend(f for f in families if f not in chosen)
            break
    for pattern, family in _GUARD_TEXT_FAMILIES:
        if pattern.search(guard_text or "") and family not in chosen:
            chosen.append(family)
    return tuple(chosen) if chosen else _DEFAULT_FAMILIES


def find_guards(lines: list[str], path: str, first: int, last: int,
                families: tuple[str, ...], extra_patterns: tuple[str, ...] = (),
                where: str = "near") -> list[GuardHit]:
    """Guard-shaped lines in ``first..last`` (1-based, inclusive); comments skipped."""
    first = max(1, first)
    last = min(len(lines), last)
    extras = []
    for raw in extra_patterns:
        try:
            extras.append(re.compile(raw))
        except re.error:
            continue
    hits: list[GuardHit] = []
    for number in range(first, last + 1):
        text = lines[number - 1]
        if _COMMENT.match(text):
            continue
        kind = None
        for family in families:
            if any(p.search(text) for p in _GUARD_PATTERNS.get(family, ())):
                kind = family
                break
        if kind is None and any(p.search(text) for p in extras):
            kind = "extra"
        if kind is not None:
            hits.append(GuardHit(path, number, kind, _clip(text), where))
    return hits


_DECLARATION = re.compile(
    r"\b(?:void|int|bool|string|Task|async|public|private|internal|protected|static|"
    r"def|func|fn|function|class|struct)\b[^()=;]*$")


def find_symbol_uses(lines: list[str], symbol: str) -> list[int]:
    """1-based lines that call ``symbol`` (``symbol(``), skipping its declarations."""
    call = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(symbol) + r"\s*\(")
    uses = []
    for number, text in enumerate(lines, 1):
        match = call.search(text)
        if match is None or _COMMENT.match(text):
            continue
        if _DECLARATION.search(text[:match.start()]):
            continue
        uses.append(number)
    return uses


def decide_verdict(checks: tuple[CheckResult, ...], guards: tuple[GuardHit, ...]) -> Verdict:
    if any(c.status is CheckStatus.FAIL for c in checks):
        return Verdict.REJECT
    if guards:
        return Verdict.FLAG
    return Verdict.KEEP


def _file_key(path: str) -> str:
    return path.replace("\\", "/").removeprefix("./").lower()


def collapse_duplicates(claims: list[Claim], window: int) -> list[int]:
    """Index of each claim's primary: the earliest same-file claim within ``window`` lines.

    Claims without a cited line never collapse; every claim is its own primary
    unless an earlier primary on the same file sits within ``window`` lines.
    """
    primaries: dict[str, list[int]] = {}
    result: list[int] = []
    for index, claim in enumerate(claims):
        if claim.line_start is None:
            result.append(index)
            continue
        key = _file_key(claim.file)
        match = None
        for candidate in primaries.get(key, ()):
            if abs(claims[candidate].line_start - claim.line_start) <= window:
                match = candidate
                break
        if match is None:
            primaries.setdefault(key, []).append(index)
            match = index
        result.append(match)
    return result


_LINE_REF = re.compile(
    r"(?:\b[Ll]ines?\s+(\d{1,6})(?:\s*[-–]\s*(\d{1,6}))?)|(?:\.[A-Za-z]{1,5}:(\d{1,6}))")
_BACKTICK = re.compile(r"`([^`\n]{1,120})`")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:(?:\.|::|->)[A-Za-z_][A-Za-z0-9_]*)*")


def extract_citations(text: str) -> tuple[tuple[int, ...], tuple[str, ...]]:
    """Cited line numbers (in order, unique) and backticked identifiers from claim prose."""
    lines: list[int] = []
    for match in _LINE_REF.finditer(text or ""):
        for group in match.groups():
            if group is not None:
                number = int(group)
                if number >= 1 and number not in lines:
                    lines.append(number)
    symbols: list[str] = []
    for match in _BACKTICK.finditer(text or ""):
        token = re.sub(r"\(.*\)$", "", match.group(1).strip()).strip()
        if _IDENT.fullmatch(token) and token not in symbols:
            symbols.append(token)
    return tuple(lines), tuple(symbols)
