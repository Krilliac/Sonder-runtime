"""The guards that stop a codegen loop from converging on deleted code.

Each test pins a failure that was measured, not imagined: see codegen_loop's
module docstring for where each number came from.
"""
import codegen_loop as cg

import pytest


def test_shrink_rejects_an_amputation():
    """44% and 13% were real repair responses to two- and one-character fixes."""
    original = "x" * 1000
    assert cg.shrink_rejected(original, "y" * 440) is True
    assert cg.shrink_rejected(original, "y" * 130) is True


def test_shrink_allows_a_real_fix():
    """Fixing a syntax error barely changes a file's length."""
    original = "x" * 1000
    assert cg.shrink_rejected(original, "y" * 1002) is False
    assert cg.shrink_rejected(original, "y" * 800) is False


def test_shrink_rejects_a_near_empty_response():
    assert cg.shrink_rejected("x" * 1000, "   ") is True


def test_shrink_allows_anything_when_there_is_no_incumbent():
    """A first generation has nothing to shrink from."""
    assert cg.shrink_rejected("", "a" * 50) is False


def test_errors_are_deduplicated_and_ordered():
    out = "a.cs(1,1): error CS1: x\nfine\na.cs(1,1): error CS1: x\nb.cs(2,2): error CS2: y"
    errors = cg.count_errors(out)
    assert len(errors) == 2
    assert "CS1" in errors[0] and "CS2" in errors[1]


def test_error_regex_can_be_narrowed():
    out = "warning: error-prone thing\nsrc.rs:1: error[E0308]: mismatch"
    assert len(cg.count_errors(out, r"error\[")) == 1


def test_strip_code_takes_the_fenced_block():
    assert cg.strip_code("blah\n```csharp\nint x = 1;\n```\ntrailing").strip() == "int x = 1;"


def test_strip_code_drops_reasoning_blocks():
    """A reasoning model emits <think> inline when not asked for it separately."""
    got = cg.strip_code("<think>let me consider</think>\nint x = 1;\n")
    assert "think" not in got
    assert "int x = 1;" in got


def test_api_extraction_reports_declarations_not_bodies():
    src = (
        "public sealed class Foo\n"
        "{\n"
        "    public int Bar;\n"
        "    public void Baz(int n)\n"
        "    {\n"
        "        int local = n + 1;\n"
        "    }\n"
        "}\n"
    )
    api = cg.extract_api(src)
    assert "class Foo" in api
    assert "public int Bar" in api
    assert "public void Baz(int n)" in api
    assert "local" not in api


def test_dependency_brief_states_the_api_is_real():
    brief = cg.dependency_brief({"a.cs": "public class A\n    public int X;\n"})
    assert "ALREADY EXIST" in brief
    assert "public class A" in brief


def test_dependency_brief_is_empty_without_sources():
    assert cg.dependency_brief({}) == ""
    assert cg.dependency_brief({"a.cs": "   "}) == ""


def test_slips_rewrite_wrong_library_calls():
    text, hits = cg.apply_slips(
        "var c = Color.FromArgb(1,2,3,4);",
        [(r"\bColor\.FromArgb\(", "new Color(")],
    )
    assert hits == 1
    assert "new Color(1,2,3,4)" in text


def test_bad_slip_pattern_is_rejected_not_swallowed():
    with pytest.raises(ValueError):
        cg.parse_slips('[["([unclosed", "x"]]')


def test_slips_must_be_pairs():
    with pytest.raises(ValueError):
        cg.parse_slips('[["only-one"]]')


def test_files_accept_both_shapes():
    assert cg.parse_files('{"a.cs": "spec a"}') == [("a.cs", "spec a")]
    assert cg.parse_files('[{"name": "a.cs", "spec": "spec a"}]') == [("a.cs", "spec a")]


def test_files_json_must_be_json():
    with pytest.raises(ValueError):
        cg.parse_files("not json")


def test_report_warns_that_a_green_build_proves_little():
    """The declared-but-never-assigned field that started all this is not a
    compile error, so success must not be reported as proof of correctness."""
    report = cg.format_report([{"name": "a.cs", "note": "kept"}], [], ok=True)
    assert "BUILD SUCCEEDED" in report
    assert "not proof" in report


def test_report_lists_failures():
    report = cg.format_report([], ["a.cs(1,1): error CS1: boom"], ok=False)
    assert "BUILD FAILED" in report
    assert "boom" in report


def test_parse_errors_are_detected_by_message_shape():
    """Code ranges leak: C# syntax errors are mostly CS1xxx, but CS8180 and
    CS8124 are the parser too. Missing one is how a masked count reads as
    near-success."""
    assert cg.parse_blocked(["a.cs(1,1): error CS8180: { or ; or => expected"]) == 1
    assert cg.parse_blocked(["a.cs(1,1): error CS1002: ; expected"]) == 1
    assert cg.parse_blocked(["a.cs(1,1): error CS8124: Tuple must contain at least two"]) == 1
    assert cg.parse_blocked(["a.rs:1: error: unexpected token `)`"]) == 1


