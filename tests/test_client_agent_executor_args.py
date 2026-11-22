"""Argument handling of the client companion executor.

A malformed numeric argument must come back as a tool error, not escape as an
exception (which the companion's WSGI app would turn into HTTP 500), and
``file_read`` must return the requested window without decoding the whole
file.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from sonder_runtime.interfaces.client_agent import (
    ClientAgentPolicy,
    execute_client_tool,
)


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("file_read", {"path": "a.txt", "offset": [1]}),
        ("file_read", {"path": "a.txt", "limit": {"n": 1}}),
        ("directory_tree", {"path": ".", "depth": [2]}),
        ("git_log", {"limit": [5]}),
    ],
)
def test_non_numeric_argument_is_a_tool_error(tmp_path: Path, tool: str, args: dict):
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    policy = ClientAgentPolicy(workspace_root=tmp_path, mode="auto")

    res = execute_client_tool(tool, args, policy=policy)

    assert res.ok is False
    assert res.error


def test_file_read_returns_the_requested_window(tmp_path: Path):
    lines = [f"line {n}" for n in range(1, 2001)]
    (tmp_path / "big.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    policy = ClientAgentPolicy(workspace_root=tmp_path, mode="manual")

    res = execute_client_tool("file_read", {"path": "big.txt", "offset": 1500, "limit": 3}, policy=policy)

    assert res.ok is True
    assert res.output.splitlines() == ["1500: line 1500", "1501: line 1501", "1502: line 1502"]


def test_file_read_handles_crlf_and_missing_final_newline(tmp_path: Path):
    (tmp_path / "w.txt").write_bytes(b"alpha\r\nbeta\r\ngamma")
    policy = ClientAgentPolicy(workspace_root=tmp_path, mode="manual")

    res = execute_client_tool("file_read", {"path": "w.txt"}, policy=policy)

    assert res.ok is True
    assert res.output.splitlines() == ["1: alpha", "2: beta", "3: gamma"]
