"""Seeded mutation fuzzing of the patch/edit input parsers and their guards.

Contract fuzzed (every input either succeeds or raises ONLY the documented
typed error, within the time budget, and never breaks a security invariant):

* ``text_patch._patch_path`` / ``_parse`` raise ``ValueError`` or
  ``PermissionError``; the public ``text_patch`` raises those plus the
  filesystem errors ``FileNotFoundError``/``FileExistsError`` and
  ``TextPatchError``. No patch, whatever its path spelling, creates, modifies
  or deletes anything outside its root, and a patch that raises leaves the
  whole tree byte-identical (the module is documented all-or-nothing).
* ``json_patch._pointer_tokens``, ``_array_index``, ``_loads_strict``,
  ``_parse_operations`` and the public ``patch_json`` raise ``ValueError``
  (``PermissionError``/``JsonPatchError`` for the guarded public entry). The
  RFC 6901/6902 behaviour is checked against a small reference oracle.
* ``repair.parse_candidate_patch`` raises ``BuildDomainError`` with code
  ``FIX_SCOPE_REJECTED``; an accepted patch only names contained relative
  files. ``scan_hostile_directives`` must still detect every hostile corpus
  entry after the bypass mutations its docstrings claim (case, line splices,
  comments, whitespace, string concatenation, macro indirection), must not
  flag mutated benign code, and ``normalize_for_scan`` must be idempotent.

Each input is timed (p99 under 50 ms, hard cap 2 s). The seed comes from
``FUZZ_SEED`` (default below) and is printed in every failure message so a CI
failure can be replayed with ``FUZZ_SEED=<n> pytest ...``.
"""
from __future__ import annotations

import copy
import json
import math
import os
import random
import re
import shutil
import subprocess
import time
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

import sonder_runtime.adapters.filesystem.json_patch as jp
import sonder_runtime.adapters.filesystem.text_patch as tp
from sonder_runtime.adapters.filesystem import file_ops
from sonder_runtime.domain.build.model import BuildDomainError
from sonder_runtime.domain.build.repair import (
    CandidatePatch,
    PatchHunk,
    added_logical_lines,
    include_checker,
    normalize_for_scan,
    parse_candidate_patch,
    scan_added_lexings,
    scan_hostile_directives,
    validate_patch,
)
from tests.test_build_hostile_scan import BENIGN, HOSTILE, SCOPE

SEED = int(os.environ.get("FUZZ_SEED", "20261116"))
TAG = f"FUZZ_SEED={SEED}"


def _rng(name: str) -> random.Random:
    return random.Random(f"{SEED}:{name}")


class _Run:
    """Run one input, time it, and return ('ok', value) or ('err', exception)."""

    def __init__(self) -> None:
        self.samples: list[float] = []

    def __call__(self, fn, *args, **kwargs):
        started = time.perf_counter()
        try:
            return "ok", fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - the caller classifies every exception type
            return "err", exc
        finally:
            self.samples.append(time.perf_counter() - started)

    def check(self, label: str, p99_limit: float = 0.05) -> None:
        samples = sorted(self.samples)
        assert samples, label
        p99 = samples[max(0, int(len(samples) * 0.99) - 1)]
        assert p99 < p99_limit, f"{label}: p99 {p99 * 1000:.1f} ms over {p99_limit * 1000:.0f} ms ({TAG})"
        assert samples[-1] < 2.0, f"{label}: max {samples[-1] * 1000:.1f} ms ({TAG})"


def _expect_error(outcome, allowed: tuple, what: str) -> None:
    kind, value = outcome
    if kind == "err":
        assert isinstance(value, allowed), f"{what} raised {type(value).__name__}: {value!r} ({TAG})"


# ---------------------------------------------------------------------------
# shared path spellings
# ---------------------------------------------------------------------------

# Spellings that must NEVER name a file outside the root (relative to a prefix).
ESCAPES = [
    "../x", "a/../../x", "a/./../..", "..", "a/b/../../../x", "./../x", "sub/../../x",
    "/etc/passwd", "/x", "//srv/share/x", "C:/x", "C:\\x", "c:x", "\\\\srv\\share\\x",
    "..\\x", "a\\..\\..\\x", "\\x", "a\\b", "C:",
]
SPELLINGS = ESCAPES + [
    "x", "a.txt", "sub/b.txt", "./a.txt", "a//b", "a.txt/", "a.txt.", "a.txt ", " a.txt",
    "a.txt::$DATA", "a.txt:s", "a:b", "b/C:/x", "%2e%2e/x", "%2E%2E%2Fx", "..%2Fx", "%5c..%5cx",
    "\u2215x", "\u2024\u2024/x", "\uff0e\uff0e/x", "\uff0f", "a\u2215b", "\u202e", "\u0000x",
    "CON", "nul.txt", "AUX", "com1", "SHADER~1.CPP", "~1/x", "a\tb", "a\nb", "a\rb",
    "/dev/null", "dev/null", "b/b/a.txt", "a/a/a.txt", "a/", "b/", "", " ", ".", "./", "...", "..../x",
    "x" * 300, "a/" * 100 + "x", "\U0001f600/x", "a?b", "a*b", "a<b", 'a"b', "a|b",
]
_ATOMS = ["..", ".", "a.txt", "sub", "b.txt", "x", "", "%2e%2e", "\u2215", "\uff0e\uff0e", "C:", "c:",
          "\\\\srv\\share", "a.txt.", "a.txt ", "a.txt::$DATA", "~1", "NUL", "con", "..\\", "\x7f",
          "SUB", "B.TXT", "a", "b"]


def _random_spelling(rng: random.Random) -> str:
    sep = rng.choice(["/", "/", "/", "\\", "//"])
    parts = [rng.choice(_ATOMS) for _ in range(rng.randint(1, 4))]
    value = sep.join(parts)
    return rng.choice(["", "", "/", "//", "C:", "\\\\?\\", "./"]) + value


_DRIVE_COMPONENT = re.compile(r"(^|/)[A-Za-z]:")


def _contained_relative(path: str) -> bool:
    """Independent oracle: a relative path that never climbs above its start.

    A ``C:`` component *after* the git prefix is judged separately (see
    ``test_patch_path_rejects_drive_letter_components``), so it is neutralised here.
    """
    path = _DRIVE_COMPONENT.sub(r"\1_", path)
    if not path or "\\" in path or path.startswith("/"):
        return False
    win = PureWindowsPath(path)
    if win.drive or win.root or PurePosixPath(path).is_absolute():
        return False
    depth = 0
    for part in path.split("/"):
        if part in ("", "."):
            return False
        if part == "..":
            return False
        depth += 1
    return depth > 0


# ---------------------------------------------------------------------------
# text_patch: _patch_path and _parse
# ---------------------------------------------------------------------------

def test_patch_path_error_contract_and_escape_rejection():
    rng = _rng("patch_path")
    run = _Run()
    values = list(SPELLINGS)
    for _ in range(3000):
        base = rng.choice(SPELLINGS + [_random_spelling(rng) for _ in range(4)])
        values.append(rng.choice(["", "a/", "b/", "a/", "b/"]) + base)
    for value in values:
        for prefix in ("a", "b"):
            outcome = run(tp._patch_path, value, prefix=prefix)
            _expect_error(outcome, (ValueError, PermissionError), f"_patch_path({value!r})")
            if outcome[0] == "ok":
                result = outcome[1]
                if value == "/dev/null":
                    assert result is None
                else:
                    assert isinstance(result, str) and _contained_relative(result), (
                        f"_patch_path({value!r}, {prefix!r}) accepted escaping/non-relative {result!r} ({TAG})")
    # Known positives: an escape, with or without the git a/ b/ prefix and a
    # benign tail, is always rejected.
    for escape in ESCAPES:
        for decor in ("", "a/", "b/", "a/a/", "b/b/"):
            for tail in ("", "/y", "/../z"):
                value = decor + escape + tail
                if value == "/dev/null":
                    continue
                for prefix in ("a", "b"):
                    kind, got = run(tp._patch_path, value, prefix=prefix)
                    if kind == "ok":
                        assert got is not None and _contained_relative(got), (
                            f"escape spelling {value!r} accepted as {got!r} ({TAG})")
    run.check("_patch_path")


def test_patch_path_rejects_drive_letter_components():
    """``b/C:/x`` is accepted as ``C:/x``; pathlib then maps it to ``<root>/x`` (same drive) so the
    duplicate-target check and the reported path disagree with the file actually written."""
    run = _Run()
    leaks = []
    for decor in ("a/", "b/"):
        for drive in ("C:", "c:", "D:", "Z:"):
            for tail in ("x", "x/y.txt", "../x", ""):
                value = decor + drive + ("/" + tail if tail else "")
                for prefix in ("a", "b"):
                    outcome = run(tp._patch_path, value, prefix=prefix)
                    _expect_error(outcome, (ValueError, PermissionError), f"_patch_path({value!r})")
                    if outcome[0] == "ok" and _DRIVE_COMPONENT.search(outcome[1] or ""):
                        leaks.append((value, prefix, outcome[1]))
    assert not leaks, f"drive-letter components survive _patch_path ({TAG}): {leaks[:6]!r}"


_MODIFY = "--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1,2 @@\n one\n-two\n+three\n"
_CREATE = "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1,2 @@\n+hello\n+world\n"
_MULTI = ("--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1,2 @@\n one\n-two\n+three\n"
          "@@ -5,0 +6 @@\n+tail\n--- /dev/null\n+++ b/sub/new.txt\n@@ -0,0 +1 @@\n+x\n"
          "\\ No newline at end of file\n")
_CRLF = "--- a/a.txt\r\n+++ b/a.txt\r\n@@ -1 +1 @@\r\n-one\r\n+uno\r\n"
_PATCH_SEEDS = [_MODIFY, _CREATE, _MULTI, _CRLF]
_BOUNDARY_NUMBERS = ("0", "1", "2", "4096", "65535", "2147483647", "2147483648", "9223372036854775808",
                     "1" + "0" * 40, "-1", "+1", "00", "\u0661")
_FRAGMENTS = ["--- ", "+++ ", "@@ ", " @@", "\\ No newline at end of file\n", "/dev/null", "../", "a/", "b/",
              "\t", "\x00", "\r", "\r\n", "\n", "-", "+", " ", ",", "C:", "\\", "\u2215", "%2e%2e/", "\ud800"]


def _mutate_patch(rng: random.Random, text: str) -> str:
    op = rng.randrange(8)
    if op == 0:
        chars = list(text)
        for _ in range(rng.randint(1, 6)):
            if chars:
                chars[rng.randrange(len(chars))] = rng.choice("@+- \\\n\r\t/.:~%0123456789ab")
        return "".join(chars)
    if op == 1:
        return text[: rng.randrange(len(text) + 1)]
    lines = text.splitlines(keepends=True) or [""]
    index = rng.randrange(len(lines))
    if op == 2:
        return "".join(lines[:index] + [lines[index]] * rng.randint(2, 40) + lines[index:])
    if op == 3:
        return "".join(lines[:index] + lines[index + 1:])
    if op == 4:
        position = rng.randrange(len(text) + 1)
        return text[:position] + rng.choice(_FRAGMENTS) + text[position:]
    if op == 5:
        return re.sub(r"\d+", lambda m: rng.choice(_BOUNDARY_NUMBERS) if rng.random() < 0.4 else m.group(0), text)
    if op == 6:
        header = [i for i, line in enumerate(lines) if line.startswith(("--- ", "+++ "))]
        if header:
            i = rng.choice(header)
            lines[i] = lines[i][:4] + rng.choice(["a/", "b/", ""]) + rng.choice(SPELLINGS) + "\n"
        return "".join(lines)
    return text + rng.choice(_PATCH_SEEDS)


def _check_parsed(files: list, label: str) -> None:
    seen = set()
    for item in files:
        assert item["action"] in {"create", "modify"} and item["hunks"], (label, TAG)
        assert _contained_relative(item["path"]), "{}: parsed path {!r} not contained ({})".format(label, item["path"], TAG)
        key = os.path.normcase(item["path"]).casefold()
        assert key not in seen, "{}: duplicate target {!r} accepted ({})".format(label, item["path"], TAG)
        seen.add(key)
        for old_start, old_count, new_start, new_count, body in item["hunks"]:
            assert old_count == sum(tag != "+" for tag, _ in body), (label, TAG)
            assert new_count == sum(tag != "-" for tag, _ in body), (label, TAG)
            assert min(old_start, new_start) >= 0


def test_parse_mutation_error_contract_and_structure():
    rng = _rng("text_parse")
    run = _Run()
    accepted = 0
    for _ in range(3000):
        text = rng.choice(_PATCH_SEEDS)
        for _step in range(rng.randint(1, 3)):
            text = _mutate_patch(rng, text)
        outcome = run(tp._parse, text)
        _expect_error(outcome, (ValueError, PermissionError), f"_parse({text!r})")
        if outcome[0] == "ok":
            accepted += 1
            _check_parsed(outcome[1], f"_parse({text!r})")
    assert accepted >= 20, f"mutator never produced an accepted patch, so structure checks were vacuous ({TAG})"
    for seed in _PATCH_SEEDS:  # the unmutated seeds are valid
        assert run(tp._parse, seed)[0] == "ok", seed
    run.check("_parse")


def test_parse_rejects_planted_escape_headers():
    rng = _rng("text_parse_plant")
    run = _Run()
    for escape in ESCAPES:
        for decor in ("", "a/", "b/"):
            path = decor + escape
            if "\n" in path or "\r" in path:
                continue
            for template in (
                "--- a/a.txt\n+++ %s\n@@ -1 +1 @@\n-one\n+x\n",
                "--- %s\n+++ b/a.txt\n@@ -1 +1 @@\n-one\n+x\n",
                "--- /dev/null\n+++ %s\n@@ -0,0 +1 @@\n+x\n",
                "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-one\n+x\n--- %s\n+++ %s\n@@ -0,0 +1 @@\n+x\n",
            ):
                text = template.replace("%s", path)
                prefix_text = rng.choice(["", _MODIFY.replace("a.txt", "keep.txt")]) if template.count("%s") == 1 else ""
                outcome = run(tp._parse, prefix_text + text)
                _expect_error(outcome, (ValueError, PermissionError), f"_parse planted {path!r}")
                if outcome[0] == "ok":
                    for item in outcome[1]:
                        assert _contained_relative(item["path"]), (
                            "planted {!r} parsed to {!r} ({})".format(path, item["path"], TAG))
    run.check("_parse planted")


def test_parse_limits_and_ten_thousand_line_inputs_stay_in_budget():
    run = _Run()
    body = "".join(f"+line {i}\n" for i in range(10_000))
    big_create = "--- /dev/null\n+++ b/big.txt\n@@ -0,0 +1,10000 @@\n" + body
    kind, files = run(tp._parse, big_create)
    assert kind == "ok" and len(files[0]["hunks"][0][4]) == 10_000, (kind, files, TAG)
    kind, files = run(tp._parse, "--- a/f\n+++ b/f\n@@ -1,10000 +1,10000 @@\n" + "".join(f" c{i}\n" for i in range(10_000)))
    assert kind == "ok", (kind, files, TAG)
    garbage = "".join(f"junk {i}\n" for i in range(10_000))
    _expect_error(run(tp._parse, garbage), (ValueError,), "10k garbage lines")
    assert run(tp._parse, garbage)[0] == "err"
    sections = "".join(f"--- a/f{i}\n+++ b/f{i}\n@@ -1 +1 @@\n-a\n+b\n" for i in range(2000))
    outcome = run(tp._parse, sections)
    assert outcome[0] == "err" and isinstance(outcome[1], ValueError), (outcome, TAG)  # > MAX_FILES
    outcome = run(tp._parse, "\n" * (tp.MAX_PATCH_LINES + 1))
    assert outcome[0] == "err" and isinstance(outcome[1], ValueError)
    outcome = run(tp._parse, "-" * (tp.MAX_PATCH_BYTES + 1))
    assert outcome[0] == "err" and isinstance(outcome[1], ValueError)
    assert run(tp._parse, "")[0] == "err" and run(tp._parse, "x\x00y")[0] == "err"
    hunks = "".join(f"@@ -{2 * i + 1} +{2 * i + 1} @@\n-a\n+b\n" for i in range(tp.MAX_HUNKS + 1))
    outcome = run(tp._parse, "--- a/f\n+++ b/f\n" + hunks)
    assert outcome[0] == "err" and isinstance(outcome[1], ValueError), (outcome, TAG)
    run.check("_parse limits")


