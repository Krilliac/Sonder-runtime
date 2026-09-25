"""Recognize the final summary line test runners and build tools print.

Pure: each recognizer reads text lines only. ``find_summary`` is the
generalization of ``tail -1``: it scans the last ``max_tail`` lines from the
end and tries each runner's grammar in a fixed priority order, so a pytest
summary is preferred to the ``make: *** ... Error 1`` line a Makefile wrapper
prints after it.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .model import clean_text


MAX_SUMMARY_LINE_CHARS = 400
HARD_MAX_TAIL = 2_000

STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RunSummary:
    tool: str
    line: str
    passed: int | None
    failed: int | None
    skipped: int | None
    errors: int | None
    total: int | None
    duration_seconds: float | None
    status: str

    def to_wire(self) -> dict:
        return {
            "tool": self.tool, "line": self.line, "passed": self.passed,
            "failed": self.failed, "skipped": self.skipped, "errors": self.errors,
            "total": self.total, "duration_seconds": self.duration_seconds,
            "status": self.status,
        }


def _status(failed: int | None, errors: int | None, passed: int | None) -> str:
    if (failed or 0) > 0 or (errors or 0) > 0:
        return STATUS_FAILED
    if passed is not None and passed > 0:
        return STATUS_PASSED
    return STATUS_UNKNOWN


def _summary(tool: str, line: str, **counts) -> RunSummary:
    passed = counts.get("passed")
    failed = counts.get("failed")
    skipped = counts.get("skipped")
    errors = counts.get("errors")
    total = counts.get("total")
    status = counts.get("status") or _status(failed, errors, passed)
    return RunSummary(
        tool=tool, line=clean_text(line, MAX_SUMMARY_LINE_CHARS),
        passed=passed, failed=failed, skipped=skipped, errors=errors, total=total,
        duration_seconds=counts.get("duration"), status=status,
    )


# --- pytest ------------------------------------------------------------------

_PYTEST_PART_RE = re.compile(
    r"^(?P<n>\d+) (?P<word>failed|passed|skipped|errors?|warnings?|xfailed|"
    r"xpassed|deselected|reruns?)$"
)
_PYTEST_TAIL_RE = re.compile(
    r"^(?P<body>.+?) in (?P<dur>\d+(?:\.\d+)?)s(?: \(\d+(?::\d+){1,2}\))?$"
)


def parse_pytest_summary(line: str) -> RunSummary | None:
    """``= 3 failed, 10 passed, 2 skipped, 1 error in 4.21s =`` or its ``-q`` form."""
    text = clean_text(line, 1_000).strip()
    if not text:
        return None
    text = text.strip("=").strip()
    match = _PYTEST_TAIL_RE.match(text)
    if not match:
        return None
    body = match.group("body").strip()
    counts = {"failed": 0, "passed": 0, "skipped": 0, "errors": 0,
              "xfailed": 0, "xpassed": 0}
    if body == "no tests ran":
        pass
    else:
        seen = False
        for part in body.split(", "):
            item = _PYTEST_PART_RE.match(part.strip())
            if not item:
                return None
            seen = True
            word = item.group("word")
            number = int(item.group("n"))
            if word in ("error", "errors"):
                counts["errors"] += number
            elif word in counts:
                counts[word] += number
        if not seen:
            return None
    passed = counts["passed"] + counts["xpassed"]
    skipped = counts["skipped"] + counts["xfailed"]
    total = passed + counts["failed"] + skipped + counts["errors"]
    status = None if total else STATUS_UNKNOWN
    return _summary(
        "pytest", line, passed=passed, failed=counts["failed"], skipped=skipped,
        errors=counts["errors"], total=total, duration=float(match.group("dur")),
        status=status,
    )


# --- unittest ----------------------------------------------------------------

_UNITTEST_RAN_RE = re.compile(r"^Ran (?P<n>\d+) tests? in (?P<dur>\d+(?:\.\d+)?)s$")
_UNITTEST_STATUS_RE = re.compile(r"^(?P<word>OK|FAILED)(?: \((?P<detail>[^)]{0,200})\))?$")
_UNITTEST_DETAIL_RE = re.compile(r"(?P<key>failures|errors|skipped|expected failures|"
                                 r"unexpected successes)=(?P<n>\d+)")


def _parse_unittest(lines: Sequence[str], index: int) -> RunSummary | None:
    text = lines[index].strip()
    status = _UNITTEST_STATUS_RE.match(text)
    if not status:
        return None
    ran = None
    for back in range(1, 6):
        if index - back < 0:
            break
        ran = _UNITTEST_RAN_RE.match(lines[index - back].strip())
        if ran:
            break
    if ran is None:
        return None
    detail = {m.group("key"): int(m.group("n"))
              for m in _UNITTEST_DETAIL_RE.finditer(status.group("detail") or "")}
    total = int(ran.group("n"))
    failed = detail.get("failures", 0)
    errors = detail.get("errors", 0)
    skipped = detail.get("skipped", 0) + detail.get("expected failures", 0)
    passed = max(0, total - failed - errors - skipped)
    word = status.group("word")
    return _summary(
        "unittest", "%s; %s" % (ran.group(0), text), passed=passed, failed=failed,
        skipped=skipped, errors=errors, total=total,
        duration=float(ran.group("dur")),
        status=STATUS_PASSED if word == "OK" else STATUS_FAILED,
    )


# --- cargo libtest -------------------------------------------------------------

_LIBTEST_RE = re.compile(
    r"^test result: (?P<word>ok|FAILED)\. (?P<passed>\d+) passed; (?P<failed>\d+) failed; "
    r"(?P<ignored>\d+) ignored(?:; (?P<measured>\d+) measured)?"
    r"(?:; (?P<filtered>\d+) filtered out)?(?:; finished in (?P<dur>\d+(?:\.\d+)?)s)?$"
)


def _collect_libtest(lines: Sequence[str], window: range) -> RunSummary | None:
    passed = failed = ignored = 0
    duration = 0.0
    last_line = ""
    any_failed = False
    found = False
    for index in window:
        match = _LIBTEST_RE.match(lines[index].strip())
        if not match:
            continue
        found = True
        passed += int(match.group("passed"))
        failed += int(match.group("failed"))
        ignored += int(match.group("ignored"))
        duration += float(match.group("dur") or 0.0)
        any_failed = any_failed or match.group("word") == "FAILED"
        last_line = lines[index]
    if not found:
        return None
    return _summary(
        "cargo", last_line, passed=passed, failed=failed, skipped=ignored,
        errors=0, total=passed + failed + ignored, duration=duration,
        status=STATUS_FAILED if any_failed or failed else STATUS_PASSED,
    )


# --- go test ------------------------------------------------------------------

_GO_OK_RE = re.compile(r"^ok\s+\S+\s+(?:\d+(?:\.\d+)?s|\(cached\))(?:\s.*)?$")
_GO_FAIL_RE = re.compile(r"^FAIL(?:\s+\S+(?:\s+(?:\d+(?:\.\d+)?s|\[[^\]]{1,80}\]))?)?$")


def _collect_go(lines: Sequence[str], window: range) -> RunSummary | None:
    saw_fail = False
    last_line = ""
    for index in window:
        text = lines[index].strip()
        if _GO_OK_RE.match(text):
            last_line = lines[index]
        elif _GO_FAIL_RE.match(text):
            saw_fail = True
            last_line = lines[index]
    if not last_line:
        return None
    # ``ok``/``FAIL`` lines count packages, not tests, so the test fields stay
    # unknown rather than reporting package counts as test counts.
    return _summary("go", last_line, status=STATUS_FAILED if saw_fail else STATUS_PASSED)


# --- single-line recognizers ---------------------------------------------------

_CTEST_RE = re.compile(
    r"^(?P<pct>\d+)% tests passed, (?P<failed>\d+) tests? failed out of (?P<total>\d+)$"
)
_JEST_RE = re.compile(r"^Tests:\s+(?P<body>.+?),\s+(?P<total>\d+) total$")
_JEST_PART_RE = re.compile(r"^(?P<n>\d+) (?P<word>failed|passed|skipped|todo|pending)$")
_VITEST_RE = re.compile(r"^Tests\s+(?P<body>.+?)\s+\((?P<total>\d+)\)$")
_VITEST_PART_RE = re.compile(r"^(?P<n>\d+) (?P<word>failed|passed|skipped|todo)$")
_DOTNET_RE = re.compile(
    r"^(?P<word>Failed|Passed)!\s+-\s+Failed:\s+(?P<failed>\d+),\s+Passed:\s+(?P<passed>\d+),"
    r"\s+Skipped:\s+(?P<skipped>\d+),\s+Total:\s+(?P<total>\d+)"
    r"(?:,\s+Duration:\s+(?P<dur>[\d.]+)\s*(?P<unit>ms|s|m))?"
)
_MAVEN_RE = re.compile(
    r"^(?:\[(?:INFO|ERROR|WARNING)\]\s+)?Tests run: (?P<run>\d+), Failures: (?P<failures>\d+), "
    r"Errors: (?P<errors>\d+), Skipped: (?P<skipped>\d+)(?P<rest>.*)$"
)
_GRADLE_RE = re.compile(
    r"^(?P<total>\d+) tests? completed, (?P<failed>\d+) failed(?:, (?P<skipped>\d+) skipped)?$"
)
_MAKE_RE = re.compile(r"^(?:g?make|mingw32-make)(?:\[\d+\])?: \*\*\* .*\bError (?P<code>\d+)$")


def _parse_ctest(line: str) -> RunSummary | None:
    match = _CTEST_RE.match(line.strip())
    if not match:
        return None
    total = int(match.group("total"))
    failed = int(match.group("failed"))
    return _summary("ctest", line, passed=max(0, total - failed), failed=failed,
                    skipped=None, errors=0, total=total)


def _parts(body: str, part_re: re.Pattern, separator: str) -> dict | None:
    counts: dict[str, int] = {}
    for part in body.split(separator):
        item = part_re.match(part.strip())
        if not item:
            return None
        counts[item.group("word")] = counts.get(item.group("word"), 0) + int(item.group("n"))
    return counts


def _parse_jest(line: str) -> RunSummary | None:
    match = _JEST_RE.match(line.strip())
    if not match:
        return None
    counts = _parts(match.group("body"), _JEST_PART_RE, ",")
    if counts is None:
        return None
    return _summary(
        "jest", line, passed=counts.get("passed", 0), failed=counts.get("failed", 0),
        skipped=counts.get("skipped", 0) + counts.get("todo", 0) + counts.get("pending", 0),
        errors=0, total=int(match.group("total")),
    )


def _parse_vitest(line: str) -> RunSummary | None:
    match = _VITEST_RE.match(line.strip())
    if not match:
        return None
    counts = _parts(match.group("body"), _VITEST_PART_RE, "|")
    if counts is None:
        return None
    return _summary(
        "vitest", line, passed=counts.get("passed", 0), failed=counts.get("failed", 0),
        skipped=counts.get("skipped", 0) + counts.get("todo", 0), errors=0,
        total=int(match.group("total")),
    )


def _parse_dotnet(line: str) -> RunSummary | None:
    match = _DOTNET_RE.match(line.strip())
    if not match:
        return None
    duration = None
    if match.group("dur"):
        value = float(match.group("dur"))
        duration = {"ms": value / 1000.0, "s": value, "m": value * 60.0}[match.group("unit")]
    failed = int(match.group("failed"))
    return _summary(
        "dotnet", line, passed=int(match.group("passed")), failed=failed,
        skipped=int(match.group("skipped")), errors=0, total=int(match.group("total")),
        duration=duration,
        status=STATUS_FAILED if match.group("word") == "Failed" or failed else STATUS_PASSED,
    )


def _parse_maven(line: str) -> RunSummary | None:
    match = _MAVEN_RE.match(line.strip())
    if not match or "Time elapsed" in match.group("rest"):
        return None
    run = int(match.group("run"))
    failures = int(match.group("failures"))
    errors = int(match.group("errors"))
    skipped = int(match.group("skipped"))
    return _summary(
        "maven", line, passed=max(0, run - failures - errors - skipped),
        failed=failures, skipped=skipped, errors=errors, total=run,
    )


def _parse_gradle(line: str) -> RunSummary | None:
    match = _GRADLE_RE.match(line.strip())
    if not match:
        return None
    total = int(match.group("total"))
    failed = int(match.group("failed"))
    skipped = int(match.group("skipped") or 0)
    return _summary(
        "gradle", line, passed=max(0, total - failed - skipped), failed=failed,
        skipped=skipped, errors=0, total=total,
    )


def _parse_make(line: str) -> RunSummary | None:
    if not _MAKE_RE.match(line.strip()):
        return None
    return _summary("make", line, status=STATUS_FAILED)


def _single(parser: Callable[[str], RunSummary | None]):
    def scan(lines: Sequence[str], window: range) -> RunSummary | None:
        for index in reversed(window):
            found = parser(lines[index])
            if found is not None:
                return found
        return None
    return scan


def _scan_unittest(lines: Sequence[str], window: range) -> RunSummary | None:
    for index in reversed(window):
        found = _parse_unittest(lines, index)
        if found is not None:
            return found
    return None


# Priority order: the first recognizer to find its shape anywhere in the tail
# window wins, each scanning from the end.
_RECOGNIZERS = (
    _single(parse_pytest_summary),
    _scan_unittest,
    _collect_libtest,
    _collect_go,
    _single(_parse_ctest),
    _single(_parse_jest),
    _single(_parse_vitest),
    _single(_parse_dotnet),
    _single(_parse_maven),
    _single(_parse_gradle),
    _single(_parse_make),
)


def find_summary(lines: Sequence[str], *, max_tail: int = 200) -> RunSummary | None:
    """The run's own summary line, from the last ``max_tail`` lines."""
    max_tail = max(1, min(int(max_tail), HARD_MAX_TAIL))
    cleaned = [clean_text(line, 1_000) for line in lines[-max_tail:]]
    window = range(len(cleaned))
    for recognizer in _RECOGNIZERS:
        found = recognizer(cleaned, window)
        if found is not None:
            return found
    return None


__all__ = [
    "MAX_SUMMARY_LINE_CHARS", "RunSummary", "STATUS_FAILED", "STATUS_PASSED",
    "STATUS_UNKNOWN", "find_summary", "parse_pytest_summary",
]
