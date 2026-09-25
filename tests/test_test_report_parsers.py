"""Report parsers over captured real-runner output (fixtures inline).

The pytest, ctest, cargo, go and unittest fixtures were captured from real
runs on the development host (pytest 9, CMake 3.28, cargo 1.9x, go 1.2x,
CPython 3.12) with temporary paths shortened; the TRX, jest and surefire
fixtures follow those tools' documented shapes.
"""
from __future__ import annotations

import json
import time

import pytest

from sonder_runtime.domain.common.errors import InvalidInput
from sonder_runtime.domain.testing import report_parsers as rp
from sonder_runtime.domain.testing.report_parsers import (
    merge_parsed,
    parse_go_test_json,
    parse_jest_json,
    parse_junit_xml,
    parse_libtest_text,
    parse_trx,
    parse_unittest_text,
)

pytestmark = pytest.mark.unit

PYTEST_XUNIT2 = b"""<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests"><testsuite name="pytest" errors="1" failures="2" skipped="1" tests="6" time="0.038"><testcase classname="test_mod" name="test_ok1" time="0.001" /><testcase classname="test_mod" name="test_ok2" time="0.000" /><testcase classname="test_mod" name="test_bad" time="0.001"><failure message="assert 1 == 2">def test_bad():
&gt;       assert 1 == 2
E       assert 1 == 2

test_mod.py:14: AssertionError</failure></testcase><testcase classname="test_mod" name="test_skip" time="0.000"><skipped type="pytest.skip" message="nope">/tmp/p/test_mod.py:16: nope</skipped></testcase><testcase classname="test_mod" name="test_setup_error" time="0.000"><error message="failed on setup with &quot;RuntimeError: fixture exploded&quot;">@pytest.fixture
    def broken():
&gt;       raise RuntimeError("fixture exploded")
E       RuntimeError: fixture exploded

test_mod.py:5: RuntimeError</error></testcase><testcase classname="tests.test_pkg.TestK" name="test_inner" time="0.000"><failure message="AssertionError: inner failed&#10;assert False">self = &lt;tests.test_pkg.TestK object at 0x7fe881b55cd0&gt;

    def test_inner(self):
&gt;       assert False, "inner failed"
E       AssertionError: inner failed

tests/test_pkg.py:25: AssertionError</failure></testcase></testsuite></testsuites>"""

CTEST_JUNIT = b"""<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="(empty)"
	tests="2" failures="1" disabled="0" skipped="0" hostname="" time="0" timestamp="2026-09-25T10:14:25">
	<testcase name="pass" classname="pass" time="0.00429486" status="run">
		<properties/>
		<system-out>arg 0
</system-out>
	</testcase>
	<testcase name="fail" classname="fail" time="0.0166031" status="fail">
		<failure message="Failed"/>
		<properties/>
		<system-out>arg 1
</system-out>
	</testcase>
</testsuite>
"""

SUREFIRE_A = b"""<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="com.x.FooTest" time="0.01" tests="2" errors="0" skipped="0" failures="1">
  <testcase name="adds" classname="com.x.FooTest" time="0.001"/>
  <testcase name="fails" classname="com.x.FooTest" time="0.002">
    <failure message="expected: &lt;1&gt; but was: &lt;2&gt;" type="org.opentest4j.AssertionFailedError">org.opentest4j.AssertionFailedError: expected: &lt;1&gt; but was: &lt;2&gt;
	at org.junit.jupiter.api.AssertionUtils.fail(AssertionUtils.java:55)
	at com.x.FooTest.fails(FooTest.java:12)
</failure>
  </testcase>
</testsuite>
"""
SUREFIRE_B = b"""<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="com.x.BarTest" tests="2" errors="1" skipped="1" failures="0">
  <testcase name="later" classname="com.x.BarTest"><skipped/></testcase>
  <testcase name="boom" classname="com.x.BarTest">
    <error message="java.lang.IllegalStateException: boom" type="java.lang.IllegalStateException">java.lang.IllegalStateException: boom
	at com.x.BarTest.boom(BarTest.java:30)
</error>
  </testcase>
</testsuite>
"""

