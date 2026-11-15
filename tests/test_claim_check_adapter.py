"""The claim checker over a synthetic repository on disk (no real project rows)."""
import json
import os
import subprocess
import sys
from dataclasses import replace

import pytest

from sonder_runtime.adapters.claim_checker import (
    ClaimChecker, check_claim, check_jsonl, claim_from_mapping,
)
from sonder_runtime.domain.claim_check import CheckStatus, Claim, ClaimError, Verdict
from sonder_runtime.platform.claim_check_config import ClaimCheckConfig

PUMP = """\
using System;
class Pump
{
    const int MaxItems = 64;
    public void Fill(byte[] data, int count)
    {
        var buffer = new byte[count];
        Array.Copy(data, buffer, count);
    }
    public void Safe(byte[] data, int count)
    {
        if (count > MaxItems) throw new ArgumentOutOfRangeException(nameof(count));
        var n = Math.Min(count, data.Length);
    }
}
"""

CALLER = """\
class Caller
{
    void Run(Pump pump, byte[] data, int n)
    {
        if (n > MaxItems) return;
        pump.Fill(data, n);
    }
}
"""

UNGUARDED_CALLER = """\
class Plain
{
    void Run(Pump pump, byte[] data, int n)
    {
        pump.Fill(data, n);
    }
}
"""

DOS = "crash/DoS from client input"


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "Pump.cs").write_text(PUMP, encoding="utf-8", newline="\n")
    (root / "src" / "Caller.cs").write_text(CALLER, encoding="utf-8", newline="\n")
    (root / "bin").mkdir()
    (root / "bin" / "Stale.cs").write_text("pump.Fill(x, 1000000);\nif (x > Max) return;\n")
    (tmp_path / "outside.txt").write_text("secret\nsecret\n")
    return root


def names(report):
    return {c.name: c.status for c in report.checks}


def test_correct_citation_without_guard_is_kept(repo):
    claim = Claim(file="src/Pump.cs", line_start=8, symbols=("Array.Copy",), category=DOS)
    cfg = replace(ClaimCheckConfig(), guard_window_lines=0, max_callers=0)
    report = check_claim(repo, claim, cfg)
    assert names(report)["file_in_root"] is CheckStatus.PASS
    assert names(report)["line_in_range"] is CheckStatus.PASS
    assert names(report)["symbols_near"] is CheckStatus.PASS
    assert report.verdict is Verdict.KEEP and report.guards == ()


def test_guard_near_the_citation_flags_with_file_and_line(repo):
    report = check_claim(repo, Claim(file="src/Pump.cs", line_start=7, category=DOS))
    assert report.verdict is Verdict.FLAG
    near = [g for g in report.guards if g.where == "near"]
    assert {g.line for g in near} >= {12, 13}
    assert all(g.path == "src/Pump.cs" for g in near)


def test_guard_in_direct_caller_found_by_word_search(repo):
    cfg = replace(ClaimCheckConfig(), guard_window_lines=0)
    claim = Claim(file="src/Pump.cs", line_start=7, symbols=("Fill",), category=DOS)
    report = check_claim(repo, claim, cfg)
    callers = [g for g in report.guards if g.where == "caller"]
    assert [(g.path, g.line) for g in callers] == [("src/Caller.cs", 5)]
    assert report.verdict is Verdict.FLAG


def test_caller_without_guard_stays_kept_and_skip_dirs_are_not_scanned(repo):
    (repo / "src" / "Caller.cs").write_text(UNGUARDED_CALLER, encoding="utf-8", newline="\n")
    cfg = replace(ClaimCheckConfig(), guard_window_lines=0)
    claim = Claim(file="src/Pump.cs", line_start=7, symbols=("Fill",), category=DOS)
    report = check_claim(repo, claim, cfg)
    assert report.guards == ()  # bin/Stale.cs holds a guard but is in skip_dirs
    assert report.verdict is Verdict.KEEP


def test_entry_point_guard_is_reported_as_entry(repo):
    cfg = replace(ClaimCheckConfig(), guard_window_lines=0)
    (repo / "src" / "Entry.cs").write_text(
        "void Main() {\n if (n > MaxItems) return;\n Run(1);\n}\n", newline="\n")
    claim = Claim(file="src/Pump.cs", line_start=7, entry_point="Run", category=DOS)
    report = check_claim(repo, claim, cfg)
    assert [g.where for g in report.guards] == ["entry"]


