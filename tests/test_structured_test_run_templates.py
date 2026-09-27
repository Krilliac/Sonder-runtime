"""Host-owned runner templates: golden argv for every runner."""
from __future__ import annotations

from dataclasses import replace

import pytest

from sonder_runtime.domain.common.errors import InvalidInput
from sonder_runtime.domain.testing.runners import (
    RUNNER_TEMPLATES,
    WORKERS_UNSUPPORTED,
    ReportFormat,
    TestRunner,
    batch_safe,
    build_argv,
    clamp_timeout,
    command_digest,
    ctest_template,
    js_template,
    without_report,
)
from sonder_runtime.domain.testing.selectors import SelectorRejected, parse_selector

pytestmark = pytest.mark.unit

R = "/state/test-runs/abc/junit.xml"
D = "/state/test-runs/abc"
PH = {"report": R, "report_dir": D, "build_dir": "build", "project_file": "App.Tests.csproj"}

GOLDEN = {
    TestRunner.PYTEST: ["py", "-m", "pytest", "-q", "-rfE", "--color=no", "-p", "no:cacheprovider",
                        "-o", "junit_family=xunit2", "--junitxml=" + R],
    TestRunner.UNITTEST: ["py", "-m", "unittest", "-v"],
    TestRunner.CTEST: ["ctest", "--test-dir", "build", "--output-on-failure", "--no-tests=error",
                       "--output-junit", R],
    TestRunner.CARGO: ["cargo", "test", "--color", "never", "--no-fail-fast"],
    TestRunner.GO: ["go", "test", "-json", "./..."],
    TestRunner.DOTNET: ["dotnet", "test", "App.Tests.csproj", "--nologo", "--logger",
                        "trx;LogFileName=results.trx", "--results-directory", D],
    TestRunner.NPM: ["npm", "test", "--"],
    TestRunner.PNPM: ["pnpm", "test", "--"],
    TestRunner.YARN: ["yarn", "test"],
    TestRunner.GRADLE: ["gradle", "test", "--console=plain", "--no-daemon"],
    TestRunner.MAVEN: ["mvn", "-B", "test", "-Dstyle.color=never"],
    TestRunner.MAKE: ["make", "test"],
}
EXE = {TestRunner.PYTEST: "py", TestRunner.UNITTEST: "py"}

WITH_SELECTOR = {
    TestRunner.PYTEST: ("k:fast", ["-k", "fast"]),
    TestRunner.UNITTEST: ("tests.test_a", ["tests.test_a"]),
    TestRunner.CTEST: ("fail", ["-R", "^fail$"]),
    TestRunner.CARGO: ("it_works", ["it_works"]),
    TestRunner.DOTNET: ("A.B", ["--filter", "FullyQualifiedName~A.B"]),
    TestRunner.NPM: ("src", ["src"]),
    TestRunner.PNPM: ("src", ["src"]),
    TestRunner.YARN: ("src", ["src"]),
    TestRunner.GRADLE: ("Foo*", ["--tests", "Foo*"]),
    TestRunner.MAVEN: ("Foo", ["-Dtest=Foo", "-Dsurefire.failIfNoSpecifiedTests=false"]),
}


def _argv(runner, selector=None, workers=None, template=None):
    template = template or RUNNER_TEMPLATES[runner]
    return list(build_argv(template, executable=EXE.get(runner, template.tool),
                           selector=selector, workers=workers, placeholders=PH))


@pytest.mark.parametrize("runner", list(TestRunner))
def test_every_runner_has_a_golden_argv(runner):
    assert _argv(runner) == GOLDEN[runner]


@pytest.mark.parametrize("runner", sorted(WITH_SELECTOR, key=lambda r: r.value))
def test_a_selector_is_appended_last(runner):
    raw, extra = WITH_SELECTOR[runner]
    assert _argv(runner, parse_selector(runner, raw)) == GOLDEN[runner] + extra


def test_go_keeps_the_package_last():
    assert _argv(TestRunner.GO, parse_selector(TestRunner.GO, "run:TestX")) == [
        "go", "test", "-json", "-run", "^TestX$", "./..."]
    assert _argv(TestRunner.GO, parse_selector(TestRunner.GO, "./internal/...")) == [
        "go", "test", "-json", "./internal/..."]
    assert _argv(TestRunner.GO, workers=3) == ["go", "test", "-json", "-p", "3", "./..."]


def test_workers_are_admitted_only_where_the_runner_takes_them():
    assert _argv(TestRunner.PYTEST, workers=2)[-4:] == ["-n", "2", "--dist", "load"]
    assert _argv(TestRunner.CTEST, workers=4)[-2:] == ["-j", "4"]
    for runner in (TestRunner.CARGO, TestRunner.MAKE, TestRunner.NPM, TestRunner.MAVEN):
        with pytest.raises(InvalidInput) as caught:
            _argv(runner, workers=2)
        assert caught.value.code == WORKERS_UNSUPPORTED
    with pytest.raises(InvalidInput):
        _argv(TestRunner.PYTEST, workers=9)
    with pytest.raises(InvalidInput):
        _argv(TestRunner.PYTEST, workers=True)


