import json

import pytest

from scripts import check_release_version as policy

REVISION = "a" * 40


def _codes(report):
    return {item["code"] for item in report["diagnostics"]}


def _repo(tmp_path, runtime="0.9.0.dev0", app="1.0.0+1"):
    (tmp_path / "sonder_version.py").write_text(
        f'VERSION = "{runtime}"\n', encoding="utf-8"
    )
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "pubspec.yaml").write_text(
        f"name: sonder_runtime\nversion: {app}\n", encoding="utf-8"
    )
    return tmp_path


def test_current_development_divergence_is_valid_but_not_release_ready():
    report = policy.evaluate("0.9.0.dev0", "1.0.0+1", revision=REVISION)
    assert report["ok"] is True
    assert report["mode"] == "development"
    assert report["release_ready"] is False
    assert "DEVELOPMENT_VERSION_DIVERGENCE" in _codes(report)
    assert report["build_identity"]["commit_sha"] == REVISION


def test_stable_untagged_candidate_requires_matching_app_base():
    good = policy.evaluate("1.2.3", "1.2.3+7", revision=REVISION)
    assert good["ok"] is True
    assert good["mode"] == "release-candidate"
    assert good["release_ready"] is False

    bad = policy.evaluate("1.2.3", "1.2.4+7", revision=REVISION)
    assert bad["ok"] is False
    assert "STABLE_VERSION_MISMATCH" in _codes(bad)


def test_tagged_release_requires_all_three_public_versions_to_match():
    report = policy.evaluate(
        "1.2.3", "1.2.3+7", tag_raw="app-v1.2.3", revision=REVISION
    )
    assert report["ok"] is True
    assert report["release_ready"] is True
    assert report["release_version"] == "1.2.3"
    assert report["app"]["build"] == 7


@pytest.mark.parametrize(
    ("runtime", "app", "tag", "code"),
    [
        ("1.2.3.dev0", "1.2.3+1", "app-v1.2.3", "TAGGED_RUNTIME_IS_DEVELOPMENT"),
        ("1.2.4", "1.2.3+1", "app-v1.2.3", "RUNTIME_TAG_MISMATCH"),
        ("1.2.3", "1.2.4+1", "app-v1.2.3", "APP_TAG_MISMATCH"),
        ("1.2.3", "1.2.3+1", "v1.2.3", "TAG_INVALID"),
    ],
)
def test_tagged_release_mismatches_fail(runtime, app, tag, code):
    report = policy.evaluate(runtime, app, tag_raw=tag, revision=REVISION)
    assert report["ok"] is False
    assert code in _codes(report)


def test_release_requires_exact_revision_and_tag():
    unknown = policy.evaluate(
        "1.2.3", "1.2.3+1", tag_raw="app-v1.2.3", revision="unknown"
    )
    assert unknown["ok"] is False
    assert "REVISION_UNKNOWN" in _codes(unknown)
    untagged = policy.evaluate(
        "1.2.3", "1.2.3+1", revision=REVISION, require_release=True
    )
    assert untagged["ok"] is False
    assert "RELEASE_TAG_REQUIRED" in _codes(untagged)


def test_parsers_are_strict_and_do_not_execute_runtime_module(tmp_path):
    root = _repo(tmp_path, runtime="1.2.3", app="1.2.3+4")
    runtime_file = root / "sonder_version.py"
    runtime_file.write_text(
        'raise RuntimeError("must not execute")\nVERSION = "1.2.3"\n',
        encoding="utf-8",
    )
    assert policy.parse_runtime_version(runtime_file) == "1.2.3"
    assert policy.parse_app_version(root / "app/pubspec.yaml") == "1.2.3+4"

    runtime_file.write_text('VERSION = get_version()\n', encoding="utf-8")
    with pytest.raises(ValueError, match="string literal"):
        policy.parse_runtime_version(runtime_file)


def test_cli_json_exposes_diagnostics_and_identity(tmp_path, capsys):
    root = _repo(tmp_path)
    assert policy.main([
        "--root", str(root), "--revision", REVISION, "--json"
    ]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "development"
    assert report["build_identity"]["display"].endswith("@aaaaaaaaaaaa")
    assert "DEVELOPMENT_VERSION_DIVERGENCE" in _codes(report)


def test_cli_tagged_mismatch_returns_failure(tmp_path, capsys):
    root = _repo(tmp_path)
    assert policy.main([
        "--root", str(root), "--revision", REVISION,
        "--tag", "app-v1.0.0", "--require-release",
    ]) == 1
    output = capsys.readouterr().out
    assert "sonder-runtime/0.9.0.dev0@aaaaaaaaaaaa" in output
    assert "TAGGED_RUNTIME_IS_DEVELOPMENT" in output
    assert "RUNTIME_TAG_MISMATCH" in output
