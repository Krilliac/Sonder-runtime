"""Agent work coverage lives in the adapters layer; root names are aliases."""
import server
import os
from sonder_runtime.adapters import agent_work_coverage as coverage


def test_root_names_are_identity_preserving_aliases():
    assert server._AGENT_NO_OP_COMMAND_FLAGS is coverage.NO_OP_COMMAND_FLAGS
    assert server._AGENT_BUILD_DRIVERS is coverage.BUILD_DRIVERS
    assert server._agent_normalized_path is coverage.normalized_path
    assert server._agent_path_within is coverage.path_within
    assert server._agent_explicit_command_paths is coverage.explicit_command_paths
    assert server._agent_paths_covered_by_targets is coverage.paths_covered_by_targets
    assert server._agent_build_command_examines is coverage.build_command_examines
    assert server._agent_verification_covers is coverage.verification_covers
    assert server._agent_mutation_records is coverage.mutation_records
    assert server._agent_validation_covers is coverage.validation_covers


def test_paths_normalize_and_contain(tmp_path):
    project = (tmp_path / "proj").resolve()
    (project / "src").mkdir(parents=True)
    assert coverage.normalized_path(str(project / "src" / "..")) == os.path.normcase(str(project))
    assert coverage.normalized_path("") == ""
    assert coverage.path_within(str(project / "src" / "a.py"), str(project))
    assert not coverage.path_within(str(tmp_path / "other.py"), str(project))
    assert not coverage.path_within("", str(project))
    targets = coverage.explicit_command_paths(["-x", "src/app.py", "README.md", "word"], str(project))
    assert targets == [os.path.normcase(str(project / "src" / "app.py")), os.path.normcase(str(project / "README.md"))]
    assert coverage.paths_covered_by_targets([str(project / "src" / "app.py")], [str(project / "src")])
    assert not coverage.paths_covered_by_targets([], [str(project)])


def test_build_commands_must_examine_the_work(tmp_path):
    project = (tmp_path / "proj").resolve()
    (project / "src").mkdir(parents=True)
    changed = [str(project / "src" / "main.c")]
    examines = coverage.build_command_examines
    assert examines("", str(project), str(project), changed)
    assert examines("make", str(project), str(project), changed)
    assert examines("cargo test", str(project), str(project), changed)
    assert not examines("make --version", str(project), str(project), changed)
    assert not examines("make clean", str(project), str(project), changed)
    assert not examines("python -c print(1)", str(project), str(project), changed)
    assert examines("gcc -o app src/main.c", str(project), str(project), changed)
    assert not examines("gcc -o app other/x.c", str(project), str(project), changed)
    assert not examines("git --version", str(project), str(project), changed)


def test_verifiers_are_judged_on_the_narrowed_scope(tmp_path):
    project = (tmp_path / "proj").resolve()
    (project / "tests").mkdir(parents=True)
    inside = [{"path": str(project / "tests" / "test_a.py")}]
    outside = [{"path": str(project / "payments.py")}]
    covers = coverage.verification_covers
    assert covers("test_run", {"root": str(project), "path": "tests"}, inside)
    assert not covers("test_run", {"root": str(project), "path": "tests"}, outside)
    assert covers("test_run", {"root": str(project)}, outside)
    assert not covers("build_run", {"root": str(project), "command": "make --version"}, outside)
    assert covers("test_run", {"root": str(project)}, [], project_scope=str(project / "tests"))
    assert not covers("test_run", {"root": str(project / "tests")}, [], project_scope=str(project))


def test_mutation_records_name_the_paths_a_tool_changes(tmp_path):
    project = (tmp_path / "proj").resolve()
    records = coverage.mutation_records
    assert records("file_write", {"path": str(project / "a.txt")}) == [
        {"tool": "file_write", "path": os.path.normcase(str(project / "a.txt"))},
    ]
    moved = records("file_move", {"source": str(project / "a"), "destination": str(project / "b")})
    assert moved == [{"tool": "file_move", "path": os.path.normcase(str(project / "b")), "source": os.path.normcase(str(project / "a"))}]
    assert records("data_convert", {"output_path": str(project / "o.json")}) == []
    assert records("archive_create", {"root": str(project), "destination": "out.zip"}) == [
        {"tool": "archive_create", "path": os.path.normcase(str(project / "out.zip"))},
    ]


def test_validators_must_touch_the_changed_disk_state(tmp_path):
    project = (tmp_path / "proj").resolve()
    project.mkdir()
    note = str(project / "notes.md")
    # Validators consume persisted canonical mutation records, as the real loop does.
    mutations = coverage.mutation_records("file_write", {"path": note})
    covers = coverage.validation_covers
    assert covers("file_read", {"path": note}, mutations)
    assert not covers("file_read", {"path": str(project / "app.py")}, [{"tool": "file_write", "path": str(project / "app.py")}])
    assert covers("script_run", {"path": str(project / "run_tests.py")}, [])
    assert not covers("script_run", {"path": str(project / "deploy.py")}, [])
    pytest_args = {"program": "pytest", "cwd": str(project), "args": []}
    assert covers("workspace_run", pytest_args, mutations, observation="3 passed")
    assert not covers("workspace_run", pytest_args, mutations, observation="collected 0 items")
    assert not covers("workspace_run", {"program": "pytest", "cwd": str(project), "args": ["--help"]}, mutations)
    assert not covers("run_code", {}, mutations)
