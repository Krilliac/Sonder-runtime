"""Seeded mutation fuzzing of the slash-command and intent parsers.

Contract fuzzed: a line typed by a model or a user must never become a
different command, a second command, or a runner flag than the one it names.

Functions under test:

* ``command_catalog.parse_invocation`` -- ``None`` for an uncatalogued line,
  ``InvocationError`` (a ``ValueError``) for a malformed one, otherwise
  ``(tool, kwargs)``; ``_coerce`` (never raises); ``near_misses`` (suggestions
  only, never an execution).
* ``command_router.resolve`` -- natural turn -> slash line or ``None``.
* ``command_parsing._parse_game_campaign_command``.
* ``control_timeout.parse_control_timeout`` -- ``(1..60, None)``,
  ``(8, None)`` for blank, or ``(None, usage)``.
* ``testing.selectors.parse_selector`` -- ``TestSelector`` or
  ``SelectorRejected``; the selector reaches the runner as argv elements
  (``runners.py`` extends a list, no shell), so the invariant is that no
  element derived from the raw text can be read as a flag.
* ``web_intents._clean_location`` / ``extract_weather_location``.

Case folding is deliberate and ASCII-only in intent: ``/TASK_DELETE`` is the
same command as ``/task_delete``. The oracle folds ASCII and nothing more.

Replay a failure with ``FUZZ_SEED=<n>`` (the failing assertion prints it).
Every input is timed: p99 under 50 ms, no single input over 2 s.
"""
from __future__ import annotations

import math
import os
import random
import re
import sys
import time
import unicodedata

import pytest

import command_catalog as cc
import command_router as cr
import intents
import web_intents as wi
from sonder_runtime.application.command_surface import command_text as ct
from sonder_runtime.adapters.command_parsing import _parse_game_campaign_command
from sonder_runtime.domain.control_timeout import (
    DEFAULT_TIMEOUT,
    MAX_TIMEOUT,
    parse_control_timeout,
)
from sonder_runtime.domain.testing.selectors import (
    INVALID_SELECTOR,
    MAX_SELECTOR_CHARS,
    RUNNER_SELECTOR_KINDS,
    SELECTOR_ESCAPES_PROJECT,
    SELECTOR_UNSUPPORTED,
    SelectorKind,
    SelectorRejected,
    parse_selector,
)

pytestmark = pytest.mark.unit

SEED = int(os.environ.get("FUZZ_SEED", "20261116"))
KELVIN = "\u212a"
ZERO_WIDTH = ["\u200b", "\u200c", "\u200d", "\u2060", "\ufeff", "\u00ad"]
# One visible look-alike per ASCII letter that has one, plus fullwidth below.
HOMOGLYPHS = {
    "a": ["\u0430"], "e": ["\u0435"], "o": ["\u043e"], "p": ["\u0440"], "c": ["\u0441"],
    "x": ["\u0445"], "i": ["\u0456", "\u0131"], "k": [KELVIN, "\u043a"], "s": ["\u017f", "\u0455"],
    "l": ["\u04cf"], "t": ["\u0442"],
}


def _info(salt: str) -> str:
    return f"FUZZ_SEED={SEED} salt={salt}"


def _rng(salt: int) -> random.Random:
    return random.Random(SEED * 1000 + salt)


def _check_timings(timings: list[float], info: str) -> None:
    timings = sorted(timings)
    p99 = timings[max(0, math.ceil(len(timings) * 0.99) - 1)]
    assert p99 < 0.05, f"{info}: p99 {p99 * 1000:.1f} ms"
    assert timings[-1] < 2.0, f"{info}: max {timings[-1] * 1000:.1f} ms"


def _call(fn, allowed, data, timings, info):
    """Run ``fn(data)``; return (result, typed_error). Anything else fails."""
    started = time.perf_counter()
    try:
        result, error = fn(data), None
    except allowed as exc:
        result, error = None, exc
    except Exception as exc:  # noqa: BLE001 -- the point is to report any escape
        pytest.fail(f"{info}: {type(exc).__name__} escaped for input {data!r}: {exc!r}")
    elapsed = time.perf_counter() - started
    timings.append(elapsed)
    assert elapsed < 2.0, f"{info}: {elapsed:.2f} s for input {data[:200] if isinstance(data, str) else data!r}"
    return result, error


def _fold(text: str) -> str:
    """ASCII-only case fold: the only fold the catalog is meant to apply."""
    return "".join(chr(ord(ch) + 32) if "A" <= ch <= "Z" else ch for ch in text)


class _Cat:
    def __init__(self):
        self.commands = [c for c in cc.catalog() if c.name]
        self.by_folded: dict[str, list] = {}
        for command in self.commands:
            for name in command.all_names:
                self.by_folded.setdefault(_fold(name), []).append(command)
        self.risky = [c for c in self.commands if c.tool and c.risk in ("mutation", "dangerous", "execution")]
        self.risky_tools = {c.tool for c in self.risky}
        self.names = {_fold(n) for c in self.commands for n in c.all_names}
        self.categories = set(cc.categories())

    def allowed_tools(self, line: str) -> set:
        """Tools the line may legitimately name, by the ASCII-fold oracle."""
        text = line.strip()
        if not text.startswith("/"):
            return set()
        head = text.partition(" ")[0].strip()
        return {c.tool for c in self.by_folded.get(_fold(head), []) if c.tool}


@pytest.fixture(scope="module")
def cat() -> _Cat:
    return _Cat()


def _check_head(cat, line, result, info):
    """Whatever parse_invocation returns must name the line's own head."""
    if result is None:
        return
    assert isinstance(result, tuple) and len(result) == 2, f"{info}: {line!r} -> {result!r}"
    tool, kwargs = result
    assert tool in cat.allowed_tools(line), f"{info}: {line!r} resolved to {tool!r}"
    assert isinstance(kwargs, dict)


# --- name mutation ----------------------------------------------------------

def _ascii_case(rng, stem):
    return "".join(ch.swapcase() if rng.random() < 0.5 else ch for ch in stem)


def _homoglyph(rng, stem, index=None):
    index = rng.randrange(len(stem)) if index is None else index
    ch = stem[index]
    if ch.lower() in HOMOGLYPHS and rng.random() < 0.7:
        sub = rng.choice(HOMOGLYPHS[ch.lower()])
    else:
        sub = chr(ord(ch) + 0xFEE0) if 0x21 <= ord(ch) < 0x7F else ch
    return stem[:index] + sub + stem[index + 1:]


def _mutations(rng, name):
    """Yield (kind, mutated_line_head) for a catalogued slash name."""
    stem = name.lstrip("/")
    pos = rng.randrange(len(stem) + 1)
    yield "fold", "/" + _ascii_case(rng, stem)
    yield "homoglyph", "/" + _homoglyph(rng, stem)
    yield "zero-width", "/" + stem[:pos] + rng.choice(ZERO_WIDTH) + stem[pos:]
    yield "trailing-punct", "/" + stem + rng.choice(list(".,;:!?)\"'`/\\"))
    yield "prefix-junk", rng.choice(["x", "//", "/ ", ".", "\\", "`", "\u200b", "@"]) + "/" + stem
    yield "mid-junk", "/" + rng.choice(["x", "-", ".", "_", "/"]) + stem
    yield "suffix-junk", "/" + stem + rng.choice(["x", "_", "s", "2", "-", "..", "_x", "%"])
    yield "embedded-ws", "/" + stem[:pos] + rng.choice(["\n", "\t", "\r", "\x00", "\x0b", "\u00a0"]) + stem[pos:]
    if len(stem) > 2:
        swap = rng.randrange(len(stem) - 1)
        yield "swap", "/" + stem[:swap] + stem[swap + 1] + stem[swap] + stem[swap + 2:]
        yield "truncate", "/" + stem[:-1]


def test_dangerous_command_name_mutations_never_resolve(cat):
    """No mutated spelling of a mutating/dangerous/execution command runs it.

    Exact ASCII case variants are the one documented fold: they must resolve to
    precisely the named command. Every other mutation may only produce None or
    an InvocationError, or a *different* catalogued command -- never a risky
    tool the mutated head does not name under the ASCII-fold oracle.
    """
    rng = _rng(1)
    info = _info("dangerous-names")
    timings: list[float] = []
    checked = 0
    for command in cat.risky:
        for name in command.all_names:
            stem = name.lstrip("/")
            lines = [(kind, head) for kind, head in _mutations(rng, name)]
            # every position gets one homoglyph, so no letter is skipped by chance
            lines += [("homoglyph", "/" + _homoglyph(rng, stem, i)) for i in range(len(stem))]
            for kind, head in lines:
                for tail in ("", " x", " path=x", "\n/file_read y"):
                    line = head + tail
                    result, _ = _call(cc.parse_invocation, cc.InvocationError, line, timings, info)
                    _check_head(cat, line, result, info)
                    if kind == "fold" and tail == "" and command.tool:
                        assert result == (command.tool, {}), f"{info}: {line!r} -> {result!r}"
                    checked += 1
    assert checked > 3000
    _check_timings(timings, info)