GO_JSON = "\n".join([
    '{"ImportPath":"example.com/m/broken [example.com/m/broken.test]","Action":"build-output","Output":"# example.com/m/broken [example.com/m/broken.test]\\n"}',
    '{"ImportPath":"example.com/m/broken [example.com/m/broken.test]","Action":"build-output","Output":"broken/b.go:2:23: cannot use \\"x\\" (untyped string constant) as int value in return statement\\n"}',
    '{"ImportPath":"example.com/m/broken [example.com/m/broken.test]","Action":"build-fail"}',
    '{"Action":"start","Package":"example.com/m"}',
    '{"Action":"start","Package":"example.com/m/broken"}',
    '{"Action":"output","Package":"example.com/m/broken","Output":"FAIL\\texample.com/m/broken [build failed]\\n"}',
    '{"Action":"fail","Package":"example.com/m/broken","Elapsed":0,"FailedBuild":"example.com/m/broken [example.com/m/broken.test]"}',
    '{"Action":"run","Package":"example.com/m","Test":"TestPass"}',
    '{"Action":"output","Package":"example.com/m","Test":"TestPass","Output":"--- PASS: TestPass (0.00s)\\n"}',
    '{"Action":"pass","Package":"example.com/m","Test":"TestPass","Elapsed":0}',
    '{"Action":"run","Package":"example.com/m","Test":"TestFail"}',
    '{"Action":"output","Package":"example.com/m","Test":"TestFail","Output":"=== RUN   TestFail\\n"}',
    '{"Action":"output","Package":"example.com/m","Test":"TestFail","Output":"    m_test.go:4: want 1 got 2\\n"}',
    '{"Action":"output","Package":"example.com/m","Test":"TestFail","Output":"--- FAIL: TestFail (0.00s)\\n"}',
    '{"Action":"fail","Package":"example.com/m","Test":"TestFail","Elapsed":0}',
    '{"Action":"run","Package":"example.com/m","Test":"TestSkip"}',
    '{"Action":"skip","Package":"example.com/m","Test":"TestSkip","Elapsed":0}',
    'not json at all',
    '{"Action":"output","Package":"example.com/m","Output":"FAIL\\texample.com/m\\t0.003s\\n"}',
    '{"Action":"fail","Package":"example.com/m","Elapsed":0.004}',
])

TRX = b"""<?xml version="1.0" encoding="utf-8"?>
<TestRun id="1" name="run" xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010">
  <Results>
    <UnitTestResult testName="Calc.Tests.Adds" outcome="Passed" />
    <UnitTestResult testName="Calc.Tests.Fails" outcome="Failed">
      <Output><ErrorInfo><Message>Assert.Equal() Failure
Expected: 1</Message><StackTrace>   at Calc.Tests.Fails() in /src/Calc.Tests/UnitTest1.cs:line 14</StackTrace></ErrorInfo></Output>
    </UnitTestResult>
    <UnitTestResult testName="Calc.Tests.Later" outcome="NotExecuted" />
  </Results>
  <ResultSummary outcome="Failed"><Counters total="3" executed="2" passed="1" failed="1" error="0" timeout="0" notExecuted="1" /></ResultSummary>
</TestRun>
"""

JEST = json.dumps({
    "numPassedTests": 4, "numFailedTests": 1, "numPendingTests": 1, "numTodoTests": 0,
    "numRuntimeErrorTestSuites": 1, "numTotalTests": 6,
    "testResults": [
        {"name": "/work/app/src/sum.test.js", "status": "failed", "assertionResults": [
            {"fullName": "sum adds", "status": "passed", "failureMessages": []},
            {"fullName": "sum breaks", "status": "failed", "location": {"line": 7, "column": 3},
             "failureMessages": ["Error: expect(received).toBe(expected)\n\nExpected: 3"]},
        ]},
        {"name": "/work/app/src/broken.test.js", "status": "failed", "assertionResults": [],
         "message": "Test suite failed to run\n  SyntaxError: Unexpected token"},
    ],
}).encode()

LIBTEST = """
running 3 tests
test tests::ignored ... ignored
test tests::it_works ... ok
test tests::it_fails ... FAILED

failures:

---- tests::it_fails stdout ----

thread 'tests::it_fails' (31646) panicked at src/lib.rs:8:21:
assertion `left == right` failed
  left: 4
 right: 5

failures:
    tests::it_fails

test result: FAILED. 1 passed; 1 failed; 1 ignored; 0 measured; 0 filtered out; finished in 0.15s

     Running tests/api.rs (target/debug/deps/api-1)

running 2 tests
test api_ok ... ok
test api_old ... FAILED

failures:

---- api_old stdout ----
thread 'api_old' panicked at 'old style message', tests/api.rs:3:5

test result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s

   Doc-tests crate1

running 0 tests

test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s
"""