# ---------------------------------------------------------------------------
# text_patch: public containment + atomicity
# ---------------------------------------------------------------------------

_DEVICE = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def _is_device_spelling(path: str) -> bool:
    return any(part.split(".", 1)[0].strip().upper() in _DEVICE for part in re.split(r"[/\\]", path))


def _snapshot(base: Path) -> dict[str, str | None]:
    import hashlib
    out: dict[str, str | None] = {}
    for item in sorted(base.rglob("*")):
        rel = item.relative_to(base).as_posix()
        out[rel] = None if item.is_dir() else hashlib.sha256(item.read_bytes()).hexdigest()
    return out


def _build_world(base: Path) -> Path:
    if base.exists():
        shutil.rmtree(base)
    root = base / "root"
    (root / "sub").mkdir(parents=True)
    (base / "sibling").mkdir()
    (base / "root-evil").mkdir()
    (base / "canary.txt").write_bytes(b"canary\n")
    (base / "sibling" / "s.txt").write_bytes(b"sib\n")
    (base / "root-evil" / "r.txt").write_bytes(b"one\ntwo\n")
    (root / "a.txt").write_bytes(b"one\ntwo\n")
    (root / "sub" / "b.txt").write_bytes(b"bee\n")
    return root


def _hostile_patch(rng: random.Random, spelling: str) -> str:
    lower = spelling.lower()
    new = rng.choice(["b/" + spelling, "b/" + spelling, spelling, "a/" + spelling])
    lead = ""
    if rng.random() < 0.5:  # a valid first section: atomicity must undo it if a later one fails
        lead = "--- a/sub/b.txt\n+++ b/sub/b.txt\n@@ -1 +1 @@\n-bee\n+BEE\n"
    if rng.random() < 0.4:
        return lead + f"--- /dev/null\n+++ {new}\n@@ -0,0 +1 @@\n+created\n"
    if "a.txt" in lower:
        body = "@@ -1,2 +1,2 @@\n one\n-two\n+TWO\n"
    elif "b.txt" in lower:
        body = "@@ -1 +1 @@\n-bee\n+BEE\n"
    elif "r.txt" in lower or "s.txt" in lower:
        body = "@@ -1,2 +1,2 @@\n one\n-two\n+TWO\n"
    else:
        body = "@@ -1 +1 @@\n-canary\n+PWNED\n"
    old = rng.choice(["a/" + spelling, spelling, new])
    return lead + f"--- {old}\n+++ {new}\n{body}"


def _enter_snapshot(request) -> None:
    """Resolve the control-plane inventory once (documented ``read_policy_snapshot``).

    Without it every guarded call re-derives ~1000 path resolutions (~150-500 ms), which
    would make thousands of fuzz inputs impossible. All other checks still run per path.
    """
    scope = file_ops.read_policy_snapshot()
    scope.__enter__()
    request.addfinalizer(lambda: scope.__exit__(None, None, None))


_PUBLIC_ERRORS = (ValueError, PermissionError, FileNotFoundError, FileExistsError, tp.TextPatchError)


def test_text_patch_never_touches_anything_outside_root_and_is_atomic(tmp_path, request):
    _enter_snapshot(request)
    rng = _rng("text_patch_public")
    run = _Run()
    base = tmp_path / "world"
    applied_ok = rejected = 0
    spellings = list(SPELLINGS)
    for _ in range(70):
        spellings.append(rng.choice(["", "./", "sub/", "SUB/", "sub//", "a/"]) + rng.choice(
            ["a.txt", "b.txt", "A.TXT", "a.txt/", "r.txt", "s.txt", "../root-evil/r.txt", "../sibling/s.txt",
             "../canary.txt", "sub/../a.txt", "..%2fcanary.txt"]))
        spellings.append(_random_spelling(rng))
    for spelling in spellings:
        if "\n" in spelling or "\r" in spelling:
            continue
        root = _build_world(base)
        patch = _hostile_patch(rng, spelling)
        before = _snapshot(base)
        device = _is_device_spelling(spelling)
        outcome = run(tp.text_patch, str(root), patch, apply=not device, extra_roots=str(root))
        after = _snapshot(base)
        label = f"text_patch(spelling={spelling!r}, patch={patch!r})"
        _expect_error(outcome, _PUBLIC_ERRORS, label)
        if outcome[0] == "err":
            rejected += 1
            assert after == before, f"{label} raised {outcome[1]!r} but changed the tree: {({k for k in set(before) | set(after) if before.get(k) != after.get(k)})} ({TAG})"
            continue
        applied_ok += 1
        changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
        assert all(k.startswith("root/") for k in changed), f"{label} changed outside root: {changed} ({TAG})"
        assert not any(".sonder-patch-" in k for k in after), f"{label} leaked a staging file ({TAG})"
        if device:
            assert not changed, (label, TAG)
        for entry in outcome[1]["files"]:
            assert _contained_relative(entry["path"]), (label, entry, TAG)
    assert applied_ok >= 10 and rejected >= 60, (applied_ok, rejected, TAG)
    # Staging + fsync + a rollback-capable transaction per call: 50 ms is not meaningful here.
    run.check("text_patch public", p99_limit=0.4)


def test_text_patch_atomic_when_a_later_section_fails(tmp_path, request):
    _enter_snapshot(request)
    rng = _rng("text_patch_atomic")
    base = tmp_path / "world"
    failing = [
        "--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1,2 @@\n one\n-WRONG\n+x\n",           # context mismatch
        "--- /dev/null\n+++ b/a.txt\n@@ -0,0 +1 @@\n+x\n",                         # create over existing
        "--- a/missing.txt\n+++ b/missing.txt\n@@ -1 +1 @@\n-a\n+b\n",            # modify missing
        "--- /dev/null\n+++ b/nodir/new.txt\n@@ -0,0 +1 @@\n+x\n",                # parent missing
        "--- a/../canary.txt\n+++ b/../canary.txt\n@@ -1 +1 @@\n-canary\n+x\n",  # escape
        "--- /dev/null\n+++ b/sub\n@@ -0,0 +1 @@\n+x\n",                           # create over a directory
        "--- a/sub\n+++ b/sub\n@@ -1 +1 @@\n-a\n+b\n",                             # modify a directory
    ]
    valid = ["--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1,2 @@\n one\n-two\n+TWO\n",
             "--- /dev/null\n+++ b/created.txt\n@@ -0,0 +1 @@\n+hello\n",
             "--- a/sub/b.txt\n+++ b/sub/b.txt\n@@ -1 +1 @@\n-bee\n+BEE\n"]
    for bad in failing:
        for _ in range(8):
            root = _build_world(base)
            sections = rng.sample(valid, rng.randint(1, 2))
            position = rng.randint(0, len(sections))
            sections.insert(position, bad)
            patch = "".join(sections)
            before = _snapshot(base)
            with pytest.raises(_PUBLIC_ERRORS):
                tp.text_patch(str(root), patch, apply=True, extra_roots=str(root))
            assert _snapshot(base) == before, f"partial write for {patch!r} ({TAG})"
    # Control: the valid sections alone do commit (so the above is not vacuous).
    root = _build_world(base)
    result = tp.text_patch(str(root), "".join(valid), apply=True, extra_roots=str(root))
    assert result["transaction"] == "committed"
    assert (root / "a.txt").read_bytes() == b"one\nTWO\n" and (root / "created.txt").read_bytes() == b"hello\n"


def test_text_patch_refuses_to_write_through_a_junction_or_symlink(tmp_path, request):
    _enter_snapshot(request)
    base = tmp_path / "world"
    root = _build_world(base)
    target = base / "sibling"
    link = root / "link"
    made = False
    if os.name == "nt":
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                              capture_output=True, text=True, check=False).returncode == 0
    if not made:
        try:
            os.symlink(target, link, target_is_directory=True)
            made = True
        except (OSError, NotImplementedError):
            made = False
    if not made:
        pytest.skip("cannot create a junction or symlink without elevation on this machine")
    before = _snapshot(base / "sibling")
    for patch in (
        "--- a/link/s.txt\n+++ b/link/s.txt\n@@ -1 +1 @@\n-sib\n+PWNED\n",
        "--- /dev/null\n+++ b/link/new.txt\n@@ -0,0 +1 @@\n+PWNED\n",
    ):
        for apply in (False, True):
            with pytest.raises(_PUBLIC_ERRORS):
                tp.text_patch(str(root), patch, apply=apply, extra_roots=str(root))
    assert _snapshot(base / "sibling") == before, TAG
    assert (target / "s.txt").read_bytes() == b"sib\n"


# ---------------------------------------------------------------------------
# json_patch: pointer / index oracles
# ---------------------------------------------------------------------------

def _ref_decode(pointer: str) -> list[str] | None:
    """RFC 6901 section 4: split on '/', then ~1 -> '/', then ~0 -> '~'."""
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        return None
    out = []
    for raw in pointer[1:].split("/"):
        if re.search(r"~(?![01])", raw):
            return None
        out.append(raw.replace("~1", "/").replace("~0", "~"))
    return out if len(out) <= jp.MAX_POINTER_SEGMENTS else None


def _ref_encode(tokens: list[str]) -> str:
    return "".join("/" + token.replace("~", "~0").replace("/", "~1") for token in tokens)


def test_pointer_tokens_match_rfc6901_oracle_and_round_trip():
    rng = _rng("pointer")
    run = _Run()
    alphabet = "ab01~/ \u00e9.\u2215%-"
    for _ in range(4000):
        tokens = ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 6))) for _ in range(rng.randint(0, 6))]
        pointer = _ref_encode(tokens)
        kind, got = run(jp._pointer_tokens, pointer, 0)
        assert (kind, got) == ("ok", tokens), f"round trip of {tokens!r} via {pointer!r} gave {got!r} ({TAG})"
    for _ in range(6000):
        pointer = "".join(rng.choice("/~01a ") for _ in range(rng.randint(0, 14)))
        want = _ref_decode(pointer)
        outcome = run(jp._pointer_tokens, pointer, 3)
        _expect_error(outcome, (ValueError,), f"_pointer_tokens({pointer!r})")
        got = outcome[1] if outcome[0] == "ok" else None
        assert got == want, f"_pointer_tokens({pointer!r}) = {got!r}, RFC oracle says {want!r} ({TAG})"
    # RFC 6901 section 5 examples: "~01" is the literal "~1", never "/".
    assert jp._pointer_tokens("/a~01b", 0) == ["a~1b"] and jp._pointer_tokens("/m~0n", 0) == ["m~n"]
    assert jp._pointer_tokens("/a~1b", 0) == ["a/b"] and jp._pointer_tokens("", 0) == [""][:0]
    for not_a_string in (None, 5, b"/a", ["a"], {"a": 1}):
        assert run(jp._pointer_tokens, not_a_string, 0)[0] == "err"
    assert run(jp._pointer_tokens, "/a" * jp.MAX_POINTER_SEGMENTS, 0)[0] == "ok"
    assert run(jp._pointer_tokens, "/a" * (jp.MAX_POINTER_SEGMENTS + 1), 0)[0] == "err"
    run.check("_pointer_tokens")


def _ref_index(token: str, length: int, allow_end: bool, allow_dash: bool) -> int | None:
    if token == "-":
        return length if allow_dash else None
    if not re.fullmatch(r"0|[1-9][0-9]*", token, re.ASCII):
        return None
    try:
        number = int(token)
    except ValueError:  # CPython's 4300-digit int() guard
        return None
    return number if number <= (length if allow_end else length - 1) else None


def test_array_index_matches_oracle():
    rng = _rng("array_index")
    run = _Run()
    fixed = ["0", "1", "2", "9", "10", "00", "01", "-0", "-1", "+1", "+0", " 1", "1 ", "1\n", "", "-", "--", "1.0", "1e1",
             "0x1", "1_0", "\u0661", "\uff11", "\u00b2", "\u0967", "9" * 20, "9" * 5000, str(2 ** 63), str(2 ** 64),
             "0" * 5000, "1" + "0" * 4299, "1" + "0" * 4300, "\x00", "1\x00", "NaN", "inf"]
    tokens = list(fixed)
    for _ in range(3000):
        tokens.append("".join(rng.choice("0123456789-+_ .\u0661") for _ in range(rng.randint(0, 5))))
    for token in tokens:
        for length in (0, 1, 3):
            for allow_end in (False, True):
                for allow_dash in (False, True):
                    outcome = run(jp._array_index, token, length, allow_end=allow_end, allow_dash=allow_dash)
                    _expect_error(outcome, (ValueError,), f"_array_index({token[:40]!r})")
                    want = _ref_index(token, length, allow_end, allow_dash)
                    got = outcome[1] if outcome[0] == "ok" else None
                    assert got == want, (
                        f"_array_index({token[:40]!r}, {length}, end={allow_end}, dash={allow_dash}) = {got!r}, "
                        f"oracle {want!r} ({TAG})")
    for token in ("01", "-1", "+1", str(10 ** 30), "007"):
        assert run(jp._array_index, token, 5, allow_end=True, allow_dash=True)[0] == "err", (token, TAG)
    run.check("_array_index")


# ---------------------------------------------------------------------------
# json_patch: _loads_strict / _parse_operations
# ---------------------------------------------------------------------------

def _walk(value):
    stack = [value]
    while stack:
        item = stack.pop()
        yield item
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)


_JSON_SEEDS = [
    '{"a": 1, "b": [true, null, 2.5, "x"], "c": {"d": "e"}}', "[1, 2, 3]", '[{"op":"add","path":"/a","value":1}]',
    '{"a": NaN}', '{"a": Infinity}', '{"a": -Infinity}', '{"a": 1, "a": 2}', '{"x": {"y": 1, "y": 1}}',
    '{"a": 1e999}', '[-1e999]', '{"a": 1E400}', '"str"', "7", "[]", "{}", '{"k": "\\ud800"}', '{"\\u0061": 1, "a": 2}',
]


def _mutate_json(rng: random.Random, text: str) -> str:
    op = rng.randrange(6)
    if op == 0:
        chars = list(text)
        for _ in range(rng.randint(1, 4)):
            if chars:
                chars[rng.randrange(len(chars))] = rng.choice('{}[]",:\\0123456789eE.-+nNtfIa \n\t\x00\u00e9')
        return "".join(chars)
    if op == 1:
        return text[: rng.randrange(len(text) + 1)]
    if op == 2:
        position = rng.randrange(len(text) + 1)
        return text[:position] + rng.choice(["NaN", "Infinity", "-Infinity", "1e999", ",", '"a":1,"a":2', "{", "[", "\\u0000", "//", "/*"]) + text[position:]
    if op == 3:
        return text * rng.randint(2, 3)
    if op == 4:
        return rng.choice(["", " ", "\ufeff", "\n"]) + text
    return text.replace(":", ": ", 1)


_OVERFLOW_LITERAL = re.compile(r"\d[eE][+-]?\d{3,}")


def test_loads_strict_error_contract_and_strictness():
    rng = _rng("loads_strict")
    run = _Run()
    for _ in range(4000):
        text = rng.choice(_JSON_SEEDS)
        for _step in range(rng.randint(0, 3)):
            text = _mutate_json(rng, text)
        outcome = run(jp._loads_strict, text, "doc")
        _expect_error(outcome, (ValueError,), f"_loads_strict({text!r})")
        if outcome[0] == "ok":
            if _OVERFLOW_LITERAL.search(text):
                continue  # pinned by test_loads_strict_rejects_exponent_overflow_to_infinity
            for node in _walk(outcome[1]):
                assert not (isinstance(node, float) and not math.isfinite(node)), (
                    f"_loads_strict({text!r}) accepted non-finite number {node!r} ({TAG})")
    for text in ('{"a":1,"a":2}', '{"x":{"y":1,"y":1}}', '[{"k":1,"k":1}]', "NaN", "[Infinity]", "-Infinity", '{"a":NaN}'):
        outcome = run(jp._loads_strict, text, "doc")
        assert outcome[0] == "err" and isinstance(outcome[1], ValueError), (text, outcome, TAG)
    for text in (b"x", 5, None):
        assert run(jp._loads_strict, text, "doc")[0] == "err"
    run.check("_loads_strict")