def test_kelvin_sign_does_not_fold_into_a_command_name(cat):
    """Regression: KELVIN SIGN lowercases to ``k`` and used to reach /task_delete.

    ``by_name`` lowercases with ``str.lower`` (full Unicode), so a name written
    with U+212A resolved to the command spelled with ASCII ``k``. The ASCII
    fold is the documented one; this is a look-alike, not a case variant.
    """
    line = f"/tas{KELVIN}_delete x"
    assert cc.by_name(f"/tas{KELVIN}_delete") is None
    assert cc.parse_invocation(line) is None


def test_non_ascii_lookalike_by_name_is_never_a_command(cat):
    rng = _rng(2)
    for command in cat.commands:
        for name in command.all_names:
            stem = name.lstrip("/")
            for index in range(len(stem)):
                mutated = _homoglyph(rng, stem, index)
                if mutated.isascii():
                    continue
                found = cc.by_name("/" + mutated)
                assert found is None or _fold(mutated) in cat.names, (
                    "{}: by_name({!r}) -> {}".format(_info("by-name"), "/" + mutated, found.name))


# --- argument smuggling ------------------------------------------------------

_SEPARATORS = ["; ", ";", "\n", " \n", "\r\n", ' "x" ', " 'x' ", " && ", " || ", " | ", " `", " $(", " # ", " -- ",
               "\\\n", '" ', "' ", " \u2028", "\t", "\x00"]


def test_arguments_never_smuggle_a_second_invocation(cat):
    """``/a x SEP /b ZQMARK`` is one invocation of /a, or a refusal.

    The result is a single ``(tool, kwargs)`` for /a; its keys are /a's own
    parameters; the smuggled text may only appear inside a *str*-typed value
    (a typed value that cannot hold text would be a coercion leak). A
    key=value pair that belongs to nothing refuses outright.
    """
    rng = _rng(3)
    info = _info("smuggle")
    timings: list[float] = []
    callable_cmds = [c for c in cat.commands if c.tool]
    for _ in range(900):
        a = rng.choice(callable_cmds)
        b = rng.choice(cat.risky)
        sep = rng.choice(_SEPARATORS)
        lead = rng.choice(["", " x", " x y", " 1", ' "q"', " 0x10"])
        line = f"{a.name}{lead}{sep}{b.name} ZQMARK"
        keyed = f"{a.name}{lead}{sep}{b.name} ZQMARK zq_smuggle_key=ZQVAL"
        result, error = _call(cc.parse_invocation, cc.InvocationError, line, timings, info)
        _check_head(cat, line, result, info)
        if result is not None:
            tool, kwargs = result
            params = {p.name: p for p in a.params}
            assert tool == a.tool
            assert set(kwargs) <= set(params), f"{info}: {line!r} -> {kwargs!r}"
            for value in kwargs.values():
                if "ZQMARK" in str(value):
                    # Positional words keep the documented lenient coercion, so an int/num/bool
                    # parameter may carry the raw word (documented leniency, not a bug); what
                    # must not happen is a typed value or a second invocation holding it.
                    assert isinstance(value, str), f"{info}: {line!r} -> {kwargs!r}"
            if not a.params and lead + sep:
                pytest.fail(f"{info}: param-less {a.name} accepted trailing text in {line!r} -> {result!r}")
        if error is not None:
            assert error.command == a.name
        result, error = _call(cc.parse_invocation, cc.InvocationError, keyed, timings, info)
        assert result is None, f"{info}: stray key reached {keyed!r} -> {result!r}"
        if error is None:
            assert not cat.allowed_tools(keyed), f"{info}: {keyed!r} was a catalogued head yet raised nothing"
        else:
            assert error.problem in ("unknown-parameter", "conflicting-duplicate", "invalid-value"), (
                f"{info}: {keyed!r} -> {error!r}")
    _check_timings(timings, info)


def test_second_command_on_next_line_or_after_a_semicolon_is_not_a_head(cat):
    """``/a\\n/b`` and ``/a;/b`` have no space after /a, so the head is the whole token."""
    for a in (c for c in cat.commands if c.tool):
        for glue in ("\n", ";", "\r\n", "&&", "|"):
            line = a.name + glue + "/task_delete x"
            assert cc.parse_invocation(line) is None, repr(line)


def test_parse_invocation_fuzz_error_contract_and_head_oracle(cat):
    rng = _rng(4)
    info = _info("parse-invocation")
    timings: list[float] = []
    commands = [c for c in cat.commands if c.tool]
    pool = ["x", "path=x", "a=b", 'a="b c"', "'", '"', "\\", "`", "$(x)", "limit=nan", "timeout=1e309",
            "dry_run=nope", "ZQ=1", "=", "==", "k=", "=v", "\u200bx", "\u00e9", "0x10", "-5", "1_0", "[1,2", '{"a":',
            "\n", "\t", "\x00", "/", "/task_delete", ";", "x=1 x=2", 'steps=["a","b"]']
    outcomes = {"ok": 0, "error": 0, "none": 0}
    for _ in range(2500):
        command = rng.choice(commands)
        names = list(command.all_names)
        head = _ascii_case(rng, rng.choice(names)) if rng.random() < 0.3 else rng.choice(names)
        words = [rng.choice(pool) for _ in range(rng.randint(0, 6))]
        for param in command.params:
            if rng.random() < 0.3:
                words.append(f"{param.name}={rng.choice(pool)}")
        line = rng.choice(["", " "]) + head + rng.choice([" ", "  ", " \t"]) + rng.choice([" ", ""]).join(words)
        result, error = _call(cc.parse_invocation, cc.InvocationError, line, timings, info)
        _check_head(cat, line, result, info)
        if error is not None:
            assert isinstance(error, ValueError) and error.command
            outcomes["error"] += 1
        else:
            outcomes["ok" if result else "none"] += 1
    assert outcomes["ok"] and outcomes["error"]
    _check_timings(timings, info)


def test_parse_invocation_non_slash_and_empty_are_none():
    rng = _rng(5)
    for _ in range(300):
        text = "".join(rng.choice("abc /\n\t\"'\u200b") for _ in range(rng.randint(0, 30)))
        if text.strip().startswith("/"):
            continue
        assert cc.parse_invocation(text) is None, repr(text)
    for value in (None, "", " ", 0, False):
        assert cc.parse_invocation(value) is None


# --- _coerce -----------------------------------------------------------------

_DIGITS = r"\d+(?:_\d+)*"
_INT_RE = re.compile(rf"[+-]?{_DIGITS}")
_NUM_RE = re.compile(
    rf"[+-]?(?:inf(?:inity)?|nan|(?:{_DIGITS}(?:\.(?:{_DIGITS})?)?|\.{_DIGITS})(?:e[+-]?{_DIGITS})?)",
    re.IGNORECASE,
)
_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


def _int_oracle(value):
    """int() the documented way: optional sign, decimal digits (Unicode Nd and
    single underscores allowed), surrounding whitespace ignored, and the
    interpreter's digit limit; anything else stays the raw string."""
    text = value.strip()
    if _INT_RE.fullmatch(text) and len(re.sub(r"\D", "", text)) <= sys.get_int_max_str_digits():
        return int(text.replace("_", ""))
    return value


def _num_ok(value):
    return bool(_NUM_RE.fullmatch(value.strip()))


_COERCE_SEEDS = ["", " ", "0", "-0", "+5", "-5", "7", " 7 ", "1e309", "-1e309", "nan", "NaN", "-nan", "inf", "-Infinity",
                 "0x10", "0b1", "0o7", "1_000", "1__0", "_1", "1_", "5.0", ".5", "5.", "1e5", "1E-5", "e5", "٣", "１２",
                 "99999999999999999999999999", "9" * 4300, "9" * 4301, "9" * 5000, "true", "TRUE", " Yes ", "on", "OFF",
                 "no", "0 ", "tru", "\x00", "\u200b1", "1\u200b", "\u212a", "x" * 1000, "1 2", "--1", "1-", "\n7\n"]
_COERCE_CHARS = "0123456789+-._eE xnaifINFTRUYSO\u0663\uff11\u200b\n\x00\u212a"


def _coerce_inputs(rng, count):
    yield from _COERCE_SEEDS
    for _ in range(count):
        base = rng.choice(_COERCE_SEEDS)
        chars = list(base)
        for _ in range(rng.randint(1, 4)):
            op = rng.randrange(3)
            at = rng.randrange(len(chars) + 1)
            if op == 0:
                chars.insert(at, rng.choice(_COERCE_CHARS))
            elif op == 1 and chars:
                del chars[min(at, len(chars) - 1)]
            else:
                chars[at:at] = [rng.choice(_COERCE_CHARS)] * rng.randint(1, 30)
        yield "".join(chars)


