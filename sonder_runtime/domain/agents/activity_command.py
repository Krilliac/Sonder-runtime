"""Pure rendering of an agent tool call as its activity-ledger command line.

The activity ledger records what a tool was asked to do without echoing the
whole argument payload. This module normalizes JSON-encoded argv and batch
operations so the activity redactor still sees flag/value pairs as separate
items, and renders each tool family's one-line command. It is explicit-input
and side-effect free. Moved from ``server.py`` in the WP1
Three-Hundred-Seventh Slice with its behaviour byte-for-byte intact.
"""
from __future__ import annotations

import json


def activity_argv(value):
    """Normalize JSON-encoded argv before the activity renderer sees it.

    ``workspace_run`` and ``script_run`` accept ``args_json`` as a string.
    Serializing that string again would turn a secret-bearing argv into a
    JSON string literal, preventing the activity redactor from recognizing
    flag/value pairs as separate argv items.
    """
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, RecursionError):
            decoded = value
        if isinstance(decoded, list):
            try:
                # json.loads can accept a nesting depth that json.dumps cannot
                # serialize on the activity path.  Keep the original text so
                # recording the failed tool call never raises from finally.
                json.dumps(decoded, ensure_ascii=False)
            except (TypeError, ValueError, RecursionError):
                return value
            return decoded
    return value


def batch_operations(args):
    value = args.get("operations_json", args.get("operations", []))
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    return value if isinstance(value, list) else None


def agent_argv(args):
    argv = args.get("args_json", args.get("args", []))
    if isinstance(argv, str):
        try:
            argv = json.loads(argv)
        except (TypeError, ValueError):
            argv = [argv]
    return [str(item) for item in (argv or [])]


def activity_command(tool_name, args):
    args = args if isinstance(args, dict) else {}
    if tool_name == "file_batch_write":
        operations = batch_operations(args) or []
        return json.dumps(
            [item.get("path", "") for item in operations if isinstance(item, dict)],
            ensure_ascii=False,
        )
    if tool_name == "workspace_compare":
        return "%s | %s" % (args.get("left", ""), args.get("right", ""))
    if tool_name == "data_convert":
        return "%s -> %s" % (
            args.get("input_path", ""), args.get("output_path", ""),
        )
    if tool_name == "workspace_run":
        return "%s %s" % (
            args.get("program", ""),
            json.dumps(
                activity_argv(args.get("args_json", args.get("args", []))),
                ensure_ascii=False,
            ),
        )
    if tool_name == "script_run":
        return "%s %s" % (
            args.get("path", ""),
            json.dumps(
                activity_argv(args.get("args_json", args.get("args", []))),
                ensure_ascii=False,
            ),
        )
    if tool_name in {"file_copy", "file_move"}:
        return "%s -> %s" % (
            args.get("source", ""), args.get("destination", ""),
        )
    if tool_name == "local_service_probe":
        return "%s %s" % (
            str(args.get("method", "GET")).upper(), args.get("url", ""),
        )
    if tool_name == "process_memory_risk_inspect":
        return "pid=%s" % args.get("pid", "")
    if tool_name == "process_list":
        return "max_processes=%s" % args.get("max_processes", 128)
    path = args.get("path") or args.get("root") or ""
    if path:
        return str(path)
    if args.get("query"):
        return "query=%s" % args["query"]
    return ""
