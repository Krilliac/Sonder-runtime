"""Read-only claim checker over a repository on disk.

The domain module (``domain.claim_check``) holds the pure checks; this adapter
resolves the cited file inside the repository root (no escape through ``..``,
symlinks or junctions), reads it with size and binary guards, searches direct
callers by word, and runs a JSONL batch. Nothing here writes to the repository.

Thread affinity: a ``ClaimChecker`` caches file text and is not thread-safe;
use one per thread. State ownership: the checker owns its caches and never
hands out its internal line lists.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from sonder_runtime.domain.claim_check import (
    CheckResult, CheckStatus, Claim, ClaimError, ClaimReport, GuardHit, Verdict,
    check_line_in_range, check_symbols_near, collapse_duplicates, decide_verdict,
    extract_citations, extract_missing_guards, find_guards, find_named_guards,
    find_symbol_uses, guard_families,
)
from sonder_runtime.platform.claim_check_config import ClaimCheckConfig

_MIN_NEEDLE = 3
_NOT_NEEDLES = frozenset({"if", "for", "new", "get", "set", "var", "int", "the"})


def _split_lines(text: str) -> list[str]:
    """Lines as an editor numbers them: split on LF, drop CR, no phantom last line."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def _needle(symbol: str) -> str:
    parts = [p for p in re.split(r"\.|::|->|#", symbol) if p]
    return parts[-1] if parts else ""