def test_line_past_end_rejects(repo):
    report = check_claim(repo, Claim(file="src/Pump.cs", line_start=500))
    assert report.verdict is Verdict.REJECT
    assert names(report)["line_in_range"] is CheckStatus.FAIL


def test_wrong_symbol_rejects_even_when_a_guard_is_present(repo):
    claim = Claim(file="src/Pump.cs", line_start=12, symbols=("Nonexistent",))
    report = check_claim(repo, claim)
    assert report.verdict is Verdict.REJECT
    assert names(report)["symbols_near"] is CheckStatus.FAIL


def test_missing_file_rejects(repo):
    report = check_claim(repo, Claim(file="src/Nope.cs", line_start=1))
    assert report.verdict is Verdict.REJECT
    assert names(report) == {"file_in_root": CheckStatus.FAIL}


@pytest.mark.parametrize("name", ["../outside.txt", "src/../../outside.txt"])
def test_parent_escape_rejects(repo, name):
    report = check_claim(repo, Claim(file=name, line_start=1))
    assert report.verdict is Verdict.REJECT
    assert "outside" in report.checks[0].detail


def test_absolute_path_outside_root_rejects(repo, tmp_path):
    report = check_claim(repo, Claim(file=str(tmp_path / "outside.txt"), line_start=1))
    assert report.verdict is Verdict.REJECT


def test_absolute_path_inside_root_is_accepted(repo):
    report = check_claim(repo, Claim(file=str(repo / "src" / "Pump.cs"), line_start=4))
    assert names(report)["file_in_root"] is CheckStatus.PASS


def test_symlink_escape_rejects(repo, tmp_path):
    link = repo / "src" / "link.txt"
    try:
        os.symlink(tmp_path / "outside.txt", link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted here")
    report = check_claim(repo, Claim(file="src/link.txt", line_start=1))
    assert report.verdict is Verdict.REJECT
    assert "outside" in report.checks[0].detail


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS junctions")
def test_junction_escape_rejects(repo, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "x.cs").write_text("a\nb\n")
    junction = repo / "src" / "jn"
    done = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(target)],
                          capture_output=True, text=True)
    if done.returncode != 0:
        pytest.skip("junction creation failed: " + done.stderr)
    report = check_claim(repo, Claim(file="src/jn/x.cs", line_start=1))
    assert report.verdict is Verdict.REJECT
    assert "outside" in report.checks[0].detail


def test_binary_and_oversize_files_are_not_checkable_never_rejected(repo):
    (repo / "src" / "blob.bin").write_bytes(b"\x00\x01\x02" * 100)
    (repo / "src" / "big.cs").write_text("x\n" * 2000)
    cfg = replace(ClaimCheckConfig(), max_file_bytes=1024)
    for name in ("src/blob.bin", "src/big.cs"):
        report = check_claim(repo, Claim(file=name, line_start=1), cfg)
        assert names(report)["file_in_root"] is CheckStatus.NOT_CHECKABLE
        assert report.verdict is Verdict.KEEP


def test_crlf_files_count_lines_like_an_editor(repo):
    (repo / "src" / "Crlf.cs").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    assert check_claim(repo, Claim(file="src/Crlf.cs", line_start=3)).verdict is Verdict.KEEP
    assert check_claim(repo, Claim(file="src/Crlf.cs", line_start=4)).verdict is Verdict.REJECT


def test_no_citation_beyond_file_means_keep(repo):
    report = check_claim(repo, Claim(file="src/Pump.cs"))
    assert report.verdict is Verdict.KEEP
    assert names(report)["line_in_range"] is CheckStatus.NOT_CHECKABLE


def test_evidence_is_capped(repo):
    body = "\n".join("if (n > MaxItems) return;" for _ in range(200))
    (repo / "src" / "Many.cs").write_text(body)
    cfg = replace(ClaimCheckConfig(), max_evidence=5, guard_window_lines=200)
    report = check_claim(repo, Claim(file="src/Many.cs", line_start=100), cfg)
    assert len(report.guards) == 5


def test_duplicates_collapse_and_share_the_primary_verdict(repo):
    checker = ClaimChecker(repo, replace(ClaimCheckConfig(), duplicate_window_lines=5))
    reports = checker.check_many([
        Claim(file="src/Pump.cs", line_start=7, claim_id="a"),
        Claim(file="src/Pump.cs", line_start=9, claim_id="b"),
        Claim(file="src/Pump.cs", line_start=500, claim_id="c"),
    ])
    assert [r.duplicate_of for r in reports] == ["", "a", ""]
    assert reports[1].verdict == reports[0].verdict
    assert reports[2].verdict is Verdict.REJECT


