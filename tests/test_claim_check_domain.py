"""Pure claim-check logic over in-memory source lines (synthetic fixtures only)."""
import pytest

from sonder_runtime.domain.claim_check import (
    Claim, ClaimError, CheckStatus, Verdict, check_line_in_range, check_symbols_near,
    collapse_duplicates, decide_verdict, extract_citations, find_guards, find_symbol_uses,
    guard_families,
)

SOURCE = """\
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
    // if (count > MaxItems) return;
}
""".splitlines()


def test_claim_rejects_bad_shapes():
    with pytest.raises(ClaimError):
        Claim(file="")
    with pytest.raises(ClaimError):
        Claim(file="a.cs", line_start=0)
    with pytest.raises(ClaimError):
        Claim(file="a.cs", line_start=5, line_end=3)
    with pytest.raises(ClaimError):
        Claim(file="a.cs", line_end=3)
    assert Claim(file="a.cs", line_start=4).line_end == 4


def test_line_in_range_statuses():
    ok = check_line_in_range(Claim(file="a.cs", line_start=7, line_end=8), len(SOURCE))
    assert ok.status is CheckStatus.PASS
    past = check_line_in_range(Claim(file="a.cs", line_start=7, line_end=99), len(SOURCE))
    assert past.status is CheckStatus.FAIL and "16" in past.detail
    none = check_line_in_range(Claim(file="a.cs"), len(SOURCE))
    assert none.status is CheckStatus.NOT_CHECKABLE


def test_symbols_near_uses_word_boundaries_and_window():
    claim = Claim(file="a.cs", line_start=7, symbols=("Fill",))
    near = check_symbols_near(claim, SOURCE, window=3)
    assert near.status is CheckStatus.PASS
    assert near.evidence[0].line == 5
    far = check_symbols_near(Claim(file="a.cs", line_start=15, symbols=("Fill",)), SOURCE, window=3)
    assert far.status is CheckStatus.FAIL
    partial = check_symbols_near(Claim(file="a.cs", line_start=7, symbols=("Fil",)), SOURCE, window=3)
    assert partial.status is CheckStatus.FAIL  # "Fil" is not the word "Fill"
    assert check_symbols_near(Claim(file="a.cs", line_start=7), SOURCE, window=3).status \
        is CheckStatus.NOT_CHECKABLE
    assert check_symbols_near(Claim(file="a.cs", symbols=("Fill",)), SOURCE, window=3).status \
        is CheckStatus.NOT_CHECKABLE


def test_symbols_policy_all_versus_any():
    claim = Claim(file="a.cs", line_start=7, symbols=("Fill", "Nowhere"))
    assert check_symbols_near(claim, SOURCE, window=3).status is CheckStatus.FAIL
    assert check_symbols_near(claim, SOURCE, window=3, require="any").status is CheckStatus.PASS


def test_dotted_symbol_matches_last_segment():
    claim = Claim(file="a.cs", line_start=8, symbols=("Array.Copy",))
    assert check_symbols_near(claim, SOURCE, window=1).status is CheckStatus.PASS
    claim = Claim(file="a.cs", line_start=8, symbols=("Other.Copy",))
    assert check_symbols_near(claim, SOURCE, window=1).status is CheckStatus.PASS


def test_guard_families_from_category_and_guard_text():
    assert "bounds" in guard_families("crash/DoS from client input", "")
    assert "auth" in guard_families("auth", "")
    assert "path" in guard_families("path/file", "")
    assert "null" in guard_families("logic bug (non-security)", "a null check on the buffer")
    assert guard_families("unheard-of", "") == ("bounds", "null", "validation")


def test_find_guards_reports_line_and_kind_and_skips_comments():
    hits = find_guards(SOURCE, "a.cs", first=10, last=16, families=("bounds",))
    lines = {h.line for h in hits}
    assert 12 in lines and 13 in lines
    assert 15 not in lines  # a commented-out guard is not a guard
    assert all(h.path == "a.cs" for h in hits)
    assert find_guards(SOURCE, "a.cs", first=5, last=8, families=("bounds",)) == []


def test_find_guards_extra_patterns_and_bad_regex_ignored_never_raises():
    hits = find_guards(SOURCE, "a.cs", first=1, last=16, families=(), extra_patterns=(r"Pump",))
    assert [h.line for h in hits] == [2]
    assert hits[0].kind == "extra"


def test_find_symbol_uses_skips_declarations():
    uses = find_symbol_uses(["public void Fill(int a)", "  pump.Fill(1);", "x = Fill;"], "Fill")
    assert uses == [2]


def test_decide_verdict_precedence():
    from sonder_runtime.domain.claim_check import CheckResult, GuardHit
    fail = CheckResult("line_in_range", CheckStatus.FAIL, "x")
    ok = CheckResult("line_in_range", CheckStatus.PASS, "x")
    na = CheckResult("symbols_near", CheckStatus.NOT_CHECKABLE, "x")
    guard = GuardHit("a.cs", 3, "bounds", "if (n > Max)", "near")
    assert decide_verdict((ok, na), ()) is Verdict.KEEP
    assert decide_verdict((ok, na), (guard,)) is Verdict.FLAG
    assert decide_verdict((fail, ok), (guard,)) is Verdict.REJECT
    assert decide_verdict((na,), ()) is Verdict.KEEP  # not-checkable never rejects


def test_collapse_duplicates_within_k_lines_same_file_only():
    claims = [
        Claim(file="src/A.cs", line_start=10),
        Claim(file="src\\a.cs", line_start=14),   # same file, within 5
        Claim(file="src/A.cs", line_start=40),   # same file, far
        Claim(file="src/B.cs", line_start=11),   # other file
        Claim(file="src/A.cs"),                   # no line: never collapses
        Claim(file="src/A.cs", line_start=12),   # within 5 of the primary at 10
    ]
    assert collapse_duplicates(claims, window=5) == [0, 0, 2, 3, 4, 0]


def test_extract_citations_lines_and_backticked_symbols():
    text = ("Line 134 compares `Foo.Bar()` and lines 290-303 use `_cache` but "
            "Plain words stay out; see src/X/Y.cs:120 and `not an identifier!`.")
    lines, symbols = extract_citations(text)
    assert lines == (134, 290, 303, 120)
    assert symbols == ("Foo.Bar", "_cache")
    assert extract_citations("no citations here") == ((), ())
