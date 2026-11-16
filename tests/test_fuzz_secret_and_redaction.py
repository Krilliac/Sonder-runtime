"""Seeded fuzzing of secret detection and redaction (secret-redaction lane).

Contracts fuzzed:

* ``CredentialScanner.scan`` (``application.security.secrets_and_fuzz``):
  returns findings for the shapes it claims (private-key header, AWS ``AKIA``,
  GitHub ``gh[pousr]_`` token, JWT), never echoes the secret, and raises only
  ``ValueError`` (content over ``max_bytes``).
* ``redact_text`` / ``redact_structure`` (``domain.security.redaction``): every
  shape in ``domain.security.credential_formats`` plus PEM private keys is
  scrubbed wherever it sits; structure walks preserve shape, replace sensitive
  keyed strings, and replace (never pass through) subtrees beyond
  ``MAX_WALK_DEPTH`` / ``MAX_WALK_ITEMS``.
* Redaction is idempotent and leaves benign text byte-identical.

Instances are generated from the documented regex shapes in
``credential_formats.py`` (no captured bytes exist for random vendor keys).
Mutated near-misses are observed and printed, never asserted, because the code
does not document their handling.

Replay a failure with ``FUZZ_SEED=<seed> pytest <this file>``.
"""
from __future__ import annotations

import copy
import os
import random
import re
import string
import time
from dataclasses import dataclass

import pytest

from sonder_runtime.application.security.secrets_and_fuzz import CredentialScanner
from sonder_runtime.domain.security import redaction as domain_redaction
from sonder_runtime.domain.security.credential_formats import (
    AWS_KEY_RUN,
    CREDENTIAL_FORMATS,
    JWT,
    KNOWN_CREDENTIAL,
    REDACTION_FORMATS,
)
from sonder_runtime.domain.security.redaction import (
    MAX_WALK_DEPTH,
    MAX_WALK_ITEMS,
    REDACTED,
    SENSITIVE_KEY,
    redact_structure,
    redact_text,
)
from sonder_runtime.platform import logging as platform_logging

SEED = int(os.environ.get("FUZZ_SEED", "20261116"))
PLANT_ITERATIONS = 1500
STRUCT_ITERATIONS = 400
BENIGN_ITERATIONS = 1500
P99_LIMIT_S = 0.05
HARD_LIMIT_S = 2.0

ALNUM = string.ascii_letters + string.digits
B64URL = ALNUM + "_-"
UPPER_DIGITS = string.ascii_uppercase + string.digits


@dataclass(frozen=True)
class Secret:
    text: str
    random: str  # the random part (leak-checked in 8-char windows)
    kind: str
    scanner_kind: str | None  # finding kind CredentialScanner claims, else None


def _rs(rng: random.Random, alphabet: str, lo: int, hi: int | None = None) -> str:
    return "".join(rng.choice(alphabet) for _ in range(rng.randint(lo, hi if hi is not None else lo)))


def _prefixed(prefix: str, body: str, kind: str, scanner_kind: str | None = None) -> Secret:
    return Secret(prefix + body, body, kind, scanner_kind)


def _pem(rng: random.Random) -> Secret:
    label = rng.choice(("", "RSA ", "EC ", "OPENSSH ", "DSA ", "ENCRYPTED "))
    lines = [_rs(rng, ALNUM + "+/", 64) for _ in range(rng.randint(1, 5))]
    eol = rng.choice(("\n", "\r\n"))
    text = f"-----BEGIN {label}PRIVATE KEY-----{eol}{eol.join(lines)}{eol}-----END {label}PRIVATE KEY-----"
    return Secret(text, "".join(lines), "pem", "private_key")


def _generators() -> dict:
    # One generator per shape in credential_formats.KNOWN_CREDENTIAL / JWT (+ PEM in PATTERNS).
    return {
        "sk": lambda r: _prefixed(r.choice(("sk-", "sk-proj-")), _rs(r, B64URL, 12, 60), "sk"),
        "anthropic": lambda r: _prefixed("sk-ant-api03-", _rs(r, B64URL, 20, 90), "anthropic"),
        "github_pat": lambda r: _prefixed("github_pat_", _rs(r, ALNUM + "_", 16, 60), "github_pat"),
        "gh": lambda r: _prefixed(
            "gh" + r.choice("pousr") + "_", _rs(r, ALNUM, 20, 40), "gh", "github_token"
        ),
        "glpat": lambda r: _prefixed("glpat-", _rs(r, B64URL, 12, 40), "glpat"),
        "hf": lambda r: _prefixed("hf_", _rs(r, ALNUM, 12, 40), "hf"),
        "npm": lambda r: _prefixed("npm_", _rs(r, ALNUM, 12, 40), "npm"),
        "pypi": lambda r: _prefixed("pypi-", _rs(r, B64URL, 16, 80), "pypi"),
        "ya29": lambda r: _prefixed("ya29.", _rs(r, B64URL, 12, 80), "ya29"),
        "aws": lambda r: _prefixed("AKIA", _rs(r, UPPER_DIGITS, 16), "aws", "aws_access_key"),
        # The scanner only claims AKIA; the redaction shape also covers temporary ASIA keys.
        "aws_asia": lambda r: _prefixed("ASIA", _rs(r, UPPER_DIGITS, 16), "aws_asia"),
        "aiza": lambda r: _prefixed("AIza", _rs(r, ALNUM + "_-", 30, 45), "aiza"),
        "stripe": lambda r: _prefixed(
            r.choice(("sk_live_", "sk_test_", "rk_live_", "rk_test_")), _rs(r, ALNUM, 16, 40), "stripe"
        ),
        "gocspx": lambda r: _prefixed("GOCSPX-", _rs(r, B64URL, 20, 40), "gocspx"),
        "slack": lambda r: _prefixed("xox" + r.choice("baprs") + "-", _rs(r, ALNUM + "-", 10, 50), "slack"),
        "jwt": lambda r: Secret(
            f"eyJ{_rs(r, B64URL, 8, 40)}.{_rs(r, B64URL, 8, 60)}.{_rs(r, B64URL, 8, 60)}",
            "",
            "jwt",
            "jwt",
        ),
        "pem": _pem,
    }


