"""Stable call signatures for de-duplicating equivalent agent tool calls.

Two calls that name the same host path in different spellings are the same
call; the signature resolves every local path-bearing argument through the real
filesystem so speculative and repeated calls match. A UNC, device or NT
spelling is kept lexical and never resolved (see ``_canonical_path``). It touches the
filesystem and the archive adapter, so it lives with the adapters. Moved
from ``server.py`` in the WP1 Three-Hundred-Twentieth Slice; since then an
archive_create value is checked for a network spelling before it is joined
to the root, so on POSIX an extended-length Windows spelling now keys like
its plain form under the root, as dispatch treats it.
"""
from __future__ import annotations

import json
import os

import sonder_runtime.adapters.archive_create as archive_create_tool
from sonder_runtime.adapters.filesystem import file_ops


def _canonical_path(text: str) -> str:
    """Return ``normcase(realpath(normpath(text)))``, or only the lexical form for a network spelling.

    This runs on model-supplied arguments before any path policy check. ``realpath`` on a UNC,
    device or NT spelling (``\\\\host\\share``, ``//host/share``, ``\\\\?\\UNC\\...``, ``\\\\.\\pipe\\...``)
    opens an SMB connection, which can send the user's NTLM credentials to that host. So a spelling
    ``file_ops._reject_network_path`` refuses is never resolved here: it keeps its lexical form, which
    is enough for a de-duplication key, and dispatch refuses the call itself.
    """
    try:
        local = file_ops._reject_network_path(text)
    except PermissionError:
        return os.path.normcase(os.path.normpath(text))
    return os.path.normcase(os.path.realpath(os.path.normpath(local)))


def _canonical_path_under(root: str, raw: str) -> str:
    """Return ``_canonical_path`` of ``raw``, joined to ``root`` when relative.

    The network check runs on ``raw`` before the join, and relativity is judged on the value it
    returns (an extended-length ``\\\\?\\C:\\`` prefix already stripped). ``os.path.isabs`` is false
    for a backslash spelling such as ``\\\\host\\share\\x`` on POSIX, and for ``\\??\\C:\\x`` on
    Windows from Python 3.13; joining either to ``root`` would hide the spelling from the check in
    ``_canonical_path``.
    """
    try:
        local = file_ops._reject_network_path(raw)
    except PermissionError:
        return os.path.normcase(os.path.normpath(raw))
    return _canonical_path(local if os.path.isabs(local) else os.path.join(root, local))


def call_signature(tool_name, args, *, project_scoped_path_tools, project_scoped_path_key):
    """Return a stable signature for equivalent host-scoped tool calls.

    ``project_scoped_path_tools`` and ``project_scoped_path_key(tool_name)``
    describe which argument carries a tool's project path; they are injected
    because the path-confinement tables stay with the dispatcher.
    """
    canonical = dict(args) if isinstance(args, dict) else args
    if isinstance(canonical, dict):
        if tool_name == "archive_create":
            root = _canonical_path(str(canonical.get("root") or "."))
            canonical["root"] = root
            destination = str(canonical.get("destination") or "")
            if destination:
                canonical["destination"] = _canonical_path_under(root, destination)
            try:
                inputs = archive_create_tool._parse_inputs(
                    canonical.get("inputs_json", canonical.get("inputs", []))
                )
                canonical["inputs_json"] = [_canonical_path_under(root, value) for value in inputs]
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
                    canonical[key] = _canonical_path(str(raw))
                except (OSError, ValueError):
                    pass
    return (
        str(tool_name),
        json.dumps(canonical, sort_keys=True, ensure_ascii=False, default=str),
    )
