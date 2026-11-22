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


def _write_tree(root, files):
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def test_adapter_scans_ordinary_dot_directories_and_dotfiles(monkeypatch, tmp_path):
    # The old walk skipped every path with a dot component except a top-level
    # .env*, so a token hard-coded in CI config or editor settings was never seen.
    files = {
        ".github/workflows/deploy.yml": "  token: ghp_FAKEFAKEFAKEFAKEFAKEFAKE\n",
        ".vscode/settings.json": '{"password": "hunter2-not-real"}\n',
        "pkg/.circleci/config.yml": "DB_PASSWORD=hunter2-not-real\n",
        ".travis.yml": "https://user:hunter2-not-real@example.invalid/repo.git\n",
        ".config/tool/settings.toml": 'api_key = "FAKE-not-real"\n',
    }
    _write_tree(tmp_path, files)
    result = _scan_tree(monkeypatch, tmp_path)
    found = {finding["file"].replace("\\", "/") for finding in result["findings"]}
    assert found == set(files)
    assert result["files_scanned"] == len(files)
    assert result["credential_files_skipped"] == 0
    assert "hunter2" not in secret_scan.format_result(result)


def test_adapter_counts_credential_stores_at_every_depth_without_opening_them(monkeypatch, tmp_path):
    # Walking dot-directories must not open the stores file_read refuses: a
    # finding's file, line and kind would fingerprint them.
    stores = {
        "sub/.env": "DB_PASSWORD=hunter2-not-real\n",
        ".aws/credentials": "aws_secret_access_key = FAKEFAKEFAKEFAKE\n",
        "deep/.ssh/id_fake": "-----BEGIN OPENSSH PRIVATE KEY-----\n",
        ".npmrc": "//registry.invalid/:_authToken=FAKEFAKEFAKE\n",
        ".netrc": "machine example.invalid login me password hunter2-not-real\n",
        ".pypirc": "password = hunter2-not-real\n",
        ".git-credentials": "https://user:hunter2-not-real@example.invalid\n",
        "app/.docker/config.json": '{"auths": {"x": {"auth": "FAKEFAKEFAKE"}}}\n',
    }
    _write_tree(tmp_path, stores)
    opened = []
    real_read_bytes = Path.read_bytes

    def read_bytes(self):
        opened.append(self.name)
        return real_read_bytes(self)

    monkeypatch.setattr(secret_scan.Path, "read_bytes", read_bytes)
    result = _scan_tree(monkeypatch, tmp_path)
    assert result["findings"] == []
    assert result["files_scanned"] == 0
    assert result["credential_files_skipped"] == len(stores)
    assert opened == []
    assert "credential-store" in secret_scan.format_result(result)


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
    (planted / "nested" / ".github").mkdir(parents=True)
    (planted / "nested" / ".github" / "ci.yml").write_text(
        "OPENAI=sk-proj-FAKEFAKEFAKEFAKEFAKEFAKE\n", encoding="utf-8",
    )
    result = _scan_tree(monkeypatch, tmp_path)
    kinds = {
        (finding["file"].replace("\\", "/"), finding["type"])
        for finding in result["findings"]
    }
    assert ("zzz_planted/keys.py", "aws_access_key") in kinds
    assert ("zzz_planted/nested/.github/ci.yml", "api_token") in kinds
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


def test_adapter_does_not_count_unreadable_files_as_scanned(monkeypatch, tmp_path):
    (tmp_path / "good.txt").write_text("clean\n", encoding="utf-8")
    (tmp_path / "locked.txt").write_text("clean\n", encoding="utf-8")
    real_read_bytes = Path.read_bytes

    def guarded_read(self):
        if self.name == "locked.txt":
            raise PermissionError("access denied")
        return real_read_bytes(self)

    monkeypatch.setattr(secret_scan.Path, "read_bytes", guarded_read)
    result = _scan_tree(monkeypatch, tmp_path)
    assert result["files_scanned"] == 1
    assert result["findings"] == []


def test_adapter_scans_non_utf8_files_between_334kb_and_1mb(monkeypatch, tmp_path):
    blob = tmp_path / "binary_mixed.dat"
    blob.write_bytes(b"\xff" * 350_000 + b"\nghp_123456789012345678901234567890123456\n")
    result = _scan_tree(monkeypatch, tmp_path)
    assert result["files_scanned"] == 1
    assert any(
        f["file"] == "binary_mixed.dat" and f["type"] == "github_token"
        for f in result["findings"]
    )


def test_harness_secret_scan_parity_skips_cred_stores_scans_workflows_and_blocks_junctions(
    monkeypatch, tmp_path
):
    import os
    import harness_tools

    root = tmp_path / "proj"
    outside = tmp_path / "outside"
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".aws").mkdir(parents=True)
    outside.mkdir()
    (root / ".env").write_text("DB_PASSWORD=hunter2-not-real\n", encoding="utf-8")
    (root / ".aws" / "credentials").write_text(
        "aws_secret_access_key = FAKEFAKEFAKEFAKE\n", encoding="utf-8",
    )
    (root / ".github" / "workflows" / "ci.yml").write_text(
        "token: ghp_123456789012345678901234567890123456\n", encoding="utf-8",
    )
    (outside / "canary.txt").write_text(
        "DB_PASSWORD=outside-leak-not-real\n", encoding="utf-8",
    )
    monkeypatch.setattr(
        secret_scan.file_ops, "allowed_roots", lambda extra_roots="": [root.resolve()],
    )
    opened = []
    real_read_bytes = Path.read_bytes

    def spy_read(self):
        opened.append(self.name)
        return real_read_bytes(self)

    monkeypatch.setattr(secret_scan.Path, "read_bytes", spy_read)
    if os.name == "nt":
        _make_junction(root / "linked_dir", outside)
    result = harness_tools.secret_scan(root=str(root))
    assert result["ok"] is True
    assert ".env" not in opened and "credentials" not in opened
    assert result["credential_files_skipped"] == 2
    files = {f["file"].replace("\\", "/") for f in result["findings"]}
    assert ".github/workflows/ci.yml" in files
    assert not any("canary.txt" in f for f in files)
    assert all(f["match"] == "[REDACTED CREDENTIAL]" for f in result["findings"])
    if os.name == "nt":
        assert result["links_skipped"] >= 1
