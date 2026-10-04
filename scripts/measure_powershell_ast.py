#!/usr/bin/env python3
"""Collect and measure PowerShell command strings without executing them.

The collector is deliberately conservative: it reads tracked text files, extracts
PowerShell fenced blocks/inline invocations and likely command-bearing literals,
then writes one deduplicated JSON object per command.  It never invokes a
collected command.  ``--measure`` optionally imports the current permission gate
and an inspector supplied by the implementation branch to report before/after
classification counts.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

PS_WORD = re.compile(r"(?i)(?:^|[\s'\"`])(?:pwsh|powershell)(?:\.exe)?(?:\s|$)")
PS_FLAG = re.compile(r"(?i)(?:^|[\s'\"`])-(?:enc(?:odedcommand)?)\b")
PS_CMDLET = re.compile(r"(?i)\b(?:Get|Set|Test|New|Remove|Start|Stop|Invoke|Add|Write|Where|ForEach|Measure|Select|Convert|Import|Export|Out|Join|Split|Clear|Copy|Move|Register|Unregister|Update|Get)-[A-Za-z][A-Za-z0-9-]*\b")
PS_COMMAND_START = re.compile(r"(?is)^\s*(?:pwsh|powershell)(?:\.exe)?\b|^\s*(?:Get|Set|Test|New|Remove|Start|Stop|Invoke|Add|Write|Where|ForEach|Measure|Select|Convert|Import|Export|Out|Join|Split|Clear|Copy|Move|Register|Unregister|Update)-[A-Za-z][A-Za-z0-9-]*\b")
INLINE = re.compile(r"(?i)(?:\b(?:pwsh|powershell)(?:\.exe)?\s+[^\r\n]+|\b(?:Get|Set|Test|New|Remove|Start|Stop|Invoke|Add|Write|Where|ForEach|Measure|Select|Convert|Import|Export|Out|Join|Split|Clear|Copy|Move|Register|Unregister|Update)-[A-Za-z][A-Za-z0-9-]*(?:\s+[^\r\n`]+)?)")
FENCE = re.compile(r"(?is)```(?:powershell|pwsh|ps1)\s*\n(.*?)```")
COMMON = (
    "Get-ChildItem", "Test-Path .", "git status --short", "git diff --check",
    "python -V", "python -m pytest --collect-only",
)


def _display_path(path: Path, repo: Path) -> str:
    """Repository-relative path for the persisted summary (no local roots)."""
    try:
        return Path(path).resolve().relative_to(Path(repo).resolve()).as_posix()
    except ValueError:
        return str(path)


def _tracked(repo: Path) -> list[Path]:
    out = subprocess.check_output(["git", "-C", str(repo), "ls-files", "-z"], text=False)
    return [repo / x for x in out.decode("utf-8", "replace").split("\0") if x]


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _candidate(value: str) -> bool:
    value = value.strip()
    return bool(value and (PS_WORD.search(value) or PS_FLAG.search(value) or PS_CMDLET.search(value)))


def _add(rows: dict[str, dict[str, Any]], command: str, path: Path, line: int, method: str, repo: Path, *, force: bool = False) -> None:
    command = command.strip().replace("\r\n", "\n")
    if not command or len(command) > 1_000_000 or (not force and not _candidate(command)):
        return
    key = command
    rows.setdefault(key, {"command": command, "kind": "command" if PS_COMMAND_START.search(command) else "candidate", "sources": [], "methods": []})
    source = {"path": str(path.relative_to(repo)).replace("\\", "/"), "line": line, "method": method}
    if source not in rows[key]["sources"]:
        rows[key]["sources"].append(source)
    if method not in rows[key]["methods"]:
        rows[key]["methods"].append(method)


def _python_literals(text: str) -> Iterable[tuple[str, int]]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, TypeError):
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and _candidate(node.value):
            yield node.value, getattr(node, "lineno", 1)
        elif isinstance(node, (ast.List, ast.Tuple)):
            parts = [item.value for item in node.elts if isinstance(item, ast.Constant) and isinstance(item.value, str)]
            if parts and any(PS_WORD.search(part) for part in parts):
                yield " ".join(parts), getattr(node, "lineno", 1)


def collect(repo: Path) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for command in COMMON:
        _add(rows, command, repo / "<common-benign>", 1, "common-benign-control", repo, force=True)
    for path in _tracked(repo):
        if not path.is_file() or path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".whl", ".zip", ".pyd", ".dll", ".exe"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        suffix = path.suffix.lower()
        if suffix == ".ps1":
            _add(rows, text, path, 1, "ps1-source", repo, force=True)
        for match in FENCE.finditer(text):
            _add(rows, match.group(1), path, _line(text, match.start(1)), "markdown-powershell-fence", repo)
        for match in INLINE.finditer(text):
            _add(rows, match.group(0), path, _line(text, match.start()), "text-inline", repo)
        if suffix == ".py":
            for value, line in _python_literals(text):
                _add(rows, value, path, line, "python-string-literal", repo)
        if suffix in {".json", ".jsonl", ".yaml", ".yml", ".toml"}:
            # JSON literals are often tool defaults; line-oriented fallback also
            # preserves provenance for JSONL and malformed/generated fragments.
            try:
                values = json.loads(text) if suffix == ".json" else None
            except (ValueError, TypeError):
                values = None
            if values is not None:
                stack = [values]
                while stack:
                    value = stack.pop()
                    if isinstance(value, str) and _candidate(value):
                        _add(rows, value, path, 1, "structured-string", repo)
                    elif isinstance(value, dict):
                        stack.extend(value.values())
                    elif isinstance(value, list):
                        stack.extend(value)
        for number, line in enumerate(text.splitlines(), 1):
            if _candidate(line) and (PS_WORD.search(line) or PS_FLAG.search(line)):
                _add(rows, line, path, number, "text-command-line", repo)
    return sorted(rows.values(), key=lambda row: row["command"].casefold())


def builtin_controls() -> list[dict[str, str]]:
    """Capture all 339 catalog defaults without invoking tools.

    This mirrors ``permission_modes.risk_of``'s documented precedence directly
    because calling the legacy compatibility wrapper once per tool re-imports
    the root catalog path and is prohibitively slow on this checkout.
    """
    try:
        import permission_modes
        from sonder_runtime.adapters.command_catalog import command_catalog
        rows = []
        for command in command_catalog.catalog():
            risk = command.risk
            if risk == "dangerous":
                effective = "dangerous"
            elif command.tool in permission_modes.EXECUTION_TOOLS or command.tool in permission_modes.EXECUTION_COMMANDS or command.tool in permission_modes.NATIVE_EXECUTION_TOOLS:
                effective = "execution"
            else:
                effective = risk or permission_modes.UNCLASSIFIED
            rows.append({"tool": command.tool, "risk_before": effective})
        return rows
    except Exception as exc:
        return [{"error": "permission_modes unavailable: %s" % exc}]


def measure_gate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Measure corpus and every catalog tool through the real permission gate."""
    import importlib
    import permission_modes
    gate = importlib.import_module("sonder_runtime.adapters.security.powershell_gate")
    no_rule = lambda _name: None
    baseline = permission_modes.decide("run_code", interactive=True, mode="auto", record=False,
                                      rule_lookup=no_rule, arguments=None)
    control_tools = [item["tool"] for item in builtin_controls() if "tool" in item]
    original = gate.inspect_tool_call
    gate.inspect_tool_call = lambda _name, _arguments: None
    try:
        controls_before = [permission_modes.decide(tool, interactive=True, mode="auto",
                                                   record=False, rule_lookup=no_rule, arguments={})
                           for tool in control_tools]
    finally:
        gate.inspect_tool_call = original
    controls_after = [permission_modes.decide(tool, interactive=True, mode="auto",
                                              record=False, rule_lookup=no_rule, arguments={})
                      for tool in control_tools]
    verdicts, batch_stats = _batch_inspect([row["command"] for row in rows])
    original_parser = gate.inspect_powershell
    gate.inspect_powershell = lambda source: verdicts[source]
    try:
        for row in rows:
            decision = permission_modes.decide("run_code", interactive=True, mode="auto", record=False,
                                               rule_lookup=no_rule,
                                               arguments={"language": "powershell", "code": row["command"]})
            row["measurement"] = {
                "inspectable": verdicts[row["command"]].inspectable,
                "baseline_risk": baseline.risk, "baseline_action": baseline.action,
                "after_risk": decision.risk, "after_action": decision.action,
                "after_reason": decision.reason,
            }
    finally:
        gate.inspect_powershell = original_parser
    controls_diff = sum((before.risk, before.action) != (after.risk, after.action)
                        for before, after in zip(controls_before, controls_after, strict=True))
    return {"baseline_run_code": {"risk": baseline.risk, "action": baseline.action},
            "builtin_controls": len(controls_after), "builtin_changed": controls_diff,
            "generator_initial_approval_prompts": {
                tool: {"before": int(before.action == permission_modes.ASK),
                       "after": int(after.action == permission_modes.ASK)}
                for tool, before, after in zip(control_tools, controls_before, controls_after, strict=True)
                if tool in {"parallel_generate_run_languages", "campaign_generate_compile_execute_record"}
            },
            "corpus_changed": sum(row["measurement"]["baseline_risk"] != row["measurement"]["after_risk"]
                                   or row["measurement"]["baseline_action"] != row["measurement"]["after_action"]
                                   for row in rows),
            "fully_inspectable_changed": sum(row["measurement"]["inspectable"] and
                                              (row["measurement"]["baseline_risk"] != row["measurement"]["after_risk"]
                                               or row["measurement"]["baseline_action"] != row["measurement"]["after_action"])
                                              for row in rows),
            "commands_executed": 0, "native_batch": batch_stats,
            "reason_counts": _reason_counts(rows),
            "benign_controls": {row["command"]: row["measurement"] for row in rows
                                if any(source["path"] == "<common-benign>" for source in row["sources"])} }