def test_coerce_never_raises_and_follows_the_reference_oracle():
    rng = _rng(6)
    info = _info("coerce")
    timings: list[float] = []
    for value in _coerce_inputs(rng, 4000):
        for kind in ("int", "num", "bool", "str", "", "weird"):
            started = time.perf_counter()
            try:
                got = cc._coerce(value, kind)
            except Exception as exc:  # noqa: BLE001
                pytest.fail(f"{info}: _coerce({value[:80]!r}, {kind!r}) raised {exc!r}")
            timings.append(time.perf_counter() - started)
            where = f"{info}: _coerce({value[:80]!r}, {kind!r}) -> {got!r}"
            if kind == "int":
                want = _int_oracle(value)
                assert got == want and type(got) is type(want), where
                if isinstance(got, str):
                    assert got is value
            elif kind == "num":
                if _num_ok(value):
                    assert type(got) is float, where
                else:
                    assert got is value, where
            elif kind == "bool":
                low = _fold(value.strip())
                want = True if low in _TRUTHY else False if low in _FALSY else value
                assert got == want and type(got) is type(want), where
            else:
                assert got is value, where
    _check_timings(timings, info)


@pytest.mark.parametrize("value,expected", [
    ("1e309", "1e309"), ("nan", "nan"), ("0x10", "0x10"), ("-5", -5), ("+5", 5), (" 7 ", 7),
    ("99999999999999999999999999", 99999999999999999999999999), ("9" * 5000, "9" * 5000),
    ("5.0", "5.0"), ("", ""),
])
def test_coerce_int_boundaries(value, expected):
    got = cc._coerce(value, "int")
    assert got == expected and type(got) is type(expected)


def test_coerce_num_boundaries_are_floats_not_text():
    assert cc._coerce("1e309", "num") == math.inf
    assert math.isnan(cc._coerce("nan", "num"))
    assert cc._coerce("0x10", "num") == "0x10"
    assert cc._coerce("-5", "num") == -5.0


def test_named_typed_values_follow_the_coerce_oracle_or_refuse(cat):
    """``key=value`` for an int/num/bool parameter either coerces exactly or raises invalid-value."""
    rng = _rng(7)
    info = _info("named-typed")
    typed = [(c, p) for c in cat.commands if c.tool for p in c.params if p.type in ("int", "num", "bool")]
    assert typed
    timings: list[float] = []
    values = [v for v in _coerce_inputs(rng, 600) if v.strip() == v and v and not re.search(r"\s|[\"'\\]", v)
              and "=" not in v]
    for value in values:
        command, param = rng.choice(typed)
        line = f"{command.name} {param.name}={value}"
        result, error = _call(cc.parse_invocation, cc.InvocationError, line, timings, info)
        want = cc._coerce(value, param.type)
        if isinstance(want, str):
            assert error is not None and error.problem == "invalid-value", f"{info}: {line!r} -> {result!r}"
        else:
            assert error is None, f"{info}: {line!r} -> {result!r} {error!r}"
            got = result[1][param.name]
            same = (math.isnan(got) and math.isnan(want)) if isinstance(want, float) and math.isnan(want)                 else got == want
            assert same, f"{info}: {line!r} -> {result!r} (wanted {want!r})"
    _check_timings(timings, info)


# --- near misses -------------------------------------------------------------

def test_near_misses_are_suggestions_only_and_never_raise(cat):
    rng = _rng(8)
    info = _info("near-misses")
    timings: list[float] = []
    stems = [n.lstrip("/") for c in cat.risky for n in c.all_names]
    limits = [5, 1, 0, -1, 3, "2", None, "x", 10 ** 30, float("nan"), float("inf"), 2.7]
    for _ in range(250):
        stem = rng.choice(stems)
        text = rng.choice(list(_mutations(rng, stem)))[1][1:]
        text = rng.choice([text, text.upper(), "/" + text, f" {text} ", stem + "\n" + stem])
        limit = rng.choice(limits)
        got, _ = _call(lambda t, n=limit: cc.near_misses(t, n), (), text, timings, info)
        assert isinstance(got, list)
        try:
            cap = int(limit)
        except (TypeError, ValueError, OverflowError):
            cap = 0
        assert len(got) <= max(cap, 0), f"{info}: {text!r} limit={limit!r} -> {got!r}"
        assert len(set(got)) == len(got)
        for suggestion in got:
            assert isinstance(suggestion, str)
            assert (suggestion.startswith("/") and _fold(suggestion) in cat.names) or suggestion in cat.categories, (
                f"{info}: {text!r} suggested unknown {suggestion!r}")
        assert isinstance(cc._did_you_mean(text), str)
    for text in (None, "", "/", "ab", "\x00" * 50, "/" * 10000, "a" * 10000):
        _call(cc.near_misses, (), text, timings, info)
    _check_timings(timings, info)


# --- resolve -----------------------------------------------------------------

# (verb phrase, argument kept verbatim, resolved line) -- all pinned by
# tests/test_router_adversarial_table.py::test_naming_a_risky_command_outright_still_resolves.
_POSITIVE_PARTS = [
    ("git merge", "", "/git_merge"),
    ("self heal repair", "", "/self_heal_repair"),
    ("update the runtime policy", "", "/runtime_policy_update"),
    ("delete task", "abc", "/task_delete abc"),
    ("use the sqlite_mutate tool with", "sql=SELECT 1", "/sqlite_mutate sql=SELECT 1"),
]
_POSITIVE_TURNS = [((verb + " " + arg).strip(), expected) for verb, arg, expected in _POSITIVE_PARTS]


def _pad(rng, turn):
    return rng.choice(["", " ", "\n", "\t ", "  "]) + turn + rng.choice(["", " ", "\n", "  \t"])


def test_resolve_known_positive_survives_case_and_padding():
    """The phrases the table pins keep resolving through ASCII case and padding noise."""
    rng = _rng(9)
    info = _info("resolve-positive")
    for verb, arg, expected in _POSITIVE_PARTS:
        for _ in range(40):
            variant = _pad(rng, (_ascii_case(rng, verb) + " " + arg).strip())
            assert cr.resolve(variant) == expected, f"{info}: {variant!r}"


# Words the router skips, so a look-alike in one of them changes nothing it acts on.
_FILLER_WORDS = {"the", "tool", "with", "use", "a", "an"}


def test_resolve_lookalike_names_never_reach_a_risky_command(cat):
    """A visible look-alike letter in the verb must not still run the command."""
    rng = _rng(10)
    info = _info("resolve-lookalike")
    timings: list[float] = []
    for turn, _expected in _POSITIVE_TURNS:
        verb_end = len(turn.split(" sql=")[0]) if "sql=" in turn else len(turn)
        if turn.startswith("delete task"):
            verb_end = len("delete task")
        spans = [m for m in re.finditer(r"[A-Za-z_]+", turn[:verb_end]) if m.group(0) not in _FILLER_WORDS]
        for index in (i for m in spans for i in range(m.start(), m.end())):
            variant = _homoglyph(rng, turn, index)
            if variant == turn:
                continue
            got, _ = _call(cr.resolve, (), variant, timings, info)
            if got is None:
                continue
            head = got.split(" ")[0]
            command = cc.by_name(head)
            assert command is None or command.tool not in cat.risky_tools, (
                f"{info}: look-alike {variant!r} resolved to {got!r}")
    _check_timings(timings, info)


def test_resolve_kelvin_sign_does_not_stand_in_for_k():
    """Regression: the router's IGNORECASE rules treat U+212A as ``k``."""
    assert cr.resolve(f"delete tas{KELVIN} abc") is None


def test_resolve_zero_width_junk_is_equivalent_to_the_visible_text():
    """Hidden characters may be ignored, but never change which command runs."""
    rng = _rng(11)
    for turn, _ in _POSITIVE_TURNS:
        visible = cr.resolve(turn)
        for _ in range(30):
            at = rng.randrange(len(turn) + 1)
            hidden = turn[:at] + rng.choice(ZERO_WIDTH) + turn[at:]
            got = cr.resolve(hidden)
            if got is not None and visible is not None:
                assert got.split(" ")[0] == visible.split(" ")[0], "{}: {!r} -> {!r}".format(_info("resolve-zw"), hidden, got)
            if got is not None:
                assert visible is not None, "{}: hidden {!r} resolved but {!r} did not".format(_info("resolve-zw"), hidden, turn)


def test_resolve_fuzz_never_raises_slash_lines_are_none_output_is_one_catalogued_line(cat):
    rng = _rng(12)
    info = _info("resolve-fuzz")
    timings: list[float] = []
    fragments = ["delete task", "git merge", "forget", "sqlite_mutate", "elevate", "unload", "show", "status",
                 "the", "tool", "with", "sql=SELECT 1", ";", "/file_delete x", "\n", "\t", "\"", "'", "`", "$(x)",
                 "\u200b", KELVIN, "\u0435", "abc", "context size 8192", "switch to the coder model", "help for", "?"]
    hits = 0
    for _ in range(700):
        turn = " ".join(rng.choice(fragments) for _ in range(rng.randint(0, 8)))
        if rng.random() < 0.1:
            turn = "/" + turn
        got, _ = _call(cr.resolve, (), turn, timings, info)
        if turn.strip().startswith("/"):
            assert got is None, f"{info}: slash turn {turn!r} -> {got!r}"
        if got is not None:
            hits += 1
            assert got.startswith("/") and "\n" not in got and "\r" not in got, f"{info}: {turn!r} -> {got!r}"
            command = cc.by_name(got.split(" ")[0])
            assert command is not None, f"{info}: {turn!r} -> uncatalogued {got!r}"
            parsed, _error = _call(cc.parse_invocation, cc.InvocationError, got, timings, info)
            _check_head(cat, got, parsed, info)
    assert hits
    for value in (None, "", 0, "\x00" * 100):
        cr.resolve(value)
    _check_timings(timings, info)


