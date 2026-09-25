"""BuildFixGrant: the narrow authority one approved ``build_fix`` carries (F1).

Pure policy: which typed file calls and child builds a grant covers, the
loop budgets the book charges, expiry, principal binding and the token
transport. Anything a grant does not cover falls through to normal grading
(which refuses an unattended caller); here that is a ``GrantDecision`` with
``allowed=False``.
"""
from __future__ import annotations

import pytest

from sonder_runtime.application.build.grants import (
    BuildFixGrant,
    BuildFixGrantBook,
    BuildFixGrantSpec,
    FileSetScope,
    diff_files_and_lines,
    grant_carrier,
    grant_token_from,
    match_child_build,
    match_file_call,
    relative_in_root,
    restore_plan_digest,
    sha256_hex,
)

pytestmark = pytest.mark.unit

ROOT = "/work/sparklite"
BUILD = ROOT + "/build/ninja"
PLAN = sha256_hex("plan")


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def edit_scope():
    """The real domain scope when lane A is present; a file set otherwise."""
    try:
        from sonder_runtime.domain.build.repair import EditScope
    except ImportError:
        return FileSetScope(("src/core/math.cpp", "src/core/math.h", "src/core/entity.cpp"))
    return EditScope(roots=(ROOT,), excluded_rel=frozenset({"tools/shadergen.cpp"}),
                     excluded_dirs=("build",))


def spec(**overrides):
    scope = overrides.pop("scope", None) or edit_scope()
    values = dict(project_root=ROOT, build_dir=BUILD, target="core", config="Debug",
                  template_ids=("cmake.build", "ninja.compile_one"), world="host",
                  network="advisory_off", scope_digest=scope.digest(), expires_at=2000.0,
                  extra_targets=("all",), max_writes=8, scope=scope)
    values.update(overrides)
    return BuildFixGrantSpec(**values)


def book_with_grant(clock=None, **overrides):
    clock = clock or Clock()
    book = BuildFixGrantBook(clock=clock)
    book.approve(PLAN, "owner")
    grant = book.issue(spec(**overrides), principal_id="owner", job_id="build-fix-" + "a" * 32,
                       plan_digest=PLAN)
    assert grant is not None
    return book, grant, clock


def diff(rel, old="a", new="b"):
    return "--- a/%s\n+++ b/%s\n@@ -1 +1 @@\n-%s\n+%s\n" % (rel, rel, old, new)


def test_an_in_scope_typed_write_passes_unattended():
    book, grant, _ = book_with_grant()
    patch = book.authorize(grant.token, principal_id="owner", tool_name="text_patch",
                           arguments={"root": ROOT, "patch": diff("src/core/math.cpp"), "apply": True})
    assert patch.allowed and patch.files == ("src/core/math.cpp",) and patch.changed_lines == 2
    assert patch.source == "build_fix_grant:" + PLAN == grant.receipt_source
    write = book.authorize(grant.token, principal_id="owner", tool_name="write_file",
                           arguments={"path": ROOT + "/src/core/math.cpp", "content": "x\n",
                                      "mode": "overwrite"})
    read = book.authorize(grant.token, principal_id="owner", tool_name="read_file",
                          arguments={"path": ROOT + "/src/core/math.cpp", "max_bytes": 100})
    assert write.allowed and read.allowed


@pytest.mark.parametrize("path", [
    "/etc/passwd",
    ROOT + "/../other/src/x.cpp",
    ROOT + "2/src/core/math.cpp",
    ROOT + "/.env",
    ROOT + "/build/ninja/generated/shaders.h",
    ROOT + "/CMakeLists.txt",
    ROOT + "/tools/shadergen.cpp",
])
def test_out_of_scope_paths_fall_through(path):
    book, grant, _ = book_with_grant()
    decision = book.authorize(grant.token, principal_id="owner", tool_name="write_file",
                              arguments={"path": path, "content": "x", "mode": "overwrite"})
    assert not decision.allowed and decision.reason


def test_the_tool_sources_exclusion_names_its_reason():
    pytest.importorskip("sonder_runtime.domain.build.repair", reason="needs lane A-domain-build")
    book, grant, _ = book_with_grant()
    decision = book.authorize(grant.token, principal_id="owner", tool_name="text_patch",
                              arguments={"root": ROOT, "patch": diff("tools/shadergen.cpp")})
    assert not decision.allowed and decision.reason.startswith("BUILD_TIME_TOOL_SOURCE")
    script = book.authorize(grant.token, principal_id="owner", tool_name="text_patch",
                            arguments={"root": ROOT, "patch": diff("CMakeLists.txt")})
    assert not script.allowed and script.reason.startswith("NEEDS_BUILD_SCRIPT_CHANGE")


