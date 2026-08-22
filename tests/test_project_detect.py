import json
import os
from pathlib import Path

import pytest

import sonder_runtime.adapters.observability.activity_tracker as activity_tracker
import sonder_runtime.adapters.filesystem.file_ops as file_ops
from sonder_runtime.adapters.inspection import project_detect
import server


@pytest.fixture
def project(monkeypatch, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    return root


def _detect(project, **kwargs):
    return project_detect.detect_project(str(project), **kwargs)


def _commands(data):
    return {(row["kind"], row["cwd"], tuple(row["argv"]), row["platform"]) for row in data["commands"]}


def test_mixed_monorepo_is_deterministic_and_evidence_backed(project):
    (project / "pyproject.toml").write_text(
        "[project]\nname='root'\ndependencies=['Django>=5', 'pytest>=8']\n"
        "[project.scripts]\nserve='root.cli:main'\n[tool.pytest.ini_options]\naddopts='-q'\n",
        encoding="utf-8",
    )
    web = project / "apps" / "web"
    web.mkdir(parents=True)
    (web / "package.json").write_text(json.dumps({
        "dependencies": {"next": "15.0.0", "react": "19.0.0"},
        "scripts": {"build": "next build", "test": "node test.js", "dev": "next dev", "lint": "eslint ."},
    }), encoding="utf-8")
    (web / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    rust = project / "crates" / "worker"
    (rust / "src").mkdir(parents=True)
    (rust / "Cargo.toml").write_text("[package]\nname='worker'\nversion='0.1.0'\n", encoding="utf-8")
    (rust / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")

    first = _detect(project)
    second = _detect(project)

    assert first == second
    assert [row["path"] for row in first["manifests"]] == [
        "pyproject.toml", "apps/web/package.json", "crates/worker/Cargo.toml",
    ]
    assert {row["name"] for row in first["languages"]} == {
        "JavaScript/TypeScript", "Python", "Rust",
    }
    assert {row["name"] for row in first["frameworks"]} == {"Django", "Next.js", "React", "pytest"}
    commands = _commands(first)
    assert ("build", "apps/web", ("pnpm", "run", "build"), "any") in commands
    assert ("test", "apps/web", ("pnpm", "run", "test"), "any") in commands
    assert ("runtime", "apps/web", ("pnpm", "run", "dev"), "any") in commands
    assert ("build", "crates/worker", ("cargo", "build"), "any") in commands
    assert ("runtime", "crates/worker", ("cargo", "run"), "any") in commands
    assert ("test", ".", ("python", "-m", "pytest"), "any") in commands
    assert ("runtime", ".", ("serve",), "any") in commands
    assert all("lint" not in row["argv"] for row in first["commands"])


def test_nested_root_and_depth_limit(project):
    nested = project / "products" / "api"
    nested.mkdir(parents=True)
    (nested / "go.mod").write_text("module example.test/api\n\ngo 1.23\n", encoding="utf-8")
    (nested / "main.go").write_text("package main\nfunc main() {}\n", encoding="utf-8")

    shallow = _detect(project, max_depth=1)
    deep = _detect(project, max_depth=2)
    scoped = project_detect.detect_project(str(nested))

    assert shallow["manifests"] == []
    assert [row["path"] for row in deep["manifests"]] == ["products/api/go.mod"]
    assert ("runtime", "products/api", ("go", "run", "."), "any") in _commands(deep)
    assert [row["path"] for row in scoped["manifests"]] == ["go.mod"]
    assert ("runtime", ".", ("go", "run", "."), "any") in _commands(scoped)


def test_malformed_manifests_report_per_file_errors_and_continue(project):
    (project / "package.json").write_text("{broken", encoding="utf-8")
    (project / "pyproject.toml").write_text("[project\n", encoding="utf-8")
    (project / "bad.csproj").write_text("<Project>", encoding="utf-8")
    (project / "go.mod").write_text("module example.test/ok\n", encoding="utf-8")

    data = _detect(project)

    errors = {row["path"]: row["error"] for row in data["errors"]}
    assert set(errors) == {"bad.csproj", "package.json", "pyproject.toml"}
    assert all("malformed" in error for error in errors.values())
    assert any(row["name"] == "Go" for row in data["languages"])
    assert ("test", ".", ("go", "test", "./..."), "any") in _commands(data)


def test_declared_scripts_do_not_invent_frameworks_or_commands(project):
    (project / "package.json").write_text(json.dumps({
        "dependencies": {},
        "scripts": {"test": "custom-test", "lint": "custom-lint"},
    }), encoding="utf-8")
    (project / "pyproject.toml").write_text(
        "[project]\nname='plain'\ndependencies=[]\n", encoding="utf-8",
    )

    data = _detect(project)

    assert data["frameworks"] == []
    assert _commands(data) == {("test", ".", ("npm", "run", "test"), "any")}
    assert all("<" not in token for row in data["commands"] for token in row["argv"])


def test_cross_platform_gradle_wrapper_shapes_are_explicit(project):
    (project / "build.gradle.kts").write_text("plugins { java }\n", encoding="utf-8")
    (project / "settings.gradle.kts").write_text("rootProject.name = \"demo\"\n", encoding="utf-8")
    (project / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
    (project / "gradlew.bat").write_text("@echo off\r\n", encoding="utf-8")

    data = _detect(project)
    commands = _commands(data)

    assert ("build", ".", ("./gradlew", "build"), "posix") in commands
    assert ("build", ".", ("gradlew.bat", "build"), "windows") in commands
    assert ("test", ".", ("./gradlew", "test"), "posix") in commands
    assert len([row for row in data["commands"] if row["kind"] == "build"]) == 2


def test_gradle_frameworks_require_declared_plugin_or_dependency(project):
    manifest = project / "build.gradle.kts"
    manifest.write_text("// org.springframework.boot is not enabled\nplugins { java }\n", encoding="utf-8")
    commented = _detect(project)
    manifest.write_text(
        'plugins { id("org.springframework.boot") version "3.4.0" }\n'
        'dependencies { testImplementation("org.junit.jupiter:junit-jupiter:5.11.0") }\n',
        encoding="utf-8",
    )
    declared = _detect(project)

    assert commented["frameworks"] == []
    assert {row["name"] for row in declared["frameworks"]} == {"Spring Boot", "JUnit Jupiter"}
    assert ("runtime", ".", ("gradle", "bootRun"), "any") in _commands(declared)


def test_maven_frameworks_ignore_project_coordinates(project):
    (project / "pom.xml").write_text(
        """<project><modelVersion>4.0.0</modelVersion>
<groupId>example</groupId><artifactId>spring-boot-sample</artifactId><version>1</version>
<dependencies><dependency><groupId>org.junit.jupiter</groupId>
<artifactId>junit-jupiter</artifactId></dependency></dependencies></project>""",
        encoding="utf-8",
    )

    data = _detect(project)

    assert {row["name"] for row in data["frameworks"]} == {"JUnit Jupiter"}


def test_gradle_ignores_block_comments_and_emits_all_wrapper_runtimes(project):
    manifest = project / "build.gradle.kts"
    manifest.write_text(
        '/* plugins { id("org.springframework.boot") }\n'
        'dependencies { testImplementation("org.junit.jupiter:junit-jupiter:5") } */\n'
        "plugins { java }\n",
        encoding="utf-8",
    )
    assert _detect(project)["frameworks"] == []

    (project / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
    (project / "gradlew.bat").write_text("@echo off\r\n", encoding="utf-8")
    manifest.write_text(
        'plugins { id("org.springframework.boot") version "3.4.0" }\n',
        encoding="utf-8",
    )

    commands = _commands(_detect(project))
    assert ("runtime", ".", ("./gradlew", "bootRun"), "posix") in commands
    assert ("runtime", ".", ("gradlew.bat", "bootRun"), "windows") in commands


def test_cmake_test_candidate_requires_declared_ctest(project):
    (project / "CMakeLists.txt").write_text("project(Demo)\nadd_executable(demo main.cpp)\n", encoding="utf-8")
    without = _detect(project)
    (project / "CMakeLists.txt").write_text("project(Demo)\ninclude(CTest)\n", encoding="utf-8")
    with_ctest = _detect(project)

    assert not any(row["kind"] == "test" for row in without["commands"])
    assert ("test", ".", ("ctest", "--test-dir", "build"), "any") in _commands(with_ctest)


def test_file_byte_result_and_depth_ceilings_are_explicit(project):
    for index in range(3):
        child = project / ("p%d" % index)
        child.mkdir()
        (child / "package.json").write_text(json.dumps({
            "scripts": {"build": "x", "test": "y", "start": "z"},
        }), encoding="utf-8")

    files = _detect(project, max_files=1)
    total = _detect(project, max_total_bytes=1)
    results = _detect(project, max_results=1)
    clamped = _detect(
        project, max_depth=10**9, max_files=10**9, max_total_bytes=10**9,
        max_file_bytes=10**9, max_results=10**9,
    )

    assert files["truncation_reasons"] == ["max_files"]
    assert total["truncation_reasons"] == ["max_total_bytes"]
    assert "max_results" in results["truncation_reasons"]
    assert clamped["limits"] == {
        "max_depth": project_detect.HARD_MAX_DEPTH,
        "max_files": project_detect.HARD_MAX_FILES,
        "max_total_bytes": project_detect.HARD_MAX_TOTAL_BYTES,
        "max_file_bytes": project_detect.HARD_MAX_FILE_BYTES,
        "max_results": project_detect.HARD_MAX_RESULTS,
    }


def test_per_file_cap_is_an_error_not_a_partial_parse(project):
    (project / "package.json").write_text(json.dumps({
        "scripts": {"build": "x"}, "padding": "x" * 500,
    }), encoding="utf-8")

    data = _detect(project, max_file_bytes=64)

    assert data["commands"] == []
    assert "exceeds max_file_bytes" in data["errors"][0]["error"]


def test_containment_sensitive_foreign_and_symlink_paths(project, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "package.json").write_text("{}", encoding="utf-8")
    (project / ".git").mkdir()
    (project / ".git" / "package.json").write_text("{}", encoding="utf-8")

    with pytest.raises(PermissionError, match="outside every authorized root"):
        project_detect.detect_project(str(outside))
    with pytest.raises(PermissionError, match="secret or control state"):
        project_detect.detect_project(str(project / ".git" / "package.json"))
    foreign = "C:\\outside\\repo" if os.name != "nt" else "/outside/repo"
    with pytest.raises(PermissionError, match="non-native absolute"):
        project_detect.detect_project(foreign)

    linked = project / "linked"
    root_link = tmp_path / "root-link"
    try:
        linked.symlink_to(outside, target_is_directory=True)
        root_link.symlink_to(project, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    assert _detect(project)["manifests"] == []
    with pytest.raises(PermissionError, match="symlink or junction"):
        project_detect.detect_project(str(root_link))


def test_server_discovery_policy_activity_dedup_and_autopilot(project):
    (project / "go.mod").write_text("module example.test/app\n", encoding="utf-8")
    activity_tracker.reset_for_tests()

    assert server.mcp._tool_manager.get_tool("project_detect") is not None
    assert "project_detect" in server.tool_manifest()
    assert "project_detect" in server._agent_tool_help(read_only=True)
    assert "project_detect" in server.REPOSITORY_READ_ONLY_TOOLS
    assert "project_detect" in server._PROJECT_BOUND_AGENT_TOOLS
    assert "project_detect" in server._WORK_INSPECTION_TOOLS
    assert "project_detect" not in server._WORK_VALIDATION_TOOLS
    assert "project_detect" in server._AGENT_DEDUPLICATED_INSPECTION_TOOLS
    assert "project_detect" in server._AUTOPILOT_OBSERVE_TOOLS

    with activity_tracker.response_span("test", "detect project"):
        output = server._agent_dispatch_observed(
            "project_detect", {"path": "."}, read_only=True, project=str(project),
        )
    payload = json.loads(output)
    assert payload["languages"][0]["name"] == "Go"
    event = next(row for row in activity_tracker.latest()["events"] if row.get("kind") == "project_detect")
    assert event["path"] == str(project.resolve())

    first = server._agent_call_signature("project_detect", {"path": str(project / "src" / "..")})
    second = server._agent_call_signature("project_detect", {"path": str(project)})
    assert first == second


def test_project_scope_escape_and_model_extra_roots_are_rejected(project):
    escaped = server._project_scope_args("project_detect", {"path": "../outside"}, str(project))
    assert "outside" in server._repository_scope_path_error("project_detect", escaped, str(project))
    error = server._repository_read_only_error(
        "project_detect", {"path": str(project), "extra_roots": str(project)},
    )
    assert "forbids argument(s): extra_roots" in error


def test_detector_has_no_shell_network_or_execution_api(project):
    source = Path(project_detect.__file__).read_text(encoding="utf-8")
    forbidden = ("subprocess", "urllib", "requests", "http.client", "os.system", "eval(", "exec(")
    assert not [name for name in forbidden if name in source]
    assert project_detect.format_detection(_detect(project)) == project_detect.format_detection(_detect(project))