def test_resolve_rule_output_is_one_invocation_even_with_smuggled_commands(cat):
    """A natural turn carrying ``; /b`` yields a line that parses as one /a call."""
    info = _info("resolve-smuggle")
    timings: list[float] = []
    for turn, expected in _POSITIVE_TURNS:
        for glue in ("; ", " && ", "\n", ' "x" ', " | ", " `"):
            for danger in ("/task_delete zz", "/git_merge main", "/file_delete a"):
                got, _ = _call(cr.resolve, (), turn + glue + danger, timings, info)
                if got is None:
                    continue
                parsed, _error = _call(cc.parse_invocation, cc.InvocationError, got, timings, info)
                if parsed is not None:
                    assert parsed[0] == cc.by_name(expected.split(" ")[0]).tool, f"{info}: {got!r}"
    _check_timings(timings, info)


# --- campaign / timeout ------------------------------------------------------

def test_parse_game_campaign_command_contract():
    rng = _rng(14)
    info = _info("campaign")
    timings: list[float] = []
    pool = ["", " ", "a", "name", "concept", "|", "||", " | ", "x|y", "\n", "\u200b", "\"", "/", "a|b|c|d|e|f", "é"]
    for _ in range(2500):
        arg = "".join(rng.choice(pool) for _ in range(rng.randint(0, 8)))
        result, _ = _call(_parse_game_campaign_command, (), arg, timings, info)
        parts = [p.strip() for p in arg.split("|", 3)]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            assert result is None, f"{info}: {arg!r} -> {result!r}"
            continue
        assert set(result) <= {"name", "concept", "language", "dimension"}
        assert result["name"] == parts[0] and result["concept"] == parts[1]
        assert "|" not in result["name"] and "|" not in result["concept"] and "|" not in result.get("language", "")
        assert result.get("language", "") == (parts[2] if len(parts) > 2 else "")
        assert result.get("dimension", "") == (parts[3] if len(parts) > 3 else "")
    for value in (None, 0, "", "a|b" * 5000, "|" * 10000):
        _call(_parse_game_campaign_command, (), value, timings, info)
    _check_timings(timings, info)


def test_game_campaign_extra_pipes_stay_inside_the_last_field():
    got = _parse_game_campaign_command("n|c|en|3d|/task_delete x|y")
    assert got == {"name": "n", "concept": "c", "language": "en", "dimension": "3d|/task_delete x|y"}


def test_parse_control_timeout_bounds_and_oracle():
    rng = _rng(15)
    info = _info("control-timeout")
    timings: list[float] = []
    seeds = ["", " ", "0", "1", "8", "59", "60", "61", "-1", "-0", "+5", " 7 ", "1e309", "nan", "inf", "-inf", "0x10",
             "5s", "5m", "1_000", "1__0", "٣", "9" * 4300, "9" * 4301, "9" * 5000, "-" + "9" * 5000, "7\n", "\u200b7",
             "7 8", "7.0", "99999999999999999999", "-99999999999999999999", "x", "/run"]
    inputs = list(seeds)
    for _ in range(3000):
        chars = list(rng.choice(seeds))
        for _ in range(rng.randint(1, 3)):
            chars.insert(rng.randrange(len(chars) + 1), rng.choice("0123456789 -+_.eEs\n\x00\u200b٣nNiIaA"))
        inputs.append("".join(chars))
    for command in ("/run", "/x"):
        for arg in inputs:
            got, _ = _call(lambda a, c=command: parse_control_timeout(a, c), (), arg, timings, info)
            timeout, error = got
            text = arg.strip()
            if not text:
                assert got == (DEFAULT_TIMEOUT, None)
            elif _INT_RE.fullmatch(text) and len(re.sub(r"\D", "", text)) <= sys.get_int_max_str_digits():
                value = int(text.replace("_", ""))
                assert got == (max(1, min(value, MAX_TIMEOUT)), None), f"{info}: {arg!r} -> {got!r}"
            else:
                assert timeout is None and error == f"usage: {command} [seconds]  (runs the previous fenced code block)"
            if timeout is not None:
                assert 1 <= timeout <= MAX_TIMEOUT and isinstance(timeout, int)
    assert parse_control_timeout(None) == (DEFAULT_TIMEOUT, None)
    _check_timings(timings, info)


# --- selectors ---------------------------------------------------------------

_RUNNERS = sorted(RUNNER_SELECTOR_KINDS)


def _parser(runner):
    return lambda raw: parse_selector(runner, raw)

_SELECTOR_SEEDS = {
    "pytest": ["tests/test_a.py", "tests/test_a.py::Test::test_b", "tests/test_a.py::test_b[x-1,2]", "k:not slow and fast",
               "k:a or b", "a.py"],
    "unittest": ["pkg.mod.Case.test_x", "a"],
    "ctest": ["unit_*", "my-test.v1", "a:b"],
    "cargo": ["tests::foo", "a_b"],
    "go": ["./pkg/...", "./a/b", "run:TestX/sub", "run:TestY"],
    "dotnet": ["My.Ns.Tests", "A"],
    "npm": ["src/a.test.js"], "pnpm": ["src/a.test.js"], "yarn": ["a/b.js"],
    "gradle": ["com.x.Tests*", "*Test"],
    "maven": ["MyTest#m+n", "*Test"],
    "make": ["x"],
}
_HOSTILE = list(";&|$`<>(){}\"'\\!\n\r\x00 \t~*?[]#%^=,") + ["-", "--", "..", "/..", "../", "-k", "--rootdir=/", "$(x)",
                                                              "\u200b", "\u00e9", "\u0430", "\uff0f", "\u2028", "::"]
# Characters that no grammar accepts anywhere (``*`` and ``#`` are gradle/maven
# globs, ``[]`` pytest params, ``=``/``,`` are not shell syntax but excluded).
_SHELL_AND_INJECTION = set(";&|$`<>(){}\"'\\!\n\r\x00\t~?")
_FIXED_FLAGS = {"-k", "-R", "-run", "--filter", "--tests", "-Dsurefire.failIfNoSpecifiedTests=false"}


def _selector_inputs(rng, runner, count):
    seeds = _SELECTOR_SEEDS[runner]
    yield from seeds
    for _ in range(count):
        chars = list(rng.choice(seeds))
        for _ in range(rng.randint(1, 3)):
            op = rng.randrange(4)
            at = rng.randrange(len(chars) + 1)
            if op == 0:
                chars.insert(at, rng.choice(_HOSTILE))
            elif op == 1 and chars:
                del chars[min(at, len(chars) - 1)]
            elif op == 2:
                chars[at:at] = list(rng.choice(["x" * rng.randint(1, 260), "/..", "../", "/" * 3]))
            else:
                chars[at:at] = list(rng.choice(["-", "k:", "run:", "./", "/", "C:", "\\"]))
        yield "".join(chars)


def _assert_selector_safe(runner, raw, selector, info):
    where = f"{info}: parse_selector({runner!r}, {raw!r})"
    assert not raw.startswith("-"), where
    assert len(raw) <= MAX_SELECTOR_CHARS, where
    assert not (set(raw) & _SHELL_AND_INJECTION), where
    assert all(ord(ch) >= 32 and ord(ch) != 127 for ch in raw), where
    keyword_kind = runner == "pytest" and raw.startswith("k:")
    # a keyword expression is normalised (Unicode whitespace folds to one space); the value is what is passed
    assert (selector.value if keyword_kind else raw).isascii(), where
    if not (runner == "pytest" and raw.startswith("k:")):
        assert not re.search(r"\s", raw), where
    assert selector.argv and all(isinstance(item, str) and item for item in selector.argv), where
    for item in selector.argv:
        if item in _FIXED_FLAGS:
            continue
        if item.startswith("-Dtest="):
            assert item == "-Dtest=" + selector.value, where
            continue
        assert not item.startswith("-"), f"{where} -> argv element {item!r} reads as a flag"
        assert "\n" not in item and "\x00" not in item, where
    if selector.kind in (SelectorKind.PYTEST_NODE, SelectorKind.JS_PATH, SelectorKind.GO_PACKAGE):
        path = selector.path
        assert path and not path.startswith("/") and "\\" not in path and not re.match(r"^[A-Za-z]:", path), where
        assert ".." not in path.split("/"), where
    if selector.kind is SelectorKind.PYTEST_KEYWORD:
        words = selector.value.split(" ")
        assert words[-1] not in ("and", "or", "not") and selector.argv == ("-k", selector.value), where
        assert all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", w) or w in ("and", "or", "not") for w in words), where