class ClaimChecker:
    """Checks claims against one repository root."""

    def __init__(self, root: str | os.PathLike[str], config: ClaimCheckConfig | None = None,
                 read_guard: Callable[[str], bool] | None = None):
        """``read_guard(real_path) -> bool`` lets the host refuse files it must not expose
        (credentials, control state); a refused file is neither checked nor searched."""
        self._config = config or ClaimCheckConfig()
        self._read_guard = read_guard
        self._root = Path(os.path.realpath(root))
        self._text: dict[str, str | None] = {}
        self._cached_bytes = 0
        self._split: dict[str, list[str]] = {}
        self._listing: dict[str, list[str]] = {}

    # -- file access ---------------------------------------------------------

    def _within(self, real: Path) -> bool:
        return real == self._root or real.is_relative_to(self._root)

    def _resolve(self, name: str) -> tuple[str | None, CheckResult | None]:
        """The real path of ``name`` inside the root, or the failing/unreadable result."""
        if "\x00" in name:
            return None, CheckResult("file_in_root", CheckStatus.FAIL, "path holds a NUL byte")
        raw = Path(name)
        candidate = raw if raw.is_absolute() else self._root / raw
        real = Path(os.path.realpath(candidate))
        if not self._within(real):
            return None, CheckResult(
                "file_in_root", CheckStatus.FAIL, "path resolves outside the repository root")
        if not real.is_file():
            return None, CheckResult("file_in_root", CheckStatus.FAIL, "no such file in the repository")
        return str(real), None

    def _read(self, real: str) -> str | None:
        """File text, or None when too large, binary or unreadable. Cached up to a budget."""
        if real in self._text:
            return self._text[real]
        text: str | None = None
        try:
            if os.path.getsize(real) <= self._config.max_file_bytes:
                with open(real, "rb") as handle:
                    data = handle.read(self._config.max_file_bytes + 1)
                if len(data) <= self._config.max_file_bytes and b"\x00" not in data[:8192]:
                    text = data.decode("utf-8", errors="replace")
        except OSError:
            text = None
        size = len(text) if text is not None else 0
        if self._cached_bytes + size > self._config.max_cache_bytes:
            self._text.clear()
            self._split.clear()
            self._cached_bytes = 0
        self._text[real] = text
        self._cached_bytes += size
        return text

    def _lines(self, real: str) -> list[str] | None:
        cached = self._split.get(real)
        if cached is not None:
            return cached
        text = self._read(real)
        if text is None:
            return None
        lines = _split_lines(text)
        if len(self._split) >= 64:
            self._split.clear()
        self._split[real] = lines
        return lines

    def _relative(self, real: str) -> str:
        try:
            return Path(real).relative_to(self._root).as_posix()
        except ValueError:
            return real

    def _candidates(self, suffix: str) -> list[str]:
        """Sorted real paths of files under the root with this suffix; skip dirs pruned."""
        key = suffix.lower()
        if key in self._listing:
            return self._listing[key]
        skip = set(self._config.skip_dirs)
        found: list[str] = []
        for base, dirs, files in os.walk(self._root):
            dirs[:] = sorted(
                d for d in dirs
                if d not in skip and not os.path.islink(os.path.join(base, d))
                and not os.path.isjunction(os.path.join(base, d)))
            for name in files:
                if name.lower().endswith(key):
                    found.append(os.path.join(base, name))
        found.sort()
        self._listing[key] = found
        return found

    # -- checks --------------------------------------------------------------

    def check(self, claim: Claim) -> ClaimReport:
        real, failure = self._resolve(claim.file)
        if failure is not None:
            return ClaimReport(claim, decide_verdict((failure,), ()), (failure,), ())
        if self._read_guard is not None and not self._read_guard(real):
            note = CheckResult(
                "file_in_root", CheckStatus.NOT_CHECKABLE, "file is refused by the read policy")
            return ClaimReport(claim, Verdict.KEEP, (note,), ())
        lines = self._lines(real)
        if lines is None:
            note = CheckResult(
                "file_in_root", CheckStatus.NOT_CHECKABLE,
                "file is binary, unreadable or over max_file_bytes")
            return ClaimReport(claim, Verdict.KEEP, (note,), ())
        cfg = self._config
        checks = [
            CheckResult("file_in_root", CheckStatus.PASS, "file is inside the repository root"),
            check_line_in_range(claim, len(lines)),
        ]
        symbols = check_symbols_near(claim, lines, cfg.symbol_window_lines, cfg.symbol_match)
        checks.append(symbols)
        guards = self._guards(claim, real, lines)
        if claim.line_start is None:
            guard_check = CheckResult(
                "guard_search", CheckStatus.NOT_CHECKABLE, "claim cites no line")
        else:
            detail = (f"{len(guards)} guard-shaped line(s) found (a hint, not a verdict)"
                      if guards else "no guard shape found nearby")
            guard_check = CheckResult("guard_search", CheckStatus.PASS, detail)
        checks.append(guard_check)
        # A guard hint never outweighs a wrong citation: any failed check rejects.
        verdict = decide_verdict(tuple(checks), tuple(guards))
        return ClaimReport(claim, verdict, tuple(checks), tuple(guards))

    def _guards(self, claim: Claim, real: str, lines: list[str]) -> list[GuardHit]:
        if claim.line_start is None:
            return []
        cfg = self._config
        families = guard_families(
            claim.category, " ".join((claim.guard, *claim.guard_symbols)))
        rel = self._relative(real)
        near_first = claim.line_start - cfg.guard_window_lines
        near_last = claim.line_end + cfg.guard_window_lines

        def scan(file_lines, path, first, last, where):
            # A name the claim says is missing outranks a generic guard shape.
            named = find_named_guards(file_lines, path, first, last, claim.guard_symbols, where)
            taken = {h.line for h in named}
            return named + [h for h in find_guards(
                file_lines, path, first, last, families, cfg.extra_guard_patterns, where)
                if h.line not in taken]

        hits = sorted(scan(lines, rel, near_first, near_last, "near"), key=lambda h: h.line)
        seen = {(h.path, h.line) for h in hits}
        needles: list[tuple[str, str]] = []
        for symbol in claim.symbols:
            needle = _needle(symbol)
            if len(needle) >= _MIN_NEEDLE and needle not in _NOT_NEEDLES:
                needles.append((needle, "caller"))
        if claim.entry_point.strip():
            entry = _needle(claim.entry_point.strip())
            if len(entry) >= _MIN_NEEDLE:
                needles.append((entry, "entry"))
        if not needles or (cfg.max_callers == 0):
            return hits[:cfg.max_evidence]
        suffix = Path(real).suffix
        if not suffix:
            return hits[:cfg.max_evidence]
        files = self._candidates(suffix)
        home = os.path.dirname(real)
        files = sorted(files, key=lambda p: (os.path.dirname(p) != home, p))
        files = files[:cfg.max_caller_files]
        uses = 0
        for path in files:
            if uses >= cfg.max_callers or len(hits) >= cfg.max_evidence:
                break
            text = self._read(path)
            if text is None:
                continue
            for needle, where in needles:
                if needle not in text:
                    continue
                if path != real and self._read_guard is not None and not self._read_guard(path):
                    break
                file_lines = self._lines(path)
                if file_lines is None:
                    continue
                rel_path = self._relative(path)
                for use in find_symbol_uses(file_lines, needle):
                    if path == real and near_first <= use <= near_last:
                        continue
                    uses += 1
                    for hit in scan(file_lines, rel_path, use - cfg.caller_window_lines,
                                    use + cfg.caller_window_lines, where):
                        if (hit.path, hit.line) not in seen:
                            seen.add((hit.path, hit.line))
                            hits.append(hit)
                    if uses >= cfg.max_callers:
                        break
                if uses >= cfg.max_callers:
                    break
        return hits[:cfg.max_evidence]

    def check_many(self, claims: Iterable[Claim]) -> list[ClaimReport]:
        """Reports in input order; claims near an earlier one on the same file collapse."""
        claims = list(claims)
        primary_of = collapse_duplicates(claims, self._config.duplicate_window_lines)
        done: dict[int, ClaimReport] = {}
        reports: list[ClaimReport] = []
        for index, claim in enumerate(claims):
            primary = primary_of[index]
            if primary == index:
                done[index] = self.check(claim)
                reports.append(done[index])
                continue
            base = done[primary]
            label = claims[primary].claim_id or f"#{primary + 1}"
            reports.append(replace(base, claim=claim, duplicate_of=label))
        return reports


