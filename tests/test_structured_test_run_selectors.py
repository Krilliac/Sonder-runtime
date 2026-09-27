"""Selector grammars: the only free text a model contributes to a test run.

Every grammar has a refusal table and a permitting control that shows the
exact argv the accepted selector contributes.
"""
from __future__ import annotations

import pytest

from sonder_runtime.domain.common.errors import InvalidInput
from sonder_runtime.domain.testing.runners import TestRunner
from sonder_runtime.domain.testing.selectors import (
    INVALID_SELECTOR,
    SELECTOR_ESCAPES_PROJECT,
    SELECTOR_UNSUPPORTED,
    SelectorKind,
    SelectorRejected,
    parse_selector,
)

pytestmark = pytest.mark.unit

# (runner, raw) that every grammar must refuse, with the refusal code.
UNIVERSAL_REJECTIONS = [
    ("--junitxml=x", INVALID_SELECTOR),
    ("-k", INVALID_SELECTOR),
    ("a;b", INVALID_SELECTOR),
    ("$(x)", INVALID_SELECTOR),
    ("%PATH%", INVALID_SELECTOR),
    ("a'b", INVALID_SELECTOR),
    ('a"b', INVALID_SELECTOR),
    ("a\x00b", INVALID_SELECTOR),
    ("a\nb", INVALID_SELECTOR),
    ("x" * 201, INVALID_SELECTOR),
    ("a b", INVALID_SELECTOR),
    ("", INVALID_SELECTOR),
]


@pytest.mark.parametrize("runner", [r for r in TestRunner if r is not TestRunner.MAKE])
@pytest.mark.parametrize("raw, code", UNIVERSAL_REJECTIONS)
def test_every_grammar_refuses_flags_shell_syntax_and_oversize(runner, raw, code):
    with pytest.raises(SelectorRejected) as caught:
        parse_selector(runner, raw)
    assert caught.value.code == code
    assert isinstance(caught.value, InvalidInput)


@pytest.mark.parametrize("raw", ["../x.py", "a/../../x.py", "/abs/test.py", "C:/x/test.py"])
def test_path_selectors_that_leave_the_project_are_refused_as_escapes(raw):
    with pytest.raises(SelectorRejected) as caught:
        parse_selector(TestRunner.PYTEST, raw)
    assert caught.value.code == SELECTOR_ESCAPES_PROJECT


@pytest.mark.parametrize("raw", ["./..", "./a/../b"])
def test_go_package_parent_segments_are_refused(raw):
    with pytest.raises(SelectorRejected) as caught:
        parse_selector(TestRunner.GO, raw)
    assert caught.value.code == SELECTOR_ESCAPES_PROJECT


def test_make_takes_no_selector_but_still_refuses_flags_first():
    with pytest.raises(SelectorRejected) as caught:
        parse_selector(TestRunner.MAKE, "check")
    assert caught.value.code == SELECTOR_UNSUPPORTED
    with pytest.raises(SelectorRejected) as caught:
        parse_selector(TestRunner.MAKE, "-j8")
    assert caught.value.code == INVALID_SELECTOR


@pytest.mark.parametrize("runner, raw, kind, argv", [
    (TestRunner.PYTEST, "tests/test_mod.py::TestK::test_inner[a-1]", SelectorKind.PYTEST_NODE,
     ("tests/test_mod.py::TestK::test_inner[a-1]",)),
    (TestRunner.PYTEST, "test_mod.py", SelectorKind.PYTEST_NODE, ("test_mod.py",)),
    (TestRunner.PYTEST, "k:not slow and   fast", SelectorKind.PYTEST_KEYWORD,
     ("-k", "not slow and fast")),
    (TestRunner.UNITTEST, "pkg.test_mod.T.test_a", SelectorKind.PYTHON_DOTTED,
     ("pkg.test_mod.T.test_a",)),
    (TestRunner.CTEST, "unit.parse-1", SelectorKind.CTEST_NAME, ("-R", r"^unit\.parse\-1$")),
    (TestRunner.CTEST, "unit*", SelectorKind.CTEST_NAME, ("-R", "^unit")),
    (TestRunner.CARGO, "tests::it_works", SelectorKind.CARGO_FILTER, ("tests::it_works",)),
    (TestRunner.GO, "./pkg/...", SelectorKind.GO_PACKAGE, ("./pkg/...",)),
    (TestRunner.GO, "run:TestA/sub_1", SelectorKind.GO_RUN, ("-run", "^TestA$/^sub_1$")),
    (TestRunner.DOTNET, "My.Tests.Parse", SelectorKind.DOTNET_FILTER,
     ("--filter", "FullyQualifiedName~My.Tests.Parse")),
    (TestRunner.NPM, "src/a.test.ts", SelectorKind.JS_PATH, ("src/a.test.ts",)),
    (TestRunner.YARN, "src", SelectorKind.JS_PATH, ("src",)),
    (TestRunner.GRADLE, "com.x.*Test", SelectorKind.JVM_PATTERN, ("--tests", "com.x.*Test")),
    (TestRunner.MAVEN, "FooTest#bar+baz", SelectorKind.JVM_PATTERN,
     ("-Dtest=FooTest#bar+baz", "-Dsurefire.failIfNoSpecifiedTests=false")),
])
def test_each_runner_turns_a_permitted_selector_into_exact_argv(runner, raw, kind, argv):
    selector = parse_selector(runner, raw)
    assert selector.kind is kind
    assert selector.argv == argv
    assert not any(item.startswith("-") and item not in {"-k", "-R", "-run", "--filter", "--tests"}
                   and not item.startswith("-Dtest=") and not item.startswith("-Dsurefire")
                   for item in selector.argv)


@pytest.mark.parametrize("runner, raw", [
    (TestRunner.PYTEST, "k:a or"),
    (TestRunner.PYTEST, "k:a; b"),
    (TestRunner.PYTEST, "test.py::1bad"),
    (TestRunner.UNITTEST, "a..b"),
    (TestRunner.UNITTEST, "tests/test_a.py"),
    (TestRunner.CTEST, "a*b"),
    (TestRunner.CARGO, "a-b"),
    (TestRunner.GO, "run:A B"),
    (TestRunner.GO, "run:1Test"),
    (TestRunner.GO, "pkg"),
    (TestRunner.DOTNET, "My-Tests"),
    (TestRunner.NPM, "src/*.ts"),
    (TestRunner.GRADLE, "Foo#bar"),
    (TestRunner.MAVEN, "Foo(bar)"),
])
def test_each_grammar_rejects_shapes_outside_it(runner, raw):
    with pytest.raises(SelectorRejected) as caught:
        parse_selector(runner, raw)
    assert caught.value.code in {INVALID_SELECTOR, SELECTOR_ESCAPES_PROJECT}


def test_whitespace_is_only_admitted_inside_a_pytest_keyword_expression():
    assert parse_selector(TestRunner.PYTEST, "k:a and b").argv == ("-k", "a and b")
    with pytest.raises(SelectorRejected):
        parse_selector(TestRunner.CARGO, "k:a and b")
    with pytest.raises(SelectorRejected):
        parse_selector(TestRunner.PYTEST, "test_a.py test_b.py")


def test_an_unknown_runner_name_is_unsupported():
    with pytest.raises(SelectorRejected) as caught:
        parse_selector("tox", "anything")
    assert caught.value.code == SELECTOR_UNSUPPORTED