def test_loads_strict_rejects_exponent_overflow_to_infinity():
    # "non-finite JSON number is not allowed" is the documented contract; 1e999 parses to inf.
    for text in ("[1e999]", '{"a": -1e999}', "[1E400]"):
        outcome = _Run()(jp._loads_strict, text, "doc")
        assert outcome[0] == "err" and isinstance(outcome[1], ValueError), (
            f"_loads_strict({text!r}) returned {outcome[1]!r} instead of rejecting a non-finite number")


_OP_SEEDS = [
    [{"op": "add", "path": "/a", "value": 1}],
    [{"op": "remove", "path": "/a/b/0"}, {"op": "replace", "path": "/k", "value": {"x": [1, 2]}}],
    [{"op": "test", "path": "/a~1b", "value": None}, {"op": "add", "path": "/l/-", "value": "v"}],
    [{"op": "add", "path": "", "value": {"root": True}}],
    [{"op": "replace", "path": "/~0~1", "value": [1.5, True, "\u00e9"]}],
]


def _mutate_ops(rng: random.Random, ops):
    ops = copy.deepcopy(ops)
    op = rng.randrange(9)
    target = rng.choice(ops)
    if op == 0:
        target["path"] = rng.choice(["a", "/a~", "/a~2", "/~", "//", "/01", "/-1", " /a", "/a/ ", None, 5, ["a"], "/" + "a/" * 70])
    elif op == 1:
        target["op"] = rng.choice(["move", "copy", "ADD", "", None, 3, "add ", "Replace", "test\x00"])
    elif op == 2:
        target.pop(rng.choice(sorted(target)), None)
    elif op == 3:
        target[rng.choice(["from", "extra", "", "value2", "OP"])] = rng.choice([1, "x", None])
    elif op == 4:
        target["value"] = rng.choice([float("nan"), float("inf"), object(), {1, 2}, b"x", [[]] * 3, {"a": {"b": {}}}, 2 ** 70, -0.0])
    elif op == 5:
        ops = ops * rng.choice([1, 50, 101, 150])
    elif op == 6:
        ops = rng.choice([[], {}, None, "", "[]", 5, [5], [[]], [None], ["x"], [{}]])
    elif op == 7:
        target["path"] = "/" + "/".join(rng.choice(["a", "~0", "~1", "~01", "b", "0", "-"]) for _ in range(rng.randint(0, 6)))
    else:
        target["value"] = {"deep": ops_depth(rng.randint(1, 40))}
    return ops


def ops_depth(depth: int):
    value: object = 1
    for _ in range(depth):
        value = [value]
    return value


def test_parse_operations_error_contract_and_normal_form():
    rng = _rng("parse_operations")
    run = _Run()
    accepted = 0
    for _ in range(3500):
        ops = _mutate_ops(rng, rng.choice(_OP_SEEDS))
        if rng.random() < 0.6:
            try:
                arg = json.dumps(ops)
            except (TypeError, ValueError):
                arg = ops
            if isinstance(arg, str) and rng.random() < 0.4:
                arg = _mutate_json(rng, arg)
        else:
            arg = ops
        outcome = run(jp._parse_operations, arg)
        _expect_error(outcome, (ValueError,), "_parse_operations(%r)" % (arg if isinstance(arg, str) else repr(arg)[:200]))
        if outcome[0] == "ok":
            accepted += 1
            normal = outcome[1]
            assert 1 <= len(normal) <= jp.MAX_OPERATIONS
            for op, tokens, _value in normal:
                assert op in {"add", "remove", "replace", "test"} and all(isinstance(t, str) for t in tokens)
                assert len(tokens) <= jp.MAX_POINTER_SEGMENTS
    assert accepted >= 100, f"mutator rarely produced valid operations ({TAG})"
    # Known positives for the exact-keys rule and the numeric limits.
    bad_ops = [
        [{"op": "add", "path": "/a"}], [{"op": "test", "path": "/a"}], [{"op": "replace", "path": "/a"}],
        [{"op": "remove", "path": "/a", "value": 1}], [{"op": "add", "path": "/a", "value": 1, "from": "/b"}],
        [{"op": "move", "from": "/a", "path": "/b"}], [{"op": "copy", "from": "/a", "path": "/b"}],
        [{"op": "add", "path": "/a~", "value": 1}], [{"op": "add", "path": "a", "value": 1}], [],
        [{"op": "add", "path": "/a", "value": 1}] * (jp.MAX_OPERATIONS + 1),
    ]
    for ops in bad_ops:
        for arg in (ops, json.dumps(ops)):
            outcome = run(jp._parse_operations, arg)
            assert outcome[0] == "err" and isinstance(outcome[1], ValueError), (arg if isinstance(arg, str) else ops, outcome, TAG)
    assert run(jp._parse_operations, [{"op": "add", "path": "/a", "value": 1}] * jp.MAX_OPERATIONS)[0] == "ok"
    run.check("_parse_operations")


# ---------------------------------------------------------------------------
# json_patch: differential against an RFC 6902 oracle (documented subset)
# ---------------------------------------------------------------------------

class _RefError(Exception):
    pass


def _tag(value):
    if isinstance(value, bool):
        return ("bool", value)
    if value is None:
        return ("null",)
    if isinstance(value, (int, float)):
        return ("num", value)
    if isinstance(value, str):
        return ("str", value)
    if isinstance(value, list):
        return ("arr", tuple(_tag(item) for item in value))
    return ("obj", tuple(sorted((key, _tag(item)) for key, item in value.items())))


def _ref_get(doc, tokens):
    for token in tokens:
        if isinstance(doc, dict):
            if token not in doc:
                raise _RefError
            doc = doc[token]
        elif isinstance(doc, list):
            index = _ref_index(token, len(doc), False, False)
            if index is None:
                raise _RefError
            doc = doc[index]
        else:
            raise _RefError
    return doc


def _ref_apply(doc, ops):
    doc = copy.deepcopy(doc)
    for op, tokens, value in ops:
        if op == "test":
            if _tag(_ref_get(doc, tokens)) != _tag(value):
                raise _RefError
            continue
        if not tokens:
            if op == "remove" or not isinstance(value, (dict, list)):
                raise _RefError
            doc = copy.deepcopy(value)
            continue
        parent, last = _ref_get(doc, tokens[:-1]), tokens[-1]
        value = copy.deepcopy(value)
        if isinstance(parent, dict):
            if op in ("remove", "replace") and last not in parent:
                raise _RefError
            if op == "remove":
                del parent[last]
            else:
                parent[last] = value
        elif isinstance(parent, list):
            index = _ref_index(last, len(parent), op == "add", op == "add")
            if index is None:
                raise _RefError
            if op == "add":
                parent.insert(index, value)
            elif op == "remove":
                del parent[index]
            else:
                parent[index] = value
        else:
            raise _RefError
    return doc