def test_widening_knobs_modes_and_other_tools_are_never_covered():
    book, grant, _ = book_with_grant()
    path = ROOT + "/src/core/math.cpp"
    for arguments in ({"path": path, "content": "x", "mode": "overwrite", "bypass": True},
                      {"path": path, "content": "x", "mode": "overwrite", "extra_roots": "/"},
                      {"path": path, "content": "x", "mode": "create"},
                      {"path": path, "content": "x", "mode": "append"}):
        assert not book.authorize(grant.token, principal_id="owner", tool_name="write_file",
                                  arguments=arguments).allowed
    for tool in ("file_delete", "run_program", "build_job", "text_search"):
        assert not book.authorize(grant.token, principal_id="owner", tool_name=tool,
                                  arguments={"path": path}).allowed
    other_root = book.authorize(grant.token, principal_id="owner", tool_name="text_patch",
                                arguments={"root": "/work", "patch": diff("sparklite/src/core/math.cpp")})
    assert not other_root.allowed


def test_the_grant_expires_with_the_job_and_on_the_clock():
    book, grant, clock = book_with_grant()
    arguments = {"path": ROOT + "/src/core/math.cpp", "max_bytes": 10}
    assert book.authorize(grant.token, principal_id="owner", tool_name="read_file",
                          arguments=arguments).allowed
    clock.now = 2001.0
    assert not book.authorize(grant.token, principal_id="owner", tool_name="read_file",
                              arguments=arguments).allowed
    assert not match_file_call(grant, principal_id="owner", tool_name="read_file",
                               arguments=arguments, now=2001.0).allowed
    book2, grant2, _ = book_with_grant()
    book2.revoke(grant2.job_id)
    assert book2.lookup(grant2.token) is None
    assert not book2.authorize(grant2.token, principal_id="owner", tool_name="read_file",
                               arguments=arguments).allowed


def test_a_foreign_principal_is_refused():
    book, grant, _ = book_with_grant()
    decision = book.authorize(grant.token, principal_id="mallory", tool_name="read_file",
                              arguments={"path": ROOT + "/src/core/math.cpp"})
    assert not decision.allowed and "principal" in decision.reason


def test_a_grant_exists_only_behind_a_matching_approval():
    clock = Clock()
    book = BuildFixGrantBook(clock=clock)
    job = "build-fix-" + "b" * 32
    assert book.issue(spec(), principal_id="owner", job_id=job, plan_digest=PLAN) is None
    book.approve(PLAN, "someone-else")
    assert book.issue(spec(), principal_id="owner", job_id=job, plan_digest=PLAN) is None
    book.approve(PLAN, "owner")
    assert book.issue(spec(), principal_id="owner", job_id=job, plan_digest=PLAN) is not None
    # The approval is consumed: a second job needs its own approval.
    assert book.issue(spec(), principal_id="owner", job_id=job + "1", plan_digest=PLAN) is None
    book.approve(PLAN, "owner")
    clock.now += 301
    assert book.issue(spec(), principal_id="owner", job_id=job, plan_digest=PLAN) is None


def test_the_book_enforces_the_loop_budgets():
    book, grant, _ = book_with_grant(scope=FileSetScope(tuple("src/f%d.cpp" % i for i in range(8))),
                                     max_files=2, max_writes=3, max_changed_lines=4)
    authorize = lambda rel, lines=1: book.authorize(  # noqa: E731
        grant.token, principal_id="owner", tool_name="text_patch",
        arguments={"root": ROOT, "patch": "--- a/%s\n+++ b/%s\n@@ -1,%d +1,%d @@\n%s%s"
                   % (rel, rel, lines, lines, "-a\n" * lines, "+b\n" * lines)})
    assert authorize("src/f0.cpp").allowed
    assert authorize("src/f1.cpp").allowed
    assert not authorize("src/f2.cpp").allowed  # a third file
    assert not authorize("src/f0.cpp", lines=5).allowed  # 10 changed lines > 2 * 4
    assert authorize("src/f0.cpp").allowed
    assert not authorize("src/f1.cpp").allowed  # write budget used up


