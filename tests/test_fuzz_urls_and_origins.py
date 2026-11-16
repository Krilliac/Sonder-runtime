"""Seeded mutation fuzzing of every URL / origin / host-policy parser.

Contract fuzzed: a hostile URL, origin or host string yields either the
function's documented typed error (``ValueError`` for the web/MCP/egress
parsers, ``InvalidInput`` for the gateway ``normalize_base_url`` functions,
a non-empty error string for ``policy_error``) or an accepted value that
satisfies a security invariant checked against a small reference oracle
written in this file (``ipaddress`` / ``socket.inet_aton`` / simple suffix
rules).  Every input is timed: p99 under 50 ms and under 2 s for any single
input.

Functions under test:
  sonder_runtime.domain.ollama_policy: normalize, policy_error, is_loopback
  sonder_runtime.platform.config: normalize_origin, normalize_origins
  sonder_runtime.adapters.external_mcp: _parse_endpoint
  sonder_runtime.domain.security.credential_egress: _normalize_host,
      _host_matches, RedirectChain.validate (and EgressPolicy / CredentialHandle)
  web_tools: _validated_public_target, _looks_like_noncanonical_numeric_host,
      _is_globally_routable, _clean_result_url, _clean_bing_result_url
  normalize_base_url in the sonder-inference, openrouter and anthropic gateways

Skipped: serve.py env parsers (importing serve.py reads env at import time);
``_redact_url_userinfo`` (not present in sonder_runtime.platform.config on
this base).

Replay a failure with ``SONDER_FUZZ_SEED=<seed> pytest ...``; the seed is
printed in every failure message.
"""
from __future__ import annotations

import base64
import ipaddress
import logging
import os
import random
import re
import socket
import time
import urllib.parse

import pytest

import web_tools
from sonder_runtime.adapters import external_mcp
from sonder_runtime.adapters.inference import (
    anthropic_gateway,
    openrouter_gateway,
    sonder_inference_gateway,
)
from sonder_runtime.domain import ollama_policy
from sonder_runtime.domain.common.errors import InvalidInput
from sonder_runtime.domain.security import credential_egress as egress
from sonder_runtime.platform import config as platform_config

SEED = int(os.environ.get("SONDER_FUZZ_SEED", "20261116"))
ITER = 1500
P99_SECONDS = 0.05
HARD_CAP_SECONDS = 2.0

_FRAGMENTS = (
    "@", ":", "/", "?", "#", "\\", "%", "%25", "[", "]", ".", "..", "。", "．", "｡",
    "０", "１２７", "\r\n", "\r", "\n", "\t", " ", "\x00", "\u00a0", "0x", "0", "00",
    "127.0.0.1", "[::1]", "[::ffff:127.0.0.1]", "localhost", "localhost.", "user:pw@",
    "http://", "https://", "file://", "javascript:", "data:", "xn--", "..%2f", "%2e",
)
_BOUNDARY_PORTS = ("0", "1", "80", "443", "11434", "65535", "65536", "99999", "-1", "٨٠", "08", "")


def _mutate(rng: random.Random, text: str, rounds: int = 3) -> str:
    chars = list(text)
    for _ in range(rng.randint(1, rounds)):
        op = rng.randrange(7)
        pos = rng.randrange(len(chars) + 1)
        if op == 0 and chars:
            chars[min(pos, len(chars) - 1)] = chr(rng.choice((0, 9, 10, 13, 32, 47, 58, 63, 64, 35, 0x3002, 0xFF0E, 0xFF11, rng.randrange(1, 0x2FF))))
        elif op == 1:
            chars[pos:pos] = list(rng.choice(_FRAGMENTS))
        elif op == 2 and chars:
            del chars[min(pos, len(chars) - 1)]
        elif op == 3 and chars:
            a = rng.randrange(len(chars))
            b = min(len(chars), a + rng.randint(1, 8))
            chars[pos:pos] = chars[a:b]
        elif op == 4 and chars:
            i = min(pos, len(chars) - 1)
            chars[i] = chars[i].swapcase()
        elif op == 5:
            chars[pos:pos] = list(rng.choice(_BOUNDARY_PORTS))
        elif op == 6 and len(chars) > 2:
            a, b = sorted((rng.randrange(len(chars)), rng.randrange(len(chars))))
            chars[a:b] = chars[a:b][::-1]
    return "".join(chars)