def test_claim_from_mapping_accepts_line_forms_and_prose():
    c = claim_from_mapping({"file": "a.cs", "lines": "12-14", "symbols": "Foo, Bar", "id": "x1"})
    assert (c.line_start, c.line_end, c.symbols, c.claim_id) == (12, 14, ("Foo", "Bar"), "x1")
    c = claim_from_mapping({"file": "a.cs", "line": 7})
    assert (c.line_start, c.line_end) == (7, 7)
    c = claim_from_mapping({"file": "a.cs", "text": "Line 9 calls `Foo.Bar()` badly"})
    assert (c.line_start, c.symbols) == (9, ("Foo.Bar",))
    c = claim_from_mapping({"file": "a.cs", "guard_symbols": ["Min"], "text": "never calls `Max`"})
    assert c.guard_symbols == ("Min",)
    with pytest.raises(ClaimError):
        claim_from_mapping({"line": 3})
    with pytest.raises(ClaimError):
        claim_from_mapping({"file": "a.cs", "lines": "abc"})
    with pytest.raises(ClaimError):
        claim_from_mapping({"file": "a.cs", "symbols": 5})


def test_batch_jsonl_reports_in_order_with_typed_errors(repo, tmp_path):
    src = tmp_path / "claims.jsonl"
    rows = [
        {"id": "ok", "file": "src/Pump.cs", "line": 8, "symbols": ["Copy"]},
        "this is not json",
        {"id": "bad", "line": 3},
        {"id": "gone", "file": "src/Nope.cs", "line": 1},
        {"id": "dup", "file": "src/Pump.cs", "line": 9},
    ]
    src.write_text("\n".join(r if isinstance(r, str) else json.dumps(r) for r in rows) + "\n")
    out = tmp_path / "out.jsonl"
    summary = check_jsonl(src, out, repo, replace(ClaimCheckConfig(), duplicate_window_lines=5))
    lines = [json.loads(x) for x in out.read_text().splitlines()]
    assert [x["claim_id"] if "claim_id" in x else "error" for x in lines] == \
        ["ok", "error", "error", "gone", "dup"]
    assert lines[4]["duplicate_of"] == "ok"
    assert lines[1]["error"]["code"] == "invalid_json" and lines[1]["line_no"] == 2
    assert lines[2]["error"]["code"] == "invalid_claim"
    assert summary.total == 5 and summary.errors == 2 and summary.duplicates == 1
    assert summary.verdicts == {"reject": 1, "flag": 2, "keep": 0}


def test_batch_row_repo_root_overrides_default(repo, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "Z.cs").write_text("a\nb\n")
    src = tmp_path / "c.jsonl"
    row = {"id": "z", "file": "Z.cs", "line": 2, "repo_root": str(other)}
    src.write_text(json.dumps(row) + "\n")
    summary = check_jsonl(src, tmp_path / "o.jsonl", repo, ClaimCheckConfig())
    assert summary.verdicts["keep"] == 1


def test_batch_size_is_bounded(repo, tmp_path):
    src = tmp_path / "c.jsonl"
    src.write_text("\n".join(json.dumps({"file": "src/Pump.cs", "line": 1}) for _ in range(5)))
    with pytest.raises(ClaimError, match="max_batch_claims"):
        check_jsonl(src, tmp_path / "o.jsonl", repo,
                    replace(ClaimCheckConfig(), max_batch_claims=3))


def test_prose_names_the_missing_guard_and_finding_it_nearby_flags(repo):
    claim = claim_from_mapping({
        "file": "src/Pump.cs", "category": DOS,
        "text": "Line 13 uses `Safe` but never calls `Min` to bound the count."})
    assert claim.symbols == ("Safe",) and claim.guard_symbols == ("Min",)
    report = check_claim(repo, claim, replace(ClaimCheckConfig(), max_callers=0))
    assert names(report)["symbols_near"] is CheckStatus.PASS
    assert report.verdict is Verdict.FLAG
    assert any(g.kind == "named" and g.line == 13 for g in report.guards)


