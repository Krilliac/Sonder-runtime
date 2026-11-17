from __future__ import annotations

from pathlib import Path

import pytest

import sonder_runtime.adapters.secret_scan as secret_scan


def test_secret_scan_redacts_findings(monkeypatch, tmp_path):
    monkeypatch.setattr(secret_scan.file_ops, "resolve_path", lambda *args, **kwargs: Path(tmp_path))
    (tmp_path / "config.env").write_text("API_KEY=super-secret-value\n", encoding="utf-8")
    result = secret_scan.scan(".")
    assert result["ok"] is True
    assert result["findings"][0]["match"] == "[REDACTED CREDENTIAL]"
    assert "super-secret" not in secret_scan.format_result(result)


def test_secret_scan_reports_bounded_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(secret_scan.file_ops, "resolve_path", lambda *args, **kwargs: Path(tmp_path))
    (tmp_path / "notes.txt").write_text("nothing", encoding="utf-8")
    clock = iter((100.0, 102.0))
    monkeypatch.setattr(secret_scan.time, "monotonic", lambda: next(clock))
    result = secret_scan.scan(".", timeout=1)
    assert result["truncated"] is True


def _scan_tree(monkeypatch, root):
    monkeypatch.setattr(secret_scan.file_ops, "resolve_path", lambda *a, **k: Path(root))
    return secret_scan.scan(".")


def test_adapter_scans_nested_env_and_credential_dotfiles(monkeypatch, tmp_path):
    files = {
        "sub/.env": "DB_PASSWORD=hunter2-not-real\n",
        ".aws/credentials": "aws_secret_access_key = FAKEFAKEFAKEFAKE\n",
        ".ssh/id_fake": "-----BEGIN OPENSSH PRIVATE KEY-----\n",
        ".npmrc": "//registry.invalid/:_authToken=FAKEFAKEFAKE\n",
        ".netrc": "machine example.invalid login me password hunter2-not-real\nsecret=FAKEFAKE\n",
        ".pypirc": "password = hunter2-not-real\n",
        ".git-credentials": "https://user:hunter2-not-real@example.invalid\n",
    }
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    result = _scan_tree(monkeypatch, tmp_path)
    found = {finding["file"].replace("\\", "/") for finding in result["findings"]}
    assert found == set(files)
    assert "hunter2" not in secret_scan.format_result(result)


def test_adapter_still_skips_heavy_directories(monkeypatch, tmp_path):
    for rel in (".git/config", "node_modules/x/a.txt", ".venv/a.txt", "venv/a.txt",
                "pkg/__pycache__/a.txt", ".mypy_cache/a.txt"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("DB_PASSWORD=hunter2-not-real\n", encoding="utf-8")
    result = _scan_tree(monkeypatch, tmp_path)
    assert result["findings"] == []
    assert result["files_scanned"] == 0


def test_high_confidence_findings_are_not_crowded_out_by_assignment_noise(monkeypatch, tmp_path):
    # 150 files of low-confidence assignments sort before the planted keys.
    for index in range(150):
        path = tmp_path / ("a%03d" % index) / "settings.py"
        path.parent.mkdir(parents=True)
        path.write_text('password = "hunter2-not-real"\n', encoding="utf-8")
    planted = tmp_path / "zzz_planted"
    planted.mkdir()
    (planted / "keys.py").write_text("aws = AKIAFAKEFAKEFAKEFAKE\n", encoding="utf-8")
    (planted / "nested").mkdir()
    (planted / "nested" / ".env").write_text(
        "OPENAI=sk-proj-FAKEFAKEFAKEFAKEFAKEFAKE\n", encoding="utf-8",
    )
    result = _scan_tree(monkeypatch, tmp_path)
    kinds = {
        (finding["file"].replace("\\", "/"), finding["type"])
        for finding in result["findings"]
    }
    assert ("zzz_planted/keys.py", "aws_access_key") in kinds
    assert ("zzz_planted/nested/.env", "api_token") in kinds
    assert result["truncated"] is True
    low = [f for f in result["findings"] if f["type"] == "credential_assignment"]
    assert len(low) == secret_scan.MAX_FINDINGS
    assert "FAKE" not in secret_scan.format_result(result)


def _make_junction(link, target):
    import os
    import subprocess

    if os.name != "nt":
        pytest.skip("directory junctions are Windows-only")
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True, check=False,
    )
    if completed.returncode != 0 or not link.exists():
        pytest.skip("cannot create a junction here: %s" % completed.stderr.strip())


def test_adapter_does_not_follow_a_junction_out_of_the_root(monkeypatch, tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "leak.env").write_text("DB_PASSWORD=hunter2-not-real\n", encoding="utf-8")
    (root / "inside.env").write_text("DB_PASSWORD=hunter2-not-real\n", encoding="utf-8")
    _make_junction(root / "jout", outside)
    result = _scan_tree(monkeypatch, root)
    files = {finding["file"].replace("\\", "/") for finding in result["findings"]}
    assert files == {"inside.env"}


def test_adapter_does_not_loop_on_a_junction_to_an_ancestor(monkeypatch, tmp_path):
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "a.env").write_text("DB_PASSWORD=hunter2-not-real\n", encoding="utf-8")
    _make_junction(root / "sub" / "loop", root)
    result = _scan_tree(monkeypatch, root)
    files = [finding["file"].replace("\\", "/") for finding in result["findings"]]
    assert files == ["sub/a.env"]
    assert result["truncated"] is False


def test_adapter_does_not_follow_a_symlinked_directory(monkeypatch, tmp_path):
    import os

    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "leak.env").write_text("DB_PASSWORD=hunter2-not-real\n", encoding="utf-8")
    try:
        os.symlink(outside, root / "lout", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip("cannot create a directory symlink here: %s" % exc)
    result = _scan_tree(monkeypatch, root)
    assert result["findings"] == []