class _Timer:
    """Collects per-input durations; ``check`` enforces the time budget."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.samples: list[float] = []
        self.slowest = ("", 0.0)

    def call(self, func, *args, **kwargs):
        start = time.perf_counter()
        try:
            return func(*args, **kwargs)
        finally:
            took = time.perf_counter() - start
            self.samples.append(took)
            if took > self.slowest[1]:
                self.slowest = (repr(args)[:200], took)

    def check(self) -> None:
        ordered = sorted(self.samples)
        p99 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))]
        assert ordered[-1] < HARD_CAP_SECONDS, f"{self.label}: {self.slowest[0]!r} took {ordered[-1]:.3f}s (seed {SEED})"
        assert p99 < P99_SECONDS, f"{self.label}: p99 {p99:.4f}s (seed {SEED})"


def _fail(label: str, value, why: str) -> str:
    return f"{label}: {why} for input {value!r} (seed {SEED})"


# ---------------------------------------------------------------------------
# Host spellings and generators
# ---------------------------------------------------------------------------

_LOOPBACK_TRICKS = (
    "2130706433", "0177.0.0.1", "0x7f.1", "0x7f000001", "017700000001", "127.1", "127.0.1",
    "127.0.0.1.nip.io", "localhost.", "LOCALHOST", "localhost.localdomain", "[::ffff:127.0.0.1]",
    "[::ffff:7f00:1]", "[::1%25lo]", "[::1%lo]", "[fe80::1%25eth0]", "[fe80::1%eth0]",
    "127。0。0。1", "127．0．0．1", "１２７.０.０.１", "127.0.0.1.", "0.0.0.0", "[::]", "0", "0x0",
    "[0:0:0:0:0:0:0:1]", "[::0001]", "10.0.0.1", "169.254.169.254", "192.168.0.1", "[fc00::1]",
    "[fe80::1]", "[::ffff:10.0.0.1]", "[::ffff:a9fe:a9fe]", "8.8.8.8", "example.com", "EXAMPLE.com.",
    "127.0.0.1\r\n", "127.0.0.1 ", " 127.0.0.1", "127.0.0.1\x00", "a b", "xn--nxasmq6b.example",
)


def _ipv4_spelling(rng: random.Random, value: int) -> str:
    octets = [(value >> s) & 0xFF for s in (24, 16, 8, 0)]
    style = rng.randrange(6)
    if style == 0:
        return ".".join(str(o) for o in octets)
    if style == 1:
        return str(value)
    if style == 2:
        return ".".join(f"0{o:o}" if o else "0" for o in octets)
    if style == 3:
        return ".".join(f"0x{o:x}" for o in octets)
    if style == 4:
        return f"{octets[0]}.{value & 0xFFFFFF}"
    return f"0x{value:x}"


_SPECIAL_V4 = (0x7F000001, 0x7F000002, 0, 0x0A000001, 0xC0A80001, 0xA9FEA9FE, 0xAC100001, 0x64400001, 0xE0000001, 0xFFFFFFFF, 0x08080808, 0x01010101)


def _host(rng: random.Random) -> str:
    kind = rng.randrange(6)
    if kind == 0:
        return rng.choice(_LOOPBACK_TRICKS)
    if kind == 1:
        return _ipv4_spelling(rng, rng.choice(_SPECIAL_V4) if rng.random() < 0.7 else rng.getrandbits(32))
    if kind == 2:
        return f"[{ipaddress.IPv6Address(rng.getrandbits(128) if rng.random() < 0.3 else rng.choice((1, 0, 0xFFFF7F000001, 0xFFFF0A000001, 0xFE80 << 112 | 1))).compressed}]"
    if kind == 3:
        return "{}.{}".format(rng.choice(("a", "good", "evil", "127", "nip")), rng.choice(("com", "io", "nip.io", "localhost", "internal")))
    return _mutate(rng, rng.choice(_LOOPBACK_TRICKS))


_SECRET_PREFIX = "ZQXSECRET"


def _origin(rng: random.Random, secret: str | None = None) -> str:
    scheme = rng.choice(("http", "https", "HTTP", "HtTpS", "ftp", "file", "javascript", "", "ws"))
    userinfo = ""
    if secret is not None:
        userinfo = rng.choice(("u:%s", "%s", "u:%s/x", "u:a/%s", "u:a?%s", "u:a#%s", "u:a%%40%s", "u:%s?", ":%s")) % secret + "@"
    elif rng.random() < 0.1:
        userinfo = "user:pw@"
    port = rng.choice(("", ":80", ":443", ":11434", ":0", ":65536", ":abc", ":" + rng.choice(_BOUNDARY_PORTS)))
    tail = rng.choice(("", "", "/", "//", "/path", "?q=1", "#f", "/?x#y", " ", "\r\n", "\\", "/ "))
    prefix = rng.choice(("", " ", "\t", "\r\n"))
    text = "{}{}{}{}{}{}".format(prefix, (scheme + "://") if scheme or rng.random() < 0.5 else "", userinfo, _host(rng), port, tail)
    return _mutate(rng, text, 2) if rng.random() < 0.35 else text


class _LogGrab(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.text: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.text.append(record.getMessage())


def _oracle_loopback(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    return (mapped or ip).is_loopback


# ---------------------------------------------------------------------------
# ollama_policy
# ---------------------------------------------------------------------------

def test_ollama_policy_error_accepts_only_safe_origins():
    rng = random.Random(SEED)
    timer = _Timer("policy_error")
    for _ in range(ITER * 2):
        value = _origin(rng)
        allow = rng.random() < 0.4
        verdict = timer.call(ollama_policy.policy_error, value, allow_remote=allow)
        assert isinstance(verdict, str), _fail("policy_error", value, f"non-str {verdict!r}")
        if verdict:
            continue
        origin = ollama_policy.normalize(value)
        parsed = urllib.parse.urlparse(origin)
        host = parsed.hostname or ""
        label = f"policy_error(allow_remote={allow})"
        assert parsed.scheme in ("http", "https"), _fail(label, value, f"scheme {parsed.scheme!r} accepted")
        assert parsed.username is None and parsed.password is None and "@" not in origin, _fail(label, value, f"userinfo accepted: {origin!r}")
        assert parsed.path == "" and not parsed.params and not parsed.query and not parsed.fragment, _fail(label, value, f"path/query/fragment accepted: {origin!r}")
        assert not any(c in origin for c in "?#\\ \t\r\n\x00"), _fail(label, value, f"control/delimiter in {origin!r}")
        assert parsed.port is not None, _fail(label, value, f"accepted without explicit port: {origin!r}")
        assert host, _fail(label, value, f"accepted without host: {origin!r}")
        assert "%" not in host, _fail(label, value, f"zone id / percent in accepted host: {origin!r}")
        loopback = _oracle_loopback(host)
        if not allow:
            assert loopback, _fail(label, value, f"non-loopback host {host!r} accepted without remote opt-in")
            assert ollama_policy.is_loopback(value), _fail(label, value, "accepted but is_loopback False")
        elif not loopback:
            assert parsed.scheme == "https", _fail(label, value, f"remote host {host!r} accepted over plain http")
    timer.check()


def test_ollama_policy_known_positives_survive_padding_and_case():
    rng = random.Random(SEED + 1)
    good = ("127.0.0.1:11434", "[::1]:11434", "localhost:80", "LocalHost.:80", "0.0.0.0:80", "[::]:80", "127.0.0.1:1", "127.0.0.1:65535")
    for _ in range(300):
        base = rng.choice(good)
        scheme = rng.choice(("http://", "HTTP://", "https://", "HtTpS://", ""))
        text = "{}{}{}{}".format(rng.choice(("", " ", "\t", "\r\n")), scheme, base, rng.choice(("", "/", "//", " ")))
        assert ollama_policy.policy_error(text) == "", _fail("policy_error", text, "legitimate loopback rejected")
        assert ollama_policy.is_loopback(text), _fail("is_loopback", text, "legitimate loopback not loopback")
    bad = ("2130706433:80", "0177.0.0.1:80", "0x7f.1:80", "127.0.0.1.nip.io:80", "[fe80::1%25eth0]:80", "127。0。0。1:80",
           "１２７.０.０.１:80", "10.0.0.1:80", "169.254.169.254:80", "example.com:80", "127.0.0.1", "127.0.0.1:99999")
    for _ in range(300):
        base = rng.choice(bad)
        text = "{}{}{}".format(rng.choice(("", " ", "\r\n")), rng.choice(("http://", "HTTPS://", "")), base)
        if "%" in base:
            continue
        assert ollama_policy.policy_error(text, allow_remote=False) != "", _fail("policy_error", text, "hostile origin accepted")


def test_ollama_policy_remote_requires_https_and_consent_is_exactly_true():
    rng = random.Random(SEED + 2)
    for _ in range(400):
        host = rng.choice(("example.com", "10.0.0.5", "[2001:db8::1]", "EXAMPLE.org."))
        assert ollama_policy.policy_error(f"http://{host}:80", allow_remote=True) != ""
        assert ollama_policy.policy_error(f"https://{host}:443", allow_remote=True) == ""
        consent = rng.choice((False, 0, 1, "yes", "1", None, (), [1]))
        assert ollama_policy.policy_error(f"https://{host}:443", allow_remote=consent) != "", _fail("policy_error", consent, "non-True consent honoured")


def test_ollama_policy_error_and_logs_never_echo_userinfo_secrets():
    rng = random.Random(SEED + 3)
    handler = _LogGrab()
    logger = logging.getLogger(ollama_policy.__name__)
    old_level, old_propagate = logger.level, logger.propagate
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        for index in range(ITER):
            secret = _SECRET_PREFIX + str(index) + "".join(rng.choice("abcxyz09") for _ in range(4))
            value = _origin(rng, secret)
            if rng.random() < 0.3:
                value = value.replace("://", "://" if rng.random() < 0.5 else ":// ", 1)
            handler.text.clear()
            allow = rng.random() < 0.5
            verdict = ollama_policy.policy_error(value, allow_remote=allow)
            ollama_policy.is_loopback(value)
            assert secret not in verdict, _fail("policy_error return", value, f"secret {secret!r} echoed in {verdict!r}")
            leaked = [line for line in handler.text if secret in line]
            assert not leaked, _fail("policy_error logs", value, f"secret {secret!r} logged: {leaked[0]!r}")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        logger.propagate = old_propagate


def test_ollama_policy_regression_userinfo_with_slash_in_password_not_logged():
    secret = "ZQXSECRETpw"
    handler = _LogGrab()
    logger = logging.getLogger(ollama_policy.__name__)
    old_level, old_propagate = logger.level, logger.propagate
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        for value in (f"http://u:{secret}@evil.example:80", f"http://u:a/{secret}@evil.example:80"):
            verdict = ollama_policy.policy_error(value)
            assert verdict and secret not in verdict
        assert not [line for line in handler.text if secret in line], "debug log echoes the raw OLLAMA_HOST value including its userinfo secret"
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        logger.propagate = old_propagate


def test_ollama_normalize_is_idempotent_and_never_raises():
    rng = random.Random(SEED + 4)
    timer = _Timer("normalize")
    for _ in range(ITER * 2):
        value = _origin(rng)
        once = timer.call(ollama_policy.normalize, value)
        twice = ollama_policy.normalize(once)
        assert once == twice, _fail("normalize", value, f"not idempotent: {once!r} -> {twice!r}")
    timer.check()


def test_ollama_regression_normalize_idempotent_on_unrecognised_text():
    for value in ("http://a/ /", "http://a/\r\n/"):
        once = ollama_policy.normalize(value)
        assert ollama_policy.normalize(once) == once, f"normalize({value!r}) -> {once!r} is not a fixed point"


# ---------------------------------------------------------------------------
# platform.config normalize_origin(s)
# ---------------------------------------------------------------------------

def test_config_normalize_origin_idempotent_and_keeps_userinfo_unchanged():
    rng = random.Random(SEED + 5)
    timer = _Timer("normalize_origin")
    for _ in range(ITER * 2):
        value = _origin(rng)
        once = timer.call(platform_config.normalize_origin, value)
        assert isinstance(once, str)
        assert platform_config.normalize_origin(once) == once, _fail("normalize_origin", value, f"not idempotent: {once!r}")
        stripped = value.strip()
        if "@" in stripped.partition("://")[2].split("/")[0]:
            assert once == stripped, _fail("normalize_origin", value, f"userinfo origin rewritten to {once!r}")
        if once != stripped:
            # A rewrite only ever lower-cases scheme/host and drops "/" or a default port.
            assert re.fullmatch(r"[a-z][a-z0-9+.-]*://[^/\s?#@]+", once) and once == once.lower() or once == stripped, _fail("normalize_origin", value, f"odd rewrite {once!r}")
    timer.check()
    assert platform_config.normalize_origin("HTTP://127.0.0.1:80/") == "http://127.0.0.1"
    assert platform_config.normalize_origin("https://Example.COM:443") == "https://example.com"
    assert platform_config.normalize_origin("*") == "*"


def test_config_normalize_origins_dedupes_and_preserves_order():
    rng = random.Random(SEED + 6)
    for _ in range(300):
        values = [_origin(rng) for _ in range(rng.randint(0, 6))]
        values += [rng.choice(values)] if values else []
        result = platform_config.normalize_origins(values, setting="X")
        assert isinstance(result, tuple) and len(set(result)) == len(result)
        assert result == tuple(dict.fromkeys(platform_config.normalize_origin(v) for v in values)), _fail("normalize_origins", values, "mismatch")


# ---------------------------------------------------------------------------
# external_mcp._parse_endpoint
# ---------------------------------------------------------------------------

def _inet_aton_accepts(host: str) -> bool:
    try:
        socket.inet_aton(host)
    except (OSError, ValueError, UnicodeError):
        return False
    return True


def _is_canonical_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _url(rng: random.Random, secret: str | None = None) -> str:
    return _origin(rng, secret)


def test_external_mcp_parse_endpoint_contract_and_invariants():
    rng = random.Random(SEED + 7)
    timer = _Timer("_parse_endpoint")
    accepted = 0
    for _ in range(ITER * 2):
        value = _url(rng)
        try:
            parsed = timer.call(external_mcp._parse_endpoint, value)
        except ValueError:
            continue
        accepted += 1
        host = (parsed.hostname or "").rstrip(".")
        assert parsed.scheme in ("http", "https"), _fail("_parse_endpoint", value, f"scheme {parsed.scheme!r}")
        assert parsed.username is None and parsed.password is None, _fail("_parse_endpoint", value, "userinfo accepted")
        assert not parsed.query and not parsed.fragment, _fail("_parse_endpoint", value, "query/fragment accepted")
        assert "%" not in host and host, _fail("_parse_endpoint", value, f"scoped host accepted: {host!r}")
        assert not (_inet_aton_accepts(host) and not _is_canonical_ip(host)), _fail("_parse_endpoint", value, f"non-canonical numeric host {host!r} accepted")
    timer.check()
    assert accepted > 20, f"generator produced too few accepted endpoints ({accepted})"
    for hostile in ("http://2130706433/mcp", "http://0177.0.0.1/mcp", "http://0x7f.1/mcp", "http://127.1/", "http://u:p@a.com/", "http://a.com/?q", "http://[fe80::1%25e]/", "ftp://a.com/", "http://a.com:99999/"):
        with pytest.raises(ValueError):
            external_mcp._parse_endpoint(hostile)
    external_mcp._parse_endpoint("https://127.0.0.1:8080/mcp")


# ---------------------------------------------------------------------------
# credential_egress
# ---------------------------------------------------------------------------

_LABELS = ("good", "evil", "com", "net", "a", "x1", "xn--nxasmq6b", "GOOD", "Evil")


def _egress_host(rng: random.Random) -> str:
    kind = rng.randrange(6)
    if kind == 0:
        return rng.choice(("evil.com", "good.com", "good.com.evil.net", "evilgood.com", "evil-good.com", "sub.good.com", "good.com.", "GOOD.COM", "com", "xgood.com", "a.b.good.com", "good.com.com", "evil.com.good.com"))
    if kind == 1:
        return ".".join(rng.choice(_LABELS) for _ in range(rng.randint(1, 4)))
    if kind == 2:
        return "{}.{}".format(rng.choice(_LABELS), rng.choice(("good.com", "evil.net")))
    return ".".join(rng.choice(_LABELS) for _ in range(rng.randint(1, 3))).lower()


def _oracle_host_matches(host: str, patterns: tuple[str, ...]) -> bool:
    for pattern in patterns:
        if pattern == "*" or host == pattern:
            return True
        if pattern.startswith("*.") and host.endswith(pattern[1:]) and len(host) > len(pattern) - 1:
            return True
    return False


def test_egress_host_matches_equals_simple_oracle_and_blocks_suffix_tricks():
    rng = random.Random(SEED + 8)
    timer = _Timer("_host_matches")
    for _ in range(ITER * 3):
        patterns = tuple(rng.choice(("good.com", "*.good.com", "com", "evil.net", "*.net", "a", "*.a.good.com", "*")) for _ in range(rng.randint(1, 3)))
        host = _egress_host(rng).lower()
        got = timer.call(egress._host_matches, host, patterns)
        want = _oracle_host_matches(host, patterns)
        assert got == want, _fail("_host_matches", (host, patterns), f"got {got} want {want}")
    timer.check()
    for trick in ("evil.com", "good.com.evil.net", "evilgood.com", "xgood.com", "good.com.com", "notgood.com", "good.comx"):
        assert not egress._host_matches(trick, ("good.com",)), trick
        assert not egress._host_matches(trick, ("*.good.com",)), trick
    assert not egress._host_matches("evil.com", ("com",))
    assert not egress._host_matches("good.com", ("*.good.com",))
    assert egress._host_matches("a.good.com", ("*.good.com",))


def test_egress_normalize_host_contract_and_idempotence():
    rng = random.Random(SEED + 9)
    timer = _Timer("_normalize_host")
    for _ in range(ITER * 2):
        value = _mutate(rng, _egress_host(rng) + rng.choice(("", ".", " ", "/", "://x", "\n")), 2)
        try:
            out = timer.call(egress._normalize_host, value)
        except ValueError:
            continue
        assert out == out.lower() and out.isascii() and out, _fail("_normalize_host", value, f"bad output {out!r}")
        assert "/" not in out and not out.endswith("."), _fail("_normalize_host", value, f"bad output {out!r}")
        assert egress._normalize_host(out) == out, _fail("_normalize_host", value, f"not idempotent: {out!r}")
    timer.check()


def _egress_url(rng: random.Random) -> str:
    scheme = rng.choice(("https", "http", "HTTPS", "ftp", "file", "javascript"))
    auth = rng.choice(("", "", "", "u:p@", "good.com@", "good.com\\@", "good.com#@"))
    host = rng.choice((_egress_host(rng), _host(rng)))
    port = rng.choice(("", "", ":443", ":80", ":0", ":65536", ":x", ":8443"))
    tail = rng.choice(("", "/", "/p", "?x=.good.com", "#.good.com", "/.good.com", "\\x"))
    text = f"{scheme}://{auth}{host}{port}{tail}"
    return _mutate(rng, text, 2) if rng.random() < 0.3 else text


def test_egress_redirect_chain_validate_honours_policy_and_scope():
    rng = random.Random(SEED + 10)
    timer = _Timer("validate")
    accepted = 0
    for _ in range(ITER * 2):
        allowed = tuple(rng.choice(("good.com", "*.good.com", "evil.net", "com")) for _ in range(rng.randint(0, 2)))
        policy = egress.EgressPolicy(allowed_hosts=allowed, allowed_protocols=rng.choice((("https",), ("https", "http"))))
        handle = None
        if rng.random() < 0.5:
            handle = egress.CredentialHandle.mint("i", rng.choice((("good.com",), ("*.good.com",), ("good.com", "evil.net"))))
        hops = tuple(_egress_url(rng) for _ in range(rng.randint(1, 3)))
        try:
            targets = timer.call(egress.RedirectChain(hops).validate, policy, handle)
        except egress.EgressDenied:
            continue
        except ValueError as exc:
            # _normalize_host on an allowlist entry is the only other ValueError source; ours are valid.
            raise AssertionError(_fail("validate", (hops, allowed), f"non-EgressDenied ValueError {exc!r}")) from exc
        accepted += 1
        assert len(targets) == len(hops)
        for hop, target in zip(hops, targets, strict=True):
            assert target.protocol in {p.lower() for p in policy.allowed_protocols}, _fail("validate", hop, f"protocol {target.protocol!r}")
            assert "@" not in target.host and target.host and "/" not in target.host, _fail("validate", hop, f"host {target.host!r}")
            if allowed:
                assert _oracle_host_matches(target.host, tuple(egress._normalize_host(h) for h in allowed)), _fail("validate", hop, f"host {target.host!r} outside allowlist {allowed!r}")
            if handle is not None:
                assert _oracle_host_matches(target.host, handle.hosts) and target.protocol in handle.protocols, _fail("validate", hop, "outside credential scope")
            if _is_canonical_ip(target.host):
                ip = ipaddress.ip_address(target.host)
                assert not (ip.is_loopback or ip.is_link_local or ip.is_private), _fail("validate", hop, f"private literal {target.host!r} accepted")
    timer.check()
    assert accepted > 10, f"generator produced too few accepted chains ({accepted})"
    policy = egress.EgressPolicy(allowed_hosts=("good.com",))
    handle = egress.CredentialHandle.mint("i", ("good.com",))
    for hostile in ("https://good.com.evil.net/", "https://evilgood.com/", "https://good.com@evil.com/", "https://evil.com#.good.com", "https://evil.com/?good.com", "https://good.com\\@evil.com/"):
        with pytest.raises(egress.EgressDenied):
            egress.RedirectChain((hostile,)).validate(policy, handle)
    assert not handle.allows("https://good.com.evil.net/")
    assert handle.allows("https://good.com/x")


# ---------------------------------------------------------------------------
# web_tools
# ---------------------------------------------------------------------------

_NON_GLOBAL_NETS = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24",
    "192.0.2.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
    "::/128", "::1/128", "fc00::/7", "fe80::/10", "ff00::/8", "2001:db8::/32",
))
_PUBLIC_SAMPLES = ("8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111", "2001:4860:4860::8888")


def _oracle_global(ip) -> bool:
    mapped = getattr(ip, "ipv4_mapped", None)
    ip = mapped or ip
    return not any(ip.version == net.version and ip in net for net in _NON_GLOBAL_NETS)


def _addr_text(rng: random.Random) -> str:
    kind = rng.randrange(5)
    if kind == 0:
        return str(ipaddress.IPv4Address(rng.choice(_SPECIAL_V4)))
    if kind == 1:
        return str(ipaddress.IPv4Address(rng.getrandbits(32)))
    if kind == 2:
        return str(ipaddress.IPv6Address((0xFFFF << 32) | rng.choice(_SPECIAL_V4)))
    if kind == 3:
        return str(ipaddress.IPv6Address(rng.choice((1, 0, 0xFE80 << 112 | rng.getrandbits(16), 0xFC00 << 112 | 5, rng.getrandbits(128)))))
    return rng.choice(_PUBLIC_SAMPLES)


def test_web_tools_is_globally_routable_matches_oracle():
    rng = random.Random(SEED + 11)
    timer = _Timer("_is_globally_routable")
    for _ in range(ITER * 3):
        text = _addr_text(rng)
        ip = ipaddress.ip_address(text)
        got = timer.call(web_tools._is_globally_routable, rng.choice((text, ip)))
        if not _oracle_global(ip):
            assert got is False, _fail("_is_globally_routable", text, "non-global address reported routable")
    timer.check()
    for sample in _PUBLIC_SAMPLES:
        assert web_tools._is_globally_routable(sample), sample
    for sample in ("::ffff:127.0.0.1", "::ffff:10.0.0.1", "::ffff:169.254.169.254", "::ffff:7f00:1", "::", "0.0.0.0", "fe80::1", "ff02::1"):
        assert not web_tools._is_globally_routable(sample), sample
    for junk in ("2130706433", "0x7f.1", "x", ""):
        with pytest.raises(ValueError):
            web_tools._is_globally_routable(junk)


def test_web_tools_noncanonical_numeric_host_matches_oracle():
    rng = random.Random(SEED + 12)
    timer = _Timer("_looks_like_noncanonical_numeric_host")
    numeric = ("2130706433", "0177.0.0.1", "0x7f.1", "0x7f000001", "017700000001", "127.1", "127.0.1", "0X7F.0.0.1", "0", "00", "0x0", "4294967295")
    for _ in range(ITER * 2):
        host = rng.choice(numeric) if rng.random() < 0.4 else _mutate(rng, rng.choice(numeric + ("127.0.0.1", "a.com", "1.2.3.4")), 2)
        got = timer.call(web_tools._looks_like_noncanonical_numeric_host, host)
        stripped = host.rstrip(".")
        parts = stripped.split(".")
        all_numeric = bool(stripped) and ":" not in stripped and all(p and p.isascii() and _is_numeric_part(p) for p in parts)
        want = all_numeric and not _is_canonical_ip(stripped)
        assert got == want, _fail("_looks_like_noncanonical_numeric_host", host, f"got {got} want {want}")
    timer.check()
    for host in numeric:
        for suffix in ("", "."):
            assert web_tools._looks_like_noncanonical_numeric_host(host + suffix) is True, host + suffix
    assert web_tools._looks_like_noncanonical_numeric_host("127.0.0.1") is False
    assert web_tools._looks_like_noncanonical_numeric_host("") is False
    assert web_tools._looks_like_noncanonical_numeric_host(None) is False


def _is_numeric_part(part: str) -> bool:
    if part[:2].lower() == "0x":
        return len(part) > 2 and all(c in "0123456789abcdefABCDEF" for c in part[2:])
    return part.isdigit() and part.isascii()  # decimal, or octal-with-leading-zero (incl. invalid octal like 081)


def _fake_resolver(rng: random.Random):
    """Return a deterministic getaddrinfo replacement; also records its answers."""
    answers: dict[str, list[str]] = {}

    def getaddrinfo(host, port, *args, **kwargs):
        if _is_canonical_ip(host):  # a real resolver returns a literal unchanged
            answers[host] = [host]
        if host not in answers:
            count = rng.choice((0, 1, 1, 1, 2, 3))
            answers[host] = [_addr_text(rng) if rng.random() < 0.5 else rng.choice(_PUBLIC_SAMPLES) for _ in range(count)]
        rows = []
        for text in answers[host]:
            ip = ipaddress.ip_address(text)
            if ip.version == 4:
                rows.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (text, port)))
            else:
                rows.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "", (text + rng.choice(("", "%eth0")), port, 0, 0)))
        return rows

    getaddrinfo.answers = answers
    return getaddrinfo


def _public_url(rng: random.Random) -> str:
    return _origin(rng, None).replace("ftp", "http") if rng.random() < 0.2 else _origin(rng, None)


def test_web_tools_validated_public_target_never_accepts_non_global_or_hostile():
    rng = random.Random(SEED + 13)
    timer = _Timer("_validated_public_target")
    resolver = _fake_resolver(rng)
    real = socket.getaddrinfo
    socket.getaddrinfo = resolver
    accepted = 0
    try:
        for _ in range(ITER * 2):
            value = _public_url(rng)
            try:
                parsed, addresses = timer.call(web_tools._validated_public_target, value)
            except ValueError:
                continue
            accepted += 1
            host = (parsed.hostname or "").rstrip(".")
            assert parsed.scheme in ("http", "https"), _fail("_validated_public_target", value, f"scheme {parsed.scheme!r}")
            assert parsed.username is None and parsed.password is None, _fail("_validated_public_target", value, "userinfo accepted")
            assert "%" not in host, _fail("_validated_public_target", value, f"host {host!r}")
            assert not (_inet_aton_accepts(host) and not _is_canonical_ip(host)), _fail("_validated_public_target", value, f"non-canonical numeric host {host!r} accepted")
            assert addresses, _fail("_validated_public_target", value, "accepted with no addresses")
            for text in addresses:
                ip = ipaddress.ip_address(text)
                assert _oracle_global(ip), _fail("_validated_public_target", value, f"non-global {text} in {addresses!r}")
            if not _is_canonical_ip(host):
                # Every address the resolver returned must have been global.
                for key, texts in resolver.answers.items():
                    if key == host.encode("idna").decode("ascii") if host.isascii() else False:
                        assert all(_oracle_global(ipaddress.ip_address(t)) for t in texts), _fail("_validated_public_target", value, f"resolver answers {texts!r}")
    finally:
        socket.getaddrinfo = real
    timer.check()
    assert accepted > 10, f"generator produced too few accepted URLs ({accepted})"


def test_web_tools_validated_public_target_blocks_planted_private_literals():
    rng = random.Random(SEED + 14)
    real = socket.getaddrinfo

    def public_only(host, port, *args, **kwargs):
        if _is_canonical_ip(host):
            return [(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, port))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    socket.getaddrinfo = public_only
    try:
        planted = [h for h in _LOOPBACK_TRICKS if not h.startswith(("example", "EXAMPLE", "xn--", "8.8.8.8"))]
        planted += ["[::ffff:127.0.0.1]", "[::1]", "[::]", "169.254.169.254", "0.0.0.0", "localhost", "LOCALHOST.", "localhost.localdomain"]
        for _ in range(600):
            host = rng.choice(planted)
            scheme = rng.choice(("http", "https", "HTTP"))
            suffix = rng.choice(("", "/", "/x?y=1", ":8080/a", "#f"))
            url = f"{scheme}://{host}{suffix}"
            if host in ("127.0.0.1.nip.io",) or host.startswith(("10.", "192.168.")) is False and host in ("a b",):
                continue
            try:
                web_tools._validated_public_target(url)
            except ValueError:
                continue
            # Only the public-resolving DNS spelling may be accepted; never an IP-literal or alias of private space.
            parsed = urllib.parse.urlparse(url)
            h = (parsed.hostname or "").rstrip(".")
            assert not _is_canonical_ip(h) or _oracle_global(ipaddress.ip_address(h)), _fail("_validated_public_target", url, "private literal accepted")
            assert not _inet_aton_accepts(h) or _is_canonical_ip(h), _fail("_validated_public_target", url, "numeric alias accepted")
            assert h.lower() not in ("localhost", "localhost.localdomain") or h != (parsed.hostname or ""), _fail("_validated_public_target", url, "bare localhost accepted")
    finally:
        socket.getaddrinfo = real
    for bad in ("file:///etc/passwd", "ftp://a.com/", "javascript:alert(1)", "http://u:p@a.com/", "http:///x", "http://a.com:99999/", "//a.com/"):
        with pytest.raises(ValueError):
            web_tools._validated_public_target(bad)


def test_web_tools_resolver_mixing_private_and_public_is_rejected():
    real = socket.getaddrinfo
    for private in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1", "0.0.0.0", "::", "fe80::1"):
        for order in (0, 1):
            texts = ["93.184.216.34", private] if order else [private, "93.184.216.34"]

            def fake(host, port, *args, _texts=tuple(texts), **kwargs):
                return [((socket.AF_INET6 if ":" in t else socket.AF_INET), socket.SOCK_STREAM, 6, "", (t, port)) for t in _texts]

            socket.getaddrinfo = fake
            try:
                with pytest.raises(ValueError):
                    web_tools._validated_public_target("https://rebind.example/")
            finally:
                socket.getaddrinfo = real


_RESULT_PAYLOADS = (
    "javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,<script>1</script>", "file:///etc/passwd", "vbscript:x",
    "//evil.com/x", "/relative", "ftp://a.com/", "https://ok.example/a", "http://ok.example/b?c=d", " javascript:alert(1)",
    "java\tscript:alert(1)", "java&#x09;script:alert(1)", "&#106;avascript:alert(1)", "\x01javascript:alert(1)", "",
)


def _result_hrefs(rng: random.Random, bing: bool) -> str:
    payload = rng.choice(_RESULT_PAYLOADS)
    if rng.random() < 0.3:
        payload = _mutate(rng, payload, 2)
    style = rng.randrange(4)
    if style == 0:
        return payload
    if style == 1:
        return "//duckduckgo.com/l/?uddg={}&rut=1".format(urllib.parse.quote(payload, safe=""))
    if style == 2:
        encoded = base64.urlsafe_b64encode(payload.encode("utf-8", "replace")).decode().rstrip("=")
        return f"https://www.bing.com/ck/a?!&&p=1&u=a1{encoded}&ntb=1"
    return rng.choice(("https://www.bing.com/ck/a?u=a1", "https://www.bing.com/ck/a?u=a1%%%", "x?u=a1" + _mutate(rng, "aHR0cDovL2E=", 2), "?u=", "?uddg=")) + payload[:0]


def _only_http(url) -> bool:
    return urllib.parse.urlparse(str(url)).scheme.lower() in ("http", "https")


def test_web_tools_clean_result_urls_are_http_only():
    rng = random.Random(SEED + 15)
    timer = _Timer("clean_result_url")
    for cleaner in (web_tools._clean_result_url, web_tools._clean_bing_result_url):
        for _ in range(ITER):
            href = _result_hrefs(rng, cleaner is web_tools._clean_bing_result_url)
            try:
                out = timer.call(cleaner, href)
            except ValueError as exc:
                raise AssertionError(_fail(cleaner.__name__, href, f"raised {exc!r}")) from exc
            assert isinstance(out, str)
            assert _only_http(out), _fail(cleaner.__name__, href, f"non-http(s) result {out!r}")
            assert "\r" not in out and "\n" not in out, _fail(cleaner.__name__, href, f"CR/LF in {out!r}")
    timer.check()


def test_web_tools_regression_javascript_href_is_not_returned_as_result_url():
    for href in ("javascript:alert(1)", "data:text/html,x", "file:///etc/passwd"):
        assert _only_http(web_tools._clean_result_url(href)), f"_clean_result_url({href!r}) -> {web_tools._clean_result_url(href)!r}"
        assert _only_http(web_tools._clean_bing_result_url(href)), f"_clean_bing_result_url({href!r}) -> {web_tools._clean_bing_result_url(href)!r}"


def test_web_tools_search_rows_pipeline_only_emits_http_urls():
    rng = random.Random(SEED + 16)
    for source in ("https://duckduckgo.com/html/", "https://www.bing.com/search", "https://www.mojeek.com/search"):
        for _ in range(150):
            href = _result_hrefs(rng, "bing" in source).replace('"', "%22")
            page = f'<li class="b_algo"><h2><a href="{href}">T</a></h2></li><a class="result__a" href="{href}">T</a>'
            for row in web_tools._search_rows(page, source, 5):
                assert _only_http(row["url"]), _fail("_search_rows", href, "non-http(s) {!r}".format(row["url"]))


def test_web_tools_clean_result_known_positives():
    target = "https://ok.example/path?a=b"
    quoted = urllib.parse.quote(target, safe="")
    assert web_tools._clean_result_url(f"//duckduckgo.com/l/?uddg={quoted}&rut=x") == target
    assert web_tools._clean_result_url(target) == target
    encoded = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    assert web_tools._clean_bing_result_url(f"https://www.bing.com/ck/a?u=a1{encoded}") == target
    jsenc = base64.urlsafe_b64encode(b"javascript:alert(1)").decode().rstrip("=")
    assert _only_http(web_tools._clean_bing_result_url(f"https://www.bing.com/ck/a?u=a1{jsenc}"))


# ---------------------------------------------------------------------------
# gateways: normalize_base_url
# ---------------------------------------------------------------------------

def _gateway_url(rng: random.Random) -> str:
    text = _origin(rng, None)
    if rng.random() < 0.5:
        text += rng.choice(("/v1", "/v1/", "/api/v1", "/v2", "/x/y", "/v1?k=v", "/v1#f", "/v1\r\nX: y"))
    if rng.random() < 0.15:
        text = text.replace("://", "://u:p@", 1)
    return text


def _check_gateway_common(label, value, out):
    assert isinstance(out, str)
    parts = urllib.parse.urlsplit(out)
    assert parts.scheme in ("http", "https"), _fail(label, value, f"scheme in {out!r}")
    assert parts.hostname, _fail(label, value, f"no host in {out!r}")
    assert parts.username is None and parts.password is None and "@" not in parts.netloc, _fail(label, value, f"userinfo in {out!r}")
    assert not parts.query and not parts.fragment and "?" not in out and "#" not in out, _fail(label, value, f"query/fragment in {out!r}")
    assert not any(c in out for c in "\r\n\t\x00"), _fail(label, value, f"CR/LF/tab/NUL in {out!r}")
    assert not any(c.isspace() for c in parts.hostname), _fail(label, value, f"whitespace in host of {out!r}")
    return parts


@pytest.mark.parametrize("module,label", [
    (sonder_inference_gateway, "sonder"), (openrouter_gateway, "openrouter"), (anthropic_gateway, "anthropic"),
])
def test_gateway_normalize_base_url_contract(module, label):
    rng = random.Random(SEED + 20 + len(label))
    timer = _Timer(f"normalize_base_url[{label}]")
    accepted = 0
    for _ in range(ITER * 2):
        value = _gateway_url(rng)
        try:
            out = timer.call(module.normalize_base_url, value)
        except InvalidInput:
            continue
        accepted += 1
        parts = _check_gateway_common(label, value, out)
        host = parts.hostname
        if label == "anthropic":
            assert parts.scheme == "https", _fail(label, value, f"plain http accepted: {out!r}")
        elif label == "openrouter":
            if parts.scheme != "https":
                assert host == "localhost" or _oracle_loopback(host), _fail(label, value, f"remote plain http accepted: {out!r}")
            assert parts.path.endswith("/v1"), _fail(label, value, f"path not /v1: {out!r}")
        else:
            assert host not in ("0.0.0.0", "::"), _fail(label, value, f"bind-all host survived: {out!r}")
            if _oracle_loopback(host):
                assert host in module.ACCEPTED_LOOPBACK_HOSTS, _fail(label, value, f"loopback alias {host!r} accepted")
            assert out == module.normalize_base_url(out), _fail(label, value, f"not idempotent: {out!r}")
    timer.check()
    assert accepted > 10, f"{label} generator produced too few accepted URLs ({accepted})"


def test_gateway_normalize_base_url_documented_rules_by_example():
    for bad in ("http://example.com/v1", "https://u:p@example.com/v1", "https://example.com/v1?x=1", "https://example.com/v1#f", "ftp://example.com/v1", "https:///v1", "https://example.com:99999/v1", "", "javascript:alert(1)"):
        for module in (openrouter_gateway, anthropic_gateway):
            with pytest.raises(InvalidInput):
                module.normalize_base_url(bad)
    with pytest.raises(InvalidInput):
        anthropic_gateway.normalize_base_url("http://127.0.0.1:8080")
    assert openrouter_gateway.normalize_base_url("http://127.0.0.1:8080/v1/") == "http://127.0.0.1:8080/v1"
    with pytest.raises(InvalidInput):
        openrouter_gateway.normalize_base_url("https://example.com/api")
    assert sonder_inference_gateway.normalize_base_url("http://0.0.0.0:9000") == "http://127.0.0.1:9000"
    assert sonder_inference_gateway.normalize_base_url("http://[::]:9000") == "http://[::1]:9000"
    with pytest.raises(InvalidInput):
        sonder_inference_gateway.normalize_base_url("http://127.0.0.2:9000")
    with pytest.raises(InvalidInput):
        sonder_inference_gateway.normalize_base_url("http://u:p@example.com:9000")
