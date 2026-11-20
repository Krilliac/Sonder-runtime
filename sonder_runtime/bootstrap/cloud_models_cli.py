"""``python -m sonder_runtime models cloud-refresh`` -- the operator's cloud model refresh.

``cloud-refresh``  probe every cloud model Sonder knows with Ollama
                   ``/api/show`` (metadata only, never a prompt), retire the
                   ones the provider retired, record the ollama.com catalog
                   (inert: nothing is bound by it) and rebind a retired tier
                   default only to an entry of the operator ladder in
                   ``cloud_model_preferences.json``.  A dry run unless
                   ``--apply``; ``--no-catalog`` skips the public catalog.

Exit status: 0 ok (dry run or apply), 1 aborted / refused / busy, 2 usage.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from typing import Any

from ..adapters.cloud_model_refresh import run_refresh


def add_parser(
    sub: Any,
    *,
    common: Callable[..., None],
    func: Callable[[argparse.Namespace], int],
) -> None:
    """Add ``models`` with its required ``cloud-refresh`` sub-command to ``sub``."""
    parser = sub.add_parser("models", help="model-list maintenance: refresh Ollama cloud models")
    commands = parser.add_subparsers(dest="models_command", required=True)
    refresh = commands.add_parser(
        "cloud-refresh",
        help="probe known Ollama cloud models; retire, record the catalog, rebind from the"
        " operator ladder (dry run unless --apply)",
    )
    common(refresh)
    refresh.add_argument(
        "--apply", action="store_true",
        help="write the overlay, state file and change log (default: dry run, which writes"
        " none of them; it still takes the run lock file)",
    )
    refresh.add_argument(
        "--no-catalog", action="store_true", help="do not read the public ollama.com catalog",
    )
    refresh.set_defaults(func=func)


def _list(values: Any) -> str:
    items = [str(value) for value in values]
    return ", ".join(items) if items else "none"


def _retirement_lines(report: dict[str, Any]) -> list[str]:
    lines = []
    for row in report.get("retirements") or []:
        suggestion = row.get("suggestion")
        lines.append("retired: %s (provider %s, retired on %s)%s" % (
            row.get("model"), row.get("provider_name"), row.get("retired_on"),
            "; provider suggests %s" % suggestion if suggestion else ""))
    return lines


def _decision_lines(report: dict[str, Any]) -> list[str]:
    lines = []
    for decision in report.get("decisions") or []:
        tier, ladder = decision.get("tier"), decision.get("ladder") or []
        entries = _list("%s (%s)" % (e.get("model"), e.get("state")) for e in ladder)
        if decision.get("action") == "rebind":
            index = next((i for i, e in enumerate(ladder) if e.get("state") == "selected"), -1)
            lines.append("default_cloud_models.%s: %s -> %s (ladder #%d)" % (
                tier, decision.get("effective_model"), decision.get("new_model"), index + 1))
        elif decision.get("action") == "needs_operator_choice":
            suggestion = decision.get("suggestion")
            lines.append("needs operator choice: tier %s: %s retired%s; ladder: %s" % (
                tier, decision.get("effective_model"),
                "; provider suggests %s" % suggestion if suggestion else "", entries))
        else:
            lines.append("keep: tier %s: %s (%s)" % (
                tier, decision.get("effective_model"), decision.get("effective_source")))
        note = str(decision.get("note") or "")
        lines.extend(part for part in note.split("; ") if part.startswith("env "))
    return lines


def format_report(report: dict[str, Any]) -> str:
    """Human-readable report, one fact per line."""
    lines = ["cloud model refresh (%s): %s%s" % (
        report.get("mode"), report.get("outcome"),
        " (%s)" % report["reason"] if report.get("reason") else "")]
    lines.append("probed: %d; unprobed (budget): %d" % (
        int(report.get("probed") or 0), len(report.get("unprobed") or [])))
    lines.extend(_retirement_lines(report))
    lines.extend(_decision_lines(report))
    for row in report.get("pulls") or []:
        lines.append("pull %s: %s" % (row.get("model"), row.get("outcome")))
    catalog = report.get("catalog") or {}
    lines.append("catalog: %s; families %s, new families %d, new tags %d (inert, nothing bound)" % (
        catalog.get("status"), catalog.get("families", 0),
        len(catalog.get("new_families") or []), len(catalog.get("new_tags") or [])))
    for key in ("preferences_error", "state_error"):
        if report.get(key):
            lines.append("%s: %s" % (key.replace("_", " "), report[key]))
    for key in report.get("changed_concurrently") or []:
        lines.append("skipped %s: changed concurrently" % key)
    if report.get("mode") == "dry_run":
        if report.get("outcome") == "ok":
            lines.append("dry run: nothing written; re-run with --apply")
    else:
        for row in report.get("files_written") or []:
            lines.append("wrote %s%s" % (
                row.get("file"), " (backup %s)" % row["backup"] if row.get("backup") else ""))
        if not report.get("files_written"):
            lines.append("nothing written")
        lines.append("live effect: %s" % report.get("live_effect"))
    return "\n".join(lines)


def run_cloud_refresh(
    args: argparse.Namespace, *, origin: str, home: str | os.PathLike[str] | None
) -> int:
    """Run the parsed ``models cloud-refresh`` command and print its report."""
    if getattr(args, "models_command", None) != "cloud-refresh":
        print("usage: python -m sonder_runtime models cloud-refresh [--apply] [--json]",
              file=sys.stderr)
        return 2
    report = run_refresh(
        origin=origin, apply=bool(getattr(args, "apply", False)), trigger="cli", home=home,
        use_catalog=False if getattr(args, "no_catalog", False) else None,
    )
    if getattr(args, "json", False):
        print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        print(format_report(report))
    return 0 if report.get("outcome") == "ok" else 1


__all__ = ["add_parser", "format_report", "run_cloud_refresh"]