def test_missing_guard_symbol_far_from_the_citation_does_not_reject(repo):
    claim = Claim(file="src/Pump.cs", line_start=7, guard_symbols=("Min",))
    report = check_claim(repo, claim, replace(ClaimCheckConfig(), guard_window_lines=2,
                                              max_callers=0))
    assert all(c.status is not CheckStatus.FAIL for c in report.checks)
    assert not any(g.kind == "named" for g in report.guards)


def test_named_guard_in_a_direct_caller_is_found(repo):
    claim = Claim(file="src/Pump.cs", line_start=7, symbols=("Fill",), guard_symbols=("MaxItems",),
                  category="")
    cfg = replace(ClaimCheckConfig(), guard_window_lines=0)
    report = check_claim(repo, claim, cfg)
    assert [(g.path, g.kind, g.where) for g in report.guards if g.where == "caller"] \
        == [("src/Caller.cs", "named", "caller")]


def test_read_guard_refuses_the_cited_file_and_hides_other_files_from_caller_search(repo):
    refused = []

    def guard(path):
        refused.append(os.path.basename(path))
        return os.path.basename(path) != "Caller.cs"

    cfg = replace(ClaimCheckConfig(), guard_window_lines=0)
    claim = Claim(file="src/Pump.cs", line_start=7, symbols=("Fill",), category=DOS)
    report = ClaimChecker(repo, cfg, read_guard=guard).check(claim)
    assert report.guards == ()  # Caller.cs holds the guard but the policy refuses it
    assert "Caller.cs" in refused


def test_refused_file_answers_exactly_like_a_missing_file(repo):
    refused = ClaimChecker(repo, read_guard=lambda path: False).check(
        Claim(file="src/Pump.cs", line_start=4))
    missing = ClaimChecker(repo).check(Claim(file="src/Nope.cs", line_start=4))
    assert refused.checks == missing.checks
    assert refused.verdict is missing.verdict is Verdict.REJECT


def test_caller_search_does_not_follow_a_file_symlink_out_of_the_root(repo, tmp_path):
    (repo / "src" / "Caller.cs").unlink()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "Out.cs").write_text(CALLER, newline="\n")
    try:
        os.symlink(outside / "Out.cs", repo / "src" / "Linked.cs")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted here")
    seen = []

    def guard(path):
        seen.append(path)
        return True

    cfg = replace(ClaimCheckConfig(), guard_window_lines=0)
    claim = Claim(file="src/Pump.cs", line_start=7, symbols=("Fill",), category=DOS)
    report = ClaimChecker(repo, cfg, read_guard=guard).check(claim)
    assert report.guards == ()
    root = os.path.realpath(repo)
    assert all(os.path.realpath(p).startswith(root) for p in seen)
    assert not any(p.endswith("Out.cs") for p in seen)


def test_caller_search_consults_the_read_guard_before_reading(repo, monkeypatch):
    order = []
    real_read = ClaimChecker._read

    def read(self, real):
        order.append(("read", os.path.basename(real)))
        return real_read(self, real)

    def guard(path):
        order.append(("guard", os.path.basename(path)))
        return True

    monkeypatch.setattr(ClaimChecker, "_read", read)
    cfg = replace(ClaimCheckConfig(), guard_window_lines=0)
    ClaimChecker(repo, cfg, read_guard=guard).check(
        Claim(file="src/Pump.cs", line_start=7, symbols=("Fill",), category=DOS))
    assert order.index(("guard", "Caller.cs")) < order.index(("read", "Caller.cs"))


def test_caller_search_skips_non_regular_files(repo):
    if not hasattr(os, "mkfifo"):
        pytest.skip("no FIFOs on this platform")
    os.mkfifo(repo / "src" / "Pipe.cs")
    cfg = replace(ClaimCheckConfig(), guard_window_lines=0)
    report = ClaimChecker(repo, cfg).check(
        Claim(file="src/Pump.cs", line_start=7, symbols=("Fill",), category=DOS))
    assert any(g.path == "src/Caller.cs" for g in report.guards)


def test_caller_search_works_without_os_path_isjunction(repo, monkeypatch):
    monkeypatch.delattr(os.path, "isjunction", raising=False)
    cfg = replace(ClaimCheckConfig(), guard_window_lines=0)
    report = ClaimChecker(repo, cfg).check(
        Claim(file="src/Pump.cs", line_start=7, symbols=("Fill",), category=DOS))
    assert any(g.path == "src/Caller.cs" for g in report.guards)
