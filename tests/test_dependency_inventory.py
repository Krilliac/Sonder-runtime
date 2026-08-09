import json
import os
from pathlib import Path

import pytest

import dependency_inventory as inventory


def _run(root, **kwargs):
    return inventory.dependency_inventory(
        str(root), extra_roots=str(root), bypass=True, **kwargs,
    )


def test_parses_declared_and_resolved_dependencies_across_ecosystems(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies=["requests>=2", "rich[ansi]==13.7"]\n'
        '[project.optional-dependencies]\ntest=["pytest~=8"]\n', encoding="utf-8",
    )
    (tmp_path / "package.json").write_text(json.dumps({
        "dependencies": {"react": "^18"}, "devDependencies": {"vite": "~5"},
    }), encoding="utf-8")
    (tmp_path / "package-lock.json").write_text(json.dumps({
        "lockfileVersion": 3,
        "packages": {"": {}, "node_modules/react": {"version": "18.3.1"}},
    }), encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text(
        '[dependencies]\nserde="1"\n[dev-dependencies]\npretty_assertions={version="1.4"}\n',
        encoding="utf-8",
    )
    (tmp_path / "Cargo.lock").write_text(
        'version=3\n[[package]]\nname="serde"\nversion="1.0.203"\n', encoding="utf-8",
    )
    (tmp_path / "go.mod").write_text(
        'module example.test/x\nrequire (\n example.com/a v1.2.3\n example.com/b v0.4.0 // indirect\n)\n',
        encoding="utf-8",
    )
    (tmp_path / "app.csproj").write_text(
        '<Project><ItemGroup><PackageReference Include="Newtonsoft.Json" Version="13.0.3" />'
        '</ItemGroup></Project>', encoding="utf-8",
    )
    (tmp_path / "packages.lock.json").write_text(json.dumps({
        "dependencies": {"net8.0": {"Newtonsoft.Json": {"resolved": "13.0.3"}}},
    }), encoding="utf-8")
    (tmp_path / "pom.xml").write_text(
        '<project><dependencies><dependency><groupId>org.slf4j</groupId>'
        '<artifactId>slf4j-api</artifactId><version>2.0.13</version></dependency>'
        '</dependencies></project>', encoding="utf-8",
    )
    (tmp_path / "build.gradle.kts").write_text(
        'dependencies {\n implementation("com.google.guava:guava:33.2.0-jre")\n}\n', encoding="utf-8",
    )
    (tmp_path / "gradle.lockfile").write_text(
        'com.google.guava:guava:33.2.0-jre=runtimeClasspath\n', encoding="utf-8",
    )
    (tmp_path / "pubspec.yaml").write_text(
        'name: demo\ndependencies:\n  http: ^1.2.1\ndev_dependencies:\n  test: any\n', encoding="utf-8",
    )
    (tmp_path / "pubspec.lock").write_text(
        'packages:\n  http:\n    dependency: direct main\n    version: "1.2.1"\n', encoding="utf-8",
    )

    result = _run(tmp_path)
    keys = {(row["ecosystem"], row["name"], row["version"], row["kind"]) for row in result["items"]}

    assert ("python", "requests", ">=2", "declared") in keys
    assert ("node", "react", "18.3.1", "resolved") in keys
    assert ("rust", "serde", "1.0.203", "resolved") in keys
    assert ("go", "example.com/b", "v0.4.0", "declared") in keys
    assert ("dotnet", "Newtonsoft.Json", "13.0.3", "resolved") in keys
    assert ("maven", "org.slf4j:slf4j-api", "2.0.13", "declared") in keys
    assert ("gradle", "com.google.guava:guava", "33.2.0-jre", "resolved") in keys
    assert ("dart", "http", "1.2.1", "resolved") in keys
    assert not any(row["ecosystem"] == "dart" and row["name"] == "sdk" for row in result["items"])
    assert all(not Path(row["evidence"]).is_absolute() for row in result["items"])
    assert result["errors"] == []


def test_is_deterministic_and_reports_malformed_files_per_path(tmp_path):
    (tmp_path / "package.json").write_text("[]", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("zeta==2\nalpha==1\n", encoding="utf-8")

    first = _run(tmp_path)
    second = _run(tmp_path)

    assert first == second
    assert [row["name"] for row in first["items"]] == ["alpha", "zeta"]
    assert first["errors"][0]["path"] == "package.json"


def test_parses_pipfile_declared_dependencies(tmp_path):
    (tmp_path / "Pipfile").write_text(
        '[packages]\nrequests="*"\n[dev-packages]\npytest={version="~=8.0", extras=["testing"]}\n',
        encoding="utf-8",
    )
    result = _run(tmp_path)
    assert [(row["name"], row["scope"]) for row in result["items"]] == [
        ("pytest", "dev"), ("requests", "dependencies"),
    ]


def test_depth_file_byte_and_result_caps_are_enforced(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("a==1\nb==2\nc==3\n", encoding="utf-8")
    (tmp_path / "package.json").write_text('{"dependencies":{"d":"4"}}', encoding="utf-8")
    deep = tmp_path / "one" / "two"
    deep.mkdir(parents=True)
    (deep / "requirements.txt").write_text("hidden==1\n", encoding="utf-8")

    result = _run(tmp_path, max_depth=1, max_files=1, max_results=2)
    assert len(result["items"]) == 1  # package.json sorts before requirements.txt
    assert "files" in result["truncation_reasons"]
    assert all(row["name"] != "hidden" for row in result["items"])

    result = _run(tmp_path, max_depth=5, max_files=10, max_results=2)
    assert len(result["items"]) == 2
    assert "results" in result["truncation_reasons"]

    monkeypatch.setattr(inventory, "MAX_FILE_BYTES", 8)
    result = _run(tmp_path, max_depth=5, max_files=10, max_results=20)
    assert any("per-file cap" in row["error"] for row in result["errors"])


def test_skips_sensitive_and_ignored_trees(tmp_path):
    for name in (".git", ".ssh", "node_modules"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "package.json").write_text('{"dependencies":{"bad":"1"}}', encoding="utf-8")
    (tmp_path / "package.json").write_text('{"dependencies":{"good":"1"}}', encoding="utf-8")

    result = _run(tmp_path)
    assert [row["name"] for row in result["items"]] == ["good"]


def test_rejects_directory_symlink_and_does_not_follow_manifest_symlink(tmp_path):
    target = tmp_path / "target-project"
    target.mkdir()
    (target / "package.json").write_text('{"dependencies":{"escape":"1"}}', encoding="utf-8")
    root_link = tmp_path / "root-link"
    file_link_root = tmp_path / "file-link-root"
    file_link_root.mkdir()
    try:
        root_link.symlink_to(target, target_is_directory=True)
        (file_link_root / "package.json").symlink_to(target / "package.json")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(PermissionError, match="symlink|junction"):
        _run(root_link)
    result = _run(file_link_root)
    assert result["items"] == []
    assert result["errors"][0]["path"] == "package.json"


@pytest.mark.parametrize("name", ["max_depth", "max_files", "max_total_bytes", "max_results"])
def test_refuses_values_above_hard_caps(tmp_path, name):
    hard = {
        "max_depth": inventory.MAX_DEPTH,
        "max_files": inventory.MAX_FILES,
        "max_total_bytes": inventory.MAX_TOTAL_BYTES,
        "max_results": inventory.MAX_RESULTS,
    }[name]
    with pytest.raises(ValueError, match=name):
        _run(tmp_path, **{name: hard + 1})


def test_pnpm_lock_versions_are_read_without_yaml_dependency(tmp_path):
    (tmp_path / "pnpm-lock.yaml").write_text(
        "lockfileVersion: '9.0'\npackages:\n  '@scope/pkg@2.3.4': {}\n  lodash@4.17.21: {}\n",
        encoding="utf-8",
    )
    result = _run(tmp_path)
    assert [(row["name"], row["version"]) for row in result["items"]] == [
        ("@scope/pkg", "2.3.4"), ("lodash", "4.17.21"),
    ]
