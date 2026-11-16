"""Seeded mutation fuzzing of the HTTP request-input helpers.

Contract fuzzed: every attacker-controlled request input (target path, ``Host``,
``Origin``, ``If-None-Match``, ``X-Forwarded-For``) either yields the helper's
documented result or its documented typed error, within the time budget, and
the accepted results satisfy the security invariants asserted per test:

* ``web_app``: ``valid_segment``, ``valid_relpath``, ``_unquote_strict``
  (``UnicodeDecodeError``), ``is_app_target``, ``_etag_matches``,
  ``same_origin``, ``same_origin_request`` and ``resolve_request`` (a "file"
  result is always a listed file inside the release directory).
* ``host_policy``: ``parse_host_header``, ``host_decision``, ``host_allowed``,
  ``reflectable_origin``, ``forwarded_client_ip`` and ``normalize_allowed_host``
  (``ValueError``).

Every input is timed (p99 < 50 ms, hard cap 2 s).  The seed is fixed and is
printed in every failure message; override it with ``SONDER_FUZZ_SEED`` to
replay or widen a run.
"""
from __future__ import annotations

import http.client
import io
import ipaddress
import os
import random
import re
import string
import time
import urllib.parse
from pathlib import Path

import pytest

from sonder_runtime.interfaces.http import web_app
from sonder_runtime.interfaces.http.host_policy import (
    HOST_CREDENTIALED,
    HOST_TRUSTED,
    forwarded_client_ip,
    host_allowed,
    host_decision,
    normalize_allowed_host,
    parse_host_header,
    reflectable_origin,
)
from tests.web_app_fixtures import DEFAULT_FILES, write_release

SEED = int(os.environ.get("SONDER_FUZZ_SEED", "20261116"))
ITER = 3000

_KINDS = {"file": 200, "redirect": 308, "not_modified": 304, "not_built": 404, "not_found": 404}
_ALNUM = string.ascii_lowercase + string.digits
_NOISE = string.ascii_letters + string.digits + " %/\\?#:@.-_~+*[]\r\n\x00\t\"'<>|^`{}&=;,$!()"
_SEGMENT_CHARS = frozenset(string.ascii_letters + string.digits + " ._@+~-")


# -- harness -----------------------------------------------------------------

def _rng(salt: int) -> random.Random:
    return random.Random(SEED * 1000 + salt)


def _fail(message: str, data: object) -> None:
    raise AssertionError(f"seed={SEED}: {message}; input={data!r}")


class _Probe:
    """Calls a function under test, timing it and enforcing the error contract."""

    def __init__(self, allowed: tuple[type[BaseException], ...] = ()) -> None:
        self.allowed = allowed
        self.timings: list[float] = []

    def call(self, fn, *args, **kwargs):
        started = time.perf_counter()
        try:
            return fn(*args, **kwargs), None
        except self.allowed as error:
            return None, error
        except Exception as error:
            raise AssertionError(
                f"seed={SEED}: {fn.__name__} raised {error!r}; input={(args, kwargs)!r}") from error
        finally:
            self.timings.append(time.perf_counter() - started)

    def finish(self) -> None:
        timings = sorted(self.timings)
        assert timings, "no inputs were run"
        p99 = timings[max(int(len(timings) * 0.99) - 1, 0)]
        assert p99 < 0.05, f"seed={SEED}: p99 {p99 * 1000:.1f} ms"
        assert timings[-1] < 2.0, f"seed={SEED}: max {timings[-1] * 1000:.1f} ms"


def _mutate(rng: random.Random, seed: str, seeds: list[str], frags: tuple[str, ...]) -> str:
    text = seed
    for _ in range(rng.randint(1, 4)):
        op = rng.randrange(7)
        at = rng.randrange(len(text) + 1)
        if op == 0 and text:
            at = min(at, len(text) - 1)
            text = text[:at] + rng.choice(_NOISE) + text[at + 1:]
        elif op == 1:
            text = text[:at] + text[at + rng.randint(1, 6):]
        elif op == 2:
            text = text[:at] + rng.choice(frags) + text[at:]
        elif op == 3:
            donor = rng.choice(seeds)
            start = rng.randrange(len(donor) + 1)
            text = text[:at] + donor[start:start + rng.randint(1, 20)] + text[at:]
        elif op == 4 and text:
            start = rng.randrange(len(text))
            chunk = text[start:start + rng.randint(1, 8)]
            text = text[:at] + chunk * rng.randint(2, 40) + text[at:]
        elif op == 5:
            text = "".join(c.swapcase() if rng.random() < 0.3 else c for c in text)
        else:
            text = text[:at] + str(rng.choice((0, 1, 65535, 65536, 99999, 2**31, 2**32, -1))) + text[at:]
        text = text[:700]
    return text


def _case_fold_noise(rng: random.Random, text: str) -> str:
    return "".join(c.swapcase() if rng.random() < 0.5 else c for c in text)


def _plant(rng: random.Random, token: str, prefixes: list[str], suffixes: list[str]) -> str:
    return rng.choice(prefixes) + token + rng.choice(suffixes)


# -- web_app: segments and relative paths -------------------------------------

_BAD_SEGMENT_TOKENS = (
    "\\", "\x00", ":", "::$DATA", "/", "%", "\r", "\n", "?", "#", "*", "<", ">", "|", '"', "\t", "\u202e",
    "\u2024", "\uff0e", "\u00e9", "%2e", "%00", "\x7f",
)
_WHOLE_BAD_SEGMENTS = ("", ".", "..", "...", ".hidden", ".git", ".htaccess")
_SEG_PREFIXES = ["", "a", "main", "x-1", "assets", "Flutter_bootstrap", "a b", "9"]
_SEG_SUFFIXES = ["", "b", ".js", ".woff2", "-2", "~1", "+v"]


def test_valid_segment_matches_documented_alphabet_oracle():
    """Segments are non-empty, not dot-led, and drawn only from ``[A-Za-z0-9 ._@+~-]``."""
    rng = _rng(1)
    probe = _Probe()
    seeds = ["main.dart.js", "a", "..", ".", "canvaskit", "a b", "x@2+y~z", "con", "a.", "NUL"]
    frags = _BAD_SEGMENT_TOKENS + _WHOLE_BAD_SEGMENTS + ("%252e", "%c0%ae", "..", "a" * 300)
    for _ in range(ITER):
        segment = _mutate(rng, rng.choice(seeds), seeds, frags)
        got, _ = probe.call(web_app.valid_segment, segment)
        oracle = (bool(segment) and not segment.startswith(".")
                  and all(ch in _SEGMENT_CHARS for ch in segment))
        if got is not oracle:
            _fail(f"valid_segment={got}, oracle={oracle}", segment)
    probe.finish()


def test_valid_segment_rejects_planted_hostile_characters_and_dot_segments():
    """Known-positive: a hostile char planted in valid context is always refused."""
    rng = _rng(2)
    probe = _Probe()
    for _ in range(ITER):
        token = rng.choice(_BAD_SEGMENT_TOKENS)
        segment = _plant(rng, token, _SEG_PREFIXES, _SEG_SUFFIXES)
        got, _ = probe.call(web_app.valid_segment, segment)
        if got:
            _fail("hostile character accepted by valid_segment", segment)
        whole = rng.choice(_WHOLE_BAD_SEGMENTS)
        got, _ = probe.call(web_app.valid_segment, whole)
        if got:
            _fail("empty/dot segment accepted by valid_segment", whole)
        got, _ = probe.call(web_app.valid_segment, "." + _plant(rng, "", _SEG_PREFIXES, _SEG_SUFFIXES))
        if got:
            _fail("dot-led segment accepted by valid_segment", segment)
    probe.finish()


