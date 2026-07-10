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
    monkeypatch.setattr(file_ops.trilobite_paths, "default_home", lambda: tmp_path / "home")


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


def test_script_search_identifies_runners(monkeypatch, tmp_path):
    _guard_root(monkeypatch, tmp_path)
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "build.ps1").write_text("Write-Output ok\n", encoding="utf-8")
    (tmp_path / "tools" / "check.py").write_text("print('ok')\n", encoding="utf-8")

    result = workbench.script_search("*", root=".")

    runners = {row["name"]: row["runner"] for row in result["results"]}
    assert runners == {"build.ps1": "powershell", "check.py": "python"}


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

    result = workbench.run_script("hello.py", args_json=["trilobite"], timeout=10)

    assert result["ok"]
    assert result["stdout"].strip() == "hello trilobite"


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
