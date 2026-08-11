"""What the codegen build loop believes about disk and about the compiler.

Both pins here are failures that were measured in the shipped tool, not
imagined: the loop read a key file_ops.read_file does not return (so every
guard that needs the current file silently no-opped), and it derived
BUILD SUCCEEDED from "no line matched the error regex" while throwing away
the build process's own exit status.
"""
import activity_tracker
import server


def _build(ok=True, stdout="", stderr="", timed_out=False):
    """The exact dict shape workbench.run_program returns (single return path)."""
    return {
        "ok": ok,
        "program": "build",
        "command": "build",
        "cwd": ".",
        "returncode": 0 if ok else 1,
        "timed_out": timed_out,
        "elapsed_ms": 1,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_truncated": False,
        "stderr_truncated": False,
    }


def _prepare(monkeypatch, tmp_path, run_program):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))
    monkeypatch.setattr(server.workbench, "run_program", run_program)


def test_an_existing_clean_file_is_read_from_disk_and_not_regenerated(
    monkeypatch, tmp_path,
):
    """read() pulled data["content"], but read_file returns "text".

    `existing` was therefore always "", which disabled the shrink floor, the
    already-clean skip, the score-against-the-incumbent rule and the sibling
    API brief all at once.
    """
    (tmp_path / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    _prepare(monkeypatch, tmp_path, lambda *a, **k: _build(ok=True, stdout="ok"))

    asked = []

    def fake_ensemble(prompt, **kwargs):
        asked.append(prompt)
        return "int main(void) { return 1; }"

    monkeypatch.setattr(server, "ensemble_answer", fake_ensemble)

    out = server.codegen_build_loop(
        str(tmp_path), '{"main.c": "an entry point"}', "build",
    )

    assert "already clean, not regenerated" in out
    assert asked == []
    # The file the caller already had must survive untouched.
    assert (tmp_path / "main.c").read_text(encoding="utf-8") == (
        "int main(void) { return 0; }\n"
    )


def test_a_failing_exit_status_is_not_reported_as_a_green_build(
    monkeypatch, tmp_path,
):
    """A restore failure under a stricter regex exited 1 and read as SUCCESS."""
    (tmp_path / "main.cs").write_text("class C { }\n", encoding="utf-8")
    _prepare(
        monkeypatch, tmp_path,
        lambda *a, **k: _build(
            ok=False, stdout="error NU1101: Unable to find package Foo",
        ),
    )
    monkeypatch.setattr(server, "ensemble_answer", lambda prompt, **kw: "class C { }")

    out = server.codegen_build_loop(
        str(tmp_path), '{"main.cs": "a class"}', "dotnet",
        build_args_json='["build"]', attempts=1, error_regex=r"CS\d{4}",
    )

    assert "BUILD SUCCEEDED" not in out
    assert "failure status" in out


def test_a_truncated_build_is_not_a_green_build_under_a_stricter_error_regex(
    monkeypatch, tmp_path,
):
    """The whole loop, driven through the documented `error_regex` knob.

    workbench dropped the tail of the build output, which is where the errors
    are. Under `CS\\d{4}` the harness's truncation notice does not match, so the
    parsed error list is EMPTY -- and an empty list from a build nobody
    finished reading is indistinguishable from a clean compile. The loop then
    skipped the file as "already clean" and reported BUILD SUCCEEDED.
    """
    (tmp_path / "main.cs").write_text("class C { }\n", encoding="utf-8")
    truncated = _build(ok=True, stdout="Build started 12:00:01")
    truncated["stdout_truncated"] = True
    _prepare(monkeypatch, tmp_path, lambda *a, **k: truncated)
    monkeypatch.setattr(server, "ensemble_answer", lambda prompt, **kw: "class C { }")

    out = server.codegen_build_loop(
        str(tmp_path), '{"main.cs": "a class"}', "dotnet",
        build_args_json='["build"]', attempts=1, error_regex=r"CS\d{4}",
    )

    assert "BUILD SUCCEEDED" not in out
    assert "MEASUREMENT INCOMPLETE" in out
    assert "already clean" not in out


def test_program_search_says_when_the_list_was_cut(monkeypatch):
    """The handler dropped workbench's truncated flag, so a PATH-order slice
    read as the machine's whole program list -- "absent" meant "not installed"."""
    activity_tracker.reset_for_tests()
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(
        server.workbench, "program_search",
        lambda query, **kw: {
            "query": query,
            "results": [{"name": "cl.exe", "path": "C:/cl.exe", "source": "PATH"}],
            "truncated": True,
        },
    )

    out = server.program_search(query="*", max_results=1)

    assert "cl.exe" in out
    assert "truncated" in out


def test_program_search_stays_quiet_when_nothing_was_cut(monkeypatch):
    activity_tracker.reset_for_tests()
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(
        server.workbench, "program_search",
        lambda query, **kw: {
            "query": query,
            "results": [{"name": "cl.exe", "path": "C:/cl.exe", "source": "PATH"}],
            "truncated": False,
        },
    )

    assert "truncated" not in server.program_search(query="cl")


def test_an_unreadable_backlog_is_reported_as_unknown_not_as_the_batch(
    monkeypatch,
):
    """`backlog = deferred` reinstated the floor-as-total bug it was fixing:
    when the count query lost the race with the campaign's own writers, the
    batch number was printed as the backlog (500 outstanding read as 0)."""
    monkeypatch.setattr(
        server.master_orchestrator, "active_model_call_count", lambda: 0,
    )
    monkeypatch.setattr(
        server.memory_store, "list_retryable_distillations",
        lambda conn, limit: [("iid-1", "compiled")],
    )
    monkeypatch.setattr(
        server, "_record_outcome_and_maybe_distill",
        lambda iid, signal: {"lesson_id": "lesson-1"},
    )

    def locked(conn):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(
        server.memory_store, "count_retryable_distillations", locked,
    )

    drain = server._drain_deferred_distillations(limit=1)

    assert drain["stored"] == 1
    assert drain["deferred"] == 0
    assert drain["backlog"] is None
    assert "unknown" in server._drain_backlog_text(drain)


# --- who judges the code this loop writes ----------------------------------


def _grounded():
    import grounded_outcomes
    grounded_outcomes.reset()
    return grounded_outcomes


def test_a_dual_role_tool_is_routed_by_role_not_by_branch_order(monkeypatch):
    """codegen_build_loop is in GENERATORS *and* VERIFIERS, and the call site
    picked with `if/elif` -- so the generator branch won and the verifier
    branch was unreachable. Whether a tool's compiler verdict counts as
    evidence must not depend on which membership test happens to be written
    first, and the loop's own build is the most execution-grounded evidence the
    runtime produces."""
    go = _grounded()
    written = []
    monkeypatch.setattr(
        server, "_record_outcome_signal", lambda ident, signal: written.append((ident, signal)),
    )
    go.note_generation("gen-1", "offload", project="p")

    server._feed_grounded_outcome(
        "codegen_build_loop", True, "=== codegen build loop ===\nBUILD SUCCEEDED",
        {"project": "p"},
    )

    assert written == [("gen-1", "compiled")]


def test_a_shrinking_regeneration_never_replaces_the_file_on_disk(
    monkeypatch, tmp_path,
):
    """The shrink floor, exercised through the whole tool rather than as a unit.

    Measured (2026-08-06): a "repair" asked only to insert two '.' characters
    came back at 44% of the original, and one asked to add a type name at 13%.
    Both parse, so an unguarded loop reports a green build on a gutted file.
    codegen_loop.shrink_rejected() is unit-tested; nothing pinned that
    codegen_build_loop still consults it, and it silently no-opped once before
    when `existing` was read from a key file_ops.read_file does not return.
    """
    original = "int main(void) {\n" + "    /* padding */\n" * 60 + "    return 0;\n}\n"
    # Comfortably over shrink_rejected()'s 40-character near-empty rule, so this
    # binds on SHRINK_FLOOR itself. The first draft used a 25-character reply,
    # which was rejected as near-empty and still passed with the floor mutated
    # to 0.0 -- a guard that cannot fail is not a guard.
    amputated = "int main(void) {\n" + "    /* x */\n" * 20 + "    return 0;\n}\n"
    assert len(amputated) > 40
    assert 0.0 < len(amputated) / len(original) < 0.75
    (tmp_path / "main.c").write_text(original, encoding="utf-8")
    _prepare(
        monkeypatch, tmp_path,
        lambda *a, **k: _build(
            ok=False, stdout="main.c(3): error C2065: undeclared identifier",
        ),
    )
    monkeypatch.setattr(
        server, "ensemble_answer", lambda prompt, **kw: amputated,
    )

    out = server.codegen_build_loop(
        str(tmp_path), '{"main.c": "an entry point"}', "cl", attempts=2,
    )

    assert "rejected: shrank to" in out
    assert (tmp_path / "main.c").read_text(encoding="utf-8") == original