def check_claim(root, claim: Claim, config: ClaimCheckConfig | None = None) -> ClaimReport:
    return ClaimChecker(root, config).check(claim)


# -- claim input --------------------------------------------------------------

_LINE_FORM = re.compile(r"\s*(\d{1,7})(?:\s*[-:]\s*(\d{1,7}))?\s*")


def _line_pair(row: Mapping) -> tuple[int | None, int | None]:
    start, end = row.get("line_start"), row.get("line_end")
    if start is not None:
        return start, end
    for key in ("line", "lines"):
        value = row.get(key)
        if value is None or value == "":
            continue
        if type(value) is int:
            return value, value
        if isinstance(value, str):
            match = _LINE_FORM.fullmatch(value)
            if match is None:
                raise ClaimError(f"{key} must be a number or 'first-last', got {value!r}")
            first = int(match.group(1))
            return first, int(match.group(2) or first)
        raise ClaimError(f"{key} must be a number or 'first-last'")
    return None, None


def _symbol_tuple(value) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return tuple(v.strip() for v in value if v.strip())
    raise ClaimError("symbols must be a string or a list of strings")


def claim_from_mapping(row: Mapping) -> Claim:
    """Build a claim from a JSON-style mapping; prose in ``text`` fills missing citations."""
    if not isinstance(row, Mapping):
        raise ClaimError("claim must be a JSON object")
    file = row.get("file")
    if not isinstance(file, str):
        raise ClaimError("claim needs a file")
    start, end = _line_pair(row)
    symbols = _symbol_tuple(row.get("symbols"))
    prose = row.get("text")
    missing = _symbol_tuple(row.get("guard_symbols"))
    if isinstance(prose, str) and prose:
        cited, named = extract_citations(prose)
        if start is None and cited:
            start, end = cited[0], cited[0]
        if not symbols:
            symbols = named
        missing = missing or extract_missing_guards(prose)
    text_fields = {}
    for key in ("category", "guard", "entry_point"):
        value = row.get(key, "")
        if not isinstance(value, str):
            raise ClaimError(f"{key} must be a string")
        text_fields[key] = value
    ident = row.get("claim_id", row.get("id", ""))
    return Claim(file=file, line_start=start, line_end=end, symbols=symbols,
                 guard_symbols=missing,
                 claim_id=str(ident) if ident is not None else "", **text_fields)


# -- batch --------------------------------------------------------------------

@dataclass
class BatchSummary:
    total: int = 0
    errors: int = 0
    duplicates: int = 0
    verdicts: dict[str, int] = field(
        default_factory=lambda: {"reject": 0, "flag": 0, "keep": 0})


def check_jsonl(source, destination, root, config: ClaimCheckConfig | None = None) -> BatchSummary:
    """Check every claim of a JSONL file; write one JSON line per input row, in order.

    A row may carry ``repo_root`` to override ``root``. Malformed rows become
    ``{"line_no", "error": {"code", "message"}}`` entries instead of failing the batch.
    """
    config = config or ClaimCheckConfig()
    rows: list[tuple[int, object]] = []
    with open(source, encoding="utf-8") as handle:
        for number, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            rows.append((number, raw))
            if len(rows) > config.max_batch_claims:
                raise ClaimError(
                    f"batch holds more than max_batch_claims={config.max_batch_claims} claims")
    outputs: list[dict | None] = [None] * len(rows)
    groups: dict[str, list[tuple[int, Claim]]] = {}
    for position, (number, raw) in enumerate(rows):
        try:
            row = json.loads(raw)
        except ValueError as exc:
            outputs[position] = {"line_no": number, "error": {
                "code": "invalid_json", "message": str(exc)[:200]}}
            continue
        try:
            claim = claim_from_mapping(row)
            row_root = row.get("repo_root") or root
            if not isinstance(row_root, (str, os.PathLike)):
                raise ClaimError("repo_root must be a path")
        except ClaimError as exc:
            outputs[position] = {"line_no": number, "error": {
                "code": "invalid_claim", "message": str(exc)[:200]}}
            continue
        groups.setdefault(os.fspath(row_root), []).append((position, claim))
    summary = BatchSummary(total=len(rows))
    for row_root, members in groups.items():
        checker = ClaimChecker(row_root, config)
        reports = checker.check_many([claim for _, claim in members])
        for (position, _), report in zip(members, reports):
            body = report.to_dict()
            body["line_no"] = rows[position][0]
            outputs[position] = body
            summary.verdicts[report.verdict.value] += 1
            if report.duplicate_of:
                summary.duplicates += 1
    summary.errors = sum(1 for item in outputs if item is not None and "error" in item)
    with open(destination, "w", encoding="utf-8", newline="\n") as handle:
        for item in outputs:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    return summary


__all__ = [
    "BatchSummary", "ClaimChecker", "check_claim", "check_jsonl", "claim_from_mapping",
]