@pytest.mark.parametrize("runner", _RUNNERS)
def test_parse_selector_fuzz_typed_error_and_argv_safety(runner):
    rng = random.Random(f"{SEED}:{runner}")
    info = _info("selector-" + runner)
    timings: list[float] = []
    accepted = rejected = 0
    for raw in _selector_inputs(rng, runner, 700):
        selector, error = _call(_parser(runner), SelectorRejected, raw, timings, info)
        if error is not None:
            rejected += 1
            assert error.code in (INVALID_SELECTOR, SELECTOR_UNSUPPORTED, SELECTOR_ESCAPES_PROJECT), info
            continue
        accepted += 1
        assert selector.value and selector.kind in RUNNER_SELECTOR_KINDS[runner], info
        _assert_selector_safe(runner, raw, selector, info)
    assert rejected
    if RUNNER_SELECTOR_KINDS[runner]:
        assert accepted, f"{info}: no seed was accepted, the oracle never ran"
    _check_timings(timings, info)


def test_parse_selector_non_text_runner_and_huge_inputs_are_typed_refusals():
    timings: list[float] = []
    info = _info("selector-types")
    for runner in (None, 5, object(), "", "PYTEST", "pytest ", "make", "nope"):
        for raw in ("a.py", "", None, 7, b"a.py", ["a.py"]):
            _, error = _call(_parser(runner), SelectorRejected, raw, timings, info)
            assert error is not None, f"{info}: {runner!r} {raw!r} accepted"
    for runner in _RUNNERS:
        for raw in ("a" * 10000, "/" * 10000, "a/" * 5000, "a " * 5000, "k:" + "a and " * 3000, "-" * 10000,
                    "." * 10000, "[" * 10000, "::" * 5000):
            _call(_parser(runner), SelectorRejected, raw, timings, info)
    _check_timings(timings, info)


def test_parse_selector_known_positive_hostile_plants_are_always_rejected():
    """Plant a flag, shell syntax, or a parent segment inside valid context."""
    rng = _rng(16)
    info = _info("selector-plant")
    timings: list[float] = []
    for runner in (r for r in _RUNNERS if RUNNER_SELECTOR_KINDS[r]):
        for seed in _SELECTOR_SEEDS[runner]:
            assert parse_selector(runner, seed)
            for _ in range(60):
                bad = rng.choice(list(_SHELL_AND_INJECTION - {"\t"}) + ["\t", " && x", "$(x)", "`x`", "|x", ";x"])
                at = rng.randrange(len(seed) + 1)
                _, error = _call(_parser(runner), SelectorRejected, seed[:at] + bad + seed[at:],
                                 timings, info)
                assert error is not None, f"{info}: planted {bad!r} in {seed!r} for {runner} accepted"
                _, error = _call(_parser(runner), SelectorRejected, "-" + seed, timings, info)
                assert error is not None and error.code == INVALID_SELECTOR
                _, error = _call(_parser(runner), SelectorRejected, seed + "\n", timings, info)
                assert error is not None
    for runner, seed in (("pytest", "tests/a.py"), ("npm", "src/a.js"), ("pnpm", "src/a.js"), ("yarn", "a/b.js")):
        for planted in ("../" + seed, "x/../" + seed, seed + "/..", "a/b/../../../" + seed, "/" + seed,
                        "C:" + seed, "a\\b", "x/../../y.py"):
            _, error = _call(_parser(runner), SelectorRejected, planted, timings, info)
            assert error is not None and error.code == SELECTOR_ESCAPES_PROJECT, f"{info}: {runner} {planted!r}"
    for planted in ("./../x", "./a/../b", "./..", "./a/.."):
        _, error = _call(_parser("go"), SelectorRejected, planted, timings, info)
        assert error is not None and error.code == SELECTOR_ESCAPES_PROJECT, planted
    _check_timings(timings, info)


@pytest.mark.parametrize("runner,raw,argv", [
    ("pytest", "k:a  and   b", ("-k", "a and b")),
    ("ctest", "unit_*", ("-R", "^unit_")),
    ("ctest", "a.b", ("-R", "^a\\.b$")),
    ("go", "run:TestX/sub", ("-run", "^TestX$/^sub$")),
    ("dotnet", "A.B", ("--filter", "FullyQualifiedName~A.B")),
    ("maven", "T#m", ("-Dtest=T#m", "-Dsurefire.failIfNoSpecifiedTests=false")),
    ("gradle", "*T", ("--tests", "*T")),
])
def test_parse_selector_argv_is_exactly_the_documented_elements(runner, raw, argv):
    assert parse_selector(runner, raw).argv == argv


# --- weather location --------------------------------------------------------

_LOC_SEEDS = ["Paris", "the Paris, today", "  New York  ", "the the x", "the .. x", "here", "the area", "x", "ab",
              "A" * 119, "A" * 121, "Austin, TX tonight", "Paris today today", "?!.,;: Rome :;,.!?", "the ", "the\tthe\nx",
              "San Jose right now", "a\x01b", "\u200bParis", "Z\u00fcrich", "THE THE THE x"]
_LOC_CHARS = "ab the\t\n\r\x01\x00?!.,;: \u200b\u00e9todaytomorrowtonight"


def _loc_inputs(rng, count):
    yield from _LOC_SEEDS
    for _ in range(count):
        chars = list(rng.choice(_LOC_SEEDS))
        for _ in range(rng.randint(1, 4)):
            op = rng.randrange(3)
            at = rng.randrange(len(chars) + 1)
            if op == 0:
                chars.insert(at, rng.choice(_LOC_CHARS))
            elif op == 1 and chars:
                del chars[min(at, len(chars) - 1)]
            else:
                chars[at:at] = list(rng.choice(["the ", " today", " the the ", "  ", "." * 5, "x" * 90]))
        yield "".join(chars)


def test_clean_location_never_raises_bounded_and_control_free():
    rng = _rng(17)
    info = _info("clean-location")
    timings: list[float] = []
    for value in list(_loc_inputs(rng, 4000)) + [None, 0, "", " ", "x" * 10000, " " * 10000, "the " * 3000, "." * 10000]:
        got, _ = _call(wi._clean_location, (), value, timings, info)
        assert isinstance(got, str)
        if got:
            assert 2 <= len(got) <= 120 and not any(ord(ch) < 32 for ch in got), f"{info}: {value!r} -> {got!r}"
            assert len(got) <= len(str(value)), f"{info}: output grew for {value!r}"
    _check_timings(timings, info)


def test_clean_location_is_idempotent():
    """A cleaned location is clean: running it again changes nothing.

    ``_previous_weather_location`` and ``_plausible_location_reply`` feed an
    already-extracted place back through the cleaner, so a second pass that
    erases or trims it again yields a different place than the first.
    """
    rng = _rng(18)
    info = _info("clean-location-idem")
    failures = []
    for value in _loc_inputs(rng, 3000):
        once = wi._clean_location(value)
        twice = wi._clean_location(once)
        if once and twice != once:
            failures.append((value, once, twice))
    assert not failures, (
        f"{info}: not idempotent, first of {len(failures)}: input={failures[0][0]!r} "
        f"once={failures[0][1]!r} twice={failures[0][2]!r}")


def test_clean_location_the_the_x_regression():
    once = wi._clean_location("the the x")
    assert wi._clean_location(once) == once


_WEATHER_TEMPLATES = ["what's the weather in %s", "weather in %s", "weather in %s today", "%s weather",
                      "forecast for %s", "how is the weather in %s?", "what is the forecast for %s tonight"]
_PLACES = ["Paris", "Austin, TX", "New York", "Seattle"]


def test_extract_weather_location_never_raises_bounded_and_planted_place_is_found():
    rng = _rng(19)
    info = _info("weather")
    timings: list[float] = []
    inputs = list(_loc_inputs(rng, 1500))
    inputs += ["weather in " + v for v in list(_loc_inputs(rng, 600))]
    inputs += [f"how much rain will {v} get" for v in list(_loc_inputs(rng, 300))]
    inputs += ["weather " + "in " * 3000, "weather in " + "x " * 5000, "in " * 5000 + "weather", "/" * 10000,
               '"' * 10000, " " * 10000, "what is the " + "a " * 4000 + "weather", "12345 " * 2000, None, 0]
    for text in inputs:
        got, _ = _call(wi.extract_weather_location, (), text, timings, info)
        assert isinstance(got, str)
        source = str(text or "")
        assert len(got) <= min(120, len(source)), f"{info}: {text!r} -> {got!r}"
        assert not any(ord(ch) < 32 for ch in got), f"{info}: {text!r} -> {got!r}"
        assert set(got) <= set(source) | {" "}, f"{info}: {text!r} -> {got!r} invents characters"
    for _ in range(300):
        place = rng.choice(_PLACES)
        template = rng.choice(_WEATHER_TEMPLATES)
        turn = rng.choice(["", " ", "\n"]) + template % place + rng.choice(["", " "])
        got, _ = _call(wi.extract_weather_location, (), turn, timings, info)
        assert got == place, f"{info}: {turn!r} -> {got!r} (wanted {place!r})"
    for zipcode in ("90210", "90210-1234", "02134"):
        assert wi.extract_weather_location("weather " + zipcode) == zipcode
    _check_timings(timings, info)


# --- regression and time-budget pins ----------------------------------------