def test_semantic_errors_are_not_counted_as_parse_errors():
    semantic = ["a.cs(1,1): error CS0246: The type or namespace name 'X' could not be found"]
    assert cg.parse_blocked(semantic) == 0


def test_a_parse_clean_candidate_beats_a_parse_broken_one():
    """The whole point: a build that cannot parse stops before binding, so its
    small total is fiction. Measured -- a run reading '2 errors' was really 99."""
    broken = ["a.cs(1,1): error CS8180: { or ; or => expected",
              "a.cs(2,1): error CS1002: ; expected"]
    clean = ["a.cs(%d,1): error CS0246: missing type" % i for i in range(50)]
    assert cg.score(clean) < cg.score(broken)


def test_fewer_errors_wins_when_both_parse():
    a = ["a.cs(1,1): error CS0246: missing type"]
    b = ["a.cs(%d,1): error CS0246: missing type" % i for i in range(5)]
    assert cg.score(a) < cg.score(b)


def test_describe_total_flags_an_untrustworthy_count():
    assert "UNRELIABLE" in cg.describe_total(["a.cs(1,1): error CS1002: ; expected"])
    assert "UNRELIABLE" not in cg.describe_total(
        ["a.cs(1,1): error CS0246: The type or namespace name 'X' could not be found"]
    )


def test_a_duplicate_member_is_a_masking_error():
    """The measured one: a build reported '1 error' when the truth was 109. The
    single line was CS0111 -- the type parsed but could not be DECLARED, so the
    compiler never bound a method body and no dependent's errors were printed."""
    dup = ["Board.cs(31,17): error CS0111: Type 'Board' already defines a member "
           "called 'Reset' with the same parameter types"]
    assert cg.definition_blocked(dup) == 1
    assert cg.count_unreliable(dup) == 1
    assert "UNRELIABLE" in cg.describe_total(dup)


def test_a_type_or_namespace_redeclaration_is_masking():
    """CS0101/CS0102 are the same declare-phase failure one level up: two files
    both defining Board leaves no usable symbol for either, so the 109 that a
    duplicate member hid hides exactly the same way here."""
    assert cg.definition_blocked(
        ["Game.cs(3,18): error CS0101: The namespace 'Game' already contains a "
         "definition for 'Board'"]
    ) == 1
    assert cg.definition_blocked(
        ["Board.cs(9,16): error CS0102: The type 'Board' already contains a "
         "definition for 'cells'"]
    ) == 1


def test_masking_shapes_generalise_across_toolchains():
    """Shape, not code range -- the reason CS8180 was caught in the parse guard.
    Every one of these is a declaration the compiler could not install, and the
    C/C++ case is the worst of them: a redefinition in a header takes out every
    translation unit that includes it while reporting one line."""
    for line in [
        "board.h:12:8: error: redefinition of 'struct Board'",
        "board.c:20:5: error: conflicting types for 'reset'",
        "src/board.rs:14:1: error[E0428]: the name `reset` is defined multiple times",
        "ld: error: duplicate symbol: _reset",
        "Board.java:9: error: duplicate class: game.Board",
        "Board.java:14: error: method reset() is already defined in class Board",
        "src/board.ts(4,7): error TS2300: Duplicate identifier 'Board'.",
    ]:
        assert cg.definition_blocked([line]) == 1, line


def test_a_missing_member_is_not_a_masking_error():
    """The trap this guard has to avoid. CS0117/CS1061 read almost identically
    to CS0102 -- 'contain a definition for' appears in both -- but they mean the
    binder RAN and a call site is wrong. They are the loop's normal progress
    signal; flagging them would mark nearly every mid-run build unreliable."""
    for line in [
        "Game.cs(7,15): error CS0117: 'Board' does not contain a definition for 'Reset'",
        "Game.cs(8,15): error CS1061: 'Board' does not contain a definition for "
        "'Reset' and no accessible extension method 'Reset' could be found",
        "game.cpp:12:9: error: no member named 'reset' in 'Board'",
    ]:
        assert cg.definition_blocked([line]) == 0, line
        assert cg.count_unreliable([line]) == 0, line


def test_a_duplicate_local_is_not_a_masking_error():
    """CS0128 is a method-BODY error: the type declared fine, the binder was
    already running, and every other file's errors were reported. Its wording
    ('already defined in this scope') is one word away from javac's genuinely
    masking 'already defined in class Foo', which is why the guard requires the
    containing declaration kind rather than a bare 'already defined'."""
    assert cg.definition_blocked(
        ["Board.cs(22,17): error CS0128: A local variable or function named 'i' "
         "is already defined in this scope"]
    ) == 0


