"""E2: code_runner's workspace root is the checkout, not the adapter package.

workspace_root() returned dirname(__file__) after the module moved into
sonder_runtime/adapters/execution_tools, so resolve_cwd("tests") failed,
game_forge's <checkout>/games/forge cwd was "outside the workspace", and
cwd="." ran the child inside the runtime package. Nothing is monkeypatched
here: the real root must be the directory that holds server.py.
"""
import os
from pathlib import Path

import code_runner
import sonder_runtime.adapters.filesystem.file_ops as file_ops

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_workspace_root_is_the_checkout():
    assert (REPO_ROOT / "server.py").is_file()
    assert os.path.realpath(code_runner.workspace_root()) == os.path.realpath(REPO_ROOT)


def test_workspace_root_has_one_source_of_truth():
    assert os.path.realpath(code_runner.workspace_root()) == os.path.realpath(
        file_ops.workspace_root()
    )


def test_resolve_cwd_accepts_a_checkout_directory():
    assert code_runner.resolve_cwd("tests") == os.path.realpath(REPO_ROOT / "tests")
    assert code_runner.resolve_cwd(".") == os.path.realpath(REPO_ROOT)
    assert code_runner.resolve_cwd("") == os.path.realpath(REPO_ROOT)