def test_pytest_selector_with_trailing_parent_segment_before_node_id_is_refused():
    """Regression: ``..`` as the last path segment before ``::`` slips the ``..`` check.

    ``_has_parent_segment`` splits the whole raw selector on ``/``, so the last
    segment is ``..::Test::test_b`` and is not equal to ``..``; ``.path`` (what
    the adapter resolves under the project root) is ``tests/test_a.py/..``.
    """
    for raw in ("tests/test_a.py/..::Test::test_b", "tests/..::T", "a/..[x]"):
        with pytest.raises(SelectorRejected) as caught:
            parse_selector("pytest", raw)
        assert caught.value.code == SELECTOR_ESCAPES_PROJECT, raw


def test_long_hostile_inputs_stay_inside_the_time_budget(cat):
    """10k-character runs of ``/``, ``"``, spaces and key=value noise: 2 s hard cap each."""
    cc.catalog()  # the cold catalog build (seconds) is not what the cap measures
    cr.resolve("help")
    info = _info("long-inputs")
    long_inputs = [
        "/" * 10000, '"' * 10000, " " * 10000, "'" * 10000, "\\" * 10000, "\n" * 10000, "=" * 10000,
        "/file_read " + '"' * 10000, "/file_read " + '" ' * 5000, "/file_read " + "/ " * 5000,
        "/file_read " + 'a="' * 3000, "/file_read " + "a=b " * 2500, "/file_read " + "a=" * 5000,
        "/" + "a" * 10000, "/file_read " + "x" * 10000, "/file_read " + (chr(92) + " ") * 5000,
        "/file_read " + 'path="' + "\\\"" * 4000, "/file_read x" + ";/file_read x" * 800,
        "delete task " + "a " * 5000, "x " * 5000, "weather in " + "the " * 2500, "/ " * 5000,
    ]
    timings: list[float] = []
    for text in long_inputs:
        _call(cc.parse_invocation, cc.InvocationError, text, timings, info)
        _call(cr.resolve, (), text, timings, info)
        _call(cc.near_misses, (), text, timings, info)
        _call(wi.extract_weather_location, (), text, timings, info)
        _call(wi._clean_location, (), text, timings, info)
        _call(_parse_game_campaign_command, (), text, timings, info)
        _call(parse_control_timeout, (), text, timings, info)
        for kind in ("int", "num", "bool", "str"):
            _call(lambda t, k=kind: cc._coerce(t, k), (), text, timings, info)
        for runner in _RUNNERS:
            _call(_parser(runner), SelectorRejected, text, timings, info)
    assert max(timings) < 2.0


# --- non-ASCII free-data captures ---------------------------------------------

@pytest.mark.parametrize("line, want", [
    ("weather Zürich", "/weather Zürich"),
    ("forecast Málaga", "/weather Málaga"),
    ("weather São Paulo", "/weather São Paulo"),
    ("weather Montréal", "/weather Montréal"),
    ("create a new rust project named café", "/scaffold rust café"),
    ("switch to the café tier", "/model café"),
    ("read notes.téxt", "/read notes.téxt"),
])
def test_non_ascii_free_data_captures_resolve_as_on_base(line, want):
    assert cr.resolve(line) == want


def test_slash_head_with_a_kelvin_sign_does_not_dispatch_work_over_http(monkeypatch):
    from sonder_runtime.interfaces.http import serve

    seen = []
    monkeypatch.setattr(serve, "_http_slash_refusal",
                        lambda cmd, arg, **kw: seen.append(cmd) or "REFUSED")
    plain = serve._handle_slash("/work x", context=None)
    assert seen[-1] == "/work" and plain == "REFUSED"
    serve._handle_slash("/worK x", context=None)
    assert seen[-1] == "/worK"


def test_clean_location_strips_repeated_articles_in_one_call_and_is_idempotent():
    assert wi._clean_location("the " * 12 + "Paris") == "Paris"
    for value in ("the " * 12 + "Paris", "the the x", "Paris tomorrow", "  the The Paris?! "):
        once = wi._clean_location(value)
        assert wi._clean_location(once) == once, value


# --- fold characters: one rule at the router entry and at the slash choke points ---

LONG_S, DOTTED_I, DOTLESS_I = "\u017f", "\u0130", "\u0131"
_ARGUMENT_EDGES = "?!,;:\"'`()[]{}<>"


def test_fold_characters_are_exactly_the_code_points_python_folds_into_ascii():
    """The entry rule is complete only if this list is, so enumerate every code point.

    A character is a fold character when ``str.lower()`` turns it into text
    holding an ASCII letter or digit, or IGNORECASE matches it against one.
    """
    ascii_word = re.compile(r"[a-z0-9]", re.IGNORECASE)
    found = set()
    for point in range(0x80, sys.maxunicode + 1):
        char = chr(point)
        if ascii_word.fullmatch(char) or any(c.isascii() and c.isalnum() for c in char.lower()):
            found.add(char)
    assert found == set(ct.FOLD_CHARACTERS) == {KELVIN, LONG_S, DOTTED_I, DOTLESS_I}


def test_opaque_reading_has_no_ascii_meaning_and_maps_back():
    typed = f"weather in Diyarbak{DOTLESS_I}r, {DOTTED_I}zmir 4{KELVIN} {LONG_S}"
    opaque = ct.opaque_reading(typed)
    assert opaque is not None and not ct.has_fold_character(opaque) and len(opaque) == len(typed)
    assert ct.restore_fold_characters(opaque) == typed
    assert ct.ascii_reading(typed) == "weather in Diyarbakir, Izmir 4K s"
    stand_ins = set(opaque) - set(typed)
    assert len(stand_ins) == 4
    for char in stand_ins:
        for changed in (char.lower(), char.upper(), char.casefold(), char.title(),
                        unicodedata.normalize("NFKC", char), unicodedata.normalize("NFKD", char)):
            assert changed == char
        assert re.fullmatch(r"\w", char) and not re.fullmatch(r"[a-z0-9_]", char, re.IGNORECASE)
    # A turn that already holds a stand-in could not be mapped back, so it has no opaque reading.
    assert ct.opaque_reading(opaque) is None
    assert cr.resolve(f"weather in {sorted(stand_ins)[0]}x {DOTLESS_I}") is None
    assert not ct.has_fold_character("weather in Z\u00fcrich") and ct.has_fold_character(f"x{LONG_S}")


@pytest.mark.parametrize("line, want", [
    ("/Work", "/work"), ("/WORK fix it", "/work"), ("  /Task_Delete x", "/task_delete"), ("", ""), (None, ""), ("   ", ""),
    ("/wor\u212a", "/wor\u212a"), ("/wor\u212a x", "/wor\u212a"), ("/\u017fTATS", "/\u017ftats"), ("/STRA\u00dfE", "/stra\u00dfe"),
    ("/\uff37ork", "/\uff37ork"),
])
def test_slash_head_lowercases_ascii_letters_and_nothing_else(line, want):
    assert ct.slash_head(line) == want
    assert ct.ascii_lower("Sess\u0130ON \u212aEEP \u00c9") == "sess\u0130on \u212aeep \u00c9"


@pytest.mark.parametrize("folded, want", [
    # hand-written rules (the review's list)
    ("ma\u212ae a new rust project", "/scaffold rust NewProject"),
    ("\u017ftart a new rust project", "/scaffold rust NewProject"),
    ("create a new rust con\u017fole project", "/scaffold rust NewProject"),
    ("\u017felect the x model", "/model x"),
    ("\u017fet the x model", "/model x"),
    ("u\u017fe the x model", "/model x"),
    ("foreca\u017ft Z\u00fcrich", "/weather Z\u00fcrich"),
    ("\u017fwitch to the x tier", "/model x"),
    ("switch to the x t\u0131er", "/model x"),
    ("switch to the x t\u0130er", "/model x"),
    # the tier stage (intents.classify_command)
    ("\u0131mprove parse in parser.py", "/refactor parser.py parse"),
    ("\u017fecond opinion on the parser", "/consult the parser"),
    ("wh\u0131ch model \u017fhould handle the parser", "/route the parser"),
    # the structured form and the catalog stage
    ("u\u017fe the status tool", "/status"),
    ("delete tas\u212a abc", "/task_delete abc"),
    ("\u017fhow context health", "/context"),
    # an ASCII-only capture is a keyword position too
    ("run saved workflow \u212aeep", "/workflow_run keep"),
    ("show task \u212aabc", "/task_show Kabc"),
    ("weather \u0130stanbul", "/weather Istanbul"),
    # same command, different argument: the keyword region holds the fold character
    ("weather \u0131n Chicago", "/weather Chicago"),
    ("resume sess\u0131on abc123", "/resume abc123"),
])
def test_a_fold_character_never_spells_a_keyword(folded, want):
    """Every line here resolved before the fix because a fold character spelled a keyword.

    The first assertion keeps the pin honest: the same line with the ASCII
    letter in place is a real command, so None below is the refusal and not a
    phrase that never resolved.
    """
    assert cr.resolve(ct.ascii_reading(folded)) == want
    assert cr.resolve(folded) is None
    report = cr.explain(folded)
    assert report["resolved"] is None and report["source"] == "none"
    assert report["detail"].get("reason") == "fold-character"