def test_child_builds_must_match_the_plan_tuple_and_template_family():
    _, grant, _ = book_with_grant()
    base = dict(principal_id="owner", template_id="cmake.build", build_dir=BUILD, target="core",
                config="Debug", platform="", world="host", network="advisory_off", now=1500.0)
    assert match_child_build(grant, **base).allowed
    assert match_child_build(grant, **{**base, "target": "all"}).allowed  # verify_dependents
    for change in ({"template_id": "cmake.configure"}, {"template_id": "profile.deploy"},
                   {"build_dir": ROOT + "/build/other"}, {"target": "deploy"},
                   {"config": "Release"}, {"world": "container"}, {"network": "allowed"},
                   {"principal_id": "mallory"}, {"now": 2500.0}):
        assert not match_child_build(grant, **{**base, **change}).allowed, change
    compile_one = {**base, "template_id": "ninja.compile_one", "compile_one": True}
    assert match_child_build(grant, **{**compile_one, "file_rel": "src/core/math.cpp"}).allowed
    assert not match_child_build(grant, **{**compile_one, "file_rel": "../escape.cpp"}).allowed


def test_the_grant_never_carries_network_unless_approved():
    _, grant, _ = book_with_grant(network="allowed")
    args = dict(principal_id="owner", template_id="cmake.build", build_dir=BUILD, target="core",
                config="Debug", platform="", world="host", network="allowed", now=1500.0)
    assert not match_child_build(grant, **args).allowed
    _, approved, _ = book_with_grant(network="allowed", allow_network=True)
    assert match_child_build(approved, **args).allowed


def test_the_token_rides_in_approval_token_and_nothing_else():
    _, grant, _ = book_with_grant()
    carried = grant_carrier(grant.token)
    assert carried.startswith("build_fix_grant:") and grant_token_from(carried) == grant.token
    for bad in (None, "", grant.token, "build_fix_grant:short", "build_fix_grant:" + "!" * 40):
        assert grant_token_from(bad) == ""


def test_spec_digest_is_stable_and_ignores_the_clock():
    assert spec(expires_at=1.0).digest() == spec(expires_at=99.0).digest()
    assert spec(target="game").digest() != spec().digest()
    assert restore_plan_digest("j", ("b", "a")) == restore_plan_digest("j", ["a", "b"])


def test_grant_values_are_validated():
    with pytest.raises(ValueError):
        BuildFixGrant(token="short", principal_id="owner", job_id="j", plan_digest=PLAN, spec=spec(),
                      issued_at=0)
    with pytest.raises(ValueError):
        spec(template_ids=("x",) * 17)
    with pytest.raises(ValueError):
        spec(scope_digest="nothex")


@pytest.mark.parametrize("path,root,expected", [
    ("/p/src/a.cpp", "/p", "src/a.cpp"),
    ("/p/src/../src/a.cpp", "/p", "src/a.cpp"),
    ("/p/../q/a.cpp", "/p", None),
    ("/p2/a.cpp", "/p", None),
    ("/p", "/p", None),
    ("src/a.cpp", "/p", "src/a.cpp"),
    (r"C:\Work\Proj\Src\A.cpp", r"c:\work\proj", "Src/A.cpp"),
    (r"C:\Work\Proj2\a.cpp", r"C:\Work\Proj", None),
    (r"\\server\share\proj\a.cpp", r"\\server\share\proj", "a.cpp"),
    ("/p/a\x00.cpp", "/p", None),
])
def test_relative_in_root_is_lexical_and_windows_aware(path, root, expected):
    assert relative_in_root(path, root) == expected


def test_diff_parsing_uses_hunk_counts():
    removed_dashes = "--- a/x.cpp\n+++ b/x.cpp\n@@ -1,2 +1,2 @@\n--- old comment\n+new\n ctx\n"
    assert diff_files_and_lines(removed_dashes) == (("x.cpp",), 2)
    no_newline = ("--- a/x.cpp\n+++ b/x.cpp\n@@ -1 +1 @@\n-a\n\\ No newline at end of file\n"
                  "+b\n\\ No newline at end of file\n")
    assert diff_files_and_lines(no_newline) == (("x.cpp",), 2)
    two = diff("a.cpp") + diff("b.cpp")
    assert diff_files_and_lines(two) == (("a.cpp", "b.cpp"), 4)
    for bad in ("", "garbage", "--- a/x\n+++ /dev/null\n@@ -1 +0,0 @@\n-a\n",
                "--- a/x\n+++ b/x\n@@ -1,3 +1,3 @@\n-a\n+b\n", "--- a/x\n+++ b/x\n"):
        assert diff_files_and_lines(bad) is None
