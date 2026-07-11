import json

import server


def _guard_root(monkeypatch, tmp_path):
    monkeypatch.setattr(
        server.file_ops,
        "allowed_roots",
        lambda extra_roots="": [tmp_path.resolve()],
    )


def test_artifact_ground_uses_guarded_path_and_markdown_requirements(
    monkeypatch, tmp_path
):
    _guard_root(monkeypatch, tmp_path)
    report = tmp_path / "release report.md"
    report.write_text(
        "# Release\n\n## Verification\n\nAll checks passed.\n",
        encoding="utf-8",
    )

    output = server.artifact_ground(
        str(report),
        "writing",
        json.dumps(
            {
                "required_headings": ["Release", "Verification"],
                "required_text": ["checks passed"],
            }
        ),
    )

    assert output.startswith("artifact grounding: PASS")
    assert "recipe: markdown" in output
    assert str(report) in output


def test_artifact_ground_rejects_path_outside_guarded_roots(monkeypatch, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    _guard_root(monkeypatch, allowed)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")

    output = server.artifact_ground(str(outside), "json")

    assert output.startswith("ERROR:")
    assert "outside allowed roots" in output


def test_generated_pack_is_grounded_before_success(monkeypatch, tmp_path):
    monkeypatch.setattr(server.assetgen, "workspace_root", lambda: str(tmp_path))
    _guard_root(monkeypatch, tmp_path)

    generated = server.artifact_generate(
        "launch-kit",
        "Brand document, spreadsheet, presentation, animation, MIDI, captions, timeline, sample data, web UI, icon, sound, and model",
        kinds="document,docx,spreadsheet,presentation,animation,midi,captions,timeline,data,web,icon,sound,model",
        dimension="3d",
        theme="frost",
        seed=42,
    )
    root = tmp_path / "artifacts" / "generated" / "launch-kit"
    verified = server.artifact_verify(str(root))
    grounded = server.artifact_ground(
        str(root),
        "bundle",
        {
            "require_manifest": True,
            "required_files": [
                "brief.md",
                "animation.gif",
                "captions.srt",
                "captions.vtt",
                "document.docx",
                "workbook.xlsx",
                "presentation.pptx",
                "preview.html",
                "score.mid",
                "timeline.edl",
            ],
            "no_external_dependencies": True,
        },
    )

    assert "grounding: PASS" in generated
    assert verified.startswith("artifact verification: PASS")
    assert "deterministic checks:" in verified
    assert grounded.startswith("artifact grounding: PASS")
    assert (root / "document.docx").is_file()
    assert (root / "workbook.xlsx").is_file()
    assert (root / "presentation.pptx").is_file()
    assert (root / "animation.gif").is_file()
    assert (root / "score.mid").is_file()
    assert (root / "captions.srt").is_file()
    assert (root / "captions.vtt").is_file()
    assert (root / "timeline.edl").is_file()


def test_artifactcheck_slash_preserves_spaces_and_recipe(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server,
        "artifact_ground",
        lambda **kwargs: calls.append(kwargs) or "grounded",
    )

    output = server.control_command(
        "/artifactcheck artifacts/generated/my report | markdown"
    )

    assert output == "grounded"
    assert calls == [
        {
            "path": "artifacts/generated/my report",
            "recipe": "markdown",
        }
    ]


def test_agent_and_loop_dispatch_artifact_ground(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server,
        "artifact_ground",
        lambda **kwargs: calls.append(kwargs) or "artifact grounding: PASS",
    )

    agent = server._agent_dispatch(
        "artifact_ground",
        {
            "path": "report.md",
            "recipe": "markdown",
            "requirements": {"required_headings": ["Results"]},
        },
        read_only=True,
    )
    loop = server._loop_dispatch(
        {
            "type": "artifact_ground",
            "path": "data.csv",
            "recipe": "csv",
            "requirements": {"required_columns": ["id"]},
        }
    )

    assert agent == "artifact grounding: PASS"
    assert loop["ok"] is True
    assert calls[0]["requirements_json"] == {"required_headings": ["Results"]}
    assert calls[1]["requirements_json"] == {"required_columns": ["id"]}

    monkeypatch.setattr(
        server,
        "artifact_ground",
        lambda **kwargs: "artifact grounding: FAIL\n  [FAIL] valid-csv",
    )
    failed = server._loop_dispatch(
        {"type": "artifact_ground", "path": "broken.csv", "recipe": "csv"}
    )
    assert failed["ok"] is False


def test_artifact_ground_missing_path_is_a_failed_report(monkeypatch, tmp_path):
    _guard_root(monkeypatch, tmp_path)

    output = server.artifact_ground(str(tmp_path / "missing.json"), "json")

    assert output.startswith("artifact grounding: FAIL")
    assert "files: 0" in output


def test_loop_respects_existing_in_memory_grounding_failure():
    result = server._loop_dispatch(
        {
            "type": "ground_artifact",
            "artifact": "unfinished",
            "checks": [{"type": "not_contains", "text": "unfinished"}],
        }
    )

    assert result["ok"] is False
    assert result["summary"] == "grounding: failed"
    assert server._agent_observation_ok("artifact grounding: PASS") is True
    assert server._agent_observation_ok("artifact grounding: FAIL") is False