def test_valid_relpath_never_accepts_absolute_drive_traversal_or_backslash():
    """An accepted relpath is relative, drive-free, has no ``..``/empty segment, NUL or backslash."""
    rng = _rng(3)
    probe = _Probe()
    seeds = ["index.html", "assets/fonts/a.ttf", "/etc/passwd", "C:/x", "..", "a/../b", "a//b", "a\\b", "a/",
             "/", "", "canvaskit/canvaskit.wasm", "a/b/c/d.js"]
    frags = ("/", "//", "../", "/..", "\\", "C:", "c:/", "%2e%2e", "\x00", "::$DATA", ".", "./", "a.", "a ", "..\\")
    for _ in range(ITER):
        relpath = _mutate(rng, rng.choice(seeds), seeds, frags)
        got, _ = probe.call(web_app.valid_relpath, relpath)
        oracle = bool(relpath) and all(
            part and not part.startswith(".") and all(ch in _SEGMENT_CHARS for ch in part)
            for part in relpath.split("/"))
        if got is not oracle:
            _fail(f"valid_relpath={got}, oracle={oracle}", relpath)
        if got:
            parts = relpath.split("/")
            assert not relpath.startswith("/") and not re.match(r"[A-Za-z]:", relpath), relpath
            assert ".." not in parts and "" not in parts and "." not in parts, relpath
            assert "\x00" not in relpath and "\\" not in relpath and ":" not in relpath, relpath
    for value in (None, 0, b"a", ["a"], ("a",), {"a": 1}):
        got, _ = probe.call(web_app.valid_relpath, value)
        assert got is False, (SEED, value)
    probe.finish()


# -- web_app: percent decoding -----------------------------------------------

def test_unquote_strict_decodes_exactly_once_and_raises_only_unicode_decode_error():
    """One decoding pass (``%252e`` stays ``%2e``), UTF-8 strict, overlong forms refused."""
    rng = _rng(4)
    probe = _Probe((UnicodeDecodeError,))
    seeds = ["a%2eb", "%2e%2e", "%252e%252e", "%c0%ae", "%C0%AE%C0%AE", "%e0%80%af", "caf%C3%A9", "%", "%2", "%zz",
             "%f0%9f%98%80", "%ff", "%80", "plain/text.js", "%2E%2e%2F", "%5C", "%00"]
    frags = ("%", "%2", "%2e", "%2E", "%252e", "%c0%ae", "%c1%9c", "%e0%80%ae", "%ff", "%c0", "%af", "\u00e9", "\x00")
    for _ in range(ITER):
        text = _mutate(rng, rng.choice(seeds), seeds, frags)
        got, error = probe.call(web_app._unquote_strict, text)
        try:
            expected = urllib.parse.unquote_to_bytes(text).decode("utf-8")
        except UnicodeDecodeError:
            expected = None
        if error is not None:
            assert expected is None, (SEED, text, "raised but the reference decodes")
            continue
        if got != expected:
            _fail(f"decoded {got!r}, reference {expected!r}", text)
        if "%25" in text.lower():
            # the escaped percent survives as a literal '%' (no second decode)
            assert "%" in got, (SEED, text, got)
    probe.finish()


def test_unquote_strict_overlong_and_double_encoded_dots_never_become_dot_dot():
    """Known-positive: overlong ``../`` encodings fail to decode; double encoding stays escaped."""
    rng = _rng(5)
    probe = _Probe((UnicodeDecodeError,))
    overlong = ("%c0%ae%c0%ae", "%C0%AE%C0%AE", "%c0%2e", "%e0%80%ae", "%c0%af", "%c1%9c", "%c1%1c")
    for _ in range(500):
        text = _plant(rng, rng.choice(overlong), ["", "a", "x/"], ["", "/b", "%2f"])
        got, error = probe.call(web_app._unquote_strict, text)
        if error is None:
            _fail("overlong sequence decoded to " + repr(got), text)
        text = _plant(rng, "%252e%252e", ["", "a/"], ["", "/b"])
        got, error = probe.call(web_app._unquote_strict, text)
        assert error is None and ".." not in got and "%2e%2e" in got, (SEED, text, got)
    probe.finish()


def test_unquote_strict_lone_surrogate_raises_a_unicode_error_and_resolve_refuses_it():
    # A lone surrogate cannot be UTF-8 encoded, so this is UnicodeEncodeError (a UnicodeError, not the
    # documented UnicodeDecodeError); resolve_request catches UnicodeError, so the request is a clean 404.
    with pytest.raises(UnicodeError):
        web_app._unquote_strict("a\ud800b")
    load = web_app.BundleLoad.error("X", "x")
    assert web_app.resolve_request("/app/a\ud800b", load).kind == "not_built"
    assert web_app.resolve_request("/app/a\ud800b", _release_load()).kind == "not_found"


# -- web_app: routing prefix --------------------------------------------------

def test_is_app_target_matches_prefix_oracle():
    """``/app`` or ``/app/...`` only, judged on the path before the first ``?`` or ``#``."""
    rng = _rng(6)
    probe = _Probe()
    seeds = ["/app", "/app/", "/app?x=1", "/app#f", "/app/x?y#z", "/apple", "/application/x", "//app", "/APP/",
             "/app%2fx", "/v1/app", "/", "", "app", "/app\r\n/x", "/app\\x", "/app/../v1/status"]
    frags = ("/", "?", "#", "/app", "/app/", "%2f", "\r\n", "\x00", "..", "\\", "//")
    for _ in range(ITER):
        target = _mutate(rng, rng.choice(seeds), seeds, frags)
        got, _ = probe.call(web_app.is_app_target, target)
        path = re.split(r"[#?]", target, maxsplit=1)[0]
        expected = path == "/app" or path.startswith("/app/")
        if got is not expected:
            _fail(f"is_app_target={got}, expected {expected}", target)
    for _ in range(500):
        sep = rng.choice(["/", "?", "#", ""])
        tail = "".join(rng.choice(_NOISE) for _ in range(rng.randint(0, 12))) if sep else ""
        got, _ = probe.call(web_app.is_app_target, "/app" + sep + tail)
        assert got is True, (SEED, "/app" + sep + tail)
        junk = "".join(rng.choice(string.ascii_letters + "%.-_~") for _ in range(rng.randint(1, 6)))
        got, _ = probe.call(web_app.is_app_target, "/app" + junk + rng.choice(["", "/x", "?q"]))
        assert got is False, (SEED, "/app" + junk)
    probe.finish()


# -- web_app: If-None-Match ----------------------------------------------------

_ETAG = '"' + "ab" * 32 + '"'


def test_etag_matches_never_raises_and_follows_if_none_match_grammar():
    """``*`` and (weak-prefixed) exact quoted etags match; anything else does not."""
    rng = _rng(7)
    probe = _Probe()
    seeds = [_ETAG, "*", "W/" + _ETAG, f'"x", {_ETAG}', "", " ", ",,,", '"nope"', _ETAG[1:-1], "w/" + _ETAG,
             '"' + "ab" * 31 + '"', _ETAG + _ETAG, "\u00e9", "*, \"a\""]
    frags = (",", " ", "\t", "*", "W/", "w/", '"', _ETAG, "\r\n", "\x00", "\u2028")
    for _ in range(ITER):
        header = _mutate(rng, rng.choice(seeds), seeds, frags)
        got, _ = probe.call(web_app._etag_matches, header, _ETAG)
        tokens = [t.strip() for t in header.split(",")]
        expected = any(t == "*" or (t.removeprefix("W/")) == _ETAG for t in tokens)
        if got is not expected:
            _fail(f"_etag_matches={got}, expected {expected}", header)
    for value in (None, ""):
        got, _ = probe.call(web_app._etag_matches, value, _ETAG)
        assert got is False
    probe.finish()


