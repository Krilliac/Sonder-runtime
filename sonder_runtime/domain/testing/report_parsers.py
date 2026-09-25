"""Parse machine-readable test reports into typed totals and failures.

Every input is treated as hostile data written by project code: XML documents
carrying a DOCTYPE or ENTITY declaration are refused before ``xml.etree`` sees
them (no entity expansion, no external fetch), documents are size-capped,
case counts are capped, and every string is cut to a single bounded line.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Iterable, Sequence

from ..common.errors import InvalidInput
from .report import MAX_FAILURES, TestFailure, TestTotals

MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_TESTCASES = 20_000
MAX_JSON_LINES = 200_000
MAX_TEXT_CHARS = 16 * 1024 * 1024
_PY_LOCATION = re.compile(r"^(?P<file>[^\s:][^:\n]*?\.py):(?P<line>\d+): ", re.MULTILINE)
_GENERIC_LOCATION = re.compile(
    r"(?P<file>[A-Za-z0-9_./\\-]+\.[A-Za-z0-9]{1,8}):(?:line )?(?P<line>\d+)")
_GO_OUTPUT_LOCATION = re.compile(r"^\s+(?P<file>[^\s:]+\.go):(?P<line>\d+): ?(?P<msg>.*)$")
_JVM_LOCATION = re.compile(r"\((?P<file>[A-Za-z0-9_$]+\.(?:java|kt|groovy|scala)):(?P<line>\d+)\)")
_TRX_LOCATION =re.compile(r"in (?P<file>[^\n]+?):line (?P<line>\d+)")


@dataclass(frozen=True, slots=True)
class ParsedResults:
    totals: TestTotals
    failures: tuple[TestFailure, ...] = ()
    truncated: bool = False


EMPTY = ParsedResults(TestTotals())


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _guard_xml(data: bytes) -> ET.Element:
    """The shared hostile-XML guard (``domain.common.safe_xml``) at this module's size cap.

    Size cap, a byte-level DOCTYPE/ENTITY pre-check, an expat pass refusing
    every declaration in any encoding, and only then ``xml.etree``. Imported
    at first use so this module (and the test-run tools) import on their own.
    """
    from ..common.safe_xml import parse_guarded_xml

    return parse_guarded_xml(data, max_bytes=MAX_DOCUMENT_BYTES)


def _relative(path: str, strip_prefix: str) -> str:
    value = str(path or "").replace("\\", "/")
    prefix = str(strip_prefix or "").replace("\\", "/").rstrip("/")
    if prefix and (value == prefix or value.startswith(prefix + "/")):
        value = value[len(prefix):].lstrip("/")
    return value


def _int(value, default=None):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return number if number >= 0 else default


def _first_line(text: str) -> str:
    for line in str(text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _pytest_location(text: str, module_hint: str) -> tuple[str, int | None]:
    """The last ``path.py:N:`` in a pytest longrepr, preferring the test's module."""
    matches = list(_PY_LOCATION.finditer(text or ""))
    if not matches:
        return "", None
    if module_hint:
        wanted = module_hint.replace(".", "/") + ".py"
        for match in reversed(matches):
            if match.group("file").replace("\\", "/").endswith(wanted):
                return match.group("file").replace("\\", "/"), _int(match.group("line"))
    last = matches[-1]
    return last.group("file").replace("\\", "/"), _int(last.group("line"))