def _batch_inspect(sources: list[str]) -> tuple[dict[str, object], dict[str, int]]:
    """Parse up to 16 sources per static PowerShell helper invocation."""
    from sonder_runtime.adapters.security.powershell_ast import (
        PowerShellInspection, _HELPER, _powershell_environment, _powershell_executable,
    )
    executable = _powershell_executable()
    if not executable:
        return {source: PowerShellInspection(False, "PowerShell parser unavailable") for source in sources}, {"batches": 0, "fallbacks": 0, "timeouts": 0}
    marker = "$request = [Console]::In.ReadToEnd() | ConvertFrom-Json"
    if _HELPER.count(marker) != 1 or _HELPER.count("exit 0") != 1:
        raise RuntimeError("unexpected static PowerShell helper shape")
    helper = ('$requests = [Console]::In.ReadToEnd() | ConvertFrom-Json\n'
              'foreach ($request in @($requests)) {\n & {\n' +
              _HELPER.replace(marker, '').replace("exit 0", '') +
              '\n }\n [Console]::Out.Write("`n")\n}\n')
    result: dict[str, object] = {}
    stats = {"batches": 0, "fallbacks": 0, "timeouts": 0}
    for start in range(0, len(sources), 16):
        batch = sources[start:start + 16]
        stats["batches"] += 1
        try:
            completed = subprocess.run(
                [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", helper],
                input=json.dumps([{"source": source} for source in batch], ensure_ascii=True),
                capture_output=True, text=True, encoding="utf-8", timeout=10,
                env=_powershell_environment(), check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            lines = [line for line in completed.stdout.splitlines() if line.strip()]
            parsed = [json.loads(line) for line in lines]
            if completed.returncode != 0 or len(parsed) != len(batch):
                raise ValueError("batch protocol mismatch")
            for source, item in zip(batch, parsed, strict=True):
                if not isinstance(item, dict) or not isinstance(item.get("inspectable"), bool) or not isinstance(item.get("reason"), str):
                    raise ValueError("malformed batch verdict")
                result[source] = PowerShellInspection(item["inspectable"], item["reason"])
        except subprocess.TimeoutExpired:
            stats["timeouts"] += 1
            for source in batch:
                stats["fallbacks"] += 1
                from sonder_runtime.adapters.security.powershell_ast import inspect_powershell
                result[source] = inspect_powershell(source)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            stats["fallbacks"] += 1
            for source in batch:
                # Individual fallback preserves fail-closed semantics and is
                # only used when the batch protocol itself failed.
                from sonder_runtime.adapters.security.powershell_ast import inspect_powershell
                result[source] = inspect_powershell(source)
    return result, stats


def _reason_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        reason = row["measurement"]["after_reason"]
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--out", type=Path, default=None,
        help="corpus JSONL destination (defaults to the ignored architecture corpus; its .summary.json remains trackable)",
    )
    parser.add_argument("--measure", action="store_true", help="include baseline/control metadata; never executes commands")
    parser.add_argument("--controls-only", action="store_true", help="measure all catalog controls without parsing corpus rows")
    args = parser.parse_args()
    repo = args.repo.resolve()
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    rows = collect(repo)
    if args.controls_only:
        print(json.dumps(measure_gate([]), sort_keys=True))
        return 0
    # Keep the large, reproducible corpus at the conventional architecture path.
    # .gitignore excludes this JSONL while the compact sibling summary remains
    # available for review and tracking when --measure is used.
    out = args.out or repo / "docs" / "architecture" / "powershell-ast-corpus.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    summary = None
    if args.measure:
        summary = measure_gate(rows)
    with out.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    methods = {}
    for row in rows:
        for method in row["methods"]:
            methods[method] = methods.get(method, 0) + 1
    report = {"corpus": _display_path(out, repo), "unique_candidates": len(rows),
                      "kind_counts": {kind: sum(row["kind"] == kind for row in rows) for kind in ("command", "candidate")},
                      "extraction_counts": methods, "tracked_files_scanned": len(_tracked(repo)),
                      "commands_executed": 0, "builtin_controls": len(builtin_controls()),
                      "gate_measurement": summary}
    if args.measure:
        from sonder_runtime.adapters.security.powershell_ast import _HELPER
        report["helper_sha256"] = hashlib.sha256(_HELPER.encode("utf-8")).hexdigest()
        report["method"] = "Native AST batches followed by the real permission decider; no candidate execution."
        out.with_suffix(".summary.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
