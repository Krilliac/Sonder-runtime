"""Strict per-runner test selector grammars.

A selector is the only free text a model contributes to a test run. It never
becomes a flag (it may not start with ``-``), never carries shell syntax, and
is turned into argv elements here, by the host, one grammar per runner. Path
selectors are additionally resolved under the project root by the adapter.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from ..common.errors import InvalidInput

MAX_SELECTOR_CHARS = 200

INVALID_SELECTOR = "INVALID_SELECTOR"
SELECTOR_UNSUPPORTED = "SELECTOR_UNSUPPORTED"
SELECTOR_ESCAPES_PROJECT = "SELECTOR_ESCAPES_PROJECT"


class SelectorKind(str, Enum):
    PYTEST_NODE = "pytest_node"
    PYTEST_KEYWORD = "pytest_keyword"
    PYTHON_DOTTED = "python_dotted"
    CTEST_NAME = "ctest_name"
    CARGO_FILTER = "cargo_filter"
    GO_PACKAGE = "go_package"
    GO_RUN = "go_run"
    DOTNET_FILTER = "dotnet_filter"
    JS_PATH = "js_path"
    JVM_PATTERN = "jvm_pattern"


# Kinds whose value names a path under the project root.
PATH_KINDS = frozenset({SelectorKind.PYTEST_NODE, SelectorKind.GO_PACKAGE, SelectorKind.JS_PATH})

# Runner name -> selector kinds it accepts. Keyed by the runner's string value
# so this module does not import ``runners`` (which imports this one).
RUNNER_SELECTOR_KINDS: dict[str, frozenset[SelectorKind]] = {
    "pytest": frozenset({SelectorKind.PYTEST_NODE, SelectorKind.PYTEST_KEYWORD}),
    "unittest": frozenset({SelectorKind.PYTHON_DOTTED}),
    "ctest": frozenset({SelectorKind.CTEST_NAME}),
    "cargo": frozenset({SelectorKind.CARGO_FILTER}),
    "go": frozenset({SelectorKind.GO_PACKAGE, SelectorKind.GO_RUN}),
    "dotnet": frozenset({SelectorKind.DOTNET_FILTER}),
    "npm": frozenset({SelectorKind.JS_PATH}),
    "pnpm": frozenset({SelectorKind.JS_PATH}),
    "yarn": frozenset({SelectorKind.JS_PATH}),
    "gradle": frozenset({SelectorKind.JVM_PATTERN}),
    "maven": frozenset({SelectorKind.JVM_PATTERN}),
    "make": frozenset(),
}

_PYTEST_NODE = re.compile(
    r"^(?P<path>[A-Za-z0-9_][A-Za-z0-9_./-]{0,199})"
    r"(?:::[A-Za-z_][A-Za-z0-9_]{0,99}){0,3}"
    r"(?:\[[A-Za-z0-9_.,-]{1,100}\])?$"
)
_PYTEST_KEYWORD = re.compile(
    r"^(?:not\s+)?[A-Za-z_][A-Za-z0-9_]{0,63}"
    r"(?:\s+(?:and|or)\s+(?:not\s+)?[A-Za-z_][A-Za-z0-9_]{0,63}){0,7}$"
)
_PYTHON_DOTTED = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){0,8}$")
_CTEST_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,128}\*?$")
_CARGO_FILTER = re.compile(r"^[A-Za-z0-9_:]{1,200}$")
_GO_PACKAGE = re.compile(r"^\./(?:[A-Za-z0-9_.-]+/)*(?:[A-Za-z0-9_.-]+|\.\.\.)$")
_GO_RUN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,99}(?:/[A-Za-z0-9_]{1,100}){0,3}$")
_DOTNET_FILTER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,199}$")
_JS_PATH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,199}$")
_GRADLE_PATTERN = re.compile(r"^[A-Za-z_*][A-Za-z0-9_.*]{0,199}$")
_MAVEN_PATTERN = re.compile(r"^[A-Za-z_*][A-Za-z0-9_.*#+]{0,199}$")
_WHITESPACE = re.compile(r"\s")
_KEYWORD_WORDS = frozenset({"and", "or", "not"})


@dataclass(frozen=True, slots=True)
class TestSelector:
    """A validated selector and the exact argv elements it contributes."""

    __test__ = False  # not a pytest test class

    kind: SelectorKind
    value: str
    argv: tuple[str, ...]

    @property
    def path(self) -> str:
        """The project-relative path a path-bearing selector names ("" if none)."""
        if self.kind is SelectorKind.PYTEST_NODE:
            return self.value.split("::", 1)[0].split("[", 1)[0]
        if self.kind is SelectorKind.GO_PACKAGE:
            return self.value[:-4] if self.value.endswith("/...") else self.value
        if self.kind is SelectorKind.JS_PATH:
            return self.value
        return ""


class SelectorRejected(InvalidInput):
    """A selector refused by the grammar; ``code`` says which rule refused it."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _runner_name(runner) -> str:
    return str(getattr(runner, "value", runner) or "")


def _reject(code: str, message: str) -> SelectorRejected:
    return SelectorRejected(code, message)


def _has_parent_segment(value: str) -> bool:
    return any(part == ".." for part in value.replace("\\", "/").split("/"))


def _require_relative_path(value: str) -> None:
    if value.startswith("/") or "\\" in value or re.match(r"^[A-Za-z]:", value):
        raise _reject(SELECTOR_ESCAPES_PROJECT, "selector path must be project-relative")
    if _has_parent_segment(value):
        raise _reject(SELECTOR_ESCAPES_PROJECT, "selector path may not contain '..'")