def _case_identity(case: ET.Element, detail_text: str, strip_prefix: str) -> tuple[str, str, int | None]:
    name = case.get("name", "") or ""
    classname = case.get("classname", "") or ""
    file_attr = _relative(case.get("file", "") or "", strip_prefix)
    line_attr = _int(case.get("line"))
    if file_attr:
        suffix = ""
        module = file_attr[:-3].replace("/", ".") if file_attr.endswith(".py") else ""
        if module and classname.startswith(module + "."):
            suffix = classname[len(module) + 1:].replace(".", "::") + "::"
        file, line = file_attr, line_attr
        if detail_text:
            found, found_line = _pytest_location(detail_text, module)
            if found and found_line is not None and found.endswith(file_attr):
                line = found_line
        return "%s::%s%s" % (file_attr, suffix, name), file, line
    if detail_text:
        # pytest xunit2 carries no file attribute; its longrepr names the file.
        parts = classname.split(".") if classname else []
        for cut in range(len(parts), 0, -1):
            module = ".".join(parts[:cut])
            found, found_line = _pytest_location(detail_text, module)
            if found and found.replace("\\", "/").endswith(module.replace(".", "/") + ".py"):
                file = _relative(found, strip_prefix)
                rest = "::".join(parts[cut:])
                return "%s::%s%s" % (file, rest + "::" if rest else "", name), file, found_line
    if not classname or classname == name:
        location = _GENERIC_LOCATION.search(detail_text or "")
        if location:
            return name, _relative(location.group("file"), strip_prefix), _int(location.group("line"))
        return name, "", None
    if "/" in classname or re.search(r"\.(?:[jt]sx?|mjs|cjs)$", classname):
        return "%s::%s" % (_relative(classname, strip_prefix), name), _relative(classname, strip_prefix), None
    frames = list(_JVM_LOCATION.finditer(detail_text or ""))
    owner = classname.rsplit(".", 1)[-1].split("$", 1)[0]
    jvm = next((item for item in frames if item.group("file").split(".", 1)[0] == owner),
               frames[0] if frames else None)
    if jvm:
        return "%s.%s" % (classname, name), jvm.group("file"), _int(jvm.group("line"))
    return "%s.%s" % (classname, name), "", None


def parse_junit_xml(data: bytes, *, project_root_label: str = "") -> ParsedResults:
    """Totals and failures from a JUnit document (testsuites or testsuite root)."""
    root = _guard_xml(data)
    if _local(root.tag) not in {"testsuites", "testsuite", "testcase"}:
        raise InvalidInput("not a JUnit document")
    passed = failed = skipped = errors = count = 0
    failures: list[TestFailure] = []
    truncated = False
    for case in root.iter():
        if _local(case.tag) != "testcase":
            continue
        if count >= MAX_TESTCASES:
            truncated = True
            break
        count += 1
        outcome = "passed"
        detail = None
        for child in case:
            tag = _local(child.tag)
            if tag in {"failure", "error"}:
                outcome, detail = tag, child
                break
            if tag == "skipped":
                outcome = "skipped"
        if outcome == "passed" and (case.get("status") or "").lower() in {"disabled", "notrun"}:
            outcome = "skipped"
        if outcome == "passed":
            passed += 1
            continue
        if outcome == "skipped":
            skipped += 1
            continue
        if outcome == "failure":
            failed += 1
        else:
            errors += 1
        if len(failures) >= MAX_FAILURES:
            truncated = True
            continue
        text = (detail.text or "") if detail is not None else ""
        message = (detail.get("message") if detail is not None else "") or _first_line(text)
        if not message:
            for child in case:
                if _local(child.tag) == "system-out" and child.text:
                    message = _first_line(child.text)
                    break
        identity, file, line = _case_identity(case, text, project_root_label)
        failures.append(TestFailure.bounded(identity, file, line,
                                            "error" if outcome == "error" else "failure", message))
    totals = TestTotals(passed, failed, skipped, errors, passed + failed + skipped + errors)
    return ParsedResults(totals, tuple(failures), truncated)


def merge_parsed(results: Sequence[ParsedResults]) -> ParsedResults:
    passed = failed = skipped = errors = 0
    failures: list[TestFailure] = []
    truncated = False
    for item in results:
        passed += item.totals.passed
        failed += item.totals.failed
        skipped += item.totals.skipped
        errors += item.totals.errors
        truncated = truncated or item.truncated
        for failure in item.failures:
            if len(failures) >= MAX_FAILURES:
                truncated = True
                break
            failures.append(failure)
    return ParsedResults(TestTotals(passed, failed, skipped, errors,
                                    passed + failed + skipped + errors),
                         tuple(failures), truncated)