GENERATORS = _generators()
SCANNER_KINDS = tuple(k for k, g in GENERATORS.items() if g(random.Random(0)).scanner_kind)


def _jwt_fix(secret: Secret) -> Secret:
    # For a JWT the whole token after "eyJ" is random; leak-check all of it.
    if secret.kind == "jwt":
        return Secret(secret.text, secret.text, secret.kind, secret.scanner_kind)
    return secret


def _gen(rng: random.Random, kind: str) -> Secret:
    return _jwt_fix(GENERATORS[kind](rng))


# Context templates: (before, after). The secret always follows a non-word char and is
# followed by a non-word char or end of input, so lookbehinds/word boundaries hold.
_BENIGN_WORDS = ("alpha", "beta", "gamma", "skip", "task-1", "ghost", "build", "node", "value", "list")
_CONTEXTS = (
    ("", ""),  # very start and very end
    ("x = ", "\n"),  # code
    ("call(", ", 1)\n"),
    ('{"note": "', '", "n": 1}'),  # JSON value
    ("FOO=", "\n"),  # env assignment
    ("export BAR=", " # tail\n"),
    ("https://user:", "@example.com/path"),  # URL userinfo (secret as the password)
    ("'", "'"),  # single quotes
    ('"', '"'),  # double quotes
    ("line one\r\n", "\r\nline three\r\n"),  # CRLF
    ("héllo 日本語 \U0001f642 ", " über ☃"),  # unicode neighbours
    ("head ", ""),  # end of input
    ("", " tail"),  # start of input
    ("\t", "\t"),
    ("[", "]"),
)
_NO_URL_FOR = {"pem"}


def _plant(rng: random.Random, secret: Secret) -> tuple[str, int]:
    while True:
        before, after = rng.choice(_CONTEXTS)
        if before.startswith("https://") and secret.kind in _NO_URL_FOR:
            continue
        break
    pad_b = " ".join(rng.choice(_BENIGN_WORDS) for _ in range(rng.randint(0, 3)))
    pad_a = " ".join(rng.choice(_BENIGN_WORDS) for _ in range(rng.randint(0, 3)))
    if pad_b and before == "":
        before = pad_b + rng.choice((" ", "\n", "\r\n"))
    if pad_a and after == "":
        after = rng.choice((" ", "\n", "\r\n")) + pad_a
    text = before + secret.text + after
    prefix = before
    return text, len((prefix + "x").splitlines())  # 1-based line of the secret start


def _leaks(output: str, secret: Secret) -> str | None:
    """Return the leaked fragment (the whole secret or an 8-char window of its random part)."""
    if secret.text in output:
        return secret.text
    rnd = secret.random
    for i in range(max(len(rnd) - 7, 0)):
        window = rnd[i : i + 8]
        if window in output:
            return window
    return None


class Timer:
    def __init__(self) -> None:
        self.samples: list[tuple[float, str]] = []

    def run(self, fn, *args, label: str = "", **kwargs):
        t0 = time.perf_counter()
        try:
            return fn(*args, **kwargs)
        finally:
            self.samples.append((time.perf_counter() - t0, label))

    def check(self, *, p99: bool = True) -> None:
        durations = sorted(d for d, _ in self.samples)
        worst, worst_label = max(self.samples, key=lambda s: s[0])
        assert worst < HARD_LIMIT_S, f"seed={SEED}: {worst:.3f}s on {worst_label[:200]!r}"
        if p99:
            p99_s = durations[min(len(durations) - 1, int(len(durations) * 0.99))]
            assert p99_s < P99_LIMIT_S, f"seed={SEED}: p99 {p99_s * 1000:.1f} ms"


def _fail(msg: str) -> str:
    return f"seed={SEED}: {msg}"


def _platform_redact(text: str) -> str:
    out = platform_logging.Redactor(env={}).redact(text)
    # Redactor.redact turns every exception into this marker, which contains no
    # secret: without the check a crash would read as "nothing leaked".
    assert out != platform_logging.REDACTION_FAILED, _fail(f"platform Redactor failed on {text[:200]!r}")
    return out


# The patterns, the pass loop and _sub_pem exist twice (domain ``redact_text`` and
# the platform ``Redactor`` copy). Each copy needs its own run of these tests.
BOTH_COPIES = pytest.mark.parametrize(
    ("module", "redact"),
    [
        pytest.param(domain_redaction, redact_text, id="domain"),
        pytest.param(platform_logging, _platform_redact, id="platform"),
    ],
)


def _changing_passes(module, text: str, limit: int = 200) -> int:
    count = 0
    while count < limit:
        after = module._apply_patterns(text)
        if after == text:
            break
        text = after
        count += 1
    return count


# ---------------------------------------------------------------------------
# 1. Known-positive planting
# ---------------------------------------------------------------------------


def test_scan_detects_planted_claimed_formats_without_leak() -> None:
    rng = random.Random(SEED)
    scanner = CredentialScanner()
    timer = Timer()
    for i in range(PLANT_ITERATIONS):
        kind = SCANNER_KINDS[i % len(SCANNER_KINDS)]
        secret = _gen(rng, kind)
        text, line = _plant(rng, secret)
        for payload in (text, text.encode("utf-8")):
            findings = timer.run(scanner.scan, payload, label=repr(text))
            hits = [f for f in findings if f.kind == secret.scanner_kind]
            assert hits, _fail(f"scan missed {secret.scanner_kind} in {text!r}")
            assert any(f.line == line for f in hits), _fail(
                f"{secret.scanner_kind} reported on lines {[f.line for f in hits]}, expected {line}: {text!r}"
            )
            blob = repr(findings)
            assert _leaks(blob, secret) is None, _fail(f"finding leaks {_leaks(blob, secret)!r} for {text!r}")
    timer.check()