def test_a_missing_type_is_not_a_masking_error():
    """Deliberately undetected. A type that is invisible to its dependents is
    reported with the same 'could not be found' line as a type that was never
    written, and it INFLATES the count (one error per use site) rather than
    masking it. Matching it would flatten the score tier the loop converges on
    to catch a failure that does not under-report."""
    for line in [
        "Game.cs(3,13): error CS0246: The type or namespace name 'Board' could "
        "not be found (are you missing a using directive?)",
        "Game.cs(1,7): error CS0234: The type or namespace name 'Pieces' does "
        "not exist in the namespace 'Game'",
    ]:
        assert cg.count_unreliable([line]) == 0, line


def test_a_definition_broken_candidate_loses_to_a_bigger_honest_one():
    """The measured comparison, with the numbers from the run: 1 masked error
    must rank worse than 109 real ones, or the loop keeps the version that
    silenced the compiler."""
    masked = ["Board.cs(31,17): error CS0111: Type 'Board' already defines a "
              "member called 'Reset' with the same parameter types"]
    honest = ["Game.cs(%d,1): error CS0246: missing type" % i for i in range(109)]
    assert cg.score(honest) < cg.score(masked)


def test_the_parse_guard_is_unchanged_by_the_definition_guard():
    """Regression fence: generalising 'parse-blocked' to 'unreliable' must not
    have cost the original guard. A parse-broken 2 still loses to a clean 50."""
    broken = ["a.cs(1,1): error CS8180: { or ; or => expected",
              "a.cs(2,1): error CS1002: ; expected"]
    clean = ["a.cs(%d,1): error CS0246: missing type" % i for i in range(50)]
    assert cg.score(clean) < cg.score(broken)
    assert cg.parse_blocked(broken) == 2


def test_describe_total_names_both_masking_kinds():
    """A run can be blocked in both phases at once; the report has to say which,
    because 'fix the syntax' and 'delete the duplicate member' are different
    repairs and neither number is readable until both are gone."""
    both = ["a.cs(1,1): error CS1002: ; expected",
            "b.cs(9,16): error CS0111: Type 'B' already defines a member called 'X'"]
    text = cg.describe_total(both)
    assert "UNRELIABLE" in text
    assert "1 parse error(s)" in text
    assert "1 failed definition(s)" in text


def test_count_unreliable_counts_each_line_once():
    """It feeds a truthiness test in score() and a human-facing count in the
    report; a line matching both phases must not be double-billed."""
    line = "a.cs(1,1): error CS1519: redefinition of 'X' -- } expected"
    assert cg.parse_blocked([line]) == 1
    assert cg.definition_blocked([line]) == 1
    assert cg.count_unreliable([line]) == 1


def test_report_says_the_failure_count_is_a_floor_when_it_is_masked():
    """'BUILD FAILED: 1 distinct error line' was literally true and completely
    misleading on the run where the truth was 109. The final report is what a
    human reads, so it carries the warning too."""
    masked = cg.format_report(
        [], ["Board.cs(31,17): error CS0111: Type 'Board' already defines a "
             "member called 'Reset' with the same parameter types"], ok=False)
    assert "FLOOR" in masked

    honest = cg.format_report([], ["a.cs(1,1): error CS0246: missing type"], ok=False)
    assert "FLOOR" not in honest


def test_a_build_that_never_ran_loses_to_one_with_real_errors():
    """The harness's own "error: build could not run: ..." string matches the
    error regex, so it counted as exactly ONE error in the trustworthy tier --
    and a candidate whose build never launched outscored an honest one with
    thirty real errors, with every later attempt compared against that fiction."""
    infra = cg.count_errors("error: build could not run: program not found")
    honest = cg.count_errors(
        "\n".join("a.cs(%d,1): error CS0246: missing type" % i for i in range(30))
    )
    assert len(infra) == 1 and len(honest) == 30
    assert cg.score(honest) < cg.score(infra), "a real build must beat an unrun one"


def test_build_ran_detects_infrastructure_failure():
    assert cg.build_ran("a.cs(1,1): error CS0246: missing type") is True
    assert cg.build_ran("error: build could not run: bad cwd") is False
    assert cg.build_ran("error: build timed out after 120s") is False


def test_an_unrun_build_is_never_reported_as_success():
    """A build killed by a timeout before printing any error line yields an
    EMPTY error list, which is indistinguishable from a clean compile unless
    the report is told the build did not run."""
    report = cg.format_report([], [], ok=True, ran=False)
    assert "BUILD SUCCEEDED" not in report
    assert "BUILD DID NOT RUN" in report
    assert "NOT a pass" in report