def test_etag_matches_detects_planted_etag_and_star_but_not_near_misses():
    rng = _rng(8)
    probe = _Probe()
    pads = ["", " ", "  ", "\t", " \t "]
    others = ['"zz"', 'W/"zz"', '"' + "cd" * 32 + '"', "garbage", "W/", "W/W/" + _ETAG[:5]]
    for _ in range(ITER // 2):
        before = ", ".join(rng.sample(others, rng.randint(0, 3)))
        after = ", ".join(rng.sample(others, rng.randint(0, 3)))
        match = rng.choice([_ETAG, "W/" + _ETAG, "*"])
        parts = [p for p in (before, rng.choice(pads) + match + rng.choice(pads), after) if p]
        got, _ = probe.call(web_app._etag_matches, ", ".join(parts), _ETAG)
        assert got is True, (SEED, parts)
        near = rng.choice([_ETAG[1:-1], _ETAG[:-1], _ETAG + "x", "w/" + _ETAG, '"*"', "**", "*x", "W/W/" + _ETAG])
        parts = [p for p in (before, near, after) if p]
        got, _ = probe.call(web_app._etag_matches, ", ".join(parts), _ETAG)
        assert got is False, (SEED, parts)
    probe.finish()


# -- web_app: resolve_request ---------------------------------------------------

_RELEASE: dict[str, object] = {}


@pytest.fixture(scope="module", autouse=True)
def _release(tmp_path_factory):
    root = tmp_path_factory.mktemp("fuzz-http-release")
    write_release(root)
    secret = root / "outside.txt"
    secret.write_bytes(b"SECRET-OUTSIDE-THE-RELEASE")
    (root / "releases" / "r1" / "unlisted.txt").write_bytes(b"SECRET-UNLISTED")
    load = web_app.load_bundle(root)
    assert load.ok, load.message
    _RELEASE["load"] = load
    _RELEASE["root"] = root
    yield
    _RELEASE.clear()


def _release_load() -> web_app.BundleLoad:
    return _RELEASE["load"]  # type: ignore[return-value]


def _resolved_listed_files() -> dict[Path, str]:
    bundle = _release_load().bundle
    return {(bundle.release_dir / rel).resolve(): rel for rel in bundle.files}


def _assert_resolution_contract(resolution, target: str, if_none_match=None) -> None:
    bundle = _release_load().bundle
    release = bundle.release_dir.resolve()
    assert resolution.kind in _KINDS, (SEED, target, resolution.kind)
    assert resolution.status == _KINDS[resolution.kind], (SEED, target, resolution)
    if resolution.kind != "file":
        assert resolution.path is None, (SEED, target, resolution)
        if resolution.kind == "redirect":
            assert resolution.location.startswith("/app/") and "\r" not in resolution.location \
                and "\n" not in resolution.location, (SEED, target, resolution.location)
        return
    path = resolution.path
    assert path is not None and path.is_file(), (SEED, target, resolution)
    real = path.resolve()
    assert real.is_relative_to(release), (SEED, target, str(real))
    listed = _resolved_listed_files()
    assert real in listed, (SEED, target, "served a file that is not in the manifest", str(real))
    rel = listed[real]
    assert resolution.size == bundle.files[rel].size, (SEED, target)
    # Independent derivation: exactly the decoded request path, or the SPA shell for extension-less routes.
    raw = re.split(r"[#?]", target, maxsplit=1)[0]
    rest = urllib.parse.unquote_to_bytes(raw[len("/app/"):]).decode("utf-8")
    rest = rest.removesuffix("/")
    if rel == "index.html" and rest not in ("", "index.html"):
        assert "." not in rest.rsplit("/", 1)[-1], (SEED, target, "dotted name fell back to the shell")
    else:
        assert rel == (rest or "index.html"), (SEED, target, rel, rest)
    assert ".." not in rest.split("/") and "\\" not in rest and "\x00" not in rest and ":" not in rest, (SEED, target)


_TARGET_SEEDS = [
    "/app/", "/app", "/app/index.html", "/app/main.dart.js", "/app/canvaskit/canvaskit.wasm",
    "/app/assets/fonts/a.ttf", "/app/assets/NOTICES", "/app/some/route", "/app/assets/", "/app/%61ssets/NOTICES",
    "/app/main.dart.js?v=1#x", "/app/version.json", "/app/unlisted.txt", "/app/../outside.txt", "/v1/status",
    "/app/assets/%2e%2e/%2e%2e/outside.txt", "/application/x", "/app/canvaskit/", "/app/%C3%A9",
]
_TARGET_FRAGS = ("..", "%2e%2e", "%2E%2e", "%252e", "%2f", "%5c", "%00", "\\", "//", "/", "?", "#", ":", "::$DATA",
                 "%c0%ae", "%", "\x00", "\r\n", "/../", "C:/", "%3a", "%3f", "%23", ".", " ", "%20", "\ud800",
                 "index.html", "main.dart.js", "unlisted.txt", "outside.txt")


def test_resolve_request_fuzz_error_contract_and_file_containment():
    """Any target: documented kind, never raises, and a ``file`` is a listed file inside the release dir."""
    rng = _rng(9)
    probe = _Probe()
    load = _release_load()
    kinds_seen: set[str] = set()
    for _ in range(ITER):
        target = _mutate(rng, rng.choice(_TARGET_SEEDS), _TARGET_SEEDS, _TARGET_FRAGS)
        etag = rng.choice([None, "*", '"' + "0" * 64 + '"', "", "garbage"])
        resolution, _ = probe.call(web_app.resolve_request, target, load, etag)
        kinds_seen.add(resolution.kind)
        try:
            _assert_resolution_contract(resolution, target)
        except UnicodeError:
            # the reference decode failed, so the resolver must not have served a file
            assert resolution.kind != "file", (SEED, target)
    # The mutator must have exercised both a success and a refusal, or the test proves nothing.
    assert {"file", "not_found"} <= kinds_seen, kinds_seen
    probe.finish()


_HOSTILE_TOKENS = (
    "..", "%2e%2e", "%2E%2E", "%2e%2E", ".%2e", "%2e.", "%252e%252e", "%252E", "..%5c", "%5c", "%5C", "\\", "..\\",
    "%2f", "%2F", "//", "C:", "c:/", "C%3a", "%3a", "::$DATA", "%00", "\x00", "%c0%ae%c0%ae", "%e0%80%ae", "%c0%af",
    "....//", "%25", "%2e%2e%2f", "/etc/passwd", "con:", ".hidden", "%2ehidden", "\x00.js", "%ff", "%",
)
_PLANT_PREFIXES = ["", "assets/", "canvaskit/", "a/b/", "x/", "assets/fonts/", "%61ssets/", "some/route/"]
_PLANT_SUFFIXES = ["", "/", "/outside.txt", "/index.html", "/unlisted.txt", "/main.dart.js", "/../outside.txt", "/x", "outside.txt"]


def test_resolve_request_hostile_targets_never_reach_file_or_not_modified():
    """Known-positive: ``..``/escape/absolute forms planted in valid prefixes are always a plain 404."""
    rng = _rng(10)
    probe = _Probe()
    load = _release_load()
    for _ in range(ITER):
        token = rng.choice(_HOSTILE_TOKENS)
        if rng.random() < 0.3:
            token = _case_fold_noise(rng, token)
        rest = _plant(rng, token, _PLANT_PREFIXES, _PLANT_SUFFIXES)
        target = "/app/" + rest + rng.choice(["", "?q=1", "#frag", "?a=../b"])
        resolution, _ = probe.call(web_app.resolve_request, target, load, rng.choice([None, "*"]))
        if resolution.kind != "not_found":
            _fail(f"hostile target resolved as {resolution.kind} -> {resolution.path}", target)
        assert resolution.path is None
    probe.finish()


def test_resolve_request_known_good_targets_still_resolve_with_noise_around_them():
    """Positive control for the containment checks: listed files and SPA routes do resolve."""
    rng = _rng(11)
    probe = _Probe()
    load = _release_load()
    listed = _resolved_listed_files()
    for _ in range(ITER // 2):
        rel = rng.choice(sorted(DEFAULT_FILES))
        quoted = "/".join(
            "".join(f"%{ord(c):02X}" if rng.random() < 0.3 and c.isalnum() else c for c in part)
            for part in rel.split("/"))
        target = "/app/" + quoted + rng.choice(["", "?v=" + str(rng.randrange(99)), "#x", "?a=%2e%2e"])
        resolution, _ = probe.call(web_app.resolve_request, target, load)
        assert resolution.kind == "file", (SEED, target, resolution)
        assert listed[resolution.path.resolve()] == rel, (SEED, target)
        assert resolution.content_type == web_app.content_type_for(rel)
        etag, _ = probe.call(web_app.resolve_request, target, load, resolution.etag)
        assert etag.kind == "not_modified" and etag.status == 304, (SEED, target)
        route = "/app/" + "/".join(rng.choice(["home", "settings", "a-b", "x_1"]) for _ in range(rng.randint(1, 3)))
        spa, _ = probe.call(web_app.resolve_request, route + rng.choice(["", "/"]), load)
        assert spa.kind == "file" and listed[spa.path.resolve()] == "index.html", (SEED, route)
    probe.finish()


def test_resolve_request_windows_aliases_never_resolve_to_another_file_or_a_device():
    """Trailing dot/space, case, ADS and device names never alias a listed file or leave the tree."""
    probe = _Probe()
    load = _release_load()
    listed = _resolved_listed_files()
    for name in ("index.html.", "index.html ", "INDEX.HTML", "Index.Html", "main.dart.js.", "main.dart.js ",
                 "MAIN.DART.JS", "main.dart.js::$DATA", "main.dart.js%3a%3a%24DATA", "main.dart.js:stream",
                 "main~1.js", "assets/fonts/a.ttf.", "assets/FONTS/a.ttf", "canvaskit/canvaskit.wasm "):
        for suffix in ("", "/"):
            resolution, _ = probe.call(web_app.resolve_request, "/app/" + name + suffix, load)
            assert resolution.kind == "not_found", (SEED, name, resolution)
    for device in ("con", "CON", "nul", "aux", "prn", "com1", "lpt1", "COM9"):
        resolution, _ = probe.call(web_app.resolve_request, "/app/" + device, load)
        assert resolution.kind == "file" and listed[resolution.path.resolve()] == "index.html", (SEED, device)
        for dotted in (device + ".txt", device + ".", device + ":"):
            resolution, _ = probe.call(web_app.resolve_request, "/app/" + dotted, load)
            assert resolution.kind == "not_found", (SEED, dotted, resolution)
    probe.finish()


def test_resolve_request_prefix_redirect_and_unbuilt_bundle():
    probe = _Probe()
    rng = _rng(12)
    broken = web_app.BundleLoad.error("WEB_APP_NOT_BUILT", "no build")
    for _ in range(1000):
        query = "".join(rng.choice(string.ascii_letters + "=&%.-") for _ in range(rng.randint(0, 10)))
        target = "/app" + (("?" + query) if query else "") + rng.choice(["", "#f"])
        resolution, _ = probe.call(web_app.resolve_request, target, _release_load())
        assert resolution.kind == "redirect" and resolution.status == 308, (SEED, target)
        assert resolution.location == "/app/" + (("?" + query) if query else ""), (SEED, target)
        other = "/" + rng.choice(["v1/status", "application", "apps/x", "", "ap"]) + rng.choice(["", "?a"])
        resolution, _ = probe.call(web_app.resolve_request, other, _release_load())
        assert resolution.kind == "not_found", (SEED, other)
        resolution, _ = probe.call(web_app.resolve_request, "/app/" + query, broken)
        assert resolution.kind == "not_built" and resolution.path is None, (SEED, query)
    probe.finish()


# -- web_app: same_origin --------------------------------------------------------

_HOST_NAMES = ["localhost", "example.com", "a-b.example.org", "10.0.2.2", "192.168.1.5", "[::1]", "[2001:db8::1]",
               "my-machine.local", "x"]


def _host_parts(rng: random.Random) -> tuple[str, str]:
    """Return (origin-authority name, host-header name) spellings of one host."""
    name = rng.choice(_HOST_NAMES)
    return name, name


def test_same_origin_matches_scheme_authority_oracle_and_is_case_insensitive():
    """Origin and Host must agree on name and (default-aware) port; case folds; scheme gates on TLS."""
    rng = _rng(13)
    probe = _Probe()
    trues = 0
    for _ in range(ITER):
        o_name, h_name = _host_parts(rng)
        scheme = rng.choice(["http", "https"])
        declared = rng.random() < 0.5
        default = 443 if scheme == "https" else 80
        o_port = rng.choice([None, None, default, 8080, 11435, 1, 65535, 0, 70000, 99999])
        h_port = rng.choice([None, None, default, 8080, 11435, 1, 65535, 0, 65536, 80, 443])
        if rng.random() < 0.4:
            h_name = rng.choice(_HOST_NAMES)
        origin = f"{scheme}://{o_name}" + (f":{o_port}" if o_port is not None else "")
        host = h_name + (f":{h_port}" if h_port is not None else "")
        h_default = default
        host_valid_port = h_port is None or 1 <= h_port <= 65535
        expected = (
            (scheme == "http" or declared)
            and o_name.lower() == h_name.lower()
            and host_valid_port
            and (o_port if o_port is not None else default) == (h_port if h_port is not None else h_default)
        )
        got, _ = probe.call(web_app.same_origin, origin, host, https_declared=declared)
        if got is not expected:
            _fail(f"same_origin={got}, expected {expected}, declared={declared}, host={host!r}", origin)
        trues += expected
        folded, _ = probe.call(web_app.same_origin, _case_fold_noise(rng, origin), _case_fold_noise(rng, host),
                               https_declared=declared)
        if folded is not got:
            _fail(f"case folding changed the verdict ({folded} vs {got}), host={host!r}", origin)
        padded, _ = probe.call(web_app.same_origin, origin + "/", host, https_declared=declared)
        assert padded is False, (SEED, origin + "/")
    assert trues > 50, trues
    probe.finish()


_ORIGIN_BASE = "http://example.com:8080"
_HOSTILE_ORIGIN_FRAGMENTS = (
    "a@", "user:pw@", "@evil.example", "\\", "\\@", "/", "/path", "?q", "#f", " ", "\x00", "\x0b", "\u00a0",
    "%0d%0a", ".evil.example", ":", "::", "..", "[", "]", ",", ";", "\r", "\n", "\r\n",
)


def test_same_origin_refuses_userinfo_paths_and_inner_control_characters():
    """Known-positive: a hostile fragment spliced into a matching Origin is never the same origin."""
    rng = _rng(14)
    probe = _Probe()
    host = "example.com:8080"
    good, _ = probe.call(web_app.same_origin, _ORIGIN_BASE, host, https_declared=False)
    assert good is True
    for _ in range(ITER):
        fragment = rng.choice(_HOSTILE_ORIGIN_FRAGMENTS)
        # inside the authority: after the scheme separator, strictly inside the string
        at = rng.randint(len("http://"), len(_ORIGIN_BASE) - 1)
        origin = _ORIGIN_BASE[:at] + fragment + _ORIGIN_BASE[at:]
        if rng.random() < 0.3:
            origin = _case_fold_noise(rng, origin)
        got, _ = probe.call(web_app.same_origin, origin, host, https_declared=rng.random() < 0.5)
        if got:
            _fail("hostile Origin accepted as same-origin", origin)
        suffix, _ = probe.call(web_app.same_origin, _ORIGIN_BASE + fragment, host, https_declared=False)
        if suffix and fragment.strip() != "":
            _fail("Origin with a hostile suffix accepted as same-origin", _ORIGIN_BASE + fragment)
    for value in (None, "", "null", "NULL", b"http://example.com:8080", 5, ["http://example.com:8080"]):
        got, _ = probe.call(web_app.same_origin, value, host, https_declared=True)
        assert got is False, (SEED, value)
    for value in (None, "", b"example.com", 5, ["example.com"], "example.com:80:80", "example.com\r\nx", "a@example.com"):
        got, _ = probe.call(web_app.same_origin, _ORIGIN_BASE, value, https_declared=True)
        assert got is False, (SEED, value)
    probe.finish()


def test_same_origin_refuses_mismatched_port_and_scheme_for_every_port():
    rng = _rng(15)
    probe = _Probe()
    for _ in range(2000):
        port = rng.randrange(1, 65536)
        other = rng.randrange(1, 65536)
        got, _ = probe.call(web_app.same_origin, f"http://h.example:{port}", f"h.example:{other}", https_declared=True)
        assert got is (port == other), (SEED, port, other)
        # https origin against a Host that names the plain-http default, and the converse
        got, _ = probe.call(web_app.same_origin, "https://h.example", "h.example:80", https_declared=True)
        assert got is False
        got, _ = probe.call(web_app.same_origin, "http://h.example", "h.example:443", https_declared=True)
        assert got is False
        got, _ = probe.call(web_app.same_origin, f"https://h.example:{port}", f"h.example:{port}", https_declared=False)
        assert got is False, (SEED, port)
        got, _ = probe.call(web_app.same_origin, "https://h.example", "h.example", https_declared=False)
        assert got is False
    probe.finish()


def test_same_origin_refuses_line_breaks_at_the_edges_of_origin():
    # A leading or trailing CR/LF is stripped by same_origin and then matches (see the regression test below);
    # the same bytes are refused by reflectable_origin, so the two helpers disagree about the same header.
    rng = _rng(16)
    probe = _Probe()
    for _ in range(500):
        edge = rng.choice(["\r", "\n", "\r\n", "\n\r", "\r\n\r\n"])
        origin = rng.choice([edge + _ORIGIN_BASE, _ORIGIN_BASE + edge, edge + _ORIGIN_BASE + edge])
        got, _ = probe.call(web_app.same_origin, origin, "example.com:8080", https_declared=False)
        if got:
            _fail("Origin with CR/LF at an edge accepted as same-origin", origin)
    probe.finish()


def test_regression_same_origin_trailing_crlf_origin():
    assert web_app.same_origin("http://localhost\r\n", "localhost", https_declared=False) is False


class _Headers:
    """Duck-typed header map with the ``get_all`` API of ``email.message.Message``."""

    def __init__(self, pairs):
        self.pairs = pairs

    def get(self, name, default=None):
        for key, value in self.pairs:
            if key.lower() == name.lower():
                return value
        return default

    def get_all(self, name, default=None):
        values = [value for key, value in self.pairs if key.lower() == name.lower()]
        return values or default


def test_same_origin_request_refuses_repeated_or_missing_origin():
    rng = _rng(17)
    probe = _Probe()
    for _ in range(1500):
        origin = rng.choice([_ORIGIN_BASE, "http://evil.example", "null", "http://a@example.com:8080"])
        host = rng.choice(["example.com:8080", "evil.example", "example.com"])
        declared = rng.random() < 0.5
        single = _Headers([("Origin", origin), ("Host", host)])
        got, _ = probe.call(web_app.same_origin_request, single, https_declared=declared)
        assert got is web_app.same_origin(origin, host, https_declared=declared), (SEED, origin, host)
        repeated = _Headers([("Origin", origin), ("origin", rng.choice([origin, "null", _ORIGIN_BASE])), ("Host", host)])
        got, _ = probe.call(web_app.same_origin_request, repeated, https_declared=declared)
        assert got is False, (SEED, repeated.pairs)
        got, _ = probe.call(web_app.same_origin_request, _Headers([("Host", host)]), https_declared=declared)
        assert got is False
        got, _ = probe.call(web_app.same_origin_request, {"Origin": origin, "Host": host}, https_declared=declared)
        assert got is web_app.same_origin(origin, host, https_declared=declared)
    message = http.client.parse_headers(io.BytesIO(
        b"Host: example.com:8080\r\nOrigin: http://example.com:8080\r\nOrigin: http://example.com:8080\r\n\r\n"))
    assert web_app.same_origin_request(message, https_declared=False) is False
    message = http.client.parse_headers(io.BytesIO(
        b"Host: example.com:8080\r\nOrigin: http://example.com:8080\r\n\r\n"))
    assert web_app.same_origin_request(message, https_declared=False) is True
    probe.finish()


# -- host_policy: parse_host_header ------------------------------------------------

def _label(rng: random.Random) -> str:
    size = rng.randint(1, 12)
    if size == 1:
        return rng.choice(_ALNUM)
    middle = "".join(rng.choice(_ALNUM + "-") for _ in range(size - 2))
    return rng.choice(_ALNUM) + middle + rng.choice(_ALNUM)


def _dns_name(rng: random.Random) -> str:
    return ".".join(_label(rng) for _ in range(rng.randint(1, 4)))


_PARSE_HOSTILE = (
    "@", "/", "?", "#", "\\", " ", "\t", "\r", "\n", "\x00", "%", "_", ",", ";", "'", '"', "<", ">", "|", "*", "=", "!",
    "..", "-.", ".-", "a@b", "user:pw@", "[", "]", "\u00e9", "\u0661", "\uff0e", ":", "::",
)
_HOSTNAME_FORBIDDEN = set("@/?# \t\r\n\x00\\")


def test_parse_host_header_matches_oracle_for_well_formed_hosts():
    """name[:port] with a DNS name, IPv4 or bracketed IPv6; port range is 1..65535."""
    rng = _rng(18)
    probe = _Probe()
    accepted = 0
    for _ in range(ITER):
        kind = rng.choice(["dns", "dns", "v4", "v6", "dots"])
        if kind == "dns" or kind == "dots":
            name = _dns_name(rng)
            text, lowered = name, name
            if kind == "dots":
                text = name + "."
        elif kind == "v4":
            name = ".".join(str(rng.randrange(256)) for _ in range(4))
            text, lowered = name, name
        else:
            name = str(ipaddress.IPv6Address(rng.getrandbits(128)))
            text, lowered = f"[{name}]", name
        port = rng.choice([None, None, 80, 8080, 1, 65535, 0, 65536, 99999, 100000, 7, 12345, 443])
        text = _case_fold_noise(rng, text) + (f":{port}" if port is not None else "")
        pad = rng.choice(["", " ", "\t", "  "])
        header = pad + text + pad
        expected = (lowered.lower(), port) if port is None or 1 <= port <= 65535 else None
        got, _ = probe.call(parse_host_header, header)
        if got != expected:
            _fail(f"parse_host_header={got!r}, oracle {expected!r}", header)
        accepted += expected is not None
    assert accepted > ITER // 2
    probe.finish()


def test_parse_host_header_length_and_port_boundaries():
    probe = _Probe()
    cases = {
        "a" * 63: ("a" * 63, None),
        "a" * 64: None,
        ".".join(["a" * 63] * 3 + ["a" * 61]): (".".join(["a" * 63] * 3 + ["a" * 61]), None),  # 253 chars
        ".".join(["a" * 63] * 3 + ["a" * 62]): None,  # 254 chars
        "h:1": ("h", 1), "h:65535": ("h", 65535), "h:0": None, "h:65536": None, "h:000080": None, "h:080": ("h", 80),
        "h:": None, "h:-1": None, "h:+1": None, "h: 80": None, "h:8 0": None, "h:0x50": None, "h:1e2": None,
        "": None, " ": None, ".": None, "..": None, "a..b": None, "-a": None, "a-": None, "a_b": None,
        "[::1]": ("::1", None), "[::1]:80": ("::1", 80), "[::1": None, "[::1]x": None, "[1.2.3.4]": None, "::1": None,
        "[]": None,"a:b:c": None, "h.:80": ("h", 80), "h..": None,
    }
    for text, expected in cases.items():
        got, _ = probe.call(parse_host_header, text)
        assert got == expected, (text[:40], got, expected)
    for value in (None, 5, b"localhost", ["localhost"], ("localhost",)):
        got, _ = probe.call(parse_host_header, value)
        assert got is None
    got, _ = probe.call(parse_host_header, "a" * 2000)
    assert got is None
    probe.finish()


def test_parse_host_header_rejects_hostile_characters_planted_in_valid_hosts():
    """Known-positive: ``@ / ? # \\`` whitespace, NUL etc. spliced inside name or port never parse."""
    rng = _rng(19)
    probe = _Probe()
    hostile = tuple(t for t in _PARSE_HOSTILE if t.isascii() and t not in (":", "::", "[", "]", ".."))
    for _ in range(ITER):
        name = _dns_name(rng) + "-x"
        token = rng.choice(hostile)
        at = rng.randint(1, len(name) - 1)
        header = name[:at] + token + name[at:]
        if rng.random() < 0.5:
            header += rng.choice(["", ":80"])
        got, _ = probe.call(parse_host_header, header)
        if got is not None and token not in " \t":
            _fail(f"hostile {token!r} accepted in host: {got!r}", header)
        if got is not None:  # an inner space/tab must not survive into the name either
            assert not (set(got[0]) & _HOSTNAME_FORBIDDEN), (SEED, header, got)
        header = name + ":" + _plant(rng, token, ["", "8", "80"], ["0", "1"])
        got, _ = probe.call(parse_host_header, header)
        if got is not None:
            _fail(f"hostile {token!r} accepted in port: {got!r}", header)
    probe.finish()


def test_parse_host_header_fuzz_invariants_on_accepted_values():
    """Accepted: port None or 1..65535, name free of whitespace/CR/LF/NUL/@/slash/?, lowercase, valid labels."""
    rng = _rng(20)
    probe = _Probe()
    seeds = ["localhost:11435", "example.com", "[::1]:8080", "127.0.0.1", "a.b.c.d:65535", "HOST.Example.COM.",
             "[2001:db8::1]", "10.0.2.2:80", "xn--bcher-kva.example", "my-box.local:1"]
    frags = tuple(t for t in _PARSE_HOSTILE if "%" not in t) + ("[::1]", "999999", ":0", ":65536", "a" * 70)
    accepted = 0
    for _ in range(ITER):
        if rng.random() < 0.4:  # benign variants keep the accepted population large
            pad = rng.choice(["", " ", "\t"])
            header = pad + _case_fold_noise(rng, rng.choice(seeds)) + pad
        else:
            header = _mutate(rng, rng.choice(seeds), seeds, frags)
        if "%" in header:
            continue  # IPv6 scope ids are covered by test_parse_host_header_ipv6_scope_id_*
        got, _ = probe.call(parse_host_header, header)
        if got is None:
            continue
        accepted += 1
        name, port = got
        if not (port is None or (isinstance(port, int) and 1 <= port <= 65535)):
            _fail(f"port out of range: {port!r}", header)
        if set(name) & (_HOSTNAME_FORBIDDEN | set(",;'\"<>|*=!")):
            _fail(f"forbidden character in accepted name {name!r}", header)
        if not name.isascii() or name != name.lower() or not name or len(name) > 253:
            _fail(f"accepted name {name!r} is not canonical", header)
        try:
            ipaddress.ip_address(name)
        except ValueError:
            if not all(re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", label) for label in name.split(".")):
                _fail(f"accepted name {name!r} has an invalid label", header)
        again = f"[{name}]" if ":" in name else name
        again += f":{port}" if port is not None else ""
        assert parse_host_header(again) == got, (SEED, header, got)
    assert accepted > 500, accepted
    probe.finish()


def test_parse_host_header_non_ascii_digits_and_case_folds_do_not_leak_in():
    """Port digits are ASCII only, and the name is not made valid by Unicode lower-casing."""
    rng = _rng(21)
    probe = _Probe()
    digits = ("\u00b2", "\u00b3", "\u00b9", "\u0663", "\u0967", "\uff18", "\u2460", "\u06f5")
    for _ in range(1000):
        port = "".join(rng.choice(digits + ("8", "0")) for _ in range(rng.randint(1, 4)))
        header = _dns_name(rng) + ":" + port
        got, _ = probe.call(parse_host_header, header)
        if not port.isascii() and got is not None:
            _fail(f"non-ASCII port digits accepted: {got!r}", header)
    probe.finish()


def test_parse_host_header_unicode_case_folding_does_not_make_a_name_valid():
    # str.lower() maps U+212A (Kelvin) to ASCII 'k', so folding before the ASCII label check lets a non-ASCII
    # Host value through as an ASCII name.
    probe = _Probe()
    for name in ("a\u212a.example", "\u212a", "\u0130stanbul.example", "\u017f.example"):
        got, _ = probe.call(parse_host_header, name)
        if got is not None:
            _fail(f"non-ASCII host name accepted as {got!r}", name)
    probe.finish()


def test_regression_parse_host_header_superscript_port_does_not_raise():
    # ``"\xb2".isdigit()`` is True but ``int("\xb2")`` raises, and a Host header is decoded as latin-1, so any client
    # can send this byte.
    assert parse_host_header("localhost:\u00b2") is None


def test_parse_host_header_ipv6_scope_id_cannot_smuggle_controls_or_delimiters():
    """ipaddress accepts any text after ``%`` as a scope id; the parsed name must still be clean."""
    rng = _rng(22)
    probe = _Probe()
    for _ in range(1000):
        token = rng.choice(("@", "\r", "\n", "\x00", " ", "\t", "\x7f", "a@b", "\r\nX-Evil: 1", "?", "#", "\\"))
        scope = _plant(rng, token, ["", "eth", "1"], ["", "0", "x"])
        header = f"[fe80::1%{scope}]" + rng.choice(["", ":80"])
        got, _ = probe.call(parse_host_header, header)
        if got is not None and set(got[0]) & (_HOSTNAME_FORBIDDEN | {"\x7f"}):
            _fail(f"IPv6 scope id smuggled {token!r} into the host name {got[0]!r}", header)
    probe.finish()


def test_regression_parse_host_header_ipv6_scope_id_with_crlf():
    assert parse_host_header("[::1%a\rb]:80") is None


# -- host_policy: host_decision / host_allowed / normalize_allowed_host ---------------

_LOCAL_NAMES = ("devbox", "devbox.local", "devbox.lan.example")
_ALLOWED = ("api.example.org", "Portal.Example.org:8443", "[::1]:9000", "10.9.9.9")


def test_host_decision_categories_match_the_documented_policy():
    """Addresses, loopback and own names are trusted; allowlist honours ports; others need credentials."""
    rng = _rng(23)
    probe = _Probe()
    for _ in range(ITER):
        credentials = rng.random() < 0.5
        category = rng.choice(["ip", "unspecified", "loopback", "local", "allow", "allow_port_ok", "allow_port_bad",
                               "other", "other"])
        port = rng.choice([None, 80, 8080, 11435, 65535])
        if category == "ip":
            host, expected = rng.choice(["10.0.2.2", "192.168.1.9", "100.64.0.1", "[2001:db8::5]", "8.8.8.8"]), HOST_TRUSTED
        elif category == "unspecified":
            host, expected = rng.choice(["0.0.0.0", "[::]", "[::ffff:0.0.0.0]", "[0:0:0:0:0:0:0:0]"]), None
        elif category == "loopback":
            host, expected = rng.choice(["localhost", "LOCALHOST", "a.localhost", "x.y.localhost", "127.0.0.1",
                                         "[::1]", "127.9.9.9", "localhost."]), HOST_TRUSTED
        elif category == "local":
            host, expected = rng.choice(_LOCAL_NAMES), HOST_TRUSTED
        elif category == "allow":
            host, expected = "API.Example.Org", HOST_TRUSTED
        elif category == "allow_port_ok":
            host, port, expected = "portal.example.org", 8443, HOST_TRUSTED
        elif category == "allow_port_bad":
            host = "portal.example.org"
            port = rng.choice([None, 80, 8444, 65535])
            expected = HOST_CREDENTIALED if credentials else None
        else:
            host = rng.choice(["evil.example", "rebind.attacker.example", "localhost.evil.example", "xlocalhost",
                               "devbox.evil", "api.example.org.evil.example", "notlocalhost", "a" + _dns_name(rng)])
            expected = HOST_CREDENTIALED if credentials else None
        header = host + (f":{port}" if port is not None else "")
        got, _ = probe.call(host_decision, header, allowed_hosts=_ALLOWED, local_names=_LOCAL_NAMES,
                            credentials_required=credentials)
        if got != expected:
            _fail(f"host_decision={got!r}, expected {expected!r} ({category}, credentials={credentials})", header)
        allowed, _ = probe.call(host_allowed, header, allowed_hosts=_ALLOWED, local_names=_LOCAL_NAMES,
                                credentials_required=credentials)
        assert allowed is (expected is not None), (SEED, header)
    got, _ = probe.call(host_decision, None, credentials_required=False)
    assert got == HOST_TRUSTED
    for value in (5, b"localhost", ["localhost"], ("localhost", "evil.example"), {"h": 1}, "", "   "):
        got, _ = probe.call(host_decision, value, allowed_hosts=_ALLOWED, local_names=_LOCAL_NAMES)
        assert got is None, (SEED, value)
    probe.finish()


def test_host_decision_unknown_host_is_never_trusted_and_is_deterministic():
    """A name outside allowed_hosts/local_names (and not an address/loopback) is never TRUSTED."""
    rng = _rng(24)
    probe = _Probe()
    seeds = ["evil.example", "localhost:80", "[::1]", "devbox", "api.example.org:1", "portal.example.org:8443",
             "10.1.1.1", "0.0.0.0", "a.localhost", "LOCALHOST.evil.com", "x@localhost", "localhost/", "localhost\r\n"]
    frags = _PARSE_HOSTILE + ("localhost", ".localhost", "devbox", "api.example.org", "[::]", "0.0.0.0", ":8443")
    allowed_names = {"api.example.org", "portal.example.org", "10.9.9.9", "::1"}
    for _ in range(ITER):
        header = _mutate(rng, rng.choice(seeds), seeds, frags)
        credentials = rng.random() < 0.5
        kwargs = {"allowed_hosts": _ALLOWED, "local_names": _LOCAL_NAMES, "credentials_required": credentials}
        got, _ = probe.call(host_decision, header, **kwargs)
        again, _ = probe.call(host_decision, header, **kwargs)
        if got != again:
            _fail(f"host_decision is not deterministic: {got!r} then {again!r}", header)
        assert got in (None, HOST_TRUSTED, HOST_CREDENTIALED), (SEED, header, got)
        parsed = parse_host_header(header)
        if parsed is None:
            if got is not None:
                _fail("malformed Host accepted by host_decision", header)
            continue
        name, _port = parsed
        try:
            ipaddress.ip_address(name)
            is_ip = True
        except ValueError:
            is_ip = False
        known = (is_ip or name == "localhost" or name.endswith(".localhost") or name in allowed_names
                 or name in _LOCAL_NAMES)
        if not known:
            expected = HOST_CREDENTIALED if credentials else None
            if got != expected:
                _fail(f"unknown host {name!r} got {got!r}, expected {expected!r}", header)
        assert (got is not None) is host_allowed(header, **kwargs), (SEED, header)
    probe.finish()


def test_host_decision_tolerates_garbage_allowlists():
    """Bad allowlist or local-name entries are skipped, never raised and never widen access."""
    rng = _rng(25)
    probe = _Probe()
    junk = [None, 5, b"x", "", "*", "http://evil.example", "evil.example:0", "evil.example:99999", "a b", "ev\u00efl",
            ("evil.example",), ["evil.example"], (None, None), {"evil.example"}, "*.example", "evil.example/"]
    for _ in range(1500):
        allowed = [rng.choice(junk) for _ in range(rng.randint(0, 5))]
        local = [rng.choice(junk) for _ in range(rng.randint(0, 3))]
        header = rng.choice(["evil.example", "evil.example:80", "other.example", "localhost"])
        got, _ = probe.call(host_decision, header, allowed_hosts=allowed, local_names=local)
        if header != "localhost" and got is not None:
            _fail(f"garbage allowlist trusted {header!r}: {got!r}", (allowed, local))
    probe.finish()


def test_normalize_allowed_host_raises_only_value_error_and_round_trips():
    rng = _rng(26)
    probe = _Probe((ValueError,))
    seeds = ["example.com", "example.com:8443", "[::1]:80", "10.0.0.1", "http://example.com", "*", "", "a b",
             "EXAMPLE.org.", "a:0", "a:65536", "[::1", "x@y"]
    frags = _PARSE_HOSTILE + ("*.", "http://", ":0", ":65536", "[::1]")
    ok = 0
    for _ in range(ITER):
        entry = _mutate(rng, rng.choice(seeds), seeds, frags)
        got, error = probe.call(normalize_allowed_host, entry)
        assert (got is None) == (error is not None)
        assert (parse_host_header(entry) is None) == (error is not None), (SEED, entry)
        if error is not None:
            continue
        ok += 1
        assert got == parse_host_header(entry)
        name, port = got
        text = (f"[{name}]" if ":" in name else name) + (f":{port}" if port is not None else "")
        if "%" not in name:
            assert normalize_allowed_host(text) == got, (SEED, entry, text)
    for value in (None, 5, b"example.com", ["example.com"], ("example.com", 80), {"a": 1}, object()):
        got, error = probe.call(normalize_allowed_host, value)
        assert error is not None, (SEED, value)
    assert ok > 100, ok
    probe.finish()


# -- host_policy: reflectable_origin ---------------------------------------------------

_ORIGINS = ["http://localhost:11435", "https://example.com", "null", "", "http://[::1]:80", "chrome-extension://abc",
            "http://a@b", "file://", "http://evil.example/path?q#f", "https://x.y.z:8443"]


def test_reflectable_origin_never_emits_line_breaks_and_returns_the_input_unchanged():
    rng = _rng(27)
    probe = _Probe()
    frags = ("\r", "\n", "\r\n", "\n\r", " ", "\t", "\\", "%0d%0a", "%0D", "\u2028", "\x85", "\x0b", "\x0c", "\x1f",
             "http://", "a" * 200)
    reflected = 0
    for _ in range(ITER):
        origin = _mutate(rng, rng.choice(_ORIGINS), _ORIGINS, frags)
        got, _ = probe.call(reflectable_origin, origin)
        if got is None:
            continue
        reflected += 1
        if "\r" in got or "\n" in got:
            _fail(f"line break reflected: {got!r}", origin)
        if got != origin:
            _fail(f"reflected value {got!r} differs from the input", origin)
    assert reflected > 100, reflected
    probe.finish()


def test_reflectable_origin_refuses_planted_line_breaks_and_non_strings():
    """Known-positive: CR/LF anywhere (prefix, middle, suffix, obs-fold) is never echoed back."""
    rng = _rng(28)
    probe = _Probe()
    for _ in range(ITER):
        token = rng.choice(["\r", "\n", "\r\n", "\n ", "\r\n\t", "\r\nSet-Cookie: x=1", "\nX: y"])
        base = rng.choice(_ORIGINS)
        at = rng.randint(0, len(base))
        origin = base[:at] + token + base[at:]
        got, _ = probe.call(reflectable_origin, origin)
        if got is not None:
            _fail(f"origin with a line break was reflected: {got!r}", origin)
        clean = _case_fold_noise(rng, base)
        got, _ = probe.call(reflectable_origin, clean)
        assert got == clean, (SEED, clean)
    for value in (None, 5, b"http://a", ["http://a"], ("http://a",), object()):
        got, _ = probe.call(reflectable_origin, value)
        assert got is None, (SEED, value)
    probe.finish()


def test_reflectable_origin_never_emits_nul():
    rng = _rng(29)
    probe = _Probe()
    for _ in range(1000):
        base = rng.choice(_ORIGINS)
        at = rng.randint(0, len(base))
        origin = base[:at] + "\x00" + base[at:]
        got, _ = probe.call(reflectable_origin, origin)
        if got is not None and "\x00" in got:
            _fail(f"NUL reflected: {got!r}", origin)
    probe.finish()


def test_regression_reflectable_origin_nul_byte():
    assert reflectable_origin("http://a\x00b") is None


# -- host_policy: forwarded_client_ip ----------------------------------------------------

_NETS = (ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("192.168.1.0/24"),
         ipaddress.ip_network("fd00::/8"), ipaddress.ip_network("127.0.0.1/32"))


def _unmap(address):
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped if mapped is not None else address


def _random_ip(rng: random.Random) -> str:
    kind = rng.randrange(6)
    if kind == 0:
        return f"10.{rng.randrange(256)}.{rng.randrange(256)}.{rng.randrange(1, 255)}"
    if kind == 1:
        return f"192.168.1.{rng.randrange(256)}"
    if kind == 2:
        return ".".join(str(rng.randrange(256)) for _ in range(4))
    if kind == 3:
        return str(ipaddress.IPv6Address(rng.getrandbits(128)))
    if kind == 4:
        return "::ffff:" + f"10.{rng.randrange(256)}.0.{rng.randrange(256)}"
    return "fd00::" + format(rng.randrange(1, 0xFFFF), "x")


def _syntactic_ip(text: str):
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def test_forwarded_client_ip_result_is_the_peer_or_an_untrusted_ip_from_the_header():
    rng = _rng(30)
    probe = _Probe()
    trusted_hits = 0
    for _ in range(ITER):
        peer = rng.choice([_random_ip(rng), "10.0.0.1", "::ffff:10.1.2.3", "192.168.1.9", "fd00::7", "10.8.8.8", "",
                           None, "garbage", "1.2.3.4", "300.1.1.1"])
        junk = ["unknown", "", " ", "1.2.3.4:80", "[::1]", "evil\r\nx", "-", "1.2.3"]
        hops = [rng.choice(junk) if rng.random() < 0.12 else _random_ip(rng) for _ in range(rng.randint(0, 5))]
        header = rng.choice([", ", ",", " , "]).join(hops)
        declared = rng.random() < 0.7
        networks = _NETS if rng.random() < 0.85 else ()
        got, _ = probe.call(forwarded_client_ip, peer, header, proxy_declared=declared, trusted_networks=networks)
        want_peer = str(peer or "")
        peer_ip = _syntactic_ip(want_peer)
        peer_trusted = peer_ip is not None and any(_unmap(peer_ip) in net for net in networks)
        if not declared or not peer_trusted:
            if got != want_peer:
                _fail(f"untrusted path returned {got!r}, not the peer {want_peer!r}", (peer, header, declared))
            continue
        if got == want_peer:
            continue
        trusted_hits += 1
        address = _syntactic_ip(got)
        if address is None:
            _fail(f"result {got!r} is not a syntactically valid IP", (peer, header))
        if any(c in got for c in "\r\n\x00 \t,"):
            _fail(f"result {got!r} carries control or separator characters", (peer, header))
        listed = {_unmap(a) for a in (_syntactic_ip(h.strip()) for h in header.split(",")) if a is not None}
        if _unmap(address) not in listed:
            _fail(f"result {got!r} does not appear in the header", (peer, header))
        if any(_unmap(address) in net for net in networks):
            _fail(f"result {got!r} is a trusted proxy hop", (peer, header))
        # right-to-left rule: every hop to the right of the chosen one is a trusted proxy
        clean = [h.strip() for h in header.split(",") if h.strip()]
        chosen = max(i for i, h in enumerate(clean)
                     if _syntactic_ip(h) is not None and _unmap(_syntactic_ip(h)) == _unmap(address))
        for right in clean[chosen + 1:]:
            parsed = _syntactic_ip(right)
            assert parsed is not None and any(_unmap(parsed) in net for net in networks), (SEED, peer, header)
    assert trusted_hits > 300, trusted_hits
    probe.finish()


def test_forwarded_client_ip_client_supplied_prefix_cannot_choose_the_address():
    """Known-positive: junk or spoofed hops left of the proxy-appended client entry never win."""
    rng = _rng(31)
    probe = _Probe()
    spoofs = ["6.6.6.6", "127.0.0.1", "10.0.0.5", "::1", "garbage", "unknown", "1.1.1.1:80", "evil\r\nx", "", "*"]
    for _ in range(ITER):
        client = f"203.0.113.{rng.randrange(1, 255)}"
        proxies = [f"10.{rng.randrange(256)}.0.{rng.randrange(1, 255)}" for _ in range(rng.randint(0, 3))]
        prefix = [rng.choice(spoofs) for _ in range(rng.randint(0, 4))]
        header = ", ".join(prefix + [client] + proxies)
        peer = rng.choice(["10.0.0.1", "::ffff:10.0.0.1", "192.168.1.7"])
        got, _ = probe.call(forwarded_client_ip, peer, header, proxy_declared=True, trusted_networks=_NETS)
        if got != client:
            _fail(f"expected {client!r}, got {got!r}", (peer, header))
        got, _ = probe.call(forwarded_client_ip, "198.51.100.9", header, proxy_declared=True, trusted_networks=_NETS)
        assert got == "198.51.100.9", (SEED, header)
        got, _ = probe.call(forwarded_client_ip, peer, header, proxy_declared=False, trusted_networks=_NETS)
        assert got == peer, (SEED, header)
    probe.finish()


def test_forwarded_client_ip_ipv6_scope_id_cannot_inject_control_characters():
    """The returned address is used as a log and rate-limit key; it must not carry CR/LF/NUL."""
    rng = _rng(32)
    probe = _Probe()
    for _ in range(500):
        token = rng.choice(["\r", "\n", "\r\nX-Evil: 1", "\x00", " ", "\t"])
        scope = _plant(rng, token, ["", "eth", "1"], ["", "0", "x"])
        header = f"fe80::1%{scope}"
        got, _ = probe.call(forwarded_client_ip, "10.0.0.1", header, proxy_declared=True, trusted_networks=_NETS)
        if any(c in got for c in "\r\n\x00 \t"):
            _fail(f"scope id smuggled {token!r} into the client address {got!r}", header)
    probe.finish()


def test_regression_forwarded_client_ip_scope_id_with_crlf():
    got = forwarded_client_ip("10.0.0.1", "fe80::1%a\r\nb", proxy_declared=True, trusted_networks=_NETS)
    assert "\r" not in got and "\n" not in got


def test_forwarded_client_ip_legal_zone_id_hop_still_resolves_to_itself():
    for hop in ("fe80::1%eth0", "fe80::1%12"):
        got = forwarded_client_ip("10.0.0.1", hop, proxy_declared=True, trusted_networks=_NETS)
        assert got == hop


def test_parse_host_header_refuses_every_bracketed_host_with_a_zone_id():
    for value in ("[fe80::1%eth0]", "[fe80::1%12]", "[fe80::1%25eth0]:80", "[::1%lo]", "[fe80::1%]"):
        assert parse_host_header(value) is None


@pytest.mark.parametrize(
    "origin, host",
    [
        ("http://example.com:8080\t", "example.com:8080\t"),
        ("\thttp://example.com:8080 ", " example.com:8080"),
    ],
)
def test_same_origin_accepts_http_optional_whitespace(origin, host):
    # RFC 9112 allows SP/HTAB around a field value; Python keeps a trailing
    # tab, so refusing it would 403 a conforming client.
    assert web_app.same_origin(origin, host, https_declared=False) is True


@pytest.mark.parametrize("value", ["\x0blocalhost\x0c", "localhost\x0b", "\x0clocalhost", "local\x0bhost"])
def test_parse_host_header_does_not_strip_vertical_tab_or_form_feed(value):
    assert parse_host_header(value) is None