UNITTEST = """test_a (test_u.T.test_a) ... ok
test_b (test_u.T.test_b) ... FAIL
test_c (test_u.T.test_c) ... ERROR
test_d (test_u.T.test_d) ... skipped 'no'

======================================================================
ERROR: test_c (test_u.T.test_c)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/work/proj/test_u.py", line 8, in test_c
    raise ValueError("boom")
ValueError: boom

======================================================================
FAIL: test_b (test_u.T.test_b)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/work/proj/test_u.py", line 6, in test_b
    self.assertEqual(1, 2)
AssertionError: 1 != 2

----------------------------------------------------------------------
Ran 4 tests in 0.001s

FAILED (failures=1, errors=1, skipped=1)
"""


def _totals(parsed):
    t = parsed.totals
    return (t.passed, t.failed, t.skipped, t.errors, t.total)


def test_pytest_xunit2_yields_node_ids_and_failure_lines():
    parsed = parse_junit_xml(PYTEST_XUNIT2)
    assert _totals(parsed) == (2, 2, 1, 1, 6)
    by_id = {item.id: item for item in parsed.failures}
    assert by_id["test_mod.py::test_bad"].line == 14
    assert by_id["test_mod.py::test_bad"].file == "test_mod.py"
    assert by_id["test_mod.py::test_bad"].message_excerpt == "assert 1 == 2"
    assert by_id["test_mod.py::test_setup_error"].kind == "error"
    assert by_id["test_mod.py::test_setup_error"].line == 5
    inner = by_id["tests/test_pkg.py::TestK::test_inner"]
    assert (inner.file, inner.line) == ("tests/test_pkg.py", 25)
    assert "\n" not in inner.message_excerpt


def test_ctest_junit_ids_are_the_test_names():
    parsed = parse_junit_xml(CTEST_JUNIT)
    assert _totals(parsed) == (1, 1, 0, 0, 2)
    assert [(item.id, item.message_excerpt) for item in parsed.failures] == [("fail", "Failed")]


def test_multi_file_surefire_reports_merge():
    merged = merge_parsed([parse_junit_xml(SUREFIRE_A), parse_junit_xml(SUREFIRE_B)])
    assert _totals(merged) == (1, 1, 1, 1, 4)
    by_id = {item.id: item for item in merged.failures}
    assert (by_id["com.x.FooTest.fails"].file, by_id["com.x.FooTest.fails"].line) == ("FooTest.java", 12)
    assert by_id["com.x.BarTest.boom"].kind == "error"
    assert by_id["com.x.BarTest.boom"].line == 30


def test_go_json_counts_tests_and_reports_a_build_failure_as_an_error():
    parsed = parse_go_test_json(GO_JSON)
    assert _totals(parsed) == (1, 1, 1, 1, 4)
    by_id = {item.id: item for item in parsed.failures}
    fail = by_id["example.com/m.TestFail"]
    assert (fail.file, fail.line, fail.message_excerpt) == ("m_test.go", 4, "want 1 got 2")
    broken = by_id["example.com/m/broken"]
    assert broken.kind == "error" and (broken.file, broken.line) == ("broken/b.go", 2)


def test_trx_outcomes_messages_and_stack_locations():
    parsed = parse_trx(TRX)
    assert _totals(parsed) == (1, 1, 1, 0, 3)
    failure = parsed.failures[0]
    assert failure.id == "Calc.Tests.Fails"
    assert (failure.file, failure.line) == ("/src/Calc.Tests/UnitTest1.cs", 14)
    assert failure.message_excerpt == "Assert.Equal() Failure"


def test_jest_json_counters_and_failed_assertions():
    parsed = parse_jest_json(JEST, strip_prefix="/work/app")
    assert _totals(parsed) == (4, 1, 1, 1, 7)
    ids = {item.id: item for item in parsed.failures}
    assert ids["src/sum.test.js::sum breaks"].line == 7
    assert ids["src/sum.test.js::sum breaks"].message_excerpt.startswith("Error: expect")
    assert ids["src/broken.test.js"].kind == "error"


