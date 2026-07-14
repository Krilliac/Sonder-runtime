import json
import os
import sys

import activity_tracker
import assetgen
import file_ops
import pytest
import workbench


def _guard_root(monkeypatch, tmp_path):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    monkeypatch.setattr(file_ops.sonder_paths, "default_home", lambda: tmp_path / "home")


def test_directory_tree_is_bounded_and_skips_noise(monkeypatch, tmp_path):
    _guard_root(monkeypatch, tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "large.bin").write_bytes(b"x" * 100)

    result = workbench.directory_tree(".", depth=3)

    paths = {row["relative"] for row in result["entries"]}
    assert os.path.join("src", "main.py") in paths
    assert not any(path.startswith("build") for path in paths)


def test_text_search_and_line_range_return_evidence(monkeypatch, tmp_path):
    _guard_root(monkeypatch, tmp_path)
    target = tmp_path / "demo.py"
    target.write_text("one\nTODO: fix this\nthree\n", encoding="utf-8")

    found = workbench.text_search("todo", root=".", glob="*.py")
    selected = workbench.read_line_range("demo.py", start_line=2, end_line=3)

    assert found["matches"][0]["line"] == 2
    assert found["matches"][0]["text"] == "TODO: fix this"
    assert [row["text"] for row in selected["lines"]] == ["TODO: fix this", "three"]


def test_text_search_has_a_real_traversal_budget(monkeypatch, tmp_path):
    _guard_root(monkeypatch, tmp_path)
    for index in range(5):
        (tmp_path / ("%02d.txt" % index)).write_text("no match\n", encoding="utf-8")

    result = workbench.text_search("missing", root=".", max_entries=2)

    assert result["truncated"] is True
    assert result["truncation_reason"] == "max_entries"
    assert result["entries_scanned"] == 2


def test_text_search_skips_hidden_files_unless_explicit(monkeypatch, tmp_path):
    _guard_root(monkeypatch, tmp_path)
    (tmp_path / ".env").write_text("TOKEN=private-marker\n", encoding="utf-8")

    safe = workbench.text_search("private-marker", root=".")
    explicit = workbench.text_search("private-marker", root=".", include_hidden=True)

    assert safe["matches"] == []
    assert safe["skipped_by_reason"]["hidden"] == 1
    assert explicit["matches"][0]["relative"] == ".env"


def test_script_search_identifies_runners(monkeypatch, tmp_path):
    _guard_root(monkeypatch, tmp_path)
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "build.ps1").write_text("Write-Output ok\n", encoding="utf-8")
    (tmp_path / "tools" / "check.py").write_text("print('ok')\n", encoding="utf-8")

    result = workbench.script_search("*", root=".")

    runners = {row["name"]: row["runner"] for row in result["results"]}
    assert runners == {"build.ps1": "powershell", "check.py": "python"}


def test_script_search_is_not_limited_by_tree_render_budget(monkeypatch, tmp_path):
    _guard_root(monkeypatch, tmp_path)
    for index in range(510):
        (tmp_path / ("file-%03d.txt" % index)).write_text("x", encoding="utf-8")
    (tmp_path / "z-last.py").write_text("print('found')\n", encoding="utf-8")

    result = workbench.script_search("z-last", root=".", max_entries=1000)

    assert [row["name"] for row in result["results"]] == ["z-last.py"]
    assert result["entries_scanned"] == 511
    assert result["truncated"] is False


def test_workspace_inventory_is_bounded_and_actionable(monkeypatch, tmp_path):
    _guard_root(monkeypatch, tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "large.dat").write_bytes(b"x" * 10_000)
    (tmp_path / "ephemeral").mkdir()
    (tmp_path / "ephemeral" / "generated.pdb").write_bytes(b"x" * 20_000)

    result = workbench.workspace_inventory(".", top_n=10)
    explicit = workbench.workspace_inventory(".", top_n=10, include_ignored=True)

    assert result["files"] == 2
    assert result["manifests"] == ["pyproject.toml"]
    assert result["skipped_by_reason"]["ignored_directory"] == 2
    assert result["bytes"] < explicit["bytes"]
    assert result["largest_files"][0]["relative"] == "pyproject.toml"
    assert result["truncated"] is False


def test_workspace_budget_bounds_directory_enumeration_itself(monkeypatch, tmp_path):
    _guard_root(monkeypatch, tmp_path)
    for index in range(50):
        (tmp_path / ("file-%03d.txt" % index)).write_text("x", encoding="utf-8")
    real_scandir = os.scandir
    consumed = {"count": 0}

    class CountingScandir:
        def __init__(self, path):
            self._inner = real_scandir(path)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._inner.close()

        def __iter__(self):
            return self

        def __next__(self):
            item = next(self._inner)
            consumed["count"] += 1
            return item

    monkeypatch.setattr(workbench.os, "scandir", CountingScandir)

    result = workbench.workspace_inventory(".", max_entries=3)

    assert result["entries_scanned"] == 3
    assert consumed["count"] == 3
    assert result["truncation_reason"] == "max_entries"