@pytest.mark.parametrize("line, want", [
    ("weather in \u0130stanbul", "/weather \u0130stanbul"),
    ("weather in Diyarbak\u0131r", "/weather Diyarbak\u0131r"),
    ("forecast for \u0130zmir", "/weather \u0130zmir"),
    ("remember that helium boils at 4 \u212a", "/fact helium boils at 4 \u212a"),
    ("second opinion on the Diyarbak\u0131r migration plan", "/consult the Diyarbak\u0131r migration plan"),
    ("use the \u212aimi model", "/model \u212aimi"),
    ("read \u017f.txt", "/read \u017f.txt"),
    ("create a new rust project named \u212ax", "/scaffold rust \u212ax"),
])
def test_a_fold_character_typed_as_data_reaches_the_argument_unchanged(line, want):
    assert cr.resolve(line) == want
    report = cr.explain(line)
    assert report["resolved"] == want and report["source"] in ("rule", "tier")


def test_fold_character_substitutions_never_change_the_command_or_drop_what_was_typed():
    """Swap letters for their fold characters anywhere in turns that resolve.

    The turn either refuses or resolves to exactly what its ASCII spelling
    resolves to, carrying every fold character the user typed.
    """
    rng = _rng(31)
    info = _info("fold-substitution")
    swaps = {"k": KELVIN, "K": KELVIN, "s": LONG_S, "i": DOTLESS_I, "I": DOTTED_I}
    turns = [turn for turn, _ in _POSITIVE_TURNS] + [
        "weather in Kiskunhalas", "forecast for Minsk", "remember that Kirk likes skiing", "resume session kiss123",
        "second opinion on the parser", "which model should handle the risk report", "improve parse in parser.py",
        "show task k1", "delete task kiwi", "use the kimi model", "read notes.txt", "list sessions", "show my stats",
        "create a new rust project named kiosk", "switch to the fast tier", "search the web for skis in Minsk",
    ]
    refused = kept = 0
    for turn in turns:
        places = [index for index, char in enumerate(turn) if char in swaps]
        for _ in range(60):
            chosen = set(rng.sample(places, rng.randint(1, min(3, len(places))))) if places else set()
            variant = "".join(swaps[char] if index in chosen else char for index, char in enumerate(turn))
            if variant == turn:
                continue
            got = cr.resolve(variant)
            if got is None:
                refused += 1
                continue
            kept += 1
            assert ct.ascii_reading(got) == cr.resolve(ct.ascii_reading(variant)), f"{info}: {variant!r} -> {got!r}"
            for char in ct.FOLD_CHARACTERS:
                assert got.count(char) == variant.count(char), f"{info}: {variant!r} -> {got!r}"
            assert got.split(" ")[0].isascii(), f"{info}: {variant!r} -> {got!r}"
    assert refused > 200 and kept > 20, (refused, kept)


@pytest.mark.parametrize("line", [
    # fold characters next to the argument: the branch had started passing "abc", "ab" or nothing
    "delete task \u212aabc", "delete task abc\u212a", "delete task ab\u0130", "delete task \u212a", "show task \u212aabc",
    # any other letter the ASCII tokenizer would drop (before: /task_delete caf)
    "delete task caf\u00e9", "delete task \u00e1b\u00e7", "compare the workspaces caf\u00e9", "git merge \u00e9",
    "run the test s\u00fc\u00edt\u00e9", "delete task abc\u200b", "delete task \u0661\u0662",
    # an ASCII word the tokenizer would have to trim (before: abc, env, 12)
    "delete task task/abc", "delete task .env", "delete task #12", "delete task -abc",
])
def test_catalog_stage_refuses_rather_than_alter_an_argument(line):
    assert cr.resolve(line) is None
    assert cr.explain(line)["source"] == "none"


@pytest.mark.parametrize("line, want", [
    ("delete task abc", "/task_delete abc"),
    ("delete task abc?", "/task_delete abc"),
    ("delete task \"abc\"", "/task_delete abc"),
    ("i want to delete task abc!", "/task_delete abc"),
    ("compare the workspaces cafe", "/workspace_compare cafe"),
    ("inspect artifactcheck report.json", "/artifactcheck report.json"),
    ("what\u2019s the repo status", "/repo_status"),
])
def test_catalog_stage_still_passes_a_typed_word_and_reads_past_punctuation(line, want):
    assert cr.resolve(line) == want
    assert cr.explain(line)["source"] == "catalog"


def test_catalog_arguments_are_whole_words_the_user_typed():
    rng = _rng(32)
    info = _info("catalog-argument")
    stems = ["delete task", "show task", "delete the task", "i want to delete task", "compare the workspaces",
             "git merge", "inspect artifactcheck", "get weather in", "show the task progress"]
    alphabet = _ARGUMENT_EDGES + "abcXYZ019_.-/#@$~*=+%&|^ " + "\u00e9\u00e7\u00fc\u212a\u0131\u0130\u017f\u200b\u00a0\u2019\u20ac\u0661"
    hits = with_argument = 0
    for _ in range(6000):
        argument = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 7)))
        turn = (rng.choice(stems) + " " + argument).strip()
        report = cr.explain(turn)
        got = report["resolved"]
        assert got == cr.resolve(turn)
        if got is None or report["source"] != "catalog":
            continue
        hits += 1
        assert all(ord(c) < 128 or unicodedata.category(c)[0] in "PSZ" for c in turn), f"{info}: {turn!r} -> {got!r}"
        typed = set(turn.split()) | {word.strip(_ARGUMENT_EDGES) for word in turn.split()}
        for word in got.split(" ")[1:]:
            with_argument += 1
            assert word in typed, f"{info}: {turn!r} -> {got!r}"
    assert hits > 100 and with_argument > 20, (hits, with_argument)


# --- slash heads: every surface compares the head the same ASCII-only way -----

def test_control_command_does_not_fold_a_kelvin_sign_into_work(monkeypatch):
    """The chat path reaches control_command with the raw prompt and no HTTP role context."""
    import server

    calls, gated = [], []
    monkeypatch.setattr(server, "workbench_agent", lambda *args, **kwargs: calls.append((args, kwargs)) or "WORKBENCH-RAN")
    monkeypatch.setattr(server, "_control_tool_refusal",
                        lambda tools, label, **kwargs: gated.append((tuple(tools), label)) or "")
    assert "WORKBENCH-RAN" in str(server.control_command("/WORK fix the parser"))
    assert len(calls) == 1 and gated[-1][1] == "/work" and gated[-1][0]
    assert server.control_command("/wor\u212a fix the parser") is None
    assert len(calls) == 1
    assert gated[-1] == ((), "/wor\u212a")


def test_http_gates_see_the_typed_head():
    from sonder_runtime.interfaces.http import serve

    assert serve._dangerous_http_slash("/work x") and serve._dangerous_http_slash("/WORK x")
    assert not serve._dangerous_http_slash("/wor\u212a x")
    # Names gated by membership alone; the action-gated ones (/updatecheck) answer by their argument.
    by_name = serve.DANGEROUS_HTTP_SLASH_COMMANDS - serve._CONDITIONALLY_GATED_SLASH
    with_k = sorted(name for name in by_name if "k" in name)
    assert len(with_k) >= 3
    for name in with_k:
        assert serve._dangerous_http_slash(name + " apply")
        assert not serve._dangerous_http_slash(name.replace("k", KELVIN) + " apply"), name
    assert serve._slash_system_operation("/RUNTIME", "set x") == "runtime_policy_change"
    assert serve._slash_system_operation("/runtime\u0130", "set x") == ""


def test_catalog_branch_narrowing_does_not_fold_a_kelvin_sign():
    union = cc.console_tools().get("/task", ())
    assert len(union) > 1
    assert tuple(cc.narrow_branch_tools("/TASK", "list", union)) == ("task_list",)
    assert tuple(cc.narrow_branch_tools(f"/tas{KELVIN}", "list", union)) == tuple(union)
    assert f"unknown command /wor{KELVIN}" in cc.unknown_command(f"/wor{KELVIN}")


def test_repl_surfaces_treat_a_non_ascii_head_as_unknown(monkeypatch):
    import sonder_runtime.interfaces.repl.repl as repl

    assert repl._run_catalogued(f"/wor{KELVIN} x", f"/wor{KELVIN}").startswith(f"unknown command /wor{KELVIN}")
    assert repl._repl_autopilot_owner("/MISSION", "start") == repl._repl_autopilot_owner("/mission", "start")
    assert repl._history_safe("/LOGIN alice hunter2") is False and repl._history_safe("/stats") is True

    looked_up = []

    class _Tools(dict):
        def get(self, key, default=None):
            looked_up.append(key)
            return default

    monkeypatch.setattr(repl.command_catalog, "console_tools", lambda: _Tools())
    assert repl._help_policy_note("/WORK") == "" and looked_up == ["/work"]
    assert repl._help_policy_note(f"/wor{KELVIN} x") == "" and looked_up[-1] == f"/wor{KELVIN}"