def parse_go_test_json(text: str) -> ParsedResults:
    """``go test -json`` events: per-test pass/fail/skip, package failures as errors."""
    if len(text) > MAX_TEXT_CHARS:
        text = text[-MAX_TEXT_CHARS:]
    passed = failed = skipped = errors = 0
    truncated = False
    outputs: dict[tuple[str, str], list[str]] = {}
    failing: list[tuple[str, str]] = []
    package_failed: dict[str, bool] = {}
    package_has_test_failure: dict[str, bool] = {}
    package_output: dict[str, list[str]] = {}
    for index, line in enumerate(text.splitlines()):
        if index >= MAX_JSON_LINES:
            truncated = True
            break
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue  # malformed or pathologically nested: not an event
        if not isinstance(event, dict):
            continue
        action = event.get("Action")
        # Build events carry ImportPath ("pkg [pkg.test]") instead of Package.
        package = str(event.get("Package") or str(event.get("ImportPath") or "").split(" [", 1)[0])
        test = event.get("Test")
        if action == "output" or action == "build-output":
            chunk = str(event.get("Output") or "")
            if test:
                bucket = outputs.setdefault((package, str(test)), [])
            else:
                bucket = package_output.setdefault(package, [])
            if len(bucket) < 64:
                bucket.append(chunk.rstrip("\n"))
            continue
        if action == "build-fail":
            package_failed[package] = True
            continue
        if test:
            if action == "pass":
                passed += 1
            elif action == "skip":
                skipped += 1
            elif action == "fail":
                failed += 1
                package_has_test_failure[package] = True
                failing.append((package, str(test)))
        elif action == "fail":
            package_failed[package] = True
    failures: list[TestFailure] = []
    for package, test in failing:
        if len(failures) >= MAX_FAILURES:
            truncated = True
            break
        # A parent test fails when a subtest fails; report the leaf only.
        if any(other_pkg == package and other.startswith(test + "/") for other_pkg, other in failing):
            continue
        file, line_no, message = "", None, ""
        for chunk in outputs.get((package, test), ()):
            match = _GO_OUTPUT_LOCATION.match(chunk)
            if match:
                file, line_no, message = match.group("file"), _int(match.group("line")), match.group("msg")
                break
        if not message:
            for chunk in outputs.get((package, test), ()):
                stripped = chunk.strip()
                if stripped and not stripped.startswith(("=== RUN", "--- FAIL", "=== PAUSE", "=== CONT")):
                    message = stripped
                    break
        failures.append(TestFailure.bounded("%s.%s" % (package, test) if package else test,
                                            file, line_no, "failure", message))
    for package, did_fail in package_failed.items():
        if not did_fail or package_has_test_failure.get(package):
            continue
        errors += 1
        if len(failures) >= MAX_FAILURES:
            truncated = True
            continue
        message = ""
        file, line_no = "", None
        for chunk in package_output.get(package, ()):
            stripped = chunk.strip()
            location = re.match(r"^(?:\./)?(?P<file>[^\s:]+\.go):(?P<line>\d+):(?:\d+:)?\s*(?P<msg>.*)$",
                                stripped)
            if location:
                file, line_no, message = location.group("file"), _int(location.group("line")), location.group("msg")
                break
            if stripped and not stripped.startswith(("FAIL", "#", "ok ")):
                message = message or stripped
        failures.append(TestFailure.bounded(package, file, line_no, "error",
                                            message or "package failed to build or run"))
    return ParsedResults(TestTotals(passed, failed, skipped, errors,
                                    passed + failed + skipped + errors),
                         tuple(failures), truncated)


def parse_trx(data: bytes) -> ParsedResults:
    """Visual Studio TRX: Counters plus UnitTestResult outcomes and messages."""
    root = _guard_xml(data)
    if _local(root.tag) != "TestRun":
        raise InvalidInput("not a TRX document")
    counters = None
    results = []
    truncated = False
    for element in root.iter():
        tag = _local(element.tag)
        if tag == "Counters" and counters is None:
            counters = element
        elif tag == "UnitTestResult":
            if len(results) >= MAX_TESTCASES:
                truncated = True
                continue
            results.append(element)
    passed = failed = skipped = errors = 0
    for element in results:
        outcome = (element.get("outcome") or "").lower()
        if outcome == "passed":
            passed += 1
        elif outcome == "failed":
            failed += 1
        elif outcome in {"notexecuted", "inconclusive", "skipped"}:
            skipped += 1
        elif outcome in {"error", "timeout", "aborted"}:
            errors += 1
    if counters is not None and not results:
        passed = _int(counters.get("passed"), 0)
        failed = _int(counters.get("failed"), 0)
        errors = _int(counters.get("error"), 0) + _int(counters.get("timeout"), 0)
        skipped = _int(counters.get("notExecuted"), 0)
    failures: list[TestFailure] = []
    for element in results:
        outcome = (element.get("outcome") or "").lower()
        if outcome not in {"failed", "error", "timeout", "aborted"}:
            continue
        if len(failures) >= MAX_FAILURES:
            truncated = True
            break
        message = stack = ""
        for child in element.iter():
            tag = _local(child.tag)
            if tag == "Message" and not message:
                message = child.text or ""
            elif tag == "StackTrace" and not stack:
                stack = child.text or ""
        location = _TRX_LOCATION.search(stack)
        failures.append(TestFailure.bounded(
            element.get("testName", ""),
            location.group("file") if location else "",
            _int(location.group("line")) if location else None,
            "failure" if outcome == "failed" else "error",
            _first_line(message),
        ))
    return ParsedResults(TestTotals(passed, failed, skipped, errors,
                                    passed + failed + skipped + errors),
                         tuple(failures), truncated)


