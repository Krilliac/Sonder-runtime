"""Host-owned argv templates for every supported test runner.

The argv a structured test run launches is ``<executable> <argv_tail>
<report_args> <workers> <selector>`` (go keeps its package last). Every
element is a constant here except the resolved executable, placeholder values
the host computes (``{report}``, ``{report_dir}``, ``{build_dir}``,
``{project_file}``), a bounded worker count and the argv of a selector that
already passed ``selectors.parse_selector``. A model never supplies argv.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from enum import Enum
from typing import Mapping, Sequence

from ..common.errors import InvalidInput
from .selectors import (
    RUNNER_SELECTOR_KINDS,
    SELECTOR_UNSUPPORTED,
    SelectorKind,
    SelectorRejected,
    TestSelector,
)

WORKERS_UNSUPPORTED = "WORKERS_UNSUPPORTED"
MAX_WORKERS = 8
MIN_TIMEOUT_SECONDS = 10
MAX_TIMEOUT_SECONDS = 1800
PLACEHOLDERS = frozenset({"report", "report_dir", "build_dir", "project_file"})
_PLACEHOLDER = re.compile(r"\{([A-Za-z_]+)\}")
# Placeholders whose value is a host path that varies per run; the command
# digest names them instead of their value so it is stable across runs.
_REPORT_PLACEHOLDERS = ("report", "report_dir")
# CMake gained ``ctest --output-junit`` in 3.21.
CTEST_JUNIT_MIN_VERSION = (3, 21)
# Elements a Windows batch launcher (npm.cmd, gradle.bat, ...) may receive.
BATCH_SAFE_ARGUMENT = re.compile(r"^[A-Za-z0-9_./:=,+@\\-]+$")


class TestRunner(str, Enum):
    __test__ = False  # not a pytest test class

    PYTEST = "pytest"
    UNITTEST = "unittest"
    CTEST = "ctest"
    CARGO = "cargo"
    GO = "go"
    DOTNET = "dotnet"
    NPM = "npm"
    PNPM = "pnpm"
    YARN = "yarn"
    GRADLE = "gradle"
    MAVEN = "maven"
    MAKE = "make"


RUNNER_ORDER: tuple[TestRunner, ...] = tuple(TestRunner)


class ReportFormat(str, Enum):
    JUNIT_XML = "junit_xml"
    GO_JSON = "go_json"
    TRX = "trx"
    JEST_JSON = "jest_json"
    LIBTEST_TEXT = "libtest_text"
    UNITTEST_TEXT = "unittest_text"
    TEXT_DIGEST = "text_digest"


# Formats read from a report file the runner writes into the report dir (or,
# for gradle/maven, the project's own report tree).
FILE_REPORT_FORMATS = frozenset({ReportFormat.JUNIT_XML, ReportFormat.TRX, ReportFormat.JEST_JSON})


@dataclass(frozen=True, slots=True)
class RunnerTemplate:
    runner: TestRunner
    tool: str
    argv_tail: tuple[str, ...]
    selector_kinds: frozenset[SelectorKind]
    report_format: ReportFormat
    report_name: str
    workers_flag: tuple[str, ...] | None
    default_timeout_seconds: int
    max_timeout_seconds: int = MAX_TIMEOUT_SECONDS
    max_descendants: int = 64
    # Reporter switches, kept apart so a runner that cannot receive them (a
    # batch launcher refusing the report path) can drop them and fall back to
    # the text digest without touching the rest of the command.
    report_args: tuple[str, ...] = ()
    # Where the runner writes its reports when it does not take a report path
    # (gradle/maven): a glob relative to the run directory.
    report_glob: str = ""


def _template(runner, tool, tail, fmt, name, workers, timeout, *, descendants=64,
              report_args=(), report_glob=""):
    return RunnerTemplate(
        runner=runner, tool=tool, argv_tail=tuple(tail),
        selector_kinds=RUNNER_SELECTOR_KINDS[runner.value], report_format=fmt,
        report_name=name, workers_flag=workers, default_timeout_seconds=timeout,
        max_descendants=descendants, report_args=tuple(report_args),
        report_glob=report_glob,
    )


RUNNER_TEMPLATES: Mapping[TestRunner, RunnerTemplate] = {
    TestRunner.PYTEST: _template(
        TestRunner.PYTEST, "python",
        ("-m", "pytest", "-q", "-rfE", "--color=no", "-p", "no:cacheprovider",
         "-o", "junit_family=xunit2"),
        ReportFormat.JUNIT_XML, "junit.xml", ("-n", "{workers}", "--dist", "load"), 600,
        report_args=("--junitxml={report}",),
    ),
    TestRunner.UNITTEST: _template(
        TestRunner.UNITTEST, "python", ("-m", "unittest", "-v"),
        ReportFormat.UNITTEST_TEXT, "", None, 600,
    ),
    TestRunner.CTEST: _template(
        TestRunner.CTEST, "ctest",
        ("--test-dir", "{build_dir}", "--output-on-failure", "--no-tests=error"),
        ReportFormat.JUNIT_XML, "ctest-junit.xml", ("-j", "{workers}"), 600,
        report_args=("--output-junit", "{report}"),
    ),
    TestRunner.CARGO: _template(
        TestRunner.CARGO, "cargo", ("test", "--color", "never", "--no-fail-fast"),
        ReportFormat.LIBTEST_TEXT, "", None, 900, descendants=128,
    ),
    TestRunner.GO: _template(
        TestRunner.GO, "go", ("test", "-json"),
        ReportFormat.GO_JSON, "", ("-p", "{workers}"), 600,
    ),
    TestRunner.DOTNET: _template(
        TestRunner.DOTNET, "dotnet", ("test", "{project_file}", "--nologo"),
        ReportFormat.TRX, "results.trx", None, 900,
        report_args=("--logger", "trx;LogFileName=results.trx", "--results-directory", "{report_dir}"),
    ),
    TestRunner.NPM: _template(
        TestRunner.NPM, "npm", ("test", "--"), ReportFormat.TEXT_DIGEST, "", None, 600,
    ),
    TestRunner.PNPM: _template(
        TestRunner.PNPM, "pnpm", ("test", "--"), ReportFormat.TEXT_DIGEST, "", None, 600,
    ),
    TestRunner.YARN: _template(
        TestRunner.YARN, "yarn", ("test",), ReportFormat.TEXT_DIGEST, "", None, 600,
    ),
    TestRunner.GRADLE: _template(
        TestRunner.GRADLE, "gradle", ("test", "--console=plain", "--no-daemon"),
        ReportFormat.JUNIT_XML, "", None, 1200, descendants=128,
        report_glob="build/test-results/test/*.xml",
    ),
    TestRunner.MAVEN: _template(
        TestRunner.MAVEN, "mvn", ("-B", "test", "-Dstyle.color=never"),
        ReportFormat.JUNIT_XML, "", None, 1200, descendants=128,
        report_glob="target/surefire-reports/TEST-*.xml",
    ),
    TestRunner.MAKE: _template(
        TestRunner.MAKE, "make", ("test",), ReportFormat.TEXT_DIGEST, "", None, 600,
    ),
}


def runner_from_name(name: str) -> TestRunner:
    try:
        return TestRunner(str(name))
    except ValueError:
        raise InvalidInput("unknown test runner %r" % (str(name)[:32],)) from None


def js_template(runner: TestRunner, script_tool: str) -> RunnerTemplate:
    """The npm/pnpm/yarn template for a test script whose first token is known.

    ``jest`` writes its JSON report, ``vitest`` its JUnit report; any other
    script gets no reporter switches and is summarised from its output.
    """
    base = RUNNER_TEMPLATES[runner]
    if runner not in {TestRunner.NPM, TestRunner.PNPM, TestRunner.YARN}:
        raise InvalidInput("js_template applies to npm, pnpm and yarn only")
    if script_tool == "jest":
        return replace(base, report_format=ReportFormat.JEST_JSON, report_name="jest.json",
                       report_args=("--json", "--outputFile={report}"))
    if script_tool == "vitest":
        return replace(base, report_format=ReportFormat.JUNIT_XML, report_name="vitest-junit.xml",
                       report_args=("--reporter=default", "--reporter=junit",
                                    "--outputFile={report}"))
    return base


def ctest_template(cmake_version: tuple[int, ...] | None) -> RunnerTemplate:
    """The ctest template; CMake older than 3.21 has no JUnit output."""
    base = RUNNER_TEMPLATES[TestRunner.CTEST]
    if cmake_version is not None and tuple(cmake_version[:2]) >= CTEST_JUNIT_MIN_VERSION:
        return base
    return without_report(base)


def without_report(template: RunnerTemplate) -> RunnerTemplate:
    """The same command with its reporter switches dropped (text digest only)."""
    return replace(template, report_format=ReportFormat.TEXT_DIGEST, report_name="",
                   report_args=())


def parse_version_tuple(text: str) -> tuple[int, ...] | None:
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", str(text or ""))
    if not match:
        return None
    return tuple(int(part) for part in match.groups() if part is not None)


def batch_safe(argv: Sequence[str]) -> bool:
    return all(BATCH_SAFE_ARGUMENT.fullmatch(item or "") for item in argv)


def _substitute(item: str, placeholders: Mapping[str, str], workers: int | None) -> str:
    def value(match: re.Match) -> str:
        key = match.group(1)
        if key == "workers":
            if workers is None:
                raise InvalidInput("workers placeholder without a worker count")
            return str(workers)
        if key not in PLACEHOLDERS:
            raise InvalidInput("unknown argv placeholder {%s}" % key)
        if key not in placeholders:
            raise InvalidInput("argv placeholder {%s} has no value" % key)
        replacement = str(placeholders[key])
        if not replacement or "\x00" in replacement:
            raise InvalidInput("argv placeholder {%s} is empty" % key)
        return replacement

    return _PLACEHOLDER.sub(value, item)


def _workers_error(message: str) -> InvalidInput:
    error = InvalidInput(message)
    error.code = WORKERS_UNSUPPORTED
    return error


def build_argv(template: RunnerTemplate, *, executable: str, selector: TestSelector | None,
               workers: int | None, placeholders: Mapping[str, str]) -> tuple[str, ...]:
    """Assemble the exact argv for ``template``. Pure; raises ``InvalidInput``."""
    if not isinstance(executable, str) or not executable or "\x00" in executable:
        raise InvalidInput("executable must be a non-empty path")
    unknown = set(placeholders) - PLACEHOLDERS
    if unknown:
        raise InvalidInput("unknown argv placeholder {%s}" % sorted(unknown)[0])
    if selector is not None and selector.kind not in template.selector_kinds:
        raise SelectorRejected(SELECTOR_UNSUPPORTED,
                               "the %s runner does not take a %s selector"
                               % (template.runner.value, selector.kind.value))
    workers_args: tuple[str, ...] = ()
    if workers is not None:
        if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= MAX_WORKERS:
            raise _workers_error("workers must be an integer within 1..%d" % MAX_WORKERS)
        if template.workers_flag is None:
            raise _workers_error("the %s runner does not take a worker count" % template.runner.value)
        workers_args = tuple(_substitute(item, placeholders, workers) for item in template.workers_flag)
    argv = [executable]
    argv.extend(_substitute(item, placeholders, workers) for item in template.argv_tail)
    argv.extend(_substitute(item, placeholders, workers) for item in template.report_args)
    argv.extend(workers_args)
    if template.runner is TestRunner.GO:
        package = "./..."
        if selector is not None and selector.kind is SelectorKind.GO_PACKAGE:
            package = selector.value
        elif selector is not None:
            argv.extend(selector.argv)
        argv.append(package)
    elif selector is not None:
        argv.extend(selector.argv)
    if any(not item for item in argv):
        raise InvalidInput("argv contains an empty element")
    return tuple(argv)


def command_digest(argv: Sequence[str], cwd_label: str, runner: str,
                   placeholders: Mapping[str, str] | None = None) -> str:
    """sha256 over the canonical command with per-run report paths named.

    The report file and report directory differ on every run; they are
    replaced by their placeholder names, so the digest identifies the command
    an operator approves, not the run.
    """
    values = placeholders or {}
    substitutions = sorted(
        ((values[key], "{%s}" % key) for key in _REPORT_PLACEHOLDERS if values.get(key)),
        key=lambda pair: -len(pair[0]),
    )
    canonical = []
    for item in argv:
        text = str(item)
        for value, name in substitutions:
            text = text.replace(value, name)
        canonical.append(text)
    body = json.dumps({"argv": canonical, "cwd": str(cwd_label), "runner": str(runner)},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def clamp_timeout(template: RunnerTemplate, requested: int | None) -> int:
    if requested is None:
        return template.default_timeout_seconds
    if isinstance(requested, bool) or not isinstance(requested, int):
        raise InvalidInput("timeout_seconds must be an integer")
    return max(MIN_TIMEOUT_SECONDS, min(template.max_timeout_seconds, requested))


__all__ = [
    "BATCH_SAFE_ARGUMENT", "CTEST_JUNIT_MIN_VERSION", "FILE_REPORT_FORMATS", "MAX_WORKERS",
    "PLACEHOLDERS", "RUNNER_ORDER", "RUNNER_TEMPLATES", "ReportFormat", "RunnerTemplate",
    "TestRunner", "WORKERS_UNSUPPORTED", "batch_safe", "build_argv", "clamp_timeout",
    "command_digest", "ctest_template", "js_template", "parse_version_tuple",
    "runner_from_name", "without_report",
]