def test_control_intents_do_not_read_a_kelvin_sign_as_k():
    """``intents.classify`` lowercases the turn; U+212A used to come out as "k"."""
    assert intents.classify("show me your thinking") == {"trace": True}
    assert intents.classify("Show Me Your THINKING") == {"trace": True}
    assert intents.classify(f"show me your thin{KELVIN}ing") == {}
    assert intents.classify(f"STRICT ON for the {DOTTED_I}zmir report") == intents.classify("strict on for the x report")


_HEAD_FOLD_SOURCES = (
    "runtime/server.py",
    "runtime/intents.py",
    "sonder_runtime/application/server_core/control_commands.py",
    "sonder_runtime/application/server_core/slash_commands.py",
    "sonder_runtime/interfaces/http/serve.py",
    "sonder_runtime/interfaces/http/authority_contract.py",
    "sonder_runtime/interfaces/http/facades/account_routes.py",
    "sonder_runtime/interfaces/http/facades/developer_chat.py",
    "sonder_runtime/interfaces/http/facades/read_routes.py",
    "sonder_runtime/interfaces/http/facades/slash_helpers.py",
    "sonder_runtime/interfaces/repl/repl.py",
    "sonder_runtime/interfaces/repl/handler.py",
    "sonder_runtime/interfaces/repl/command_router.py",
    "sonder_runtime/interfaces/repl/facades/build_tools.py",
    "sonder_runtime/adapters/command_catalog.py",
    "sonder_runtime/adapters/command_parsing.py",
    "sonder_runtime/adapters/tool_lane_guard.py",
)
# The first word of a line, or a ``cmd``/``command`` value, folded with a Unicode-wide method.
_HEAD_FOLD = re.compile(
    r"""(?: \b(?:parts|pieces|name|command)\[0\] (?:\.lstrip\("/"\))?
          | \bstr\((?:cmd|command)\b[^)]*\) (?:\.strip\(\))?
          | \b(?:cmd|command)
          | \.split\(None,\ 1\)\[0\] )
        \.(?:lower|casefold|upper)\(\)""",
    re.VERBOSE,
)
# Folds of a word that is not a command name; each is compared with ASCII keywords only.
_NOT_A_HEAD = {
    # Not ASCII-only for /stash: this gate folds with lower() while the server
    # dispatcher (server.py control_command) uses casefold() and "_" -> "-", so
    # "/stash save_untracked" reads as non-dangerous here. Pre-existing on main;
    # the admin binding of runtime_source_stash still refuses non-admins.
    ("sonder_runtime/interfaces/http/authority_contract.py", "action = parts[0].lower() if parts else \"\""):
        "sub-action of /runtime, /update and /stash (see the /stash note above)",
    ("sonder_runtime/interfaces/repl/command_router.py", "if _norm_token(parts[0].lower()) not in _ARG_FILLERS:"):
        "filler word before a one-word argument, compared with ASCII fillers",
}


def test_no_surface_folds_a_slash_head_with_a_unicode_method():
    """New dispatch code must use ``command_text.slash_head``, not ``.lower()``.

    ``"/wor" + KELVIN`` lowercases to ``"/work"``. A head folded that way on
    one surface and not on another is how a gate and a dispatcher come to
    disagree, so the raw fold is refused here wherever it appears.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    found = set()
    scanned = 0
    for relative in _HEAD_FOLD_SOURCES:
        with open(os.path.join(root, relative), encoding="utf-8") as handle:
            for line in handle:
                scanned += 1
                if _HEAD_FOLD.search(line):
                    found.add((relative, line.strip()))
    assert scanned > 40000
    for raw in ("    cmd = parts[0].lower()", "x = str(cmd or \"\").strip().lower()", "c = value.split(None, 1)[0].lower()",
                "if str(command or \"\").casefold() != \"/model\":", "cmd = \"/\" + name[0].lstrip(\"/\").lower()",
                "cmd = parts[0].lower() if parts[0].isascii() else parts[0]", "command = pieces[0].upper()"):
        assert _HEAD_FOLD.search(raw), raw
    assert not _HEAD_FOLD.search("cmd = _slash_head(parts[0])")
    unexpected = sorted(found - set(_NOT_A_HEAD))
    assert not unexpected, "fold the head with command_text.slash_head instead: %r" % (unexpected,)
    assert set(_NOT_A_HEAD) <= found, "stale exemption: %r" % (sorted(set(_NOT_A_HEAD) - found),)


# --- _clean_location: one pass, same answers as the fixpoint definition -------

_LONG_TAIL = " I am flying out next month for a conference and want to know what to pack, " * 7


@pytest.mark.parametrize("text, place", [
    ("weather in Paris today," + _LONG_TAIL, "Paris"),
    ("what's the weather in Paris tomorrow?" + _LONG_TAIL, "Paris"),
    ("weather for Austin, TX right now" + _LONG_TAIL, "Austin, TX"),
])
def test_a_long_message_still_reduces_to_its_place(text, place):
    """A length cap on the input lost these: the time word cuts the tail, however long it is."""
    assert len(text) > 512
    assert wi.extract_weather_location(text) == place
    assert wi.classify(text)["location"] == place
    assert wi._clean_location(place + " today," + _LONG_TAIL) == place


def _clean_location_by_fixpoint(value):
    """The definition: cut the time phrase and peel one article until nothing changes."""
    edges = " \t\r\n?!.,;:"
    location = re.sub(r"\s+", " ", str(value or "")).strip(edges)
    lowered = location.lower()
    while True:
        previous = location
        location = wi._TRAILING_TIME.sub("", location).strip(edges)
        lowered = location.lower()
        if lowered.startswith("the ") and lowered not in wi._LOCATION_PLACEHOLDERS:
            location = location[4:].strip()
            lowered = location.lower()
        if location == previous:
            break
    if not 2 <= len(location) <= 120 or lowered in wi._LOCATION_PLACEHOLDERS or any(ord(ch) < 32 for ch in location):
        return ""
    return location


_ARTICLE_SEEDS = ["the the area", "the . the area", "The Area today", "the my area", "the near me tomorrow", "the . . Paris ?",
                  "the here", "the the", "the today", "today the x", "the x today the y", "th\u0130 x", "the\u00a0x",
                  "the this week x", "x this weekend y", "x right now", "the outside", "THE the The Paris"]


def test_clean_location_equals_the_fixpoint_definition_and_is_idempotent():
    rng = _rng(33)
    info = _info("clean-location-fixpoint")
    pieces = ["the ", " today", " the the ", "  ", ".....", "The ", "THE ", " area", "the area", " right now", " this week",
              "my area", "the . "]
    for seed in _LOC_SEEDS + _ARTICLE_SEEDS:
        assert wi._clean_location(seed) == _clean_location_by_fixpoint(seed), f"{info}: {seed!r}"
    changed_by_articles = 0
    for _ in range(8000):
        chars = list(rng.choice(_LOC_SEEDS + _ARTICLE_SEEDS))
        for _ in range(rng.randint(1, 5)):
            at = rng.randrange(len(chars) + 1)
            op = rng.randrange(3)
            if op == 0:
                chars.insert(at, rng.choice(_LOC_CHARS + "THEarea\u0130"))
            elif op == 1 and chars:
                del chars[min(at, len(chars) - 1)]
            else:
                chars[at:at] = list(rng.choice(pieces))
        value = "".join(chars)
        got = wi._clean_location(value)
        assert got == _clean_location_by_fixpoint(value), f"{info}: {value!r}"
        assert wi._clean_location(got) == got, f"{info}: not idempotent for {value!r}"
        changed_by_articles += bool(got) and value.lower().count("the ") >= 2
    assert changed_by_articles > 200


@pytest.mark.parametrize("value, want", [
    ("the the area", ""), ("the . the area", ""), ("the area", ""), ("the . area", "area"), ("The THE Paris", "Paris"),
    ("the here", ""), ("the the", "the"), ("the " * 12 + "Paris", "Paris"), ("  the The Paris?! ", "Paris"),
])
def test_clean_location_articles_and_placeholders(value, want):
    assert wi._clean_location(value) == want
    assert wi._clean_location(want) == want


def test_clean_location_cost_grows_with_the_input_not_its_square():
    """The fixpoint loop took 25 s on 20000 articles; one pass takes milliseconds.

    Measured 13 ms and 9 ms for the first two inputs. The bound leaves room
    for a loaded CI host and is still a hundred times under the old cost.
    """
    cases = [("the " * 50000 + "Paris", "Paris"), ("Paris" + " today x" * 50000, "Paris"),
             ("the . " * 50000 + "the area", ""), ("x " * 100000, "")]
    for value, want in cases:
        best = None
        for _ in range(3):
            start = time.perf_counter()
            got = wi._clean_location(value)
            elapsed = time.perf_counter() - start
            best = elapsed if best is None else min(best, elapsed)
        assert got == want
        assert best < 0.2, f"{len(value)} characters took {best:.3f}s"
    start = time.perf_counter()
    assert wi.classify("weather in " + "the " * 50000 + "Paris")["location"] == "Paris"
    assert time.perf_counter() - start < 1.0