def test_a_selector_kind_the_runner_does_not_take_is_refused():
    with pytest.raises(SelectorRejected):
        _argv(TestRunner.CARGO, parse_selector(TestRunner.PYTEST, "k:x"))


def test_unknown_placeholders_are_refused():
    bogus = replace(RUNNER_TEMPLATES[TestRunner.MAKE], argv_tail=("test", "{home}"))
    with pytest.raises(InvalidInput, match="unknown argv placeholder"):
        build_argv(bogus, executable="make", selector=None, workers=None, placeholders=PH)
    with pytest.raises(InvalidInput, match="unknown argv placeholder"):
        build_argv(RUNNER_TEMPLATES[TestRunner.MAKE], executable="make", selector=None,
                   workers=None, placeholders={**PH, "home": "/root"})
    # control: the declared placeholders are substituted
    assert "build" in build_argv(RUNNER_TEMPLATES[TestRunner.CTEST], executable="ctest",
                                 selector=None, workers=None, placeholders=PH)


def test_a_missing_placeholder_value_is_refused():
    with pytest.raises(InvalidInput, match="has no value"):
        build_argv(RUNNER_TEMPLATES[TestRunner.CTEST], executable="ctest", selector=None,
                   workers=None, placeholders={"report": R})


def test_the_command_digest_is_stable_and_independent_of_the_report_path():
    first = build_argv(RUNNER_TEMPLATES[TestRunner.PYTEST], executable="py", selector=None,
                       workers=None, placeholders=PH)
    other = {**PH, "report": "/state/test-runs/zzz/junit.xml", "report_dir": "/state/test-runs/zzz"}
    second = build_argv(RUNNER_TEMPLATES[TestRunner.PYTEST], executable="py", selector=None,
                        workers=None, placeholders=other)
    assert first != second
    assert command_digest(first, "[WORKSPACE]/p", "pytest", PH) == \
        command_digest(second, "[WORKSPACE]/p", "pytest", other)
    # A different selector, project or runner is a different command.
    third = first + ("-k", "x")
    assert command_digest(third, "[WORKSPACE]/p", "pytest", PH) != command_digest(
        first, "[WORKSPACE]/p", "pytest", PH)
    assert command_digest(first, "[WORKSPACE]/q", "pytest", PH) != command_digest(
        first, "[WORKSPACE]/p", "pytest", PH)


def test_ctest_drops_junit_output_below_cmake_3_21():
    assert ctest_template((3, 28, 3)).report_format is ReportFormat.JUNIT_XML
    old = ctest_template((3, 20, 0))
    assert old.report_format is ReportFormat.TEXT_DIGEST
    argv = build_argv(old, executable="ctest", selector=None, workers=None,
                      placeholders={"build_dir": "build"})
    assert "--output-junit" not in argv
    assert ctest_template(None).report_format is ReportFormat.TEXT_DIGEST


def test_js_reporters_follow_the_test_script_tool():
    jest = js_template(TestRunner.NPM, "jest")
    assert jest.report_format is ReportFormat.JEST_JSON
    assert _argv(TestRunner.NPM, template=jest) == ["npm", "test", "--", "--json",
                                                     "--outputFile=" + R]
    vitest = js_template(TestRunner.YARN, "vitest")
    assert vitest.report_format is ReportFormat.JUNIT_XML
    assert _argv(TestRunner.YARN, template=vitest) == [
        "yarn", "test", "--reporter=default", "--reporter=junit", "--outputFile=" + R]
    assert js_template(TestRunner.PNPM, "").report_format is ReportFormat.TEXT_DIGEST


def test_a_batch_launcher_that_cannot_take_the_report_path_falls_back_to_the_digest():
    windows_report = r"C:\Users\A B\AppData\sonder\test-runs\x\jest.json"
    template = js_template(TestRunner.NPM, "jest")
    argv = build_argv(template, executable=r"C:\nodejs\npm.cmd", selector=None, workers=None,
                      placeholders={"report": windows_report, "report_dir": "x"})
    assert not batch_safe(argv[1:])  # the space breaks batch quoting
    fallback = without_report(template)
    assert fallback.report_format is ReportFormat.TEXT_DIGEST
    argv = build_argv(fallback, executable=r"C:\nodejs\npm.cmd", selector=None, workers=None,
                      placeholders={})
    assert batch_safe(argv[1:])
    # control: a safe report path is kept
    safe = build_argv(template, executable=r"C:\nodejs\npm.cmd", selector=None, workers=None,
                      placeholders={"report": r"C:\state\x\jest.json", "report_dir": "x"})
    assert batch_safe(safe[1:])


def test_timeouts_are_clamped_to_the_template_bounds():
    template = RUNNER_TEMPLATES[TestRunner.PYTEST]
    assert clamp_timeout(template, None) == 600
    assert clamp_timeout(template, 1) == 10
    assert clamp_timeout(template, 99_999) == 1800
    assert RUNNER_TEMPLATES[TestRunner.CARGO].default_timeout_seconds == 900
    assert RUNNER_TEMPLATES[TestRunner.MAVEN].default_timeout_seconds == 1200
    assert RUNNER_TEMPLATES[TestRunner.GRADLE].max_descendants == 128
    with pytest.raises(InvalidInput):
        clamp_timeout(template, "60")