def parse_jest_json(data: bytes, *, strip_prefix: str = "") -> ParsedResults:
    """Jest ``--json`` output: aggregate counters and failed assertion results."""
    if not isinstance(data, (bytes, bytearray)):
        raise InvalidInput("report must be bytes")
    if len(data) > MAX_DOCUMENT_BYTES:
        raise InvalidInput("report exceeds %d bytes" % MAX_DOCUMENT_BYTES)
    try:
        body = json.loads(bytes(data).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise InvalidInput("jest report is not JSON: %s" % type(exc).__name__) from None
    if not isinstance(body, dict):
        raise InvalidInput("jest report is not an object")
    passed = _int(body.get("numPassedTests"), 0)
    failed = _int(body.get("numFailedTests"), 0)
    skipped = _int(body.get("numPendingTests"), 0) + _int(body.get("numTodoTests"), 0)
    errors = _int(body.get("numRuntimeErrorTestSuites"), 0)
    failures: list[TestFailure] = []
    truncated = False
    suites = body.get("testResults") if isinstance(body.get("testResults"), list) else []
    seen = 0
    for suite in suites:
        if not isinstance(suite, dict):
            continue
        file = _relative(str(suite.get("name") or suite.get("testFilePath") or ""), strip_prefix)
        assertions = suite.get("assertionResults") if isinstance(suite.get("assertionResults"), list) else []
        if not assertions and suite.get("status") == "failed" and suite.get("message"):
            if len(failures) < MAX_FAILURES:
                failures.append(TestFailure.bounded(file, file, None, "error",
                                                    _first_line(suite.get("message"))))
            else:
                truncated = True
        for assertion in assertions:
            seen += 1
            if seen > MAX_TESTCASES:
                truncated = True
                break
            if not isinstance(assertion, dict) or assertion.get("status") != "failed":
                continue
            if len(failures) >= MAX_FAILURES:
                truncated = True
                break
            location = assertion.get("location") if isinstance(assertion.get("location"), dict) else {}
            messages = assertion.get("failureMessages") if isinstance(assertion.get("failureMessages"), list) else []
            name = assertion.get("fullName") or assertion.get("title") or ""
            failures.append(TestFailure.bounded(
                "%s::%s" % (file, name) if file else str(name), file,
                _int(location.get("line")), "failure",
                _first_line(messages[0]) if messages else "",
            ))
    return ParsedResults(TestTotals(passed, failed, skipped, errors,
                                    passed + failed + skipped + errors),
                         tuple(failures), truncated)


_LIBTEST_RESULT = re.compile(
    r"^test result: (?:ok|FAILED)\. (?P<passed>\d+) passed; (?P<failed>\d+) failed; "
    r"(?P<ignored>\d+) ignored")
_LIBTEST_BLOCK = re.compile(r"^---- (?P<name>\S+) stdout ----$")
_LIBTEST_PANIC_NEW = re.compile(r"panicked at (?P<file>[^\s:]+):(?P<line>\d+):(?P<col>\d+):$")
_LIBTEST_PANIC_OLD = re.compile(r"panicked at '(?P<msg>.*)', (?P<file>[^\s:]+):(?P<line>\d+):(?P<col>\d+)")


def _lines(text: str) -> Iterable[str]:
    if len(text) > MAX_TEXT_CHARS:
        text = text[-MAX_TEXT_CHARS:]
    return text.splitlines()


def parse_libtest_text(text: str) -> ParsedResults:
    """cargo/libtest text: ``test result:`` lines summed, ``---- x stdout ----`` blocks."""
    passed = failed = skipped = 0
    failures: list[TestFailure] = []
    truncated = False
    lines = list(_lines(text))
    seen_names: set[str] = set()
    for index, line in enumerate(lines):
        stripped = line.strip()
        match = _LIBTEST_RESULT.match(stripped)
        if match:
            passed += int(match.group("passed"))
            failed += int(match.group("failed"))
            skipped += int(match.group("ignored"))
            continue
        block = _LIBTEST_BLOCK.match(stripped)
        if not block or block.group("name") in seen_names:
            continue
        seen_names.add(block.group("name"))
        if len(failures) >= MAX_FAILURES:
            truncated = True
            continue
        file, line_no, message = "", None, ""
        for offset in range(1, 12):
            if index + offset >= len(lines):
                break
            candidate = lines[index + offset].strip()
            if candidate.startswith("---- ") or candidate == "failures:":
                break
            new = _LIBTEST_PANIC_NEW.search(candidate)
            if new:
                file, line_no = new.group("file"), _int(new.group("line"))
                if index + offset + 1 < len(lines):
                    message = lines[index + offset + 1].strip()
                break
            old = _LIBTEST_PANIC_OLD.search(candidate)
            if old:
                file, line_no, message = old.group("file"), _int(old.group("line")), old.group("msg")
                break
        failures.append(TestFailure.bounded(block.group("name"), file, line_no, "failure", message))
    return ParsedResults(TestTotals(passed, failed, skipped, 0, passed + failed + skipped),
                         tuple(failures), truncated)


_UNITTEST_RAN = re.compile(r"^Ran (?P<count>\d+) tests? in ")
_UNITTEST_FAILED = re.compile(r"^FAILED \((?P<body>[^)]*)\)")
_UNITTEST_OK = re.compile(r"^OK(?: \((?P<body>[^)]*)\))?$")
_UNITTEST_HEADER = re.compile(r"^(?P<kind>FAIL|ERROR): (?P<name>\S+) \((?P<where>[^)]+)\)")
_TRACEBACK_FRAME = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+)')