def parse_selector(runner, raw: str) -> TestSelector:
    """Validate ``raw`` against ``runner``'s grammar and build its argv.

    Raises ``SelectorRejected`` with ``INVALID_SELECTOR`` (grammar),
    ``SELECTOR_UNSUPPORTED`` (the runner takes no such selector) or
    ``SELECTOR_ESCAPES_PROJECT`` (an absolute or parent-relative path).
    """
    name = _runner_name(runner)
    kinds = RUNNER_SELECTOR_KINDS.get(name)
    if kinds is None:
        raise _reject(SELECTOR_UNSUPPORTED, "unknown test runner")
    if not isinstance(raw, str):
        raise _reject(INVALID_SELECTOR, "selector must be text")
    if not raw:
        raise _reject(INVALID_SELECTOR, "selector is empty")
    if len(raw) > MAX_SELECTOR_CHARS:
        raise _reject(INVALID_SELECTOR, "selector exceeds %d characters" % MAX_SELECTOR_CHARS)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise _reject(INVALID_SELECTOR, "selector contains control characters")
    if raw.startswith("-"):
        raise _reject(INVALID_SELECTOR, "selector may not start with '-'")
    if not kinds:
        raise _reject(SELECTOR_UNSUPPORTED, "the %s runner takes no selector" % name)
    keyword = name == "pytest" and raw.startswith("k:")
    if not keyword and _WHITESPACE.search(raw):
        raise _reject(INVALID_SELECTOR, "selector may not contain whitespace")

    if name == "pytest":
        if keyword:
            expression = raw[2:]
            if not _PYTEST_KEYWORD.fullmatch(expression):
                raise _reject(INVALID_SELECTOR, "pytest keyword selector does not match k:<expr> grammar")
            words = expression.split()
            if words and (words[-1] in _KEYWORD_WORDS):
                raise _reject(INVALID_SELECTOR, "pytest keyword selector ends with an operator")
            normalized = " ".join(words)
            return TestSelector(SelectorKind.PYTEST_KEYWORD, normalized, ("-k", normalized))
        _require_relative_path(raw)
        if not _PYTEST_NODE.fullmatch(raw):
            raise _reject(INVALID_SELECTOR, "pytest selector does not match the node-id grammar")
        return TestSelector(SelectorKind.PYTEST_NODE, raw, (raw,))
    if name == "unittest":
        if not _PYTHON_DOTTED.fullmatch(raw):
            raise _reject(INVALID_SELECTOR, "unittest selector must be a dotted name")
        return TestSelector(SelectorKind.PYTHON_DOTTED, raw, (raw,))
    if name == "ctest":
        if not _CTEST_NAME.fullmatch(raw):
            raise _reject(INVALID_SELECTOR, "ctest selector does not match the test-name grammar")
        if raw.endswith("*"):
            pattern = "^" + re.escape(raw[:-1])
        else:
            pattern = "^" + re.escape(raw) + "$"
        return TestSelector(SelectorKind.CTEST_NAME, raw, ("-R", pattern))
    if name == "cargo":
        if not _CARGO_FILTER.fullmatch(raw):
            raise _reject(INVALID_SELECTOR, "cargo selector does not match the filter grammar")
        return TestSelector(SelectorKind.CARGO_FILTER, raw, (raw,))
    if name == "go":
        if raw.startswith("run:"):
            test = raw[4:]
            if not _GO_RUN.fullmatch(test):
                raise _reject(INVALID_SELECTOR, "go run selector does not match run:<Test>[/<sub>]")
            pattern = "/".join("^%s$" % part for part in test.split("/"))
            return TestSelector(SelectorKind.GO_RUN, test, ("-run", pattern))
        if raw.startswith("./"):
            if _has_parent_segment(raw[2:]) or raw == "./..":
                raise _reject(SELECTOR_ESCAPES_PROJECT, "go package may not contain '..'")
            if not _GO_PACKAGE.fullmatch(raw):
                raise _reject(INVALID_SELECTOR, "go package selector does not match ./<pkg> grammar")
            return TestSelector(SelectorKind.GO_PACKAGE, raw, (raw,))
        _require_relative_path(raw)
        raise _reject(INVALID_SELECTOR, "go selector must be ./<package> or run:<Test>")
    if name == "dotnet":
        if not _DOTNET_FILTER.fullmatch(raw):
            raise _reject(INVALID_SELECTOR, "dotnet selector does not match the filter grammar")
        return TestSelector(SelectorKind.DOTNET_FILTER, raw,
                            ("--filter", "FullyQualifiedName~" + raw))
    if name in {"npm", "pnpm", "yarn"}:
        _require_relative_path(raw)
        if not _JS_PATH.fullmatch(raw):
            raise _reject(INVALID_SELECTOR, "js selector does not match the path grammar")
        return TestSelector(SelectorKind.JS_PATH, raw, (raw,))
    if name == "gradle":
        if not _GRADLE_PATTERN.fullmatch(raw):
            raise _reject(INVALID_SELECTOR, "gradle selector does not match the pattern grammar")
        return TestSelector(SelectorKind.JVM_PATTERN, raw, ("--tests", raw))
    if name == "maven":
        if not _MAVEN_PATTERN.fullmatch(raw):
            raise _reject(INVALID_SELECTOR, "maven selector does not match the pattern grammar")
        return TestSelector(SelectorKind.JVM_PATTERN, raw,
                            ("-Dtest=" + raw, "-Dsurefire.failIfNoSpecifiedTests=false"))
    raise _reject(SELECTOR_UNSUPPORTED, "the %s runner takes no selector" % name)


__all__ = [
    "INVALID_SELECTOR", "MAX_SELECTOR_CHARS", "PATH_KINDS", "RUNNER_SELECTOR_KINDS",
    "SELECTOR_ESCAPES_PROJECT", "SELECTOR_UNSUPPORTED", "SelectorKind",
    "SelectorRejected", "TestSelector", "parse_selector",
]