def test_compiler_error_limit_marks_the_count_untrustworthy():
    """clang stops at 20 errors BY DEFAULT and MSVC at 100; the remainder is
    never emitted, so the total is a cap. The marker line itself matches the
    error regex, so it previously read as one more ordinary error -- the same
    1-vs-109 failure one notch past the parse and declare phases."""
    clang = cg.count_errors(
        "clang: fatal error: too many errors emitted, stopping now [-ferror-limit=]"
    )
    msvc = cg.count_errors(
        "fatal error C1003: error count exceeds 100; stopping compilation"
    )
    assert cg.count_unreliable(clang) == 1
    assert cg.count_unreliable(msvc) == 1
    assert "UNRELIABLE" in cg.describe_total(clang)
    assert "UNRELIABLE" in cg.describe_total(msvc)


def test_ordinary_errors_are_still_trusted_after_the_new_class():
    """The new shapes must not swallow honest errors -- that would flatten the
    tier the loop converges on."""
    honest = cg.count_errors(
        "a.cs(1,1): error CS0246: The type or namespace name 'X' could not be found"
    )
    assert cg.count_unreliable(honest) == 0
    assert cg.score(honest)[0] == 0
    assert "UNRELIABLE" not in cg.describe_total(honest)


def test_a_file_that_collapses_sooner_does_not_win_on_its_smaller_floor():
    """A masked total is a FLOOR, so a file that fails to parse EARLIER reports
    a SMALLER one. Ranking the masked tier by total therefore prefers the more
    broken file -- measured, a candidate at 43 errors (35 masking) beat the
    incumbent's 109 (36 masking) purely by collapsing sooner."""
    collapses_early = (
        ["a.cs(%d,1): error CS1002: ; expected" % i for i in range(35)]
        + ["a.cs(%d,1): error CS0246: missing" % i for i in range(8)]
    )
    nearly_parses = (
        ["a.cs(1,1): error CS1002: ; expected"]
        + ["a.cs(%d,1): error CS0246: missing" % i for i in range(200)]
    )
    assert len(collapses_early) < len(nearly_parses), "the trap: smaller total"
    assert cg.score(nearly_parses) < cg.score(collapses_early), (
        "one blocker away from readable must beat thirty-five, whatever the totals"
    )


def test_an_unmasked_count_still_beats_every_masked_one():
    masked = ["a.cs(1,1): error CS1002: ; expected"]
    honest = ["a.cs(%d,1): error CS0246: missing" % i for i in range(500)]
    assert cg.score(honest) < cg.score(masked)


def test_fewer_real_errors_still_wins_when_nothing_is_masked():
    assert cg.score(["a.cs(1,1): error CS0246: x"]) < cg.score(
        ["a.cs(%d,1): error CS0246: x" % i for i in range(5)]
    )


CS_SKELETON = """\
public sealed class GameMap
{
    public int Width;

    public bool IsWallCell(int x, int z)
    {
        // BODY:IsWallCell
        throw new NotImplementedException();
    }

    public float RayWallDistance(Vector3 o, Vector3 d)
    {
        // BODY:RayWallDistance
        throw new NotImplementedException();
    }
}
"""

PY_SKELETON = """\
def hit(x):
    # BODY:hit
    raise NotImplementedError
"""


def test_body_slots_and_signatures_are_language_agnostic():
    """A slot is a comment marker plus one placeholder line, not C# grammar."""
    assert cg.body_slots(CS_SKELETON) == ["IsWallCell", "RayWallDistance"]
    assert cg.body_slots(PY_SKELETON) == ["hit"]
    assert cg.body_signature(CS_SKELETON, "IsWallCell") == (
        "public bool IsWallCell(int x, int z)"
    )
    # Walks back past the brace rather than returning it.
    assert cg.body_signature(PY_SKELETON, "hit") == "def hit(x):"


def test_splice_replaces_only_the_named_slot_and_keeps_indentation():
    out = cg.splice_body(CS_SKELETON, "IsWallCell", "return x >= 0;")
    assert "        return x >= 0;" in out
    # The other slot is untouched -- one bad body cannot disturb its neighbours.
    assert "// BODY:RayWallDistance" in out
    assert cg.body_slots(out) == ["RayWallDistance"]


def test_splice_of_a_missing_or_empty_body_is_a_no_op():
    """The caller detects failure by identity, so a silent partial write is not
    allowed to look like a success."""
    assert cg.splice_body(CS_SKELETON, "NoSuchSlot", "x = 1;") == CS_SKELETON
    assert cg.splice_body(CS_SKELETON, "IsWallCell", "   \n  ") == CS_SKELETON


def test_collapse_bodies_yields_a_brief_that_cannot_drift():
    brief = cg.collapse_bodies(CS_SKELETON)
    assert "public bool IsWallCell(int x, int z)" in brief
    assert "NotImplementedException" not in brief
    assert cg.body_slots(brief) == []
