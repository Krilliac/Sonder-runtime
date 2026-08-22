"""Exercise the live HTTP slash-command catalog with bounded arguments.

The older ``probe_slash_commands.py`` derives a small set from source and
does not authenticate.  This audit uses the server's caller-visible catalog,
so it covers the actual advertised surface and does not turn a protected
runtime into a stream of failed-auth requests.

By default only ``safe`` commands are executed.  ``--include-stateful`` also
executes commands marked mutation/execution/dangerous, but supplies temporary
workspace paths, dry-run/preview flags where available, and one-item limits.
It never targets the caller's checkout unless the caller explicitly passes a
different ``--root``.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


_RISKY = {"mutation", "execution", "dangerous", "ask"}
_BOOL_FALSE = {"allow_web", "adaptive", "apply", "async", "confirm",
               "dry_run", "follow", "include_finished", "learn",
               "record_failures", "wait"}
_BOOL_TRUE = {"plan_only", "preview", "read_only"}
_PATH_NAMES = {"path", "file", "file_path", "source", "target", "archive",
               "archive_path", "root", "cwd", "directory", "workspace",
               "project_root", "output", "output_path", "script"}
_ID_NAMES = {"agent_id", "artifact_id", "iid", "job_id", "run_id", "session_id",
             "task_id", "workflow_id"}
_TEXT_NAMES = {"description", "message", "objective", "prompt", "question",
               "query", "search", "task", "text", "title", "topic"}
_JSON_NAMES = {"actions_json", "items_json", "patch_json", "payload",
               "steps_json", "changes_json", "config_json"}


def _api_key() -> str:
    value = os.environ.get("SONDER_API_KEY", "").strip()
    if value:
        return value
    secret_path = os.environ.get("SONDER_SECRETS", "").strip()
    if secret_path:
        try:
            for line in Path(secret_path).read_text(encoding="utf-8").splitlines():
                if line.startswith("SONDER_API_KEY="):
                    return line.split("=", 1)[1].strip()
        except OSError:
            pass
    return ""


def _request(base: str, method: str, route: str, payload: Any = None,
             timeout: int = 30) -> tuple[int, str]:
    headers = {"Accept": "application/json"}
    key = _api_key()
    if key:
        headers["Authorization"] = "Bearer " + key
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base.rstrip("/") + route, data=data,
                                     headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read(4096).decode("utf-8", "replace")
    except Exception as error:  # timeout/refused/malformed transport
        return 0, repr(error)


def _temporary_root() -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(prefix="sonder-slash-audit-")


def _value(param: dict[str, Any], root: str, fixture_root: str | None = None,
           command_name: str = "") -> str:
    name = str(param.get("name", "")).lower()
    kind = str(param.get("type", "str"))
    fixture_root = fixture_root or root
    if kind == "bool":
        if name == "dry_run":
            return "true"
        return "true" if name in _BOOL_TRUE else "false"
    if kind in {"int", "num", "float"}:
        if name in {"max_bytes", "limit"}:
            return "256"
        if name in {"timeout", "delay_seconds"}:
            return "1"
        return "1"
    if name == "paths_json":
        return json.dumps([str(Path(fixture_root) / "probe.txt"),])
    if name in {"projection_json", "filters_json"}:
        return "[]" if name == "projection_json" else "{}"
    if name in _JSON_NAMES or name.endswith("_json"):
        return "{}" if name == "payload" else "[]"
    if name in _PATH_NAMES or name.endswith("_path") or name in {
        "dest", "destination", "input_path", "output_path",
    }:
        if name in {"root", "cwd", "workspace", "project_root"}:
            return root
        if name == "path" and not param.get("required") and param.get("default") == ".":
            return root
        filename = "data.json" if command_name == "/data_query" else "probe.txt"
        return str(Path(fixture_root) / filename)
    if name in _ID_NAMES or name.endswith("_id"):
        return "missing-slash-audit-id"
    if name in _TEXT_NAMES:
        if name == "query":
            return "SELECT 1"
        return "slash-audit"
    if name == "sql":
        return "SELECT 1"
    if name == "pattern":
        return "*"
    if name == "tool_name":
        return "file_read"
    if name in {"model", "tier"}:
        return "sonder" if name == "model" else "code"
    if name == "mode":
        return "ask"
    if name == "languages":
        return "python"
    if name in {"left", "right"}:
        return str(Path(fixture_root) / "probe.txt")
    if name == "message":
        return "slash-audit"
    return "probe"


def _quote(value: str) -> str:
    if re.search(r"\s", value):
        return json.dumps(value)
    return value


def invocation(row: dict[str, Any], root: str, include_stateful: bool,
               fixture_root: str | None = None) -> str:
    name = str(row["name"])
    params = row.get("params") or []
    if row.get("native"):
        values = {
            str(param.get("name")): _value(param, root, fixture_root, name)
            for param in params
            if param.get("required") or include_stateful
        }
        if name == "/artifactcheck":
            return "%s %s | auto" % (name, values.get("path", ""))
        if name == "/game":
            return "/game python 2d slash-audit | compact loop"
        if name == "/asset":
            return "%s %s" % (name, " ".join(values.values()))
        if name == "/gamefleet":
            return "%s slash-audit | compact loop" % name
        if name in {"/weather", "/ensemble", "/work"}:
            return "%s %s" % (name, next(iter(values.values()), "slash-audit"))
        if name in {"/agentcancel", "/agentretry"}:
            return "%s %s" % (name, " ".join(values.values()))
        if name == "/capacity":
            return "%s %s" % (name, values.get("requested_agents", "0"))
    parts = [name]
    # These native toggles are catalogued as safe because they do not touch
    # durable data.  Explicitly turn them off so a probe cannot leave the
    # caller's next REPL turn in a surprising mode.
    if name in {"/strict", "/trace"}:
        parts.append("off")
    for param in params:
        if (not param.get("required") and not include_stateful and
                str(param.get("name", "")).lower() not in _PATH_NAMES and
                str(param.get("name", "")).lower() not in {"left", "right"}):
            continue
        value = _value(param, root, fixture_root, name)
        parts.append("%s=%s" % (param["name"], _quote(value)))
    return " ".join(parts)


def _content(body: str) -> str:
    try:
        data = json.loads(body)
        choices = data.get("choices") or []
        if choices:
            return str((choices[0].get("message") or {}).get("content") or "")
    except (TypeError, ValueError):
        pass
    return body


def classify(status: int, body: str) -> str:
    if status == 401:
        return "auth_failure"
    if status == 429:
        return "rate_limited"
    if status == 0:
        return "transport_failure"
    if status >= 400:
        return "http_%d" % status
    text = _content(body).strip()
    lowered = text.lower()
    if not text:
        return "empty"
    if "model calls:" in lowered:
        match = re.search(r"model calls:\s*(\d+)", lowered)
        if match and int(match.group(1)) > 0:
            return "model_fallthrough"
    if any(token in lowered for token in ("failed:", "not callable", "traceback")):
        return "handler_failure"
    if lowered.startswith("refused ") or "permission" in lowered:
        return "gated"
    return "handled"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("port", nargs="?", type=int, default=11435)
    parser.add_argument("--json", dest="output")
    parser.add_argument("--include-stateful", action="store_true")
    parser.add_argument("--root", default="")
    args = parser.parse_args(argv)
    base = "http://127.0.0.1:%d" % args.port
    status, body = _request(base, "GET", "/v1/commands")
    if status != 200:
        print("catalog request failed: HTTP %d %s" % (status, body[:300]))
        return 2
    try:
        catalog = json.loads(body).get("commands") or []
    except (TypeError, ValueError) as error:
        print("catalog response was not JSON: %s" % error)
        return 2

    temporary = None
    if args.root:
        root = str(Path(args.root).resolve())
        temporary = None
        fixture = tempfile.TemporaryDirectory(
            prefix=".sonder-slash-audit-", dir=root
        )
    else:
        # The runtime's default file policy authorizes the checkout, not the
        # OS temp directory.  Keeping the disposable root underneath cwd
        # makes path-bearing probes exercise real handlers instead of merely
        # proving that the policy rejects their input.
        temporary = tempfile.TemporaryDirectory(
            prefix=".sonder-slash-audit-", dir=str(Path.cwd())
        )
        root = temporary.name
        fixture = None
    fixture_root = fixture.name if fixture is not None else root
    Path(fixture_root, "probe.txt").write_text(
        "sonder slash audit\n", encoding="utf-8"
    )
    Path(fixture_root, "data.json").write_text(
        '[{"id": 1, "status": "active"}]\n', encoding="utf-8"
    )

    rows = []
    try:
        for index, row in enumerate(catalog, 1):
            risk = str(row.get("risk") or "unknown")
            if not args.include_stateful and risk != "safe":
                continue
            line = invocation(row, root, args.include_stateful, fixture_root)
            status, response = _request(
                base, "POST", "/v1/chat/completions",
                {"model": "sonder", "stream": False,
                 "messages": [{"role": "user", "content": line}]},
                timeout=90,
            )
            verdict = classify(status, response)
            text = _content(response).strip().replace("\n", " ")
            row_result = {"name": row.get("name"), "risk": risk,
                          "invocation": line, "status": status,
                          "verdict": verdict, "chars": len(text),
                          "preview": text[:180]}
            rows.append(row_result)
            print("[%3d] %-42s %-16s %s" %
                  (index, line[:42], verdict, text[:90]))
    finally:
        if temporary is not None:
            temporary.cleanup()
        if fixture is not None:
            fixture.cleanup()

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
    print("summary:", ", ".join("%s=%d" % item for item in sorted(counts.items())))
    result = {"count": len(rows), "include_stateful": args.include_stateful,
              "results": rows, "summary": counts}
    if args.output:
        Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")
        print("wrote", args.output)
    return 1 if any(row["verdict"] in {"auth_failure", "rate_limited",
                                       "transport_failure", "handler_failure",
                                       "model_fallthrough"} for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