def _unittest_counts(body: str) -> dict[str, int]:
    counts = {}
    for part in (body or "").split(","):
        key, _, value = part.strip().partition("=")
        number = _int(value)
        if key and number is not None:
            counts[key.strip()] = number
    return counts


def parse_unittest_text(text: str, *, strip_prefix: str = "") -> ParsedResults:
    """``python -m unittest -v`` output; parsed as input text only."""
    lines = list(_lines(text))
    ran = None
    counts: dict[str, int] = {}
    for line in reversed(lines):
        stripped = line.strip()
        if ran is None:
            match = _UNITTEST_RAN.match(stripped)
            if match:
                ran = int(match.group("count"))
                break
        failed_match = _UNITTEST_FAILED.match(stripped)
        ok_match = _UNITTEST_OK.match(stripped)
        if not counts and (failed_match or ok_match):
            counts = _unittest_counts((failed_match or ok_match).group("body") or "")
    failures: list[TestFailure] = []
    truncated = False
    for index, line in enumerate(lines):
        header = _UNITTEST_HEADER.match(line.strip())
        if not header:
            continue
        if len(failures) >= MAX_FAILURES:
            truncated = True
            break
        where = header.group("where")
        name = header.group("name")
        identity = where if where.endswith("." + name) else "%s.%s" % (where, name)
        file, line_no, message = "", None, ""
        for offset in range(1, 400):
            if index + offset >= len(lines):
                break
            candidate = lines[index + offset]
            if candidate.startswith(("=" * 20, "-" * 20)) and offset > 2:
                break
            frame = _TRACEBACK_FRAME.match(candidate)
            if frame:
                file, line_no = _relative(frame.group("file"), strip_prefix), _int(frame.group("line"))
            elif candidate.strip() and not candidate.startswith((" ", "-" * 20, "Traceback")):
                message = candidate.strip()
        failures.append(TestFailure.bounded(identity, file, line_no,
                                            "error" if header.group("kind") == "ERROR" else "failure",
                                            message))
    failed = counts.get("failures", 0)
    errors = counts.get("errors", 0)
    skipped = counts.get("skipped", 0) + counts.get("expected failures", 0)
    if ran is None:
        return ParsedResults(TestTotals(0, failed, skipped, errors, failed + skipped + errors),
                             tuple(failures), True)
    passed = max(0, ran - failed - errors - skipped)
    return ParsedResults(TestTotals(passed, failed, skipped, errors, ran), tuple(failures), truncated)


__all__ = [
    "EMPTY", "MAX_DOCUMENT_BYTES", "MAX_TESTCASES", "ParsedResults", "merge_parsed",
    "parse_go_test_json", "parse_jest_json", "parse_junit_xml", "parse_libtest_text",
    "parse_trx", "parse_unittest_text",
]