def test_libtest_text_sums_every_crate_and_reads_both_panic_formats():
    parsed = parse_libtest_text(LIBTEST)
    assert _totals(parsed) == (2, 2, 1, 0, 5)
    by_id = {item.id: item for item in parsed.failures}
    assert (by_id["tests::it_fails"].file, by_id["tests::it_fails"].line) == ("src/lib.rs", 8)
    assert by_id["tests::it_fails"].message_excerpt == "assertion `left == right` failed"
    assert by_id["api_old"].message_excerpt == "old style message"
    assert by_id["api_old"].line == 3


def test_unittest_text_counts_and_headers():
    parsed = parse_unittest_text(UNITTEST, strip_prefix="/work/proj")
    assert _totals(parsed) == (1, 1, 1, 1, 4)
    by_id = {item.id: item for item in parsed.failures}
    assert by_id["test_u.T.test_b"].kind == "failure"
    assert (by_id["test_u.T.test_b"].file, by_id["test_u.T.test_b"].line) == ("test_u.py", 6)
    assert by_id["test_u.T.test_c"].message_excerpt == "ValueError: boom"
    ok = parse_unittest_text("..\n------\nRan 2 tests in 0.000s\n\nOK\n")
    assert _totals(ok) == (2, 0, 0, 0, 2) and not ok.failures


@pytest.mark.parametrize("prefix", [
    b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]>',
    b'<?xml version="1.0"?><!doctype x>',
    b'<?xml version="1.0"?><! ENTITY x "y">',
])
def test_doctype_and_entity_declarations_are_refused_before_parsing(prefix, monkeypatch):
    seen = []
    monkeypatch.setattr(rp.ET, "fromstring", lambda data: seen.append(data))
    bomb = prefix + b'<testsuite><testcase name="a"/></testsuite>'
    with pytest.raises(InvalidInput, match="DOCTYPE or ENTITY"):
        parse_junit_xml(bomb)
    with pytest.raises(InvalidInput, match="DOCTYPE or ENTITY"):
        parse_trx(prefix + b"<TestRun/>")
    assert seen == []  # xml.etree never saw the document


def test_control_the_same_document_without_a_doctype_parses():
    parsed = parse_junit_xml(b'<?xml version="1.0"?><testsuite><testcase name="a"/></testsuite>')
    assert _totals(parsed) == (1, 0, 0, 0, 1)


def test_oversized_documents_are_refused(monkeypatch):
    monkeypatch.setattr(rp, "MAX_DOCUMENT_BYTES", 100)
    with pytest.raises(InvalidInput, match="exceeds"):
        parse_junit_xml(b"<testsuite>" + b" " * 200 + b"</testsuite>")
    with pytest.raises(InvalidInput, match="exceeds"):
        parse_jest_json(b"{" + b" " * 200 + b"}")


def test_case_and_failure_caps_mark_the_result_truncated(monkeypatch):
    cases = "".join('<testcase name="t%d"><failure message="m"/></testcase>' % i for i in range(120))
    parsed = parse_junit_xml(("<testsuite>%s</testsuite>" % cases).encode())
    assert parsed.totals.failed == 120
    assert len(parsed.failures) == 50 and parsed.truncated
    monkeypatch.setattr(rp, "MAX_TESTCASES", 10)
    capped = parse_junit_xml(("<testsuite>%s</testsuite>" % cases).encode())
    assert capped.totals.total == 10 and capped.truncated


def test_malformed_and_foreign_documents_are_input_errors():
    with pytest.raises(InvalidInput):
        parse_junit_xml(b"<testsuite><testcase>")
    with pytest.raises(InvalidInput):
        parse_junit_xml(b"<html/>")
    with pytest.raises(InvalidInput):
        parse_trx(b"<testsuite/>")
    with pytest.raises(InvalidInput):
        parse_jest_json(b"[1, 2]")


def test_pathological_text_parses_quickly():
    line = "test result: " + "x" * 4096
    started = time.monotonic()
    parse_libtest_text((line + "\n") * 2000)
    parse_unittest_text(("FAIL: " + "a" * 4096 + "\n") * 2000)
    parse_go_test_json(("{" + "\"a\":" * 1000 + "\n") * 500)
    assert time.monotonic() - started < 5
