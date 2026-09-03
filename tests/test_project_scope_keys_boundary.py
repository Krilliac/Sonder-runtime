"""Boundary tests for WP1 project_scope_keys migration."""
import server
from sonder_runtime.domain import project_scope_keys


def test_root_helper_is_identity_preserving_alias():
    assert server._project_scoped_path_key is project_scope_keys.project_scoped_path_key


def test_known_tool_returns_expected_key():
    assert project_scope_keys.project_scoped_path_key("ensemble_codegen_build_loop") == "project_dir"
    assert project_scope_keys.project_scoped_path_key("archive_extract") == "destination"
    assert project_scope_keys.project_scoped_path_key("fetch_artifact") == "dest"
    assert project_scope_keys.project_scoped_path_key("file_find") == "root"
    assert project_scope_keys.project_scoped_path_key("test_run") == "root"
    assert project_scope_keys.project_scoped_path_key("git_commit") == "root"
    assert project_scope_keys.project_scoped_path_key("secret_scan") == "root"


def test_unknown_tool_returns_path():
    assert project_scope_keys.project_scoped_path_key("some_unknown_tool") == "path"
    assert project_scope_keys.project_scoped_path_key("") == "path"