def test_regression_scan_jwt_with_trailing_dash_is_detected() -> None:
    """SUSPECT/bug: the JWT pattern ends in a word boundary, so a final base64url ``-`` defeats it."""
    token = "eyJabcdefgh.abcdefgh.abcdefg-"  # valid base64url shape, final segment is 8 chars
    assert JWT.fullmatch(token)  # the shared domain regex accepts it
    assert redact_text(token) == REDACTED
    findings = CredentialScanner().scan(f"Authorization: x {token}")
    assert any(f.kind == "jwt" for f in findings)


def test_scan_empty_and_benign_inputs_have_no_findings() -> None:
    scanner = CredentialScanner()
    assert scanner.scan("") == ()
    assert scanner.scan(b"") == ()
    assert scanner.scan("skip task-1 ghost build node\r\nvalue list\n") == ()


@pytest.mark.parametrize("kind", sorted(GENERATORS))
def test_redact_text_scrubs_every_planted_format_without_leak(kind: str) -> None:
    rng = random.Random(f"{SEED}:{kind}")
    timer = Timer()
    for _ in range(PLANT_ITERATIONS // 8):
        secret = _gen(rng, kind)
        text, _line = _plant(rng, secret)
        out = timer.run(redact_text, text, label=repr(text))
        leak = _leaks(out, secret)
        assert leak is None, _fail(f"{kind} leaked {leak!r}: {text!r} -> {out!r}")
        assert REDACTED in out, _fail(f"{kind} not marked: {text!r} -> {out!r}")
        # Idempotent on planted text as well.
        assert redact_text(out) == out, _fail(f"not idempotent: {text!r} -> {out!r} -> {redact_text(out)!r}")
    timer.check()


def test_redact_text_secret_values_and_multiple_plants() -> None:
    rng = random.Random(SEED + 1)
    for _ in range(400):
        kinds = [rng.choice(sorted(GENERATORS)) for _ in range(rng.randint(2, 4))]
        secrets = [_gen(rng, k) for k in kinds]
        text = "\n".join(_plant(rng, s)[0] for s in secrets)
        out = redact_text(text)
        for s in secrets:
            assert _leaks(out, s) is None, _fail(f"{s.kind} leaked from combined text {text!r} -> {out!r}")
        # An arbitrary caller-supplied secret value is scrubbed regardless of shape.
        value = _rs(rng, ALNUM, 8, 20)
        literal = f"zz {value} zz"
        assert value not in redact_text(literal, secret_values=[value])


def test_format_regexes_match_generated_instances() -> None:
    """Sanity: the generators really follow the documented regexes (not just the scrubber)."""
    rng = random.Random(SEED + 2)
    for kind in sorted(GENERATORS):
        if kind == "pem":
            continue
        for _ in range(100):
            secret = _gen(rng, kind)
            pattern = JWT if kind == "jwt" else KNOWN_CREDENTIAL
            m = pattern.fullmatch(secret.text)
            assert m, _fail(f"generated {kind} instance {secret.text!r} is not a documented shape")
    assert set(CREDENTIAL_FORMATS) == {KNOWN_CREDENTIAL, JWT}
    # The redactors add the adjacent-AWS-key rule, and only after the shared formats.
    assert REDACTION_FORMATS == (*CREDENTIAL_FORMATS, AWS_KEY_RUN)
    assert domain_redaction.PATTERNS[-4:-1] == REDACTION_FORMATS
    assert domain_redaction.PATTERNS[-1] is domain_redaction._URL_USERINFO_ANY
    assert platform_logging._PATTERNS[-4:-1] == REDACTION_FORMATS


# ---------------------------------------------------------------------------
# 2. Structure redaction
# ---------------------------------------------------------------------------

_SENSITIVE_KEYS = (
    "password", "PASSWORD", "PassWord", "api_key", "API-KEY", "ApiKey", "Authorization", "AUTHORIZATION",
    "x-api-key", "my_token", "Access_Token", "client_secret", "Cookie", "private-key", "pwd", "credentials",
)
_PLAIN_KEYS = ("name", "id", "note", "path", "status", "ghost", "skip", "task-1", "items", "data")


def _gen_tree(rng: random.Random, depth: int, secrets: list, sens_values: list) -> object:
    roll = rng.random()
    if depth <= 0 or roll < 0.25:
        leaf = rng.random()
        if leaf < 0.4:
            secret = _gen(rng, rng.choice(sorted(GENERATORS)))
            secrets.append(secret)
            return _plant(rng, secret)[0]
        if leaf < 0.6:
            return rng.choice((0, 1, -5, 3.5, True, False, None))
        return " ".join(rng.choice(_BENIGN_WORDS) for _ in range(rng.randint(0, 4)))
    if roll < 0.65:
        out: dict = {}
        for _ in range(rng.randint(0, 4)):
            if rng.random() < 0.4:
                key = rng.choice(_SENSITIVE_KEYS)
                token = _rs(rng, ALNUM, 6, 20) + "!"  # not shaped like any vendor credential
                sens_values.append(token)
                out[key] = token
            else:
                out[rng.choice(_PLAIN_KEYS) + str(rng.randint(0, 99))] = _gen_tree(rng, depth - 1, secrets, sens_values)
        return out
    items = [_gen_tree(rng, depth - 1, secrets, sens_values) for _ in range(rng.randint(0, 4))]
    return tuple(items) if roll < 0.82 else items


def _strip_strings(value):
    """Shape with string leaves collapsed (sensitive replacement may alter strings only)."""
    if isinstance(value, dict):
        return ("d", tuple((k, _strip_strings(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return ("t" if isinstance(value, tuple) else "l", tuple(_strip_strings(v) for v in value))
    if isinstance(value, str):
        return "s"
    return ("v", type(value).__name__, value)


def test_redact_structure_no_secret_survives_and_shape_preserved() -> None:
    rng = random.Random(SEED + 3)
    timer = Timer()
    for _ in range(STRUCT_ITERATIONS):
        secrets: list[Secret] = []
        sens_values: list[str] = []
        tree = _gen_tree(rng, rng.randint(1, 6), secrets, sens_values)
        snapshot = copy.deepcopy(tree)
        for sensitive in (True, False):
            out = timer.run(redact_structure, tree, label=repr(tree), sensitive_keys=sensitive)
            blob = repr(out)
            for s in secrets:
                assert _leaks(blob, s) is None, _fail(f"{s.kind} leaked {_leaks(blob, s)!r}: {tree!r} -> {out!r}")
            assert _strip_strings(out) == _strip_strings(tree), _fail(f"shape changed: {tree!r} -> {out!r}")
            if sensitive:
                for v in sens_values:
                    assert v not in blob, _fail(f"sensitive-key value {v!r} survived: {tree!r} -> {out!r}")
        # Input is not mutated (repr also separates True from 1 and a list from a tuple).
        assert tree == snapshot and repr(tree) == repr(snapshot), _fail(f"input mutated: {snapshot!r} -> {tree!r}")
    timer.check()


def test_redact_structure_sensitive_key_semantics() -> None:
    rng = random.Random(SEED + 4)
    for _ in range(500):
        key = rng.choice(_SENSITIVE_KEYS)
        assert SENSITIVE_KEY.match(key), key
        val = _rs(rng, ALNUM, 1, 30)
        # Documented: a non-empty string under a sensitive key is replaced whole, only when asked.
        assert redact_structure({key: val}, sensitive_keys=True) == {key: REDACTED}
        assert redact_structure({key: val}) == {key: redact_text(val)}
        # Empty strings and non-strings under a sensitive key are not replaced outright.
        assert redact_structure({key: ""}, sensitive_keys=True) == {key: ""}
        assert redact_structure({key: 7, "k": None}, sensitive_keys=True) == {key: 7, "k": None}
    # Dict keys pass through unchanged, even when they look like credentials.
    assert redact_structure({"ghost": "skip"}) == {"ghost": "skip"}


def test_redact_structure_depth_overflow_replaces_subtree() -> None:
    rng = random.Random(SEED + 5)
    for _ in range(60):
        secret = _gen(rng, rng.choice(sorted(GENERATORS)))
        depth = rng.randint(MAX_WALK_DEPTH - 3, MAX_WALK_DEPTH + 40)
        node: object = secret.text
        container = rng.choice((list, tuple, dict))
        for _level in range(depth):
            node = {"k": node} if container is dict else container([node])
        out = redact_structure(node)  # must not raise RecursionError or anything else
        blob = repr(out)
        assert _leaks(blob, secret) is None, _fail(f"depth={depth} leaked via {blob[:200]!r}")
        if depth > MAX_WALK_DEPTH:
            assert REDACTED in blob
    # A cyclic structure is cut by the depth bound rather than recursing forever.
    cyc: list = []
    cyc.append(cyc)
    cyc.append("ghp_" + "A" * 24)
    out = redact_structure(cyc)
    assert "A" * 8 not in repr(out)


def test_redact_structure_item_budget_overflow_replaces_remaining() -> None:
    rng = random.Random(SEED + 6)
    for _ in range(3):
        secret = _gen(rng, "gh")
        n = MAX_WALK_ITEMS + rng.randint(5, 500)
        items = [secret.text] * n  # every item is sensitive; those past the budget must be REDACTED too
        out = redact_structure(items)
        assert len(out) == n
        assert _leaks(repr(out), secret) is None
        # Budget is shared across siblings of nested containers, not per list.
        nested = [[secret.text] * 100 for _ in range(MAX_WALK_ITEMS // 50)]
        out2 = redact_structure(nested)
        assert _leaks(repr(out2), secret) is None
        assert len(out2) == len(nested)
    # Exactly within budget: the root consumes one unit, so n-1 leaves fit... boundary just must not raise.
    for n in (MAX_WALK_ITEMS - 2, MAX_WALK_ITEMS - 1, MAX_WALK_ITEMS, MAX_WALK_ITEMS + 1):
        out = redact_structure(["x"] * n)
        assert len(out) == n
        assert out[0] == "x"
        assert out[-1] in ("x", REDACTED)


def test_redact_structure_custom_redactor_and_odd_leaves() -> None:
    rng = random.Random(SEED + 7)
    for _ in range(200):
        s = _rs(rng, ALNUM, 0, 20)
        assert redact_structure([s, {"a": s}], lambda x: x.upper()) == [s.upper(), {"a": s.upper()}]
    odd = [b"bytes", {1, 2}, object, 3 + 4j]
    assert redact_structure(odd) == odd  # non-JSON leaves pass through untouched


# ---------------------------------------------------------------------------
# 3. Idempotence and false-positive bounds
# ---------------------------------------------------------------------------

_SAFE_PUNCT = (" ", " ", " ", "\n", "\r\n", ", ", ". ", ": ", " - ", "/", "(", ")", "\t")
_SAFE_WORDS = _BENIGN_WORDS + (
    "skip", "ghost", "task-1", "task-22", "risk", "desk", "disk-usage", "ghp", "hf", "npm", "sk", "pypi",
    "AKIA", "eyJ", "xox", "café", "日本", "42", "0xdeadbeef", "a1b2c3d4e5f6", "the", "quick", "fox",
    "ghosts", "skipped", "pass", "tokenizer",
)


def _benign(rng: random.Random) -> str:
    parts = []
    for _ in range(rng.randint(0, 40)):
        parts.append(rng.choice(_SAFE_WORDS))
        parts.append(rng.choice(_SAFE_PUNCT))
    return "".join(parts)


def test_redact_text_is_idempotent_and_benign_text_unchanged() -> None:
    rng = random.Random(SEED + 8)
    timer = Timer()
    for _ in range(BENIGN_ITERATIONS):
        text = _benign(rng)
        out = timer.run(redact_text, text, label=repr(text))
        assert out == text, _fail(f"benign text mangled: {text!r} -> {out!r}")
        assert len(out) <= len(text)
        assert redact_text(out) == out
    timer.check()


def test_redact_text_idempotent_on_hostile_fragments() -> None:
    rng = random.Random(SEED + 9)
    fragments = (
        "Authorization: Bearer ", "authorization=", "Cookie: a=b; c=d", "password=", "api_key: ", "token = '",
        "[REDACTED]", "[REDACTED] word", "Bearer ", "https://u:p@h/", "?access_token=", "&password=",
        "-----BEGIN RSA PRIVATE KEY-----", "-----END RSA PRIVATE KEY-----", "ghp_", "sk-", "eyJ", ".", "\r\n",
        "hunter2", " ", "\x00", "AKIA", "xoxb-", '"', "'", "}", "{", ";", ",", " ",
    )
    secrets = [_gen(rng, k).text for k in sorted(GENERATORS)]
    timer = Timer()
    for _ in range(BENIGN_ITERATIONS):
        pieces = [rng.choice(fragments + tuple(secrets) + _SAFE_WORDS) for _ in range(rng.randint(1, 14))]
        text = rng.choice(("", " ", "\n")).join(pieces)
        once = timer.run(redact_text, text, label=repr(text))
        twice = redact_text(once)
        assert twice == once, _fail(f"not idempotent: {text!r} -> {once!r} -> {twice!r}")
    timer.check()


# ---------------------------------------------------------------------------
# 4. Budgets
# ---------------------------------------------------------------------------


def test_scan_random_binary_and_size_bound() -> None:
    rng = random.Random(SEED + 10)
    scanner = CredentialScanner(max_bytes=64 * 1024)
    timer = Timer()
    for _ in range(300):
        n = rng.choice((0, 1, 7, 100, 4096, 30_000))
        blob = bytes(rng.getrandbits(8) for _ in range(n))
        findings = timer.run(scanner.scan, blob, label=repr(blob[:80]))
        assert isinstance(findings, tuple)
    timer.check()
    # Boundary: exactly max_bytes passes, one more raises (str input, ASCII and multibyte).
    small = CredentialScanner(max_bytes=1000)
    assert small.scan("a" * 1000) == ()
    with pytest.raises(ValueError):
        small.scan("a" * 1001)
    assert small.scan("é" * 500) == ()  # 1000 bytes
    with pytest.raises(ValueError):
        small.scan("é" * 501)
    with pytest.raises(ValueError):
        small.scan(b"a" * 1001)
    for bad in (0, -1):
        with pytest.raises(ValueError):
            CredentialScanner(max_bytes=bad)


def test_scan_hostile_large_inputs_stay_in_budget() -> None:
    rng = random.Random(SEED + 11)
    limit = 200_000
    scanner = CredentialScanner(max_bytes=limit)
    timer = Timer()
    for _ in range(20):
        unit = "".join(rng.choice(_HOSTILE_UNITS) for _ in range(rng.randint(1, 4)))
        text = (unit * (limit // len(unit) + 1))[:limit]
        findings = timer.run(scanner.scan, text, label=f"scan {unit!r}")
        assert isinstance(findings, tuple)
    # A max-size planted input still finds the secret and redaction scrubs it.
    secret = _gen(rng, "aws")
    big = "x " * ((limit - 100) // 2) + "\n" + secret.text + "\n"
    assert any(f.kind == "aws_access_key" for f in timer.run(scanner.scan, big, label="big aws"))
    assert _leaks(redact_text(big), secret) is None
    timer.check(p99=False)  # 200 KB inputs: only the hard cap applies


_HOSTILE_UNITS = (
    "eyJ" + "a" * 7 + ".", "AKIA", "ghp_", "api_key ", "password\t", "token  ", "secret ", "sk-", "a" * 50,
    "-----BEGIN ", "-----BEGIN RSA PRIVATE KEY-----", "-eyJ", "Bearer ", "authorization: ", "x:y@", "https://",
    "\n", "\r\n", "-", "_", ".",
)


def test_redact_text_hostile_16kb_inputs_stay_in_budget() -> None:
    rng = random.Random(SEED + 13)
    timer = Timer()
    for _ in range(40):
        unit = "".join(rng.choice(_HOSTILE_UNITS) for _ in range(rng.randint(1, 4)))
        text = (unit * (16_384 // len(unit) + 1))[:16_384]
        timer.run(redact_text, text, label=f"redact {unit!r}")
    timer.check()


def test_regression_redact_text_scheme_scan_is_not_quadratic() -> None:
    """SUSPECT/bug: the URL-userinfo pattern (PATTERNS[4]) rescans a long scheme-character run from every word boundary."""
    text = "sk-" * 5461  # 16 KB, no "://" anywhere
    t0 = time.perf_counter()
    redact_text(text)
    elapsed = time.perf_counter() - t0
    assert elapsed < P99_LIMIT_S, f"16 KB took {elapsed * 1000:.0f} ms"


def _aws_keys(count: int) -> list[str]:
    # Alternating AKIA / ASIA ids with distinct bodies, assembled at runtime.
    return [("AK" if i % 2 else "AS") + "IA" + f"{i:016d}" for i in range(1, count + 1)]


@BOTH_COPIES
def test_regression_redact_text_not_idempotent_after_adjacent_substitution(module, redact) -> None:
    """SUSPECT: a replaced credential makes ``[REDACTED]`` adjacent to text a later pass then matches."""
    adjacent = "AKIA0000000000000000Cookie: a=b"
    assert _changing_passes(module, adjacent) == 2  # the cookie only matches once the key is gone
    once = redact(adjacent)
    assert "a=b" not in once, f"{adjacent!r} -> {once!r}"
    assert redact(once) == once, f"{adjacent!r} -> {once!r} -> {redact(once)!r}"
    stray_quote = "token = 'abcdefgh\"sk"
    once = redact(stray_quote)
    assert redact(once) == once, f"{stray_quote!r} -> {once!r} -> {redact(once)!r}"


@BOTH_COPIES
def test_pass_bound_never_undoes_the_first_pass(module, redact) -> None:
    """No finite bound settles every text: each ``x'`` unit here costs one more pass.

    Past the bound the text is still safe, because a pass only swaps matched text for
    the marker; what is given up is idempotence for that text.
    """
    text = "token='hunter2secret'" + "x'" * (module._MAX_PASSES * 3) + " AKIA0000000000000000"
    assert _changing_passes(module, text) > module._MAX_PASSES  # otherwise this test checks nothing
    first = module._apply_patterns(text)
    out = redact(text)
    for secret in ("hunter2secret", "AKIA0000000000000000"):
        assert secret not in first and secret not in out
    assert len(out) < len(first)  # the later passes ran, and only removed text
    again = redact(out)
    assert len(again) < len(out) and "hunter2secret" not in again


@BOTH_COPIES
def test_regression_adjacent_aws_keys_redacted_in_one_pass(module, redact) -> None:
    """``KNOWN_CREDENTIAL`` takes the first key of a run and ``AWS_KEY_RUN`` the rest, in the same pass."""
    for count in (2, 8, 1000):
        keys = _aws_keys(count)
        for prefix in ("aws=", "", " ", "k:"):
            text = prefix + "".join(keys)
            single = module._apply_patterns(text)
            assert "AKIA" not in single and "ASIA" not in single, f"{count} keys after {prefix!r}: {single[:120]!r}"
            out = redact(text)
            assert out == single == prefix + REDACTED + REDACTED
            assert redact(out) == out


def test_regression_scan_reports_adjacent_aws_keys() -> None:
    scanner = CredentialScanner()
    akia = "".join(f"AKIA{i:016d}" for i in range(1, 9))
    assert any(f.kind == "aws_access_key" for f in scanner.scan(akia))
    assert any(f.kind == "aws_access_key" for f in scanner.scan("x " + akia))


def _jwts_carrying_credential_shapes() -> dict[str, str]:
    key = "AK" + "IA" + "Q" * 16
    key2 = "AS" + "IA" + "R" * 16
    head = "eyJhbGciOiJIUzI1NiJ9"
    return {
        "payload-key-then-ghp": f"{head}.eyJzdWIi{key}{'gh' + 'p_'}{'P' * 18}x.SIGNATUREsecretsig123",
        "payload-key-chain": f"{head}.eyJzdWIi{key}{key2}tail.SIGNATUREsecretsig123",
        "signature-key-then-hf": f"{head}.eyJzdWIiOiIx.SIGsecret{key}{'h' + 'f_'}{'Z' * 14}-restofsignature",
        "signature-key-chain": f"{head}.eyJzdWIiOiIx.SIGsecret{key}{key2}{key}",
    }


@BOTH_COPIES
@pytest.mark.parametrize("case", sorted(_jwts_carrying_credential_shapes()))
def test_regression_jwt_carrying_credential_shapes_is_redacted_whole(module, redact, case: str) -> None:
    """A credential shape inside a longer token is not free-standing; matching it there cuts the JWT apart."""
    token = _jwts_carrying_credential_shapes()[case]
    assert JWT.fullmatch(token)
    assert KNOWN_CREDENTIAL.search(token) is None
    assert AWS_KEY_RUN.search(token) is None
    assert module._apply_patterns("jwt=" + token) == "jwt=" + REDACTED
    assert redact("jwt=" + token) == "jwt=" + REDACTED


@BOTH_COPIES
def test_aws_key_run_is_applied_after_the_jwt_rule(module, redact) -> None:
    """Taking the whole key run before ``JWT`` exposes an earlier JWT start, and that match
    stops one segment short of the one the single-key rule leaves for ``JWT`` to take."""
    first, second = _aws_keys(2)
    text = first + second + "eyJaaaaaaaa.eyJbbbbbbbb.cccccccc.dddddddd"
    single = module._apply_patterns(text)
    out = redact(text)
    for kept in ("dddddddd", "cccccccc", "eyJbbbbbbbb", second):
        assert kept not in single and kept not in out, f"{kept}: {single!r} / {out!r}"


@pytest.mark.parametrize("size", [64 * 1024])
@pytest.mark.parametrize("unit", ["-----BEGIN RSA PRIVATE KEY-----", "-eyJ", "AKIA0000000000000000", "-----END "])
@pytest.mark.parametrize(
    "fn",
    [
        pytest.param(redact_text, id="domain"),
        pytest.param(_platform_redact, id="platform"),
        pytest.param(CredentialScanner().scan, id="scan"),
    ],
)
def test_regression_repeated_unit_is_linear(fn, unit: str, size: int) -> None:
    text = (unit * (size // len(unit) + 1))[:size]
    t0 = time.perf_counter()
    fn(text)
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.2, f"{unit!r} x {size}: {elapsed:.3f}s"


def test_regression_scan_jwt_after_dash_is_detected_in_linear_time() -> None:
    """The scanner is a detector: ``-eyJ...`` is reported, and a long ``-eyJ`` run stays cheap."""
    scanner = CredentialScanner()

    def found(text: str) -> bool:
        return any(f.kind == "jwt" for f in scanner.scan(text))

    jwt = "eyJaaaaaaaa.bbbbbbbb.cccccccc"
    rest = jwt[12:]  # the two segments after the header
    dash_run = "-eyJ" * 50
    detected = [
        "-" + jwt,
        "key-" + jwt,
        "eyJ-" + jwt,
        "a-b-" + jwt,
        "x -" + jwt + "-",
        dash_run + "." + rest,
    ]
    for text in detected:
        assert found(text), text
    # "eyJ" glued to a word character does not start a JWT, and short segments are not one.
    ignored = [
        "x" + jwt,
        "_" + jwt,
        "9" + jwt,
        "-eyJaaaa.bbbbbbbb.cccccccc",
        "-" + jwt[:-2],
        dash_run,
    ]
    for text in ignored:
        assert not found(text), text
    run = "-eyJ" * (256 * 1024 // 4)
    for text, expected in ((run, False), (run + "." + rest, True), ("x" + run[1:] + "." + rest, True)):
        t0 = time.perf_counter()
        assert found(text) is expected
        elapsed = time.perf_counter() - t0
        assert elapsed < 0.5, f"256 KB '-eyJ' run: {elapsed:.3f}s"


@BOTH_COPIES
def test_pem_block_semantics_unchanged(module, redact) -> None:
    body = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow==\n-----END RSA PRIVATE KEY-----"
    tail = "-----BEGIN RSA PRIVATE KEY-----\nunterminated"
    text = "a " + body + " b " + tail
    assert module._sub_pem(text) == "a " + REDACTED + " b " + tail
    out = redact(text)
    assert "MIIEow" not in out
    assert out.endswith(tail)


@BOTH_COPIES
def test_sub_pem_redacts_every_block_in_a_single_pass(module, redact) -> None:
    """Cutting the text at the first END marker instead of the last leaves the later blocks."""
    count = module._MAX_PASSES + 4  # more blocks than the pass loop could mop up one by one
    blocks = [
        f"-----BEGIN RSA PRIVATE KEY-----\r\nMIIbody{i:04d}\r\n-----END RSA PRIVATE KEY-----" for i in range(count)
    ]
    text = " mid ".join(blocks) + " tail -----BEGIN PRIVATE KEY-----\nunterminated"
    single = module._sub_pem(text)
    assert single == module._PEM.sub(REDACTED, text)
    assert single.count(REDACTED) == count and "MIIbody" not in single
    out = redact(text)
    assert "MIIbody" not in out and out.endswith("unterminated"), out


@BOTH_COPIES
@pytest.mark.parametrize(
    "text",
    [
        "-----BEGIN RSA PRIVATE KEY-----END PRIVATE KEY-----END PRIVATE KEY-----",
        "-----END PRIVATE KEY-----BEGIN PRIVATE KEY-----END PRIVATE KEY-----END PRIVATE KEY-----",
        "-----BEGIN PRIVATE KEY-----END SECRETBODY -----END PRIVATE KEY-----END PRIVATE KEY-----",
        "x -----BEGIN EC PRIVATE KEY-----END RSA PRIVATE KEY-----END EC PRIVATE KEY-----\r\n",
    ],
)
def test_regression_pem_end_marker_sharing_dashes_is_redacted(module, redact, text: str) -> None:
    """An END marker may start inside the five dashes that close the header or marker before it."""
    want = module._PEM.sub(REDACTED, text)
    assert want != text
    assert module._sub_pem(text) == want
    out = redact(text)
    assert "BEGIN" not in out and "SECRETBODY" not in out, out


@BOTH_COPIES
def test_sub_pem_equals_plain_substitution_on_marker_soup(module, redact) -> None:
    rng = random.Random(SEED + 14)
    fragments = (
        "-----BEGIN PRIVATE KEY-----", "-----BEGIN RSA PRIVATE KEY-----", "-----END PRIVATE KEY-----",
        "-----END RSA PRIVATE KEY-----", "-----END EC PRIVATE KEY-----", "-----", "END PRIVATE KEY-----",
        "BEGIN PRIVATE KEY-----", "-----END ", "-----BEGIN ", "PRIVATE KEY", "END ", "\n", "\r\n", "AB", "x", " ", "-",
    )
    redacted = 0
    for _ in range(20_000):
        text = "".join(rng.choice(fragments) for _ in range(rng.randint(1, 10)))
        want = module._PEM.sub(REDACTED, text)
        redacted += want != text
        assert module._sub_pem(text) == want, _fail(repr(text))
        assert redact(text) == redact(want), _fail(repr(text))
    assert redacted > 1000  # the soup really produces PEM blocks


@BOTH_COPIES
def test_apply_patterns_sends_the_pem_pattern_through_sub_pem(module, redact, monkeypatch) -> None:
    """Not left to a timing test: ``_PEM.sub`` gives the same text, only in quadratic time."""
    patterns = module.PATTERNS if module is domain_redaction else module._PATTERNS
    assert sum(pattern is module._PEM for pattern in patterns) == 1
    seen: list[str] = []
    real = module._sub_pem
    monkeypatch.setattr(module, "_sub_pem", lambda text: seen.append(text) or real(text))
    assert redact("plain text") == "plain text"
    assert seen == ["plain text"]


# ---------------------------------------------------------------------------
# 5. Mutated near-misses (observed, not asserted: handling is undocumented)
# ---------------------------------------------------------------------------


def test_near_miss_mutations_never_raise_and_never_leak_when_detected(capsys) -> None:
    rng = random.Random(SEED + 12)
    scanner = CredentialScanner()
    observed: dict[tuple[str, str], list[int]] = {}
    for _ in range(600):
        kind = rng.choice(sorted(GENERATORS))
        secret = _gen(rng, kind)
        truncated = secret.text[:-1]
        swapped = secret.text.swapcase() if rng.random() < 0.5 else secret.text[:3].swapcase() + secret.text[3:]
        for label, mutant in (("truncate-1", truncated), ("case-swap", swapped)):
            text = f"ctx {mutant} end"
            out = redact_text(text)
            findings = scanner.scan(text)
            stat = observed.setdefault((kind, label), [0, 0, 0])
            stat[0] += 1
            stat[1] += out != text  # redact_text changed it
            stat[2] += bool(findings)  # scan reported something
            # The one hard rule: if redaction fired, the marker is present and idempotent.
            if out != text:
                assert REDACTED in out and redact_text(out) == out, _fail(f"{mutant!r} -> {out!r}")
    with capsys.disabled():
        print(f"\n[near-miss seed={SEED}] kind/mutation: total, redact_text changed, scan found")
        for (kind, label), (total, red, scan) in sorted(observed.items()):
            print(f"  {kind:11s} {label:10s} {total:3d} {red:3d} {scan:3d}")


@pytest.mark.parametrize(
    "text",
    [
        "-postgres://u:p@h",
        "1.https://u:p@h",
        "x.http://u:p@h",
        "(http://u:p@h)",
        "HTTP://u:p@h",
        "dsn=redis+tls://user:hunter2@cache",
    ],
)
def test_regression_url_userinfo_after_scheme_run_punctuation_is_redacted(text):
    # The linear scheme anchor must not skip a scheme that follows "-", "." or
    # a digit: the old `\b` anchor redacted these, so missing one is a leak.
    out = redact_text(text)
    secret = text.split("://", 1)[1].split("@", 1)[0]
    assert secret not in out, (text, out)


# The URL-userinfo rule as it stood before the fuzz fixes. Quadratic on long
# scheme-character runs, so only ever run here on short texts.
_OLDER_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/@\s:]+:[^/@\s]+)@")
_URL_PIECES = (
    "a", "Z", "1", "_", "\u00e9", "\u212a", "\u017f", "\u0130", "+", ".", "-", "://", ":", "@",
    "/", " ", "u", "pw", "?password=x", "&api_key=y", "\n", "http", "s3",
)


@BOTH_COPIES
def test_url_userinfo_slot_matches_the_older_anchor_exactly(module, redact) -> None:
    """The linear slot-5 rule redacts exactly where the older ``\\b`` rule did.

    A wider match there would consume a ``?password=`` that the query rule
    after it must see, so redacting *more* in this slot is a leak too.
    """
    rng = random.Random(SEED)
    differ = 0
    run_chars = ("a", "Z", "1", "+", ".", "-", "K", "ſ", "İ", "ı")
    for _ in range(20_000):
        if rng.random() < 0.5:
            text = "".join(rng.choice(_URL_PIECES) for _ in range(rng.randint(1, 24)))
        else:  # a URL with userinfo, after a random scheme run and context
            run = "".join(rng.choice(run_chars) for _ in range(rng.randint(1, 6)))
            text = (
                "".join(rng.choice(_URL_PIECES) for _ in range(rng.randint(0, 3)))
                + run + "://u" + rng.choice(("", "x", "?password=")) + ":p" + rng.choice(("", "&api_key=z", ";")) + "@h"
                + "".join(rng.choice(_URL_PIECES) for _ in range(rng.randint(0, 3)))
            )
        want = _OLDER_URL_USERINFO.sub(lambda m: m.group(1) + REDACTED, text)
        got = module._sub_url_userinfo(text)
        assert got == want, f"seed={SEED} {text!r}: {got!r} != {want!r}"
        differ += want != text
    assert differ > 2_000  # the oracle saw real matches, not only misses


@BOTH_COPIES
@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("1http://u:?password=abcd;p@ssTOPSECRET", "TOPSECRET"),
        ("backup_s3://key:?api_key=abcd,p@ssw0rd-TOPSECRET rest", "TOPSECRET"),
        ('2fa://u:&credential="abcd"x@SECRETTAIL', "SECRETTAIL"),
        ("_x://u:?password=abcd;p@ssTOPSECRET", "TOPSECRET"),
    ],
)
def test_regression_url_rule_does_not_preempt_the_query_rule(module, redact, text, secret) -> None:
    out = redact(text)
    assert secret not in out, (text, out)


@BOTH_COPIES
@pytest.mark.parametrize(
    "text", ["1http://u:hunter2@h", "backup_s3://key:hunter2@h", "_x://u:hunter2@h", "\u00e9s://u:hunter2@h"]
)
def test_url_userinfo_after_a_scheme_the_older_anchor_skips_is_redacted(module, redact, text) -> None:
    out = redact(text)
    assert "hunter2" not in out, (text, out)


@BOTH_COPIES
def test_url_rules_are_ordered_around_the_query_rule(module, redact, monkeypatch) -> None:
    patterns = module.PATTERNS if module is domain_redaction else module._PATTERNS
    query = next(i for i, p in enumerate(patterns) if "password|credential)=" in p.pattern)
    assert [i for i, p in enumerate(patterns) if p is module._URL_USERINFO] == [query - 1]
    assert patterns[-1] is module._URL_USERINFO_ANY
    assert sum(p is module._URL_USERINFO_ANY for p in patterns) == 1
    seen: list[str] = []
    real = module._sub_url_userinfo
    monkeypatch.setattr(module, "_sub_url_userinfo", lambda text: seen.append(text) or real(text))
    assert redact("plain text") == "plain text"
    assert seen == ["plain text"]


@pytest.mark.parametrize("text", ["a." * 200_000, "a+" * 200_000 + "b://u:p@", "_" + "a-" * 200_000 + "://u:p@h"])
def test_url_userinfo_scan_is_linear(text) -> None:
    t0 = time.perf_counter()
    redact_text(text)
    assert time.perf_counter() - t0 < HARD_LIMIT_S