def _random_doc(rng: random.Random, depth: int = 0):
    kind = rng.randrange(6 if depth < 3 else 4)
    if kind == 0:
        return rng.choice([0, 1, -3, 1.5, 2 ** 40])
    if kind == 1:
        return rng.choice(["", "s", "a/b", "~", "\u00e9"])
    if kind == 2:
        return rng.choice([True, False, None])
    if kind == 3:
        return rng.randrange(4)
    if kind == 4:
        return [_random_doc(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    return {rng.choice(["a", "b", "a/b", "~x", "0", "01", "-", "", "k~1"]): _random_doc(rng, depth + 1) for _ in range(rng.randint(0, 4))}


def _paths(doc, prefix=()):
    yield list(prefix)
    if isinstance(doc, dict):
        for key, item in doc.items():
            yield from _paths(item, prefix + (key,))
    elif isinstance(doc, list):
        for index, item in enumerate(doc):
            yield from _paths(item, prefix + (str(index),))


def test_apply_operations_matches_rfc6902_oracle():
    rng = _rng("apply_oracle")
    run = _Run()
    agreed_ok = agreed_err = 0
    for _ in range(2500):
        doc = {"r": _random_doc(rng), "l": [1, [2, 3], {"a": 1}], "m": {"a": 1, "b/c": [True]}}
        spec = []
        for _ in range(rng.randint(1, 4)):
            path = list(rng.choice(list(_paths(doc))))
            roll = rng.random()
            if path and roll < 0.12:
                path[-1] = rng.choice(["-", "01", "-1", "+1", "99", "0", "x", "1"])
            elif roll < 0.18:
                path.append(rng.choice(["-", "0", "z", "~"]))
            op = rng.choice(["add", "remove", "replace", "test"])
            value = copy.deepcopy(rng.choice([_random_doc(rng), doc.get("m"), 1, True, None, 1.0, [], {}]))
            if op == "test" and rng.random() < 0.5:
                try:
                    value = _ref_get(doc, path)
                except _RefError:
                    pass
            entry = {"op": op, "path": _ref_encode(path)}
            if op != "remove":
                entry["value"] = value
            spec.append(entry)
        parsed = jp._parse_operations(json.dumps(spec))
        try:
            want = _tag(_ref_apply(doc, parsed))
        except _RefError:
            want = None
        outcome = run(jp._apply_operations, copy.deepcopy(doc), parsed)
        _expect_error(outcome, (ValueError,), f"_apply_operations({doc!r}, {spec!r})")
        got = _tag(outcome[1]) if outcome[0] == "ok" else None
        assert got == want, f"differential mismatch: doc={doc!r} ops={spec!r} impl={outcome[1]!r} oracle={want!r} ({TAG})"
        agreed_ok += want is not None
        agreed_err += want is None
    assert agreed_ok >= 300 and agreed_err >= 300, (agreed_ok, agreed_err, TAG)
    run.check("_apply_operations")


# ---------------------------------------------------------------------------
# json_patch: public patch_json containment + atomicity
# ---------------------------------------------------------------------------

_JSON_PATHS = [
    "data.json", "./data.json", "sub/n.json", "DATA.JSON", "data.json.", "data.json ", "data.json::$DATA", "data.json:s",
    "sub/../data.json", "../canary.json", "sub/../../canary.json", "..\\canary.json", "sub\\..\\..\\canary.json",
    "../root-evil/x.json", "../rootx/x.json", "%2e%2e/canary.json", "..%2fcanary.json", "\u2025/canary.json",
    "\u2215canary.json", "\uff0e\uff0e/canary.json", "/dev/null", "NUL", "CON", "C:canary.json",
    "data.json\x00", "", " ", ".", "sub", "sub/", "x" * 400 + ".json",
]


def _build_json_world(base: Path) -> Path:
    if base.exists():
        shutil.rmtree(base)
    root = base / "root"
    (root / "sub").mkdir(parents=True)
    (base / "rootx").mkdir()
    (base / "root-evil").mkdir()
    for path in (root / "data.json", base / "canary.json", base / "rootx" / "x.json", base / "root-evil" / "x.json",
                 root / "sub" / "n.json"):
        path.write_text(json.dumps({"a": {"b": [1, 2, 3]}, "k": "v", "n": None}), encoding="utf-8")
    return root


_JSON_OPS = [
    [{"op": "add", "path": "/a/b/-", "value": 4}],
    [{"op": "replace", "path": "/k", "value": "w"}, {"op": "remove", "path": "/n"}],
    [{"op": "add", "path": "/new", "value": {"x": [1]}}, {"op": "test", "path": "/a/b/0", "value": 1}],
    [{"op": "add", "path": "/ok", "value": 1}, {"op": "remove", "path": "/does/not/exist"}],        # fails after a change
    [{"op": "add", "path": "/ok", "value": 1}, {"op": "test", "path": "/k", "value": "WRONG"}],      # fails after a change
    [{"op": "add", "path": "/ok", "value": 1}, {"op": "add", "path": "/a/b/01", "value": 1}],
    [{"op": "add", "path": "/ok", "value": 1}, {"op": "remove", "path": ""}],
]
_JSON_PUBLIC_ERRORS = (ValueError, PermissionError, FileNotFoundError, jp.JsonPatchError)


def test_patch_json_never_touches_anything_outside_root_and_is_atomic(tmp_path, monkeypatch, request):
    rng = _rng("patch_json_public")
    run = _Run()
    base = tmp_path / "world"
    root = _build_json_world(base)
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    _enter_snapshot(request)
    ok = rejected = 0
    spellings = list(_JSON_PATHS) + ["data.json", "./data.json", "sub/n.json", "DATA.JSON", "sub//n.json", "SUB/N.JSON"] * 3
    for name in ("canary.json", "rootx/x.json", "root-evil/x.json", "sub/n.json"):
        spellings.append(str(base / name))
        spellings.append(str(base / name).replace("\\", "/"))
        spellings.append(str(root / ".." / name))
    for _ in range(40):
        spellings.append(rng.choice(["", "./", "sub/", "SUB/", "sub//"]) + rng.choice(
            ["data.json", "n.json", "../canary.json", "../../canary.json", "DATA.json", "data.json/", ".\\data.json"]))
    for spelling in spellings:
        for mode in (rng.choice(["preview", "apply"]),):
            root = _build_json_world(base)
            ops = rng.choice(_JSON_OPS)
            before = _snapshot(base)
            outcome = run(jp.patch_json, spelling, ops, mode=mode)
            after = _snapshot(base)
            label = f"patch_json({spelling!r}, {mode})"
            _expect_error(outcome, _JSON_PUBLIC_ERRORS, label)
            changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
            if outcome[0] == "err":
                rejected += 1
                assert not changed, f"{label} raised {outcome[1]!r} but changed {changed} ({TAG})"
                continue
            ok += 1
            resolved = Path(outcome[1]["path"]).resolve()
            assert resolved.is_relative_to(root.resolve()), f"{label} resolved outside root: {resolved} ({TAG})"
            assert all(k.startswith("root/") and k.endswith(".json") for k in changed), (label, changed, TAG)
            assert (mode == "apply") or not changed, (label, changed, TAG)
            assert not any(".sonder-json-patch-" in k for k in after), (label, TAG)
            if mode == "apply":
                written = json.loads(resolved.read_text(encoding="utf-8"))
                assert _tag(written) == _tag(outcome[1]["document"]), (label, TAG)
    assert ok >= 3 and rejected >= 40, (ok, rejected, TAG)
    run.check("patch_json public", p99_limit=0.25)  # temp file + fsync + replace per apply


def test_patch_json_failing_operation_leaves_bytes_identical(tmp_path, monkeypatch, request):
    base = tmp_path / "world"
    root = _build_json_world(base)
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    _enter_snapshot(request)
    target = root / "data.json"
    original = target.read_bytes()
    failing = [ops for ops in _JSON_OPS[3:]]
    for ops in failing:
        for mode in ("preview", "apply"):
            with pytest.raises(ValueError):
                jp.patch_json("data.json", ops, mode=mode)
            assert target.read_bytes() == original, (ops, mode, TAG)
    jp.patch_json("data.json", _JSON_OPS[0], mode="apply")  # control: a valid patch really rewrites the file
    assert target.read_bytes() != original
    assert json.loads(target.read_text(encoding="utf-8"))["a"]["b"] == [1, 2, 3, 4]


# ---------------------------------------------------------------------------
# deep nesting / time budgets (json)
# ---------------------------------------------------------------------------

def _nested_text(depth: int) -> str:
    return "[" * depth + "]" * depth


@pytest.mark.parametrize("depth", [65, 200, 800, 5000, 100_000])
def test_deeply_nested_json_is_rejected_with_valueerror_not_recursionerror(depth, tmp_path, monkeypatch, request):
    """Regression: json.loads raises RecursionError past ~1000 levels, escaping the ValueError contract."""
    text = _nested_text(depth)
    run = _Run()
    failures = []
    for name, call in (
        ("_loads_strict", lambda: jp._loads_strict(text, "x")),
        ("_parse_operations(str)", lambda: jp._parse_operations(text)),
        ("_parse_operations(list)", lambda: jp._parse_operations(_deep_list(depth))),
        ("parse_candidate_patch", lambda: parse_candidate_patch(text)),
    ):
        kind, value = run(call)
        allowed = (BuildDomainError,) if name == "parse_candidate_patch" else (ValueError,)
        if name == "_loads_strict" and kind == "ok":
            continue  # depth is enforced by the callers; the parser itself may simply succeed
        if not (kind == "err" and isinstance(value, allowed)):
            failures.append(f"{name}: {kind} {type(value).__name__} {str(value)[:60]!r}")
    base = tmp_path / "world"
    root = _build_json_world(base)
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    _enter_snapshot(request)
    (root / "deep.json").write_text(text, encoding="utf-8")
    kind, value = run(jp.patch_json, "deep.json", [{"op": "add", "path": "/a", "value": 1}])
    if not (kind == "err" and isinstance(value, ValueError)):
        failures.append(f"patch_json: {kind} {type(value).__name__} {str(value)[:60]!r}")
    assert not failures, f"depth {depth} escaped the typed-error contract ({TAG}): {failures}"
    run.check("deep nesting")


def _deep_list(depth: int):
    value: list = []
    for _ in range(depth):
        value = [value]
    return value


def test_json_ten_thousand_line_inputs_stay_in_budget(tmp_path, monkeypatch, request):
    run = _Run()
    big_text = json.dumps(list(range(9000)), indent=1)
    assert big_text.count("\n") > 9000
    kind, value = run(jp._loads_strict, big_text, "doc")
    assert kind == "ok" and len(value) == 9000, (kind, TAG)
    ops_text = json.dumps([{"op": "add", "path": "/a", "value": 1}] * 10_000, indent=1)
    outcome = run(jp._parse_operations, ops_text)
    assert outcome[0] == "err" and isinstance(outcome[1], ValueError), (outcome, TAG)
    wide_ops = json.dumps([{"op": "add", "path": f"/k{i}", "value": list(range(50))} for i in range(100)], indent=1)
    assert run(jp._parse_operations, wide_ops)[0] == "ok"
    root = _build_json_world(tmp_path / "world")
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    _enter_snapshot(request)
    (root / "big.json").write_text(big_text, encoding="utf-8")
    kind, report = run(jp.patch_json, "big.json", [{"op": "add", "path": "/-", "value": 9000}, {"op": "remove", "path": "/0"}])
    assert kind == "ok" and report["document"][0] == 1 and report["document"][-1] == 9000, (kind, report, TAG)
    for depth in (jp.MAX_JSON_DEPTH - 1, jp.MAX_JSON_DEPTH, jp.MAX_JSON_DEPTH + 1):
        outcome = run(jp._parse_operations, [{"op": "add", "path": "/a", "value": _deep_list(depth - 3)}])
        assert outcome[0] == ("ok" if depth <= jp.MAX_JSON_DEPTH else "err"), (depth, outcome, TAG)
    run.check("json 10k lines")


# ---------------------------------------------------------------------------
# repair.parse_candidate_patch
# ---------------------------------------------------------------------------

_ANSWER = {"hunks": [{"file": "src/core/math.cpp", "anchor": "int x = 0;", "replace": "int x = 1;"},
                     {"file": "include/spark/vec.h", "anchor": "a", "replace": ""}], "rationale": "fix the init"}


def _same(patch: CandidatePatch, answer: dict) -> bool:
    return (tuple((h.file_rel, h.anchor, h.replacement) for h in patch.hunks)
            == tuple((h["file"], h["anchor"], h["replace"]) for h in answer["hunks"])
            and patch.rationale == answer.get("rationale", ""))


def _wrap(rng: random.Random, body: str) -> tuple[str, bool]:
    """(text, expected_to_parse_to_the_same_object)."""
    kind = rng.randrange(9)
    if kind == 0:
        return body, True
    if kind == 1:
        return rng.choice(["", " ", "\n", "\t\r\n"]) + body + rng.choice(["", " ", "\n\n"]), True
    if kind == 2:
        return "```json\n" + body + "\n```", True
    if kind == 3:
        return "```\n" + body + "\n```\n", True
    if kind == 4:
        return "Here is the fix:\n" + body, False
    if kind == 5:
        return body + "\nHope that helps!", False
    if kind == 6:
        return "Sure.\n```json\n" + body + "\n```\nDone.", False
    if kind == 7:
        return "```json\n" + body + "\n``` trailing", False
    return rng.choice(["{", "[", "```", "json", "`"]) + body + rng.choice(["", "}", "]"]), False


def test_parse_candidate_patch_wrappers_prose_and_error_contract():
    rng = _rng("candidate_wrap")
    run = _Run()
    for _ in range(2500):
        answer = copy.deepcopy(_ANSWER)
        if rng.random() < 0.3:
            answer.pop("rationale")
        body = json.dumps(answer, indent=rng.choice([None, 1, 2]), ensure_ascii=rng.random() < 0.5)
        text, expect_same = _wrap(rng, body)
        outcome = run(parse_candidate_patch, text)
        _expect_error(outcome, (BuildDomainError,), f"parse_candidate_patch({text[:300]!r})")
        if outcome[0] == "err":
            assert outcome[1].code == "FIX_SCOPE_REJECTED", (text[:200], outcome[1].code, TAG)
            assert not expect_same, f"clean wrapper {text[:200]!r} was rejected: {outcome[1]!r} ({TAG})"
        else:
            # Whatever parsed must be the very same object, never a different/partial one.
            assert _same(outcome[1], answer), f"wrapped answer {text[:200]!r} parsed to a different patch {outcome[1]!r} ({TAG})"
    run.check("parse_candidate_patch wrappers")


def _mutate_answer_text(rng: random.Random, text: str) -> str:
    op = rng.randrange(5)
    if op == 0:
        chars = list(text)
        for _ in range(rng.randint(1, 5)):
            chars[rng.randrange(len(chars))] = rng.choice('{}[]",:\\`0 \n\x00\u00e9\ud800')
        return "".join(chars)
    if op == 1:
        return text[: rng.randrange(len(text) + 1)]
    if op == 2:
        position = rng.randrange(len(text) + 1)
        return text[:position] + rng.choice(['"file":"../x",', '"extra":1,', "```", "NaN", ",,", '"hunks":[],']) + text[position:]
    if op == 3:
        return text.replace('"file"', rng.choice(['"File"', '"path"', '"file "', '"fi\\u006ce"']), 1)
    return text.replace("src/core/math.cpp", rng.choice(SPELLINGS).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n"), 1)


def test_parse_candidate_patch_accepted_files_are_contained():
    rng = _rng("candidate_files")
    run = _Run()
    accepted = 0
    spellings = SPELLINGS + [_random_spelling(rng) for _ in range(300)]
    for spelling in spellings:
        for decor in ("", "./", "src/", "a/../"):
            answer = {"hunks": [{"file": decor + spelling, "anchor": "x", "replace": "y"}], "rationale": ""}
            for text in (json.dumps(answer), "```json\n" + json.dumps(answer) + "\n```"):
                outcome = run(parse_candidate_patch, text)
                _expect_error(outcome, (BuildDomainError,), "file=%r" % (decor + spelling))
                if outcome[0] == "err":
                    assert outcome[1].code == "FIX_SCOPE_REJECTED"
                    continue
                accepted += 1
                for rel in outcome[1].files():
                    assert _contained_relative(rel), f"accepted file {decor + spelling!r} -> {rel!r} is not contained ({TAG})"
    for _ in range(3000):
        text = _mutate_answer_text(rng, json.dumps(_ANSWER))
        outcome = run(parse_candidate_patch, text)
        _expect_error(outcome, (BuildDomainError,), f"parse_candidate_patch({text[:300]!r})")
        if outcome[0] == "ok":
            accepted += 1
            assert 1 <= len(outcome[1].hunks) <= 8
            for rel in outcome[1].files():
                assert _contained_relative(rel), f"mutated answer {text[:200]!r} accepted file {rel!r} ({TAG})"
    assert accepted >= 100, (accepted, TAG)
    for escape in ESCAPES:
        if re.fullmatch(r"[A-Za-z]:(?![/\\]).*", escape) or escape == "a\\b":
            # "c:x" (drive-relative) is refused later by EditScope.allows, and "a\b" is just "a/b" to
            # safe_rel: neither is an escape at this layer (the first is reported as a SUSPECT).
            continue
        answer = json.dumps({"hunks": [{"file": escape, "anchor": "x", "replace": "y"}]})
        outcome = run(parse_candidate_patch, answer)
        assert outcome[0] == "err" and outcome[1].code == "FIX_SCOPE_REJECTED", (escape, outcome, TAG)
    run.check("parse_candidate_patch files")


def test_parse_candidate_patch_big_inputs_stay_in_budget():
    run = _Run()
    lines = "\n".join(f' {{"file": "a{i}.cpp", "anchor": "x", "replace": "y"}},' for i in range(3000))
    text = '{"hunks": [\n' + lines + '\n {"file": "z.cpp", "anchor": "x", "replace": "y"}]}'
    outcome = run(parse_candidate_patch, text)
    assert outcome[0] == "err" and outcome[1].code == "FIX_SCOPE_REJECTED", (outcome, TAG)
    assert len(text) < 200_000
    padded = json.dumps(_ANSWER, indent=1) + "\n" * 150_000
    assert run(parse_candidate_patch, padded)[0] == "ok"
    outcome = run(parse_candidate_patch, "x" * 200_001)
    assert outcome[0] == "err" and outcome[1].code == "FIX_SCOPE_REJECTED"
    outcome = run(parse_candidate_patch, "```json\n" + "\n" * 190_000 + "not json")
    assert outcome[0] == "err" and outcome[1].code == "FIX_SCOPE_REJECTED"
    outcome = run(parse_candidate_patch, "```" + " " * 190_000 + "x")
    assert outcome[0] == "err" and outcome[1].code == "FIX_SCOPE_REJECTED"
    run.check("parse_candidate_patch big")


# ---------------------------------------------------------------------------
# hostile-directive scan: positives under mutation
# ---------------------------------------------------------------------------

OK = include_checker(SCOPE, "src/core/math.cpp")
_CONTEXT = [
    "int add(int a, int b) { return a + b; }\n", "namespace spark {\n", "}\n", "#include <vector>\n",
    '#include "detail/impl.inl"\n', "#define SPARK_VERSION 3\n", "// a comment\n", "/* block\n   comment */\n",
    "target_link_libraries(game PRIVATE spark)\n", "set(CMAKE_CXX_STANDARD 20)\n", "add_executable(game main.cpp)\n",
    "static_assert(sizeof(int) == 4);\n", "template <class T> struct Box { T v; };\n", 'const char* s = "hello world";\n',
    "#pragma once\n", "if(WIN32)\n  add_definitions(-DUNICODE)\nendif()\n", "\n", "    int y = x * 2; // scale\n",
    "enum class Mode { A, B };\n", "#if defined(_WIN32)\n#endif\n",
]


def _scan(text: str, raw_strings: bool = True, trigraphs: bool = True,
          digraphs: bool = True, **kwargs) -> tuple[str, ...]:
    """One reading (``separators=`` picks the quote reading; the default is C++14)."""
    return scan_hostile_directives(
        normalize_for_scan(text, raw_strings=raw_strings, trigraphs=trigraphs, digraphs=digraphs, **kwargs),
        include_ok=OK)


def _scan_every_lexing(text: str) -> tuple[str, ...]:
    """What ``validate_patch`` sees: the union over every raw-string/trigraph/digraph lexing."""
    return scan_added_lexings("", text, include_ok=OK)


def _scan_cxx_lexings(text: str) -> tuple[str, ...]:
    """Every trigraph/digraph reading, with C++ raw strings (``R"(`` masked) as the only string lexing."""
    found: list[str] = []
    for trigraphs in (True, False):
        for digraphs in (True, False):
            found.extend(_scan(text, True, trigraphs, digraphs))
    return tuple(found)


_IDENT_BEFORE_PAREN = re.compile(
    r"\b(_Pragma|__pragma|__asm__|__asm|asm|__has_include|__has_embed|__attribute__|__attribute|__declspec)(?=\()")
_GAS_WORD = re.compile(r"(?<=\.)(incbin|include|file|section|pushsection|init_array|fini_array|ctors|dtors)\b", re.IGNORECASE)
_PRAGMA_WORD = re.compile(r"\b(comment|lib|linker|user|include_alias|section|init_seg|code_seg|data_seg|const_seg|"
                          r"bss_seg|alloc_text|detect_mismatch|poison)\b")


def _code_ws(text: str) -> list[int]:
    """Indexes of blanks that sit in code (not in a string/char literal or comment)."""
    out, i, n = [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch in "\"'":
            j = i + 1
            while j < n and text[j] != ch and text[j] not in "\r\n":
                if text[j] == "\\":
                    splice = re.compile(r"\\[ \t]*(?:\r\n|\n|\r)").match(text, j)
                    j = splice.end() if splice else j + 2
                else:
                    j += 1
            i = j + 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            # Blanks next to a backslash/newline are part of a splice, not free whitespace.
            if ch in " \t" and text[i - 1:i] != "\\" and text[i + 1:i + 2] not in ("\n", "\r"):
                out.append(i)
            i += 1
    return out


def m_splice(rng: random.Random, text: str) -> str:
    # Distinct positions of the ORIGINAL text and never right after a backslash: a splice stacked on
    # another splice is the nested-splice shape, probed separately (see the report), not a plain bypass.
    # Trigraphs are replaced in phase 1, before splicing, so "?\\\n?=" is not "??=": keep them whole.
    spots = [i for i in range(len(text) + 1)
             if text[i - 1:i] != "\\" and text[i:i + 1] != "\r" and text[i - 2:i] != "??" and text[i - 1:i + 1] not in ("??", "\r\n")]
    for i in sorted(rng.sample(spots, min(len(spots), rng.randint(1, 4))), reverse=True):
        text = text[:i] + rng.choice(["\\\n", "\\\n", "\\ \n", "\\\t\n"]) + text[i:]
    return text


def m_comment(rng: random.Random, text: str) -> str:
    positions = [i for i in _code_ws(text)
                 if text[i - 1:i] not in ("/", "*") and text[i + 1:i + 2] not in ("/", "*")]
    for i in sorted(rng.sample(positions, min(len(positions), rng.randint(1, 3))), reverse=True):
        text = text[:i] + rng.choice(["/**/", "/* c */", "/* x\n y */"]) + text[i + 1:]
    if rng.random() < 0.5 and "\r" not in text:
        text = re.sub(r"(?<!\\)(?<!\\ )(?<!\\\t)\n", " // trailing\n", text, count=1)
    if rng.random() < 0.3:
        text = "/* lead */ " + text
    return text


def m_ws(rng: random.Random, text: str) -> str:
    for i in sorted(rng.sample(_code_ws(text), min(len(_code_ws(text)), 3)), reverse=True):
        text = text[:i] + rng.choice(["\t", "  ", "\t \t", "   ", "\f", " \v "]) + text[i + 1:]
    text = re.sub(r"^(\s*)#(?=\S)", lambda m: m.group(1) + "#" + rng.choice(["", " ", "\t", "  "]), text, flags=re.MULTILINE)
    return _IDENT_BEFORE_PAREN.sub(lambda m: m.group(1) + rng.choice(["", " ", "\t", "  "]), text)


def m_case(rng: random.Random, text: str) -> str:
    def scramble(match):
        return "".join(c.upper() if rng.random() < 0.5 else c.lower() for c in match.group(0))
    text = _GAS_WORD.sub(scramble, text)
    return "\n".join(_PRAGMA_WORD.sub(scramble, line) if re.search(r"pragma", line) else line
                     for line in text.split("\n"))


def m_crlf(rng: random.Random, text: str) -> str:
    return text.replace("\n", rng.choice(["\r\n", "\r"]))


def m_concat(rng: random.Random, text: str) -> str:
    # Judge the text the way the compiler lexes it (splices and comments gone), not its raw spelling.
    seen = normalize_for_scan(text).lstrip()
    if (seen.startswith("#") and not seen.startswith("#define")) or re.search(r"_Pragma|__pragma|__has_|%:|\?\?=", seen):
        return text
    spots = []
    for match in re.finditer(r'"((?:[^"\\\n]|\\.)*)"', text):
        for k in range(1, len(match.group(1))):
            body = match.group(1)
            if len(re.search(r"\\*$", body[:k]).group(0)) % 2 == 0:
                spots.append(match.start(1) + k)
    if not spots:
        return text
    i = rng.choice(spots)
    return text[:i] + '"' + rng.choice([" ", "\n", " /*c*/ ", "\t", " \\\n "]) + '"' + text[i:]


def m_macro(rng: random.Random, text: str) -> str:
    body = text.rstrip("\n")
    if "\n" in body or re.match(r"\s*(/\*.*?\*/\s*)*(#|%:|\?\?=)", body, re.DOTALL) or "\\" in body.replace('\\"', "").replace("\\\\", ""):
        return text
    if rng.random() < 0.5:
        return "#define NOP(...) __VA_ARGS__\nNOP(" + body + ")\n"
    return "#define PAYLOAD " + body + "\nPAYLOAD\n"


MUTATIONS = {"splice": m_splice, "comment": m_comment, "ws": m_ws, "case": m_case,
             "crlf": m_crlf, "concat": m_concat, "macro": m_macro}


def _mutate_code(rng: random.Random, text: str) -> tuple[str, list[str]]:
    names = rng.sample(sorted(MUTATIONS), rng.randint(1, 3))
    for name in names:
        text = MUTATIONS[name](rng, text)
    return text, names


def _in_context(rng: random.Random, core: str) -> str:
    if not core.endswith(("\n", "\r")):
        core += "\n"
    before = "".join(rng.choice(_CONTEXT) for _ in range(rng.randint(0, 5)))
    after = "".join(rng.choice(_CONTEXT) for _ in range(rng.randint(0, 5)))
    return before + core + after


def test_context_corpus_itself_is_clean():
    assert _scan("".join(_CONTEXT)) == ()
    for text in HOSTILE:
        assert _scan(text), text


def test_hostile_corpus_is_detected_when_planted_and_mutated():
    rng = _rng("hostile_mutation")
    run = _Run()
    misses: list[tuple[str, list[str], str]] = []
    for text in HOSTILE:
        for _ in range(24):
            mutated, names = _mutate_code(rng, text)
            full = _in_context(rng, mutated)
            outcome = run(_scan_every_lexing, full)
            assert outcome[0] == "ok", f"scan raised {outcome[1]!r} on {full!r} ({TAG})"
            if not outcome[1]:
                misses.append((text, names, full))
    assert not misses, "hostile text evaded detection after claimed-covered mutations ({}):\n{}".format(
        TAG, "\n".join("  {!r} via {} -> {!r}".format(*item) for item in misses[:12]))
    run.check("hostile scan")


def test_benign_corpus_stays_clean_under_mutation_in_context():
    rng = _rng("benign_mutation")
    run = _Run()
    flagged = []
    for text in BENIGN:
        for _ in range(24):
            mutated, names = _mutate_code(rng, text)
            full = _in_context(rng, mutated)
            outcome = run(_scan_cxx_lexings, full)
            assert outcome[0] == "ok", (full, outcome, TAG)
            if outcome[1]:
                flagged.append((text, names, full, outcome[1]))
    assert not flagged, "benign code flagged after semantics-preserving mutations ({}):\n{}".format(
        TAG, "\n".join("  {!r} via {} -> {!r}: {}".format(*item) for item in flagged[:12]))
    run.check("benign scan")


_NORMALIZE_SEEDS = HOSTILE + BENIGN + [
    "a??/\nb", "x ??= y", "%:%:%:", "R\"x(a\\\nb)x\" //c\n", "u8R\"(\n#include \"/etc/passwd\"\n)\"", "'\"' /* \" */ \"\n",
    "1'000'000 '", "/* unterminated", "// c \\\n continued\n", "\\\n\\\n\\\n", "R\"(", "L'", "\"abc", "a \\ \n b", "#\\\n#\\\n",
]


def test_normalize_for_scan_is_idempotent_and_total():
    rng = _rng("normalize")
    run = _Run()
    alphabet = "\\\n \t\r/*'\"#%:?=<>()R_ua1.-x"
    bad = []
    residual = 0
    for _ in range(4000):
        text = rng.choice(_NORMALIZE_SEEDS)
        if rng.random() < 0.5:
            text, _names = _mutate_code(rng, text)
        if rng.random() < 0.5:
            chars = list(text)
            for _step in range(rng.randint(1, 6)):
                if chars:
                    chars[rng.randrange(len(chars))] = rng.choice(alphabet)
            text = "".join(chars)
        for raw in (True, False):
            outcome = run(normalize_for_scan, text, raw_strings=raw)
            assert outcome[0] == "ok", f"normalize_for_scan({text!r}) raised {outcome[1]!r} ({TAG})"
            once = outcome[1]
            if re.search(r"\\[ \t\f\v]*\n", once) or "??" in once:
                # normalize is a single pass: a splice that only appears once a comment between the
                # backslash and the newline is removed (or stacked splices) survives into the output.
                residual += 1
                continue
            twice = normalize_for_scan(once, raw_strings=raw)
            if twice != once:
                bad.append((text, raw, once, twice))
            canonical = text.replace("\r\n", "\n").replace("\r", "\n")
            assert normalize_for_scan(canonical.replace("\n", "\r\n"), raw_strings=raw) == once, (text, TAG)
    assert not bad, "normalize_for_scan is not idempotent ({}):\n{}".format(
        TAG, "\n".join("  {!r} raw={} -> {!r} -> {!r}".format(*item) for item in bad[:8]))
    assert residual < 4000, f"every input left a residual splice, so idempotence was never checked ({TAG})"
    for value in (None, "", 0):
        assert normalize_for_scan(value) == ""  # type: ignore[arg-type]
    run.check("normalize_for_scan")


def test_scan_and_normalize_stay_in_budget_on_ten_thousand_line_inputs():
    run = _Run()
    benign = "".join(rng_line for rng_line in (_CONTEXT[i % len(_CONTEXT)] for i in range(10_000)))
    assert benign.count("\n") >= 10_000
    outcome = run(_scan, benign)
    assert outcome == ("ok", ()), (outcome[0], TAG)
    hostile = benign + '#include "/etc/passwd"\n'
    outcome = run(_scan, hostile)
    assert outcome[0] == "ok" and outcome[1], TAG
    # Adversarial shapes at the largest size a candidate can reach (8 hunks x 8000 chars = 64000).
    units = {
        "open_has_include": '__has_include("\n', "cxx_attr_open": "[[\n",
        "quotes": '"\n', "adjacent_literals": '"a" ', "string_run_newlines": '"a"\n', "block_comments": "/*\n",
        "raw_open": 'R"x(\n', "splices": "\\\n", "digraphs": "%:%:%:\n", "directives": "# define A 1\n",
        "pp_numbers": "1'0'0'0 ", "open_declspec": "__declspec(\n", "attr_spaces": "__attribute__ ( (" + " " * 40 + "\n",
    }
    slow = []
    for name, unit in units.items():
        text = unit * min(10_000, 64_000 // len(unit))
        outcome = run(_scan, text)
        assert outcome[0] == "ok", f"{name} raised {outcome[1]!r} ({TAG})"
        if run.samples[-1] >= 2.0:
            slow.append(f"{name}: {run.samples[-1]:.2f}s on {len(text)} chars")
    assert not slow, f"shapes over the 2 s ceiling ({TAG}): {slow}"
    text = "#include" + " " * 60_000 + '"a.h"\n'
    outcome = run(_scan, text)
    assert outcome[0] == "ok" and run.samples[-1] < 2.0, (outcome, run.samples[-1], TAG)
    for _ in range(3):
        run(normalize_for_scan, benign)
    # p99 is meaningless for this handful of giant inputs; only the 2 s ceiling applies.
    assert max(run.samples) < 2.0, (max(run.samples), TAG)


@pytest.mark.parametrize("opener", ["__attribute__((\n", "asm(\n"])
def test_regression_unterminated_opener_scan_is_not_quadratic(opener):
    """10000 unterminated openers: each one re-scans up to 4096 chars in ``_balanced``/the asm window.

    ``_Pragma(\n`` x 10000 behaves the same (2-3 s). The asm form is only 50 kB, inside the 64 kB a
    candidate can carry (8 hunks x 8000 chars).
    """
    text = opener * 10_000
    started = time.perf_counter()
    _scan(text)
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, f"{elapsed:.2f}s for {len(text)} chars of unterminated attribute openers ({TAG})"


def test_patch_json_does_not_resolve_unc_paths_before_the_root_check(tmp_path, monkeypatch, request):
    """``//host/share/x.json`` reaches ``Path.resolve`` (an SMB connection) before any containment check."""
    root = _build_json_world(tmp_path / "world")
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    _enter_snapshot(request)
    real_resolve = Path.resolve
    seen: list[str] = []

    def spy(self, *args, **kwargs):
        text = str(self)
        if text.startswith("\\\\") and not text.startswith("\\\\?\\"):
            seen.append(text)
            raise OSError("network lookup blocked by the test")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", spy)
    for spelling in ("//fuzz-host.invalid/share/x.json", "\\\\fuzz-host.invalid\\share\\x.json"):
        with pytest.raises((ValueError, PermissionError, OSError)):
            jp.patch_json(spelling, [{"op": "add", "path": "/z", "value": 1}])
    assert not seen, f"UNC path resolved (network access) before rejection: {seen!r} ({TAG})"


@pytest.mark.parametrize("blank", ["\f", "\v"])
def test_regression_splice_with_form_feed_or_vertical_tab_is_joined(blank):
    """gcc and clang both accept a backslash, form feed/vertical tab, newline as a line splice."""
    text = "#inc\\" + blank + "\nlude \"/etc/passwd\"\n"
    assert _scan(text), f"splice with {blank!r} between backslash and newline hid #include ({TAG})"


def test_text_patch_headers_never_resolve_unc_paths(tmp_path, request):
    """A ``+++ b///host/share/x`` or ``+++ //host/share/x`` header must not make ``Path.resolve`` touch SMB."""
    _enter_snapshot(request)
    root = _build_world(tmp_path / "world")
    real_resolve = Path.resolve
    seen: list[str] = []

    def spy(self, *args, **kwargs):
        text = str(self)
        if text.startswith("\\\\") and not text.startswith("\\\\?\\"):
            seen.append(text)
            raise OSError("network lookup blocked by the test")
        return real_resolve(self, *args, **kwargs)

    Path.resolve = spy  # type: ignore[method-assign]
    request.addfinalizer(lambda: setattr(Path, "resolve", real_resolve))
    for spelling in ("b///fuzz-host.invalid/share/x", "//fuzz-host.invalid/share/x", "b//fuzz-host.invalid/share/x",
                     "///fuzz-host.invalid/share/x", "b/\\\\fuzz-host.invalid\\share\\x"):
        patch = f"--- /dev/null\n+++ {spelling}\n@@ -0,0 +1 @@\n+created\n"
        with pytest.raises((ValueError, PermissionError, FileNotFoundError, FileExistsError, tp.TextPatchError, OSError)):
            tp.text_patch(str(root), patch, apply=False, extra_roots=str(root))
    for network_root in ("//fuzz-host.invalid/share", "\\\\fuzz-host.invalid\\share"):
        with pytest.raises((ValueError, PermissionError)):
            tp.text_patch(network_root, _CREATE, apply=False)
    assert not seen, f"UNC path resolved (network access) before rejection: {seen!r} ({TAG})"


@pytest.mark.parametrize("value", [
    "//host/share/x", "\\\\host\\share\\x", "\\\\?\\UNC\\host\\share\\x", "//?/UNC/host/share/x",
    "\\\\.\\pipe\\x", "  //host/share/x", "//./pipe/x", "/??/UNC/h/s/x", "\\??\\UNC\\h\\s\\x",
    "/??/C:/x", "\\??\\C:\\x", "\\\\?\\GLOBALROOT\\Device\\x", "//?/Volume{1234}/x", "\\\\?\\C:", "\\\\?\\\\C:\\x",
])
def test_resolve_path_rejects_network_spellings_before_resolving(value, monkeypatch):
    real_resolve = Path.resolve
    seen: list[str] = []

    def spy(self, *args, **kwargs):
        seen.append(str(self))
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", spy)
    with pytest.raises(PermissionError):
        file_ops.resolve_path(value)
    assert not seen, (value, seen)


@pytest.mark.parametrize("name", [
    "CON", "con", "PRN", "AUX", "NUL", "nul.txt", "CON.", "CON ", "con .txt", "COM1", "com9.log", "COM0", "LPT1", "lpt9.x",
    "COM\u00b9", "COM\u00b2.txt", "LPT\u00b3", "CONIN$", "CONOUT$", "conout$.txt",
])
def test_text_patch_rejects_windows_device_names(name, tmp_path, request):
    _enter_snapshot(request)
    root = _build_world(tmp_path / "world")
    before = _snapshot(tmp_path / "world")
    for spelling in (f"b/{name}", f"b/sub/{name}", f"b/{name}/x.txt"):
        patch = f"--- /dev/null\n+++ {spelling}\n@@ -0,0 +1 @@\n+created\n"
        with pytest.raises((ValueError, PermissionError)):
            tp.text_patch(str(root), patch, apply=False, extra_roots=str(root))
    assert _snapshot(tmp_path / "world") == before


def test_patch_path_rejects_nul_anywhere():
    for value in ("b/a\x00.txt", "\x00", "b/\x00", "a.txt\x00"):
        with pytest.raises((ValueError, PermissionError)):
            tp._patch_path(value, prefix="b")


_PASSWD = '\\"/etc/passwd\\"'


@pytest.mark.parametrize("text", [
    'asm(".\\x69ncbin ' + _PASSWD + '");\n',
    'asm(".\\x69nclude ' + _PASSWD + '");\n',
    'asm(".\\151ncbin ' + _PASSWD + '");\n',
    'asm(".\\151nclude ' + _PASSWD + '");\n',
    'asm("\\056incbin ' + _PASSWD + '");\n',
    'asm("\\x2eincbin ' + _PASSWD + '");\n',
    'asm("\\u002eincbin ' + _PASSWD + '");\n',
    'asm("\\U0000002eincbin ' + _PASSWD + '");\n',
    'const char *s = ".\\x69ncbin ' + _PASSWD + '";\n',
    'const char *s = ".\\151nclude ' + _PASSWD + '";\n',
    'asm("nop\\n.\\x69nclude ' + _PASSWD + '");\n',
])
def test_hex_and_octal_escapes_cannot_hide_assembler_directives(text):
    assert _scan(text), text
    assert _scan(text, raw_strings=False), text


def test_escape_decoding_keeps_ordinary_strings_clean():
    for text in ('puts("a\\x41b\\101\\n\\t\\\\ \\"q\\" \\u00e9");\n', 'const char *p = "x.\\x66ile_name";\n',
                 'asm volatile("nop\\n\\tnop");\n'):
        assert _scan(text) == (), text


@pytest.mark.parametrize("text", [
    "<:<:gnu::constructor:>:> void f();\n",
    "<:<:gnu::section(\".x\"):>:> int v;\n",
    "??(??(gnu::constructor??)??) void f();\n",
    "??(??(gnu::destructor??)??) void f();\n",
    "<:[gnu::constructor]:> void f();\n",
    "[<:gnu::constructor:>] void f();\n",
    "??(<:gnu::constructor??):> void f();\n",
    "??=include \"/etc/passwd\"\n",
    "%:include \"/etc/passwd\"\n",
    "??=pragma comment(lib, \"x\")\n",
])
def test_digraph_and_trigraph_attribute_brackets_are_detected(text):
    assert _scan(text), text
    assert _scan(text, raw_strings=False), text


def test_normalize_translates_every_trigraph_and_digraph():
    assert normalize_for_scan("??= ??' ??( ??) ??! ??< ??> ??-") == "# ^ [ ] | { } ~"
    assert normalize_for_scan("a ??/\nb") == "a b"
    assert normalize_for_scan("<: :> <% %> %: %:%:") == "[ ] { } # ##"
    assert normalize_for_scan("a\\\f\nb") == "ab"
    assert normalize_for_scan("a\\\v\r\nb") == "ab"


def test_lt_colon_colon_is_not_a_digraph():
    # C++11 [lex.pptoken]: ``<::`` not followed by ``:`` or ``>`` is ``<`` ``::``.
    assert normalize_for_scan("std::vector<::T> v;") == "std::vector<::T> v;"
    assert normalize_for_scan("a<::b") == "a<::b"
    assert normalize_for_scan("a<:::b") == "a[::b"
    assert normalize_for_scan("a<::>") == "a[]"
    assert normalize_for_scan("x::>y") == "x::>y"
    assert _scan("std::vector<::T> v;\nstd::map<::A, ::B> m;\n") == ()


@pytest.mark.parametrize("spelling", ["/??/UNC/h/s/x.json", "\\??\\C:\\x.json", "\\??\\UNC\\h\\s\\x.json",
                                      "//?/UNC/h/s/x.json", "//./pipe/x.json"])
def test_patch_json_does_not_resolve_nt_namespace_paths(spelling, tmp_path, monkeypatch, request):
    root = _build_json_world(tmp_path / "world")
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    _enter_snapshot(request)
    real_resolve = Path.resolve
    seen: list[str] = []

    def spy(self, *args, **kwargs):
        seen.append(str(self))
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", spy)
    with pytest.raises(PermissionError):
        jp.patch_json(spelling, [{"op": "add", "path": "/z", "value": 1}])
    assert not seen, f"{spelling!r} reached Path.resolve: {seen!r} ({TAG})"


# --- round 2 review: trigraph modes, raw header-names, literal-only __has_include ----------------

_MATH_SOURCE = "float normalized_length(const Vec3& v) {\n    float l = lenght(v);\n    return l;\n}\n"


def _validate_replacement(replacement: str) -> tuple[dict[str, str], tuple[str, ...]]:
    patch = CandidatePatch(hunks=(PatchHunk("src/core/math.cpp", "lenght(v);", replacement),), model_id="fake")
    return validate_patch(patch, scope=SCOPE, current={"src/core/math.cpp": _MATH_SOURCE})


@pytest.mark.parametrize("replacement", [
    # trigraphs ignored (gcc/clang/MSVC default): ``'??'`` is a character, ``'/*'`` opens a comment
    # only in the trigraphs-applied reading, so the pragma below is live code.
    "length(v);\n    int a = '??' + '/*';\n#pragma comment(lib, \"evil.lib\")\n    int b; // */\n",
    # trigraphs applied: ``??/`` is a backslash, so ``"a??/" "/*"`` swallows the quote; the comment
    # opener is live only in the trigraphs-ignored reading.
    "length(v);\nconst char* s = \"a??/\" \"/*\";\n#pragma comment(lib,\"evil\")\n/* */\n",
])
def test_pragma_hidden_by_either_trigraph_reading_is_refused(replacement):
    texts, reasons = _validate_replacement(replacement)
    assert not texts, f"accepted under one trigraph reading ({TAG})"
    assert any("#pragma comment is refused" in reason for reason in reasons), (reasons, TAG)


@pytest.mark.parametrize("operand", ["<:/../etc/shadow.h>", "<%/../etc/shadow.h>", "</../etc/shadow.h>"])
@pytest.mark.parametrize("macro", ["__has_include", "__has_include_next", "__has_embed"])
def test_has_include_header_name_is_checked_as_written_and_digraph_normalised(macro, operand):
    text = f"#if {macro}({operand})\n#endif\n"
    assert _scan_every_lexing(text), text
    texts, reasons = _validate_replacement("length(v);\n" + text)
    assert not texts and any("__has_include/__has_embed" in reason for reason in reasons), (reasons, TAG)


@pytest.mark.parametrize("operand", ["<:/../etc/shadow.h>", "<%/../etc/shadow.h>"])
def test_digraph_lookalike_header_name_is_checked_as_written(operand):
    """A compiler never digraph-translates a header-name, so ``<:/../x.h>`` names a path that escapes upward."""
    text = f"#if __has_include({operand})\n#endif\n"
    assert any("escaping upward" in reason for reason in _scan(text, digraphs=False)), text
    assert any("escaping upward" in reason for reason in _scan_every_lexing(text)), text
    texts, reasons = _validate_replacement("length(v);\n" + text)
    assert not texts and any("escaping upward" in reason for reason in reasons), (reasons, TAG)


@pytest.mark.parametrize("text", [
    '#define P "C:/Windows/win.ini"\n#if __has_include(P)\n#endif\n',
    "#if __has_include(PATH)\n#endif\n",
    "#if __has_include_next( NAME )\n#endif\n",
    "#if __has_embed(FILE)\n#endif\n",
    "#if __has_include(\n#endif\n",
    "#if __has_include()\n#endif\n",
    "#if __has_include(<>)\n#endif\n",
    "#if __has_include((<vector>))\n#endif\n",
])
def test_has_include_with_a_computed_operand_is_refused(text):
    assert any("__has_include/__has_embed" in reason for reason in _scan_every_lexing(text)), text
    texts, reasons = _validate_replacement("length(v);\n" + text)
    assert not texts and any("__has_include/__has_embed" in reason for reason in reasons), (reasons, TAG)


@pytest.mark.parametrize("text", [
    "#if __has_include(<optional>)\n#endif\n",
    '#if __has_include("detail/impl.inl")\n#endif\n',
    "#if defined(__has_include) && __has_include( <vector> )\n#endif\n",
    "#ifndef __has_include\n#define __has_include(x) 0\n#endif\n",
    "# define __has_embed(x) 0\n",
])
def test_has_include_with_a_literal_header_name_stays_clean(text):
    assert _scan_every_lexing(text) == (), text


def test_gap_macro_indirection_is_not_detected():
    """KNOWN GAP, pinned on purpose: a lexical scanner cannot follow macro expansion or token pasting.

    ``#define A ".inc"`` + ``asm(A B)`` assembles ``.inc`` and ``B``, and ``#define CTOR constructor`` +
    ``__attribute__((CTOR))`` names a constructor, yet neither the definition nor the use carries a
    banned token. Closing this needs a preprocessor, not a scanner; the build sandbox and review are
    the control. If this test starts failing, the scanner grew a macro model: update the docs.
    """
    assert _scan_every_lexing('#define A ".inc"\n#define B "lude \\"/etc/passwd\\""\nasm(A B);\n') == ()
    assert _scan_every_lexing("#define CTOR constructor\nvoid f() __attribute__((CTOR));\n") == ()


@pytest.mark.parametrize("opener", ["[[", "__has_include(<", "__has_include(\"", "__has_embed(<"])
def test_scan_work_for_unterminated_bracket_and_header_openers_is_linear(opener):
    text = opener * (640_000 // len(opener))
    started = time.perf_counter()
    reasons = scan_hostile_directives(text, include_ok=OK)
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, f"{elapsed:.2f}s for {len(text)} chars of {opener!r} ({TAG})"
    if opener != "[[":
        assert reasons, "unterminated header-name operands must be refused"


@pytest.mark.parametrize("opener", ["[[", "__has_include(<", "__has_include_next(<", "__has_embed(<"])
def test_unterminated_bracket_and_header_openers_charge_the_work_budget(opener):
    """Each opener walks up to 1026 / 4096 characters; the walk is charged, so a few thousand exhaust the budget.

    A timing bound alone cannot show this (``str.find`` is fast); the refusal reason proves the charge.
    """
    reasons = scan_hostile_directives(opener * 2000, include_ok=OK)
    assert any("work budget exceeded" in reason for reason in reasons), (reasons, TAG)


# --- round 2 review: network paths are refused before any filesystem call --------------------------

_NETWORK_SPELLINGS = [
    "//evilhost/share/x.txt", "\\\\evilhost\\share\\x.txt", "\\\\?\\UNC\\evilhost\\share\\x.txt",
    "//?/UNC/evilhost/share/x.txt", "\\\\.\\pipe\\x", "//./pipe/x", "\\??\\UNC\\evilhost\\share\\x",
    "/??/UNC/evilhost/share/x", "\\\\?\\GLOBALROOT\\Device\\x", "//?/Volume{1234}/x", "\\\\?\\Volume{1234}\\x",
    "  //evilhost/share/x.txt",
]


def _is_network_text(value) -> bool:
    if not isinstance(value, (str, os.PathLike)):
        return False
    flat = os.fspath(value)
    if not isinstance(flat, str):
        return False
    flat = flat.replace("/", "\\").strip()
    if re.match(r"\\\\\?\\[A-Za-z]:\\", flat):
        return False
    return flat.startswith(("\\\\", "\\??\\"))


@pytest.fixture
def fs_spy(monkeypatch):
    """Record (and block) every stat-family call that receives a network path."""
    seen: list[str] = []

    def wrap(owner, name):
        real = getattr(owner, name)

        def spy(*args, **kwargs):
            target = args[0] if args else kwargs.get("path")
            if _is_network_text(target):
                seen.append(f"{owner.__name__}.{name}({os.fspath(target)!r})")
                raise OSError("network access blocked by the test")
            return real(*args, **kwargs)

        monkeypatch.setattr(owner, name, spy)

    for name in ("lstat", "stat", "is_symlink", "resolve", "exists", "is_file", "is_dir", "mkdir", "open"):
        wrap(Path, name)
    for name in ("lstat", "stat"):
        wrap(os, name)
    return seen


def _network_entry_points(tmp_path, request, monkeypatch):
    root = _build_json_world(tmp_path / "world")
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    _enter_snapshot(request)
    ops = [{"op": "add", "path": "/z", "value": 1}]
    return {
        "resolve_path": lambda p: file_ops.resolve_path(p),
        "resolve_mutation_path": lambda p: file_ops.resolve_mutation_path(p),
        "make_directory": lambda p: file_ops.make_directory(p),
        "write_file": lambda p: file_ops.write_file(p, "x", mode="create"),
        "edit_file": lambda p: file_ops.edit_file(p, "a", "b"),
        "delete_path": lambda p: file_ops.delete_path(p),
        "copy_file_source": lambda p: file_ops.copy_file(p, str(root / "copy.json")),
        "copy_file_destination": lambda p: file_ops.copy_file(str(root / "data.json"), p),
        "move_file_source": lambda p: file_ops.move_file(p, str(root / "moved.json")),
        "batch_write_files": lambda p: file_ops.batch_write_files(
            [{"path": p, "content": "x", "mode": "create"}]),
        "requested_path": lambda p: file_ops._requested_path(p),
        "batch_requested_path": lambda p: file_ops._batch_requested_path(p),
        "patch_json": lambda p: jp.patch_json(p, ops),
        "text_patch": lambda p: tp.text_patch(p, _CREATE, apply=False),
    }


@pytest.mark.parametrize("spelling", _NETWORK_SPELLINGS)
def test_no_file_operation_touches_a_network_path_before_refusing_it(spelling, tmp_path, request, monkeypatch, fs_spy):
    entries = _network_entry_points(tmp_path, request, monkeypatch)
    for name, call in entries.items():
        fs_spy.clear()
        try:
            call(spelling)  # the batch API reports a per-operation rejection instead of raising
        except (PermissionError, ValueError, OSError, tp.TextPatchError, file_ops.BatchWriteError):
            pass
        assert not fs_spy, f"{name}({spelling!r}) touched the network path first: {fs_spy!r} ({TAG})"


@pytest.mark.parametrize("value", ["\\\\?\\D:\\proj\\pyproject.toml", "//?/D:/proj/pyproject.toml",
                                   "\\\\?\\c:\\x", "//?/z:/a/b.txt", "  \\\\?\\D:\\proj\\x"])
def test_extended_length_local_paths_lose_their_prefix(value):
    stripped = file_ops._reject_network_path(value)
    assert re.fullmatch(r"[A-Za-z]:[\\/].*", stripped), stripped
    assert "?" not in stripped


@pytest.mark.parametrize("value", [
    "\\\\?\\UNC\\h\\s\\x", "//?/UNC/h/s/x", "\\\\?\\GLOBALROOT\\Device\\x", "//?/GLOBALROOT/Device/x",
    "\\\\?\\Volume{1234}\\x", "//?/Volume{1234}/x", "\\\\?\\D:", "\\\\?\\D\\x", "\\\\?\\\\D:\\x", "\\\\?\\1:\\x",
    "\\\\.\\D:\\x", "//./D:/x", "\\??\\D:\\x", "/??/D:/x", "//host/share/x", "\\\\host\\share\\x",
    "\\\\?/D:/x", "//?\\D:\\x",
])
def test_every_other_extended_or_device_spelling_is_still_refused(value):
    with pytest.raises(PermissionError):
        file_ops._reject_network_path(value)


def test_plain_paths_pass_through_unchanged():
    for value in ("a/b.txt", "C:\\x", "C:/x", " rel/x", "/abs/x", "./x"):
        assert file_ops._reject_network_path(value) == value


@pytest.mark.skipif(os.name != "nt", reason="extended-length paths are a Windows spelling")
def test_extended_length_path_inside_the_workspace_resolves(tmp_path, monkeypatch, request):
    root = _build_json_world(tmp_path / "world")
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    _enter_snapshot(request)
    target = root / "data.json"
    resolved = file_ops.resolve_path("\\\\?\\" + str(target))
    assert os.path.normcase(str(resolved)) == os.path.normcase(str(target.resolve()))
    mutated = file_ops.resolve_mutation_path("//?/" + str(target).replace("\\", "/"))
    assert os.path.normcase(str(mutated)) == os.path.normcase(str(target.resolve()))


@pytest.mark.parametrize("name", ["CON:", "con:", "NUL:x", "COM1:", "LPT1:stream", "aux.txt:s", "PRN :"])
def test_patch_path_refuses_device_names_with_a_colon_suffix(name):
    with pytest.raises(PermissionError):
        tp._patch_path("b/" + name, prefix="b/")


# --- round 3 review: anchored shim, every __has_include token, splice blanks, all digraph spellings ---

_COMPUTED = "computed operand"


def _refused_end_to_end(text: str) -> tuple[str, ...]:
    texts, reasons = _validate_replacement("length(v);\n" + text)
    assert not texts, f"validate_patch accepted {text!r} ({TAG})"
    return reasons


@pytest.mark.parametrize("text", [
    '#define P "//evil/share/x.h"\n#define F(x) x##define __has_include(P)\n#if F(a)\n#endif\n',
    "#define S #define __has_include(P)\n",
    "%:define S %:define __has_include(P)\n",
    '#define P "//evil/share/x.h"\n#if 0 #define __has_include(P)\n#endif\n',
    "int x; #define __has_include(P)\n",
    "#define __has_include(x) __has_include_next(x)\n",
    "#define S \\ \n#define __has_include(P)\n",
])
def test_regression_has_include_shim_exemption_is_anchored_to_the_line_start(text):
    """A ``#define`` somewhere before the token is not the directive that defines it."""
    assert any(_COMPUTED in reason for reason in _scan_every_lexing(text)), text
    assert any(_COMPUTED in reason for reason in _refused_end_to_end(text)), text


@pytest.mark.parametrize("text", [
    "#define __has_include(x) 0\n",
    "#  define  __has_include_next(x) 0\n",
    "%:define __has_include(x) 0\n",
    "#ifndef __has_include\n#define __has_include(x) 0\n#endif\n",
    "#ifdef __has_include\n#if __has_include(<optional>)\n#include <optional>\n#endif\n#endif\n",
    "#undef __has_include\n",
    "#if defined(__has_include) && __has_include(<version>)\n#endif\n",
    "#if defined __has_include && __has_include(<version>)\n#endif\n",
    '#if !defined( __has_include ) || !__has_include("detail/impl.inl")\n#endif\n',
    "#if (defined(__has_include) && (__has_include(<a.h>) || __has_include_next(<b.h>)))\n#endif\n",
    "#if defined(_WIN32) && !defined SPARK_NO_X && 201703L <= 1'000'000 && 0x1F && __has_include(<version>)\n#endif\n",
    '#if __cplusplus >= 201703L && __has_include("detail/impl.inl")\n#endif\n',
    '#if 0\n#elif __has_include(<sys/types.h>) && __has_include("math.h")\n#endif\n',
    "#if __has_include__(<version>)\n#endif\n",
    "#if __has_embed(<data.h>) && __has_include(<b.h>)\n#endif\n",
    'bool h = __has_include("math.h");\n',
    '#define HAVE_IMPL __has_include("detail/impl.inl")\n',
    "#if /* why */ __has_include( <vector> ) // trailing\n#endif\n",
    "#if __has_include(<vector>) \\\n    && __has_include(<optional>)\n#endif\n",
])
def test_has_include_shim_and_benign_uses_stay_clean(text):
    assert _scan_every_lexing(text) == (), text
    texts, reasons = _validate_replacement("length(v);\n" + text)
    assert texts and reasons == (), (text, reasons, TAG)


@pytest.mark.parametrize("text", [
    "#define H __has_include\n#define P 123\n#if H(P)\n#endif\n",
    "#define LP (\n#define P 123\n#if __has_include LP P)\n#endif\n",
    "#define H __has_include_next\n",
    "#define H __has_embed\n",
    "#if __has_include\n(<vector>)\n#endif\n",
    "#define X defined __has_include\n",
    "#if defined __has_include(P)\n#endif\n",
    "#if defined(__has_include(P))\n#endif\n",
    "#if notdefined(__has_include)\n#endif\n",
    "#ifdef X\n#elif __has_include(P)\n#endif\n",
    "int probe = __has_include;\n",
])
def test_regression_has_include_token_without_a_literal_operand_is_refused(text):
    """An alias (``#define H __has_include``) or a macro-made ``(`` probes a computed path."""
    assert any(_COMPUTED in reason for reason in _scan_every_lexing(text)), text
    assert any(_COMPUTED in reason for reason in _refused_end_to_end(text)), text


@pytest.mark.parametrize("macro", ["__has_include__", "__has_include_next__"])
def test_regression_old_gcc_has_include_spellings_are_checked_like_the_new_ones(macro):
    for text in (f"#define P 1\n#if {macro}(P)\n#endif\n", f"#if {macro}(</etc/passwd>)\n#endif\n",
                 f'#if {macro}("../../../../etc/shadow.h")\n#endif\n', f"#define H {macro}\n"):
        assert any("__has_include/__has_embed" in reason for reason in _scan_every_lexing(text)), text
        assert any("__has_include/__has_embed" in reason for reason in _refused_end_to_end(text)), text
    assert _scan_every_lexing(f"#if {macro}(<vector>)\n#endif\n") == ()
    assert _scan_every_lexing(f"#define {macro}(x) 0\n") == ()


@pytest.mark.parametrize("text", [
    "#define E ..\n#define CHK __has_include(<E/secret.h>)\n#if CHK\n#endif\n",
    "#define ID(x) x\n#if ID(__has_include(<E/secret.h>))\n#endif\n",
    "#if ID(')', __has_include(<E/secret.h>))\n#endif\n",
    "#if ID(__has_include(<a)b.h>) + __has_include(<E/secret.h>))\n#endif\n",
    "#if F(1)(__has_include(<E/secret.h>))\n#endif\n",
    "#if $(__has_include(<E/secret.h>))\n#endif\n",
    "int x = __has_include(<vector>);\n",
])
def test_angle_operand_that_the_compiler_macro_expands_is_refused(text):
    """``<...>`` is a header-name only when written in ``#if``/``#elif`` outside a macro call.

    In a macro body or a macro argument clang and MSVC macro-expand its tokens first (checked with
    ``clang -E`` and ``cl /E``), so ``#define E ..`` turns ``<E/secret.h>`` into ``<../secret.h>``.
    """
    assert any("macro-expanded" in reason for reason in _scan_every_lexing(text)), text
    assert any("macro-expanded" in reason for reason in _refused_end_to_end(text)), text


@pytest.mark.parametrize("blank", ["\f", "\v", " ", "\t", " \t\f\v"])
@pytest.mark.parametrize("lead, payload, needle", [
    ("// x ", '#pragma comment(lib, "evil")\n', "#pragma comment is refused"),
    ("// x ", '#include "../../../../etc/x.h"\n', "include escapes the project root"),
    ("/* */ // ", 'asm(".incbin \\"/etc/passwd\\"");\n', "assembler"),
])
def test_regression_backslash_blank_newline_is_not_a_splice_for_msvc(blank, lead, payload, needle):
    """``cl`` splices only a backslash directly followed by the newline (checked with ``cl /E``).

    gcc and clang splice across the blank, so for them the comment swallows the payload line; MSVC
    compiles it. The scan has to refuse what either reading refuses.
    """
    text = lead + "\\" + blank + "\n" + payload
    assert _scan(text) == (), "gcc/clang reading: the comment continues over the payload line"
    strict = scan_hostile_directives(normalize_for_scan(text, strict_splices=True), include_ok=OK)
    assert any(needle in reason for reason in strict), (text, strict, TAG)
    assert any(needle in reason for reason in _scan_every_lexing(text)), text
    assert any(needle in reason for reason in _refused_end_to_end(text)), text
    trigraph = lead + "??/" + blank + "\n" + payload
    assert any(needle in reason for reason in _scan_every_lexing(trigraph)), trigraph


def test_strict_splice_reading_runs_when_only_the_old_text_has_a_blank_splice():
    """The old text decides which lines are new: for MSVC the pragma below did not exist before."""
    before = '#pragma comment(lib,\\ \n"evil")\n'
    after = '#pragma comment(lib,"evil")\n'
    assert any("#pragma comment" in reason for reason in scan_added_lexings(before, after, include_ok=OK))


def test_normalize_splice_readings():
    assert normalize_for_scan("a\\ \nb", strict_splices=True) == "a\\ \nb"
    assert normalize_for_scan("a\\\f\nb", strict_splices=True) == "a\\\f\nb"
    assert normalize_for_scan("a\\\nb", strict_splices=True) == "ab"
    assert normalize_for_scan("a??/\nb ??/ \nc", strict_splices=True) == "ab \\ \nc"
    assert normalize_for_scan("a\\\t\nb ??( <: %: ??=", legacy=True) == "ab ??( <: # #"
    assert normalize_for_scan("a\\\f\nb", legacy=True) == "a\\\f\nb"


@pytest.mark.parametrize("text", [
    '#define S(x) #x\nasm(S(a:>.incbin "f"));\n',
    '#define S(x) #x\nasm(S(a:>.incbin "f"));',
])
def test_regression_closing_digraph_is_also_read_as_written(text):
    """``:>`` normalised to ``]`` makes ``.incbin`` look like a member access; as written it is refused."""
    assert _scan_every_lexing(text), text
    _refused_end_to_end(text)


@pytest.mark.parametrize("text, readings", [
    ("int x = 1;\n", 1),
    ("int y = c ? a : b;\n", 1),
    ("int y = c ?? 1 : 2;\n", 1),
    ("int y = c ??! 1;\n", 2),
    ("x<:0:> = 1;\n", 2), ("a:>b\n", 2), ("a%>b\n", 2), ("%:define A 1\n", 2), ("<%\n", 2),
    ("// c \\ \nint x;\n", 2),
    ("// c ??/\t\nint x;\n", 3),
    ("// c \\\f\nint x;\n", 2),
    ("a:>b // \\\v\n", 4),
    ('auto s = R"(x)";\n', 2),
    ('R"(a)" ??( %: <: \\\f\nx \\ \ny ??/\t\nz\n', 18),
    ("char c = 'a', d = L'b', e = '1';\n", 1),
    ("int x = 1'000;\n", 2),  # C++14 digit separator: the C17 reading (none) runs too
    ("int x = \u0661'a';\n", 2),  # the legacy reading took U+0661 for a digit: it runs too
])
def test_only_readings_that_differ_are_normalised(text, readings, monkeypatch):
    """One reading for plain text; a reading runs only when it lexes the text differently; 18 at most."""
    from sonder_runtime.domain.build import repair as repair_module

    calls: list[int] = []
    real = repair_module._mask_literals

    def counting(value, **kwargs):
        calls.append(1)
        return real(value, **kwargs)

    monkeypatch.setattr(repair_module, "_mask_literals", counting)
    scan_added_lexings("", text, include_ok=OK)
    assert len(calls) == 2 * readings, (text, len(calls))  # old and new text per reading
    calls.clear()
    scan_added_lexings(text, "int x;\n", include_ok=OK)  # the old text counts as well
    assert len(calls) == 2 * readings, (text, len(calls))


_LINEAR_SHAPES = {
    "if_literals_one_line": "#if " + "__has_include(<a.h>) && " * 26_000,
    "if_open_calls": "#if " + "F(" * 160_000 + "__has_include(<a.h>) " * 15_000,
    "if_defined": "#if " + "defined(__has_include) && " * 24_000,
    "if_defined_blanks": "#if " + ("defined" + " " * 50 + "__has_include" + " " * 50) * 5_300,
    "leading_blanks": " " * 320_000 + "__has_include " * 22_000,
    "blank_lines": ("\t" * 40 + "__has_include\n") * 11_800,
    "shim_lines": "#define __has_include(x) 0\n" * 24_000,
    "bare_tokens": "__has_include " * 45_000,
    "open_angle_lines": "#if __has_include(<\n" * 33_000,
    "if_unterminated_strings": "#if " + '" __has_include(<a.h>) ' * 27_000,
    "blank_splices": "x \\ \n" * 128_000,
    "blank_runs": ("\\" + " " * 63) * 10_000,
    "trigraph_blank_runs": ("??/" + "\t" * 61) * 10_000,
    "digraph_pairs": "%:<::>%><%" * 64_000,
}


@pytest.mark.parametrize("shape", sorted(_LINEAR_SHAPES))
def test_has_include_and_splice_readings_stay_linear_at_640_kb(shape):
    text = _LINEAR_SHAPES[shape]
    assert 600_000 <= len(text) <= 700_000, len(text)
    for label, call in (
        ("scan", lambda: scan_hostile_directives(text, include_ok=OK)),
        ("strict", lambda: normalize_for_scan(text, strict_splices=True)),
        ("legacy", lambda: normalize_for_scan(text, legacy=True)),
    ):
        started = time.perf_counter()
        call()
        elapsed = time.perf_counter() - started
        assert elapsed < 2.0, f"{label} took {elapsed:.2f}s on {len(text)} chars of {shape} ({TAG})"


def test_all_eighteen_lexings_of_a_full_size_candidate_stay_in_budget(monkeypatch):
    from sonder_runtime.domain.build import repair as repair_module

    scans: list[int] = []
    real = repair_module.scan_hostile_directives

    def counting(added, **kwargs):
        scans.append(1)
        return real(added, **kwargs)

    monkeypatch.setattr(repair_module, "scan_hostile_directives", counting)
    unit = 'R"(a)" ??( %:if __has_include(<:a.h>) && F(\\\f\nx \\ \ny ??/\t\nz\n'
    text = unit * (64_000 // len(unit))
    started = time.perf_counter()
    reasons = scan_added_lexings("", text, include_ok=OK)
    elapsed = time.perf_counter() - started
    assert len(scans) == 18, len(scans)
    assert reasons and not any("lexing work budget" in reason for reason in reasons), reasons
    assert elapsed < 2.0, f"{elapsed:.2f}s for {len(text)} chars in 18 lexings ({TAG})"


# --- round 4 review: identifiers before an angle operand, literals after a named token, lexing work ---

@pytest.mark.parametrize("text", [
    "#define F(x) x\n#define LP F(\n#define E /abs/dir\n#if LP __has_include(<E/x.h>))\n#endif\n",
    "#if LP __has_include(<E/x.h>))\n#endif\n",
    "#if X && __has_include(<E/x.h>)\n#endif\n",
    "#if 1 && defined(A) X __has_include(<E/x.h>)\n#endif\n",
    '#if __has_include("a.h") LP __has_include(<E/x.h>))\n#endif\n',
    "#if __has_include(<a.h>) LP __has_include(<E/x.h>))\n#endif\n",
    "#if defined(__has_include) LP __has_include(<E/x.h>))\n#endif\n",
    "#if defined A B __has_include(<E/x.h>)\n#endif\n",
    "#if 0x1 LP __has_include(<E/x.h>))\n#endif\n",
    "#if 'a' __has_include(<E/x.h>)\n#endif\n",
    "#if $ __has_include(<E/x.h>)\n#endif\n",
    "#if \u00e9 __has_include(<E/x.h>)\n#endif\n",
    # Unicode decimal digits start identifiers for gcc/clang (C17, C23) and MSVC.
    "#if \u0663LP __has_include(<E/x.h>))\n#endif\n",
    "#if \uff11LP __has_include(<E/x.h>))\n#endif\n",
    "#if .\u0663 __has_include(<E/x.h>))\n#endif\n",
    "#if defined X \uff11LP __has_include(<E/x.h>))\n#endif\n",
    "#if 0\n#elif LP __has_include(<E/x.h>))\n#endif\n",
    # The price of the rule: these are fine for a compiler, but an identifier before the operand
    # cannot be told from a macro that opens a call. A nested #if (or a quoted operand) is accepted.
    "#if __cplusplus >= 201703L && __has_include(<optional>)\n#endif\n",
    "#if __has_cpp_attribute(nodiscard) && __has_include(<version>)\n#endif\n",
    "#if SPARK_AT_LEAST(3) && __has_include(<version>)\n#endif\n",
    "#if __has_embed(<data.h> limit(1)) && __has_include(<b.h>)\n#endif\n",
])
def test_regression_identifier_before_an_angle_operand_may_open_a_macro_call(text):
    """``#define LP F(`` makes ``#if LP __has_include(<E/x.h>))`` a macro argument, which is expanded.

    Checked with ``clang -E`` and ``cl /E /Zc:preprocessor``: ``E`` is replaced before the probe.
    """
    assert any("macro-expanded" in reason for reason in _scan_every_lexing(text)), text
    assert any("macro-expanded" in reason for reason in _refused_end_to_end(text)), text


_EVIL = '"//evil/share/x.h"'


@pytest.mark.parametrize("text", [
    f"#define __has_include({_EVIL})\n",
    f"#define __has_include ({_EVIL})\n",
    "#define __has_include(<../../x.h>)\n",
    f"#ifdef __has_include({_EVIL})\n#endif\n",
    f"#undef __has_include ({_EVIL})\n",
    f"#define __has_include_next ({_EVIL})\n",
    f"#define __has_include\n({_EVIL})\n",
    f"#if defined __has_include\n({_EVIL})\n#endif\n",
    f"#if defined(__has_include) && __has_include\n({_EVIL})\n#endif\n",
    f"bool h = __has_include\n  ({_EVIL});\n",
])
def test_regression_literal_after_a_named_has_include_token_is_still_path_checked(text):
    """Naming the macro (``#define``, ``#ifdef``, ``defined``) exempts the token, not a path after it."""
    assert any("__has_include/__has_embed" in reason for reason in _scan_every_lexing(text)), text
    assert any("__has_include/__has_embed" in reason for reason in _refused_end_to_end(text)), text


def test_gap_token_pasting_builds_tokens_the_scan_never_sees():
    """KNOWN GAP, pinned on purpose: ``##`` builds ``__has_include`` and ``_Pragma`` from harmless halves.

    ``CAT(__has_,include)(P)`` probes the path in ``P`` and ``CAT(_Pra,gma)("comment(lib, ...)")`` is a
    pragma, yet no banned token is spelled anywhere. A lexical scanner cannot follow pasting; the build
    sandbox and review are the control. If this test starts failing, the scanner learned pasting:
    update the docs.
    """
    assert _scan_every_lexing(
        f"#define CAT(a,b) a##b\n#define P {_EVIL}\n#if CAT(__has_,include)(P)\n#endif\n") == ()
    assert _scan_every_lexing('#define CAT(a,b) a##b\nCAT(_Pra,gma)("comment(lib, \\"evil\\")")\n') == ()


def test_gap_alias_defined_in_unchanged_code_is_not_followed():
    """KNOWN GAP, pinned on purpose: only added lines are scanned, so an existing alias macro is trusted.

    With ``#define FMT_HAS_INCLUDE(x) __has_include(x)`` already in the file, a patch that adds
    ``#if FMT_HAS_INCLUDE("//evil/share/x.h")`` probes that path without spelling ``__has_include``.
    Adding the alias itself is refused (the first assertion).
    """
    alias = "#  define FMT_HAS_INCLUDE(x) __has_include(x)\n"
    use = f"#if FMT_HAS_INCLUDE({_EVIL})\n#endif\n"
    assert scan_added_lexings("", alias + use, include_ok=OK)
    assert scan_added_lexings(alias, alias + use, include_ok=OK) == ()


def _fill(unit: str, head: str = "", tail: str = "", size: int = 640 * 1024) -> str:
    return head + unit * ((size - len(head) - len(tail)) // len(unit)) + tail


_MIX_UNIT = "%:if __has_include\\ \n(<a.h>) ??/\f\n"
_ALL_LEXING_SHAPES = {
    "if_many_ok": _fill("__has_include(<a.h>) && ", "#if ", "1\n"),
    "unterminated_angle": _fill("__has_include(<"),
    "if_unterminated_angle": _fill("__has_include(<", "#if "),
    "defined_blanks": _fill(" ", "#if ", "defined __has_include\n"),
    "defined_many": _fill("defined   __has_include || ", "#if ", "1\n"),
    "parens_then": _fill("(", "#if ", "__has_include(<a.h>)\n"),
    "call_parens": _fill("F(", "#if ", "__has_include(<a.h>)\n"),
    "identifiers_then": _fill("A ", "#if ", "__has_include(<a.h>)\n"),
    "string_then": _fill("__has_include(<a.h>)", '#if "'),
    "define_lines": _fill("#define __has_include(x) 0\n"),
    "define_literal_lines": _fill('#define __has_include("a.h")\n'),
    "token_lines": _fill("__has_include\n"),
    "token_blank": _fill("\f", "#if __has_include", "(<a.h>)\n"),
    "token_newlines": _fill("\n", "#define __has_include", '("a.h")\n'),
    "token_no_paren": _fill("__has_include "),
    "blank_splice": _fill("\\ \n"),
    "form_feed_splice": _fill("\\\f\n"),
    "trigraph_splice": _fill("??/ \n"),
    "digraph": _fill("%:<:"),
    "mix": _fill(_MIX_UNIT),
    "mix_raw": _fill('R"(a)" ??( ' + _MIX_UNIT),
    "pound_lines_head": _fill("#    \t   define\t\t\t__has_include\n"),
    "blank_head": _fill(" ", "", "#define __has_include(x) 0\n"),
    "ifs_quotes": _fill('#if __has_include("a.h") && "x" && __has_include(<b.h>)\n'),
    "elif_many": _fill("#elif __has_include(<a.h>)\n"),
    "defined_paren_blanks": _fill(" ", "#if defined(", "__has_include)\n"),
    "unterminated_quote": _fill('__has_include("'),
    "plain": _fill("int add(int a, int b) { return a + b; } // scale\n"),
}


@pytest.mark.parametrize("shape", sorted(_ALL_LEXING_SHAPES))
def test_regression_all_lexings_of_a_640_kb_file_stay_under_two_seconds(shape):
    """The whole of ``scan_added_lexings`` (every reading), as a new file and as a half-changed file."""
    text = _ALL_LEXING_SHAPES[shape]
    assert 600_000 <= len(text) <= 660_000, len(text)
    for label, before in (("new file", ""), ("half changed", text[: len(text) // 2])):
        started = time.perf_counter()
        scan_added_lexings(before, text, include_ok=OK)
        elapsed = time.perf_counter() - started
        assert elapsed < 2.0, f"{shape} ({label}): {elapsed:.2f}s for every lexing of {len(text)} chars ({TAG})"


def test_lexing_work_budget_refuses_a_large_file_that_needs_many_readings():
    """Nine distinct readings of 640 kB is more work than one patch may cost: refused, never accepted."""
    assert any("lexing work budget" in reason
               for reason in scan_added_lexings("", _ALL_LEXING_SHAPES["mix"], include_ok=OK))
    small = _fill(_MIX_UNIT, size=200 * 1024)
    assert not any("lexing work budget" in reason for reason in scan_added_lexings("", small, include_ok=OK))
    big_plain = _fill("int add(int a, int b) { return a + b; } // scale\n", size=2 * 1024 * 1024)
    assert scan_added_lexings(big_plain[: len(big_plain) // 2], big_plain, include_ok=OK) == ()
    two_readings = 'auto s = R"(x)";\n' + big_plain  # raw strings on and off: always allowed
    assert scan_added_lexings("", two_readings, include_ok=OK) == ()


def _union_of_every_lexing(before: str, after: str) -> set[str]:
    """Every reading times every quote reading, plus the refusals no reading covers."""
    from sonder_runtime.domain.build import repair as repair_module

    found: set[str] = set()
    modes: list[dict[str, bool]] = [{"trigraphs": t, "digraphs": d, "strict_splices": s}
                                    for t in (True, False) for d in (True, False) for s in (True, False)]
    modes.append({"legacy": True})
    for raw in (True, False):
        for mode in modes:
            for separators in ("cxx14", "none", "legacy"):
                found.update(scan_hostile_directives(
                    added_logical_lines(before, after, raw_strings=raw, separators=separators, **mode),
                    include_ok=OK))
            # The ambiguous quote counts in the C++14 quote reading; a bad raw prefix in C++14 and C++11.
            for separators, kinds in (("cxx14", ("ambiguous", "bad_raw")), ("none", ("bad_raw",))):
                events: set[str] = set()
                for text in (before, after):
                    repair_module._mask_literals(
                        repair_module._translation_phases(text, **mode), raw_strings=raw,
                        digraphs=mode.get("digraphs", False), separators=separators, events=events)
                if "ambiguous" in kinds and "ambiguous" in events:
                    found.add(repair_module._AMBIGUOUS_SEPARATOR)
                if "bad_raw" in events:
                    found.add(repair_module._BAD_RAW_DELIMITER)
    return found


def test_skipping_equal_readings_never_changes_the_reasons():
    """The gated, de-duplicated scan must report exactly what scanning all 18 readings reports."""
    rng = _rng("lexing_dedupe")
    noise = ["\\\n", "\\ \n", "\\\f\n", "??/\n", "??/ \n", "??(", "%:", "<:", ":>", 'R"(', ')"', "/*", "*/", "'", '"',
             "1'", "1'a", "\u0661'", 'R" (']
    checked = refused = 0
    for seed in HOSTILE + BENIGN:
        for _ in range(12):
            mutated, _names = _mutate_code(rng, seed)
            after = _in_context(rng, mutated)
            for _step in range(rng.randint(0, 3)):
                k = rng.randrange(len(after) + 1)
                after = after[:k] + rng.choice(noise) + after[k:]
            before = rng.choice(["", _in_context(rng, ""), after[: rng.randrange(len(after) + 1)]])
            got = set(scan_added_lexings(before, after, include_ok=OK))
            assert got == _union_of_every_lexing(before, after), (before, after, TAG)
            checked += 1
            refused += bool(got)
    assert checked >= 700
    # Both outcomes must occur, or the equality above says nothing about refusals.
    assert 0.1 * checked <= refused <= 0.9 * checked, (refused, checked)


# --- round 5 review: Unicode digits and quotes after numbers, defined, line heads, U+180E/U+FEFF, raw prefixes ---
# Every repro below was compiled with gcc 16.1, clang 22 and MSVC 19.44 (cl /E): the compiler opens or
# probes ../../../../secret.h, which the scan used to accept.

_SECRET = '#include "../../../../secret.h"\n'
_ESCAPES_ROOT = "include escapes the project root"
_AMBIGUOUS_QUOTE = "compilers lex differently"
_BAD_RAW = "raw string whose delimiter"
_ODD_LINE = "non-ASCII or control character"
_ODD_BLANK = "U+FEFF (byte order mark) or U+180E"


@pytest.mark.parametrize("lead", ["\u0661", "\uff11", "\u00b2", ".\u0661", "x\u0663"])
def test_regression_unicode_digit_does_not_start_a_number_that_swallows_a_quote(lead):
    """A Unicode digit is an identifier character to the compilers, so ``'/*'`` is a character literal.

    Read as a pp-number (``str.isdigit``), the quote became a digit separator, ``/*`` opened a comment
    and the include line below was masked away, while gcc, clang and MSVC include the file.
    """
    text = "int a = " + lead + "'/*';\n" + _SECRET + "int b = '*/';\n"
    assert any(_ESCAPES_ROOT in reason for reason in _scan(text)), text
    assert any(_ESCAPES_ROOT in reason for reason in _scan_every_lexing(text)), text
    assert any(_ESCAPES_ROOT in reason for reason in _refused_end_to_end(text)), text


def test_regression_quote_after_a_number_is_a_separator_only_before_an_identifier_character():
    """``1'/*'``: C++14 takes ``'`` as a separator only before a digit, letter or ``_`` (gcc, clang).

    So the quote opens ``'/*'`` and the include is live in every gcc/clang mode; MSVC takes every
    quote after a number as a separator, so the readings disagree and the input is refused as well.
    """
    text = "int a = 1'/*';\n" + _SECRET + "int b = '*/';\n"
    assert any(_ESCAPES_ROOT in reason for reason in _scan(text)), text
    assert any(_ESCAPES_ROOT in reason for reason in _scan(text, separators="none")), text
    reasons = _refused_end_to_end(text)
    assert any(_ESCAPES_ROOT in reason for reason in reasons), reasons
    assert any(_AMBIGUOUS_QUOTE in reason for reason in reasons), reasons


@pytest.mark.parametrize("number", ["1", "0x1F", "1e+", ".5"])
def test_regression_c17_reading_without_digit_separators_is_scanned(number):
    """C17 and C++11 have no digit separators: ``1'a /* '`` is ``1`` and the literal ``'a /* '``.

    gcc and clang (``-std=c17``, ``-std=c++11``) include the file; C++14, C23 and MSVC read ``1'a``
    as one number and hide the include in the comment. Both readings are scanned.
    """
    text = "int a = " + number + "'a /* ';\n" + _SECRET + "int b = 0; */\n"
    assert any(_ESCAPES_ROOT in reason for reason in _refused_end_to_end(text)), text
    assert any(_ESCAPES_ROOT in reason for reason in _scan_every_lexing(text)), text
    assert _scan(text) == (), "C++14 reading: the include is inside the comment"
    assert any(_ESCAPES_ROOT in reason for reason in _scan(text, separators="none")), text


@pytest.mark.parametrize("text", [
    "int a = 1''a'b'/*';\n" + _SECRET + "int b = '*/';\n",  # gcc: 1''a'b is one number; clang: 1 '' a 'b'
    "int a = 1'.'/*';\n" + _SECRET + "int b = '*/';\n",
    "int a = 1'$'/*';\n" + _SECRET + "int b = '*/';\n",
    "int a = 1'\u00e9'/*';\n" + _SECRET + "int b = '*/';\n",
    "int a = 1\u00e9'0'/*';\n" + _SECRET + "int b = '*/';\n",
    "int a = \u00a01'0'/*';\n" + _SECRET + "int b = '*/';\n",
    "int a = 1'0;\nint c = 2' /* ';\n" + _SECRET + "int b = 0; */\n",
])
def test_regression_quote_after_a_number_that_compilers_disagree_on_is_refused(text):
    """MSVC takes every quote after a number as a separator, gcc continues over ``''`` and ``$``, clang
    over UTF-8 identifier characters: no single reading covers them, so the input is refused."""
    assert any(_AMBIGUOUS_QUOTE in reason for reason in _scan_every_lexing(text)), text
    assert any(_AMBIGUOUS_QUOTE in reason for reason in _refused_end_to_end(text)), text


@pytest.mark.parametrize("text", [
    "int a = 1'000'000;\nlong b = 0x1F'FF'u;\ndouble c = 1.5e+3'0;\n",
    "char c = '1', d = L'x', e = u8'y', f = U'z';\nint g = x1'2';\n",
    "unsigned long long m = 0b1010'0101ull;\n",
])
def test_digit_separators_and_character_literals_stay_clean(text):
    assert _scan_every_lexing(text) == (), text
    texts, reasons = _validate_replacement("length(v);\n" + text)
    assert texts and reasons == (), (text, reasons, TAG)


@pytest.mark.parametrize("joiner", ["\u0300", "\u00b7", "\u200d", "\u200c", "\u203f", "\u00e9"])
def test_regression_identifier_ending_in_defined_is_not_the_defined_operator(joiner):
    """``X\u0300defined`` is one identifier (gcc, clang: combining marks, U+00B7, ZWJ, ZWNJ, U+203F
    continue identifiers), so the ``__has_include`` after it is evaluated: here with a macro ``(``,
    which probes the path in ``P``."""
    name = "X" + joiner + "defined"
    text = ("#define " + name + "\n#define LP2 (\n#define P \"../../../../secret.h\"\n"
            "#if " + name + " __has_include LP2 P)\n#endif\n")
    assert any(_COMPUTED in reason for reason in _scan(text)), text
    assert any(_COMPUTED in reason for reason in _scan_every_lexing(text)), text
    assert any(_COMPUTED in reason for reason in _refused_end_to_end(text)), text


@pytest.mark.parametrize("lead", ["\u00a0", "\u2028", "\x1c", "\x1d", "\x1e", "\x1f", "\x85"])
def test_regression_shim_exemption_needs_a_line_head_every_compiler_reads_as_a_directive(lead):
    """Python's ``\\s`` takes these, gcc lexes them as stray tokens: the line is not a directive to
    gcc, so it expands ``__has_include(P)`` as ordinary text and probes the path in ``P``."""
    text = '#define P "../../../../secret.h"\n' + lead + "#define __has_include(P) RESULT\n"
    assert any(_COMPUTED in reason for reason in _scan(text)), text
    assert any(_COMPUTED in reason for reason in _scan_every_lexing(text)), text
    reasons = _refused_end_to_end(text)
    assert any(_COMPUTED in reason for reason in reasons), reasons
    assert any(_ODD_LINE in reason for reason in reasons), reasons
    blank = '#define P "../../../../secret.h"\n#define\u00a0__has_include(P) RESULT\n'
    assert any(_COMPUTED in reason for reason in _scan_every_lexing(blank)), blank
    assert _scan_every_lexing(" \t#define __has_include(x) 0\n# \tif __has_include(<a.h>)\n#endif\n") == ()


@pytest.mark.parametrize("lead", ["\u180e", "\ufeff", "\u200b", "\u3000", "\u00a0", "\x1c", "#\u180e", "#\u00a0"])
def test_regression_invisible_character_before_a_directive_is_refused(lead):
    """clang treats U+180E as a blank anywhere and gcc and clang skip a U+FEFF at the start of a file,
    so ``\u180e#include`` is a directive to them; ``\\s`` does not match either. Any non-ASCII or
    control character before, after or inside the ``#`` of a line fails closed."""
    text = lead + _SECRET[1:] if lead.startswith("#") else lead + _SECRET
    reasons = _scan(text)
    assert any(_ODD_LINE in reason or _ODD_BLANK in reason for reason in reasons), (text, reasons)
    reasons = _refused_end_to_end(text)
    assert any(_ODD_LINE in reason or _ODD_BLANK in reason for reason in reasons), (text, reasons)


def test_regression_byte_order_mark_added_at_the_start_of_the_file_is_refused():
    """gcc and clang skip a leading U+FEFF, so the include on the patched first line is live."""
    patch = CandidatePatch(hunks=(PatchHunk("src/core/math.cpp", "float normalized_length",
                                            "\ufeff" + _SECRET + "float normalized_length"),), model_id="fake")
    texts, reasons = validate_patch(patch, scope=SCOPE, current={"src/core/math.cpp": _MATH_SOURCE})
    assert not texts and any(_ODD_BLANK in reason for reason in reasons), reasons
    assert any(_ODD_BLANK in reason for reason in _scan("\ufeff" + _SECRET)), "per-reading scan"
    # Refused anywhere in added text, mid-line too: one rule instead of a per-construct guess
    # (clang 22 rejects ``_Pragma`` + U+180E + ``(``, but takes U+180E before ``#include`` as a blank).
    for blank in ("\u180e", "\ufeff"):
        text = 'int x; _Pragma' + blank + '("comment(lib, \\"evil\\")")\n'
        assert any(_ODD_BLANK in reason for reason in _scan_every_lexing(text)), text
        assert any(_ODD_BLANK in reason for reason in _refused_end_to_end(text)), text


@pytest.mark.parametrize("delimiter", [" ", "\t", "$", "@", "`", '"', "\u00a0", "\u00e9", "a" * 17])
def test_regression_raw_string_with_a_delimiter_some_compiler_rejects_is_refused(delimiter):
    """gcc and clang lex an invalid raw-string prefix up to the next ``"`` (across lines), MSVC keeps a
    raw string: here gcc and clang both include the file, while every reading of the scan hid it."""
    text = 'auto s = R"' + delimiter + '(\n/* "\n' + _SECRET + "*/\n"
    assert "xxx" not in normalize_for_scan(text), "an invalid delimiter must not mask a raw-string body"
    assert any(_BAD_RAW in reason for reason in _scan_every_lexing(text)), text
    assert any(_BAD_RAW in reason for reason in _refused_end_to_end(text)), text


@pytest.mark.parametrize("text", [
    'auto a = R"(x)";\nauto b = R"--(a)" b)--";\nauto c = u8R"\'(z)\'";\nauto d = LR"x()x";\n',
    'auto e = R"abcdefghijklmnop(16)abcdefghijklmnop";\nint fR = 1; auto g = fR;\n',
])
def test_raw_strings_with_valid_delimiters_stay_clean(text):
    assert _scan_every_lexing(text) == (), text
    texts, reasons = _validate_replacement("length(v);\n" + text)
    assert texts and reasons == (), (text, reasons, TAG)