def test_workspace_inventory_never_follows_symlinks(monkeypatch, tmp_path):
    _guard_root(monkeypatch, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret\n", encoding="utf-8")
    link = tmp_path / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("symlink creation is unavailable")

    result = workbench.workspace_inventory(".")

    assert result["skipped_by_reason"]["symlink"] == 1
    assert not any(row["relative"].startswith("linked") for row in result["largest_files"])


def test_program_search_finds_path_executable(monkeypatch, tmp_path):
    executable = tmp_path / "unique-workbench-tool.exe"
    executable.write_bytes(b"MZ")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("PATHEXT", ".EXE")
    monkeypatch.setattr(workbench, "_windows_app_paths", lambda: [])

    result = workbench.program_search("unique-workbench", max_results=10)

    assert result["results"][0]["path"] == str(executable)


def test_run_program_uses_argv_cwd_and_output_cap(monkeypatch, tmp_path):
    _guard_root(monkeypatch, tmp_path)

    result = workbench.run_program(
        sys.executable,
        args_json=["-c", "print('x' * 200)"],
        cwd=".",
        max_output=32,
        timeout=10,
    )

    assert result["ok"]
    assert result["command"][1] == "-c"
    assert len(result["stdout"].encode("utf-8")) == 32
    assert result["stdout_truncated"] is True


def test_run_program_rejects_inline_shell(monkeypatch, tmp_path):
    _guard_root(monkeypatch, tmp_path)
    monkeypatch.setattr(workbench.shutil, "which", lambda name: "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")

    try:
        workbench.run_program("powershell", args_json=["-Command", "echo unsafe"])
    except PermissionError as exc:
        assert "script_run" in str(exc)
    else:
        raise AssertionError("inline shell command should be rejected")


def test_run_python_script_end_to_end(monkeypatch, tmp_path):
    _guard_root(monkeypatch, tmp_path)
    script = tmp_path / "hello.py"
    script.write_text("import sys\nprint('hello', sys.argv[1])\n", encoding="utf-8")

    result = workbench.run_script("hello.py", args_json=["sonder"], timeout=10)

    assert result["ok"]
    assert result["stdout"].strip() == "hello sonder"


@pytest.mark.skipif(os.name != "nt", reason="Windows batch runner")
def test_run_batch_script_uses_controlled_cmd_file_mode(monkeypatch, tmp_path):
    _guard_root(monkeypatch, tmp_path)
    script = tmp_path / "hello.cmd"
    script.write_text("@echo off\necho WORKBENCH_BATCH_OK\n", encoding="utf-8")

    result = workbench.run_script("hello.cmd", timeout=10)

    assert result["ok"]
    assert "WORKBENCH_BATCH_OK" in result["stdout"]
    with pytest.raises(PermissionError, match="metacharacters"):
        workbench.run_script("hello.cmd", args_json=["unsafe&whoami"], timeout=10)


def test_image_inspect_reads_png_and_svg_dimensions(monkeypatch, tmp_path):
    _guard_root(monkeypatch, tmp_path)
    canvas = assetgen.Canvas(17, 9)
    canvas.save_png(str(tmp_path / "sample.png"))
    (tmp_path / "sample.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 180"></svg>',
        encoding="utf-8",
    )

    png = workbench.image_inspect("sample.png")
    svg = workbench.image_inspect("sample.svg")

    assert (png["format"], png["width"], png["height"]) == ("PNG", 17, 9)
    assert (svg["format"], svg["width"], svg["height"]) == ("SVG", 320, 180)
    assert len(png["sha256"]) == 64


def test_image_inspect_rejects_unbounded_files(monkeypatch, tmp_path):
    _guard_root(monkeypatch, tmp_path)
    monkeypatch.setattr(workbench, "MAX_IMAGE_BYTES", 8)
    (tmp_path / "huge.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 20)

    with pytest.raises(ValueError, match="image exceeds"):
        workbench.image_inspect("huge.png")


def test_make_directory_stays_under_guarded_root(monkeypatch, tmp_path):
    _guard_root(monkeypatch, tmp_path)

    result = file_ops.make_directory("output/reports")

    assert result["created"] is True
    assert (tmp_path / "output" / "reports").is_dir()


def test_write_file_reports_implicitly_created_parent_directories(monkeypatch, tmp_path):
    _guard_root(monkeypatch, tmp_path)

    result = file_ops.write_file("new/deep/report.txt", "ok\n")

    assert result["created_directories"] == [
        str(tmp_path / "new"),
        str(tmp_path / "new" / "deep"),
    ]


def test_activity_transcript_and_end_report_are_replay_friendly():
    activity_tracker.reset_for_tests()
    with activity_tracker.response_span("work", "inspect image"):
        activity_tracker.set_checklist({
            "id": "abc", "summary": "1/2 complete",
            "items": [
                {"title": "Inspect", "status": "done"},
                {"title": "Report", "status": "in_progress"},
            ],
        })
        activity_tracker.record_tool_result(
            "image_inspect",
            {"path": "preview.png", "token": "do-not-store"},
            ok=True,
            command=["viewer", "--token", "command-secret", "preview.png"],
            output="image inspection\n  api_key=output-secret\n  format: PNG",
        )
        activity_tracker.set_result_summary("Image verified")

    latest = activity_tracker.latest()
    transcript = activity_tracker.format_transcript(latest)
    report = activity_tracker.format_end_report(latest)

    assert "• Viewed Image" in transcript
    assert "preview.png" in transcript
    assert "do-not-store" not in json.dumps(latest)
    assert "command-secret" not in json.dumps(latest)
    assert "output-secret" not in json.dumps(latest)
    assert "checklist: 1/2 complete" in report
    assert "summary: Image verified" in report


def test_find_files_signals_truncation(tmp_path, monkeypatch):
    # Regression (2026-07-13 audit): file_find silently truncated at max_results
    # with no indicator, inducing undercounts for any counting use case.
    import file_ops
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    for i in range(6):
        (tmp_path / ("m%d.log" % i)).write_text("x", encoding="utf-8")
    capped = file_ops.find_files(query="*.log", root=".", max_results=3)
    assert capped["truncated"] is True
    assert len(capped["results"]) == 3
    full = file_ops.find_files(query="*.log", root=".", max_results=50)
    assert full["truncated"] is False
    assert len(full["results"]) == 6


def test_read_line_range_rejects_inverted_range(tmp_path, monkeypatch):
    # Regression (2026-07-13 audit): an inverted range (start > end) was silently
    # clamped to a single line instead of erroring.
    import workbench
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))
    f = tmp_path / "lines.txt"
    f.write_text("\n".join("line%d" % i for i in range(1, 21)), encoding="utf-8")
    import pytest
    with pytest.raises(ValueError, match="before start_line"):
        workbench.read_line_range(str(f), start_line=10, end_line=3)
    # a normal range still works
    ok = workbench.read_line_range(str(f), start_line=2, end_line=4)
    assert [row["line"] for row in ok["lines"]] == [2, 3, 4]


