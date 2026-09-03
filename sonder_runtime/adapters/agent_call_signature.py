"""Stable call signatures for de-duplicating equivalent agent tool calls.

Two calls that name the same host path in different spellings are the same
call; the signature resolves every path-bearing argument through the real
filesystem so speculative and repeated calls match. It touches the
filesystem and the archive adapter, so it lives with the adapters. Moved
from ``server.py`` in the WP1 Three-Hundred-Twentieth Slice with its
behaviour byte-for-byte intact.
"""
from __future__ import annotations

import json
import os

import sonder_runtime.adapters.archive_create as archive_create_tool


def call_signature(tool_name, args, *, project_scoped_path_tools, project_scoped_path_key):
    """Return a stable signature for equivalent host-scoped tool calls.

    ``project_scoped_path_tools`` and ``project_scoped_path_key(tool_name)``
    describe which argument carries a tool's project path; they are injected
    because the path-confinement tables stay with the dispatcher.
    """
    canonical = dict(args) if isinstance(args, dict) else args
    if isinstance(canonical, dict):
        if tool_name == "archive_create":
            root = os.path.realpath(os.path.normpath(str(canonical.get("root") or ".")))
            canonical["root"] = os.path.normcase(root)
            destination = str(canonical.get("destination") or "")
            if destination:
                if not os.path.isabs(destination):
                    destination = os.path.join(root, destination)
                canonical["destination"] = os.path.normcase(
                    os.path.realpath(os.path.normpath(destination))
                )
            try:
                inputs = archive_create_tool._parse_inputs(
                    canonical.get("inputs_json", canonical.get("inputs", []))
                )
                canonical["inputs_json"] = [
                    os.path.normcase(os.path.realpath(os.path.normpath(
                        value if os.path.isabs(value) else os.path.join(root, value)
                    )))
                    for value in inputs
                ]
                canonical.pop("inputs", None)
            except ValueError:
                pass
        path_keys = []
        if tool_name == "data_convert":
            path_keys.extend(("input_path", "output_path"))
        elif tool_name in {"file_copy", "file_move", "archive_extract"}:
            path_keys.extend(("source", "destination"))
        elif tool_name == "archive_create":
            path_keys = []
        elif tool_name in project_scoped_path_tools:
            path_keys.append(project_scoped_path_key(tool_name))
        elif tool_name == "workspace_run":
            path_keys.append("cwd")
        elif tool_name == "script_run":
            path_keys.extend(("path", "cwd"))
        for key in path_keys:
            raw = canonical.get(key)
            if raw:
                try:
                    canonical[key] = os.path.normcase(
                        os.path.realpath(os.path.normpath(str(raw)))
                    )
                except (OSError, ValueError):
                    pass
    return (
        str(tool_name),
        json.dumps(canonical, sort_keys=True, ensure_ascii=False, default=str),
    )