def test_text_search_honors_an_explicit_glob_for_unlisted_extension(tmp_path, monkeypatch):
    # Regression (audit): text_search skipped a .tmp file (not in TEXT_SUFFIXES)
    # even when the caller explicitly globbed it, returning a misleading "no
    # matches". An explicit glob must be honored (binary check still guards).
    import workbench
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))
    (tmp_path / "probe.tmp").write_text("UNIQUEMARKER_XYZZY here", encoding="utf-8")
    hit = workbench.text_search("UNIQUEMARKER_XYZZY", root=str(tmp_path), glob="*.tmp")
    assert hit["files_scanned"] >= 1
    assert any("UNIQUEMARKER_XYZZY" in m.get("text", "") for m in hit["matches"])
    # A broad glob still applies the extension allowlist (skips the .tmp).
    broad = workbench.text_search("UNIQUEMARKER_XYZZY", root=str(tmp_path), glob="*")
    assert not broad["matches"]


def test_bundle_grounding_fails_on_absent_required_text(tmp_path):
    # Regression (audit): the bundle recipe never evaluated required_text, so an
    # absent required string produced a false PASS.
    import artifact_grounding
    (tmp_path / "a.md").write_text("hello world", encoding="utf-8")
    (tmp_path / "b.txt").write_text("more content", encoding="utf-8")

    absent = artifact_grounding.validate(
        str(tmp_path), recipe="bundle",
        requirements={"required_text": ["DEFINITELY_ABSENT_STRING_98765"]})
    assert absent["ok"] is False
    assert any(c["name"] == "bundle-required-text" and not c["ok"] for c in absent["checks"])

    present = artifact_grounding.validate(
        str(tmp_path), recipe="bundle",
        requirements={"required_text": ["hello world"]})
    assert any(c["name"] == "bundle-required-text" and c["ok"] for c in present["checks"])
