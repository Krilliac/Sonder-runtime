"""Fail closed when sensitive Git-history debt grows.

The repository has a small, explicitly pinned set of already-public historical
objects that require a coordinated history rewrite.  Normal CI permits only
that exact object set so the debt can shrink but cannot grow.  Tagged release
jobs use ``--require-clean`` and remain blocked until every flagged object is
unreachable.

This checker examines names and object identities only.  It never reads or
prints blob contents.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


MAX_OUTPUT_BYTES = 16 * 1024 * 1024
GIT_TIMEOUT_SECONDS = 30
_OBJECT_LINE = re.compile(r"^([0-9a-f]{40,64})(?: (.*))?$")

# Exact object identities already reachable before this gate was introduced.
# Deleting any entry is allowed. Adding or changing an object is not.
KNOWN_HISTORY_PRIVACY_DEBT = frozenset({
    "a47e45360ed2eb3e11e1a2700a505cc511b53017",
    "d0ea07e097451999c6b6093ffde826b82dab7b5c",
    "f6ed8c56f5670e642a64040df3f47fe98577cf73",
})

_SENSITIVE_BASENAMES = frozenset({
    ".credentials.json",
    ".env",
    "combined_personal.jsonl",
    "credentials.json",
    "id_ed25519",
    "id_rsa",
})


class HistoryPrivacyError(RuntimeError):
    """History could not be inspected completely and safely."""


def _decode_git_path(raw: str) -> str:
    if raw.startswith('"'):
        try:
            value = ast.literal_eval(raw)
        except (SyntaxError, ValueError) as exc:
            raise HistoryPrivacyError("Git returned a malformed quoted path") from exc
        if not isinstance(value, str):
            raise HistoryPrivacyError("Git returned a non-text path")
        return value
    return raw


def _is_sensitive_path(path: str) -> bool:
    normalized = path.replace("\\", "/").strip("/")
    lowered = normalized.casefold()
    basename = lowered.rsplit("/", 1)[-1]
    if basename in _SENSITIVE_BASENAMES:
        return True
    return "personal-lora/" in lowered and lowered.endswith(".safetensors")


def _git_objects(repo: Path) -> list[tuple[str, str]]:
    executable = shutil.which("git")
    if not executable:
        raise HistoryPrivacyError("Git executable is unavailable")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
        and key.upper() in {"PATH", "SYSTEMROOT", "WINDIR"}
    }
    environment.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "LC_ALL": "C",
        "LANG": "C",
    })

    def run_git(*arguments: str) -> bytes:
        try:
            process = subprocess.run(
                [
                    executable, "--no-pager", "--no-replace-objects", "-C",
                    str(repo), *arguments,
                ],
                cwd=repo,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=GIT_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HistoryPrivacyError("Git history inspection failed") from exc
        if process.returncode != 0:
            raise HistoryPrivacyError("Git history inspection failed")
        if len(process.stdout) > MAX_OUTPUT_BYTES:
            raise HistoryPrivacyError("Git history inventory exceeds the safety limit")
        return process.stdout

    top_level_raw = run_git("rev-parse", "--show-toplevel")
    try:
        top_level = Path(top_level_raw.decode("utf-8", "strict").strip()).resolve(
            strict=True
        )
    except (UnicodeDecodeError, OSError) as exc:
        raise HistoryPrivacyError("Git repository identity is invalid") from exc
    if os.path.normcase(str(top_level)) != os.path.normcase(str(repo)):
        raise HistoryPrivacyError("repository root is not the exact Git top level")

    output = run_git("-c", "core.quotePath=true", "rev-list", "--objects", "--all")
    try:
        text = output.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise HistoryPrivacyError("Git history inventory is not UTF-8") from exc

    objects: list[tuple[str, str]] = []
    for line in text.splitlines():
        match = _OBJECT_LINE.fullmatch(line)
        if match is None:
            raise HistoryPrivacyError("Git history inventory is malformed")
        raw_path = match.group(2)
        if raw_path is None:
            continue
        objects.append((match.group(1), _decode_git_path(raw_path)))
    return objects


def evaluate(objects: list[tuple[str, str]]) -> dict[str, object]:
    flagged = {
        object_id: path
        for object_id, path in objects
        if _is_sensitive_path(path)
    }
    observed = set(flagged)
    known = observed & KNOWN_HISTORY_PRIVACY_DEBT
    unexpected = observed - KNOWN_HISTORY_PRIVACY_DEBT
    removed = KNOWN_HISTORY_PRIVACY_DEBT - observed
    return {
        "schema": 1,
        "ok": not unexpected,
        "clean": not observed,
        "known_debt_count": len(known),
        "unexpected_count": len(unexpected),
        "removed_from_baseline_count": len(removed),
        "known_object_ids": sorted(object_id[:12] for object_id in known),
        "unexpected": [
            {"object_id": object_id[:12], "path": flagged[object_id]}
            for object_id in sorted(unexpected)
        ],
    }


def inspect(repo: Path) -> dict[str, object]:
    resolved = repo.resolve(strict=True)
    if not (resolved / ".git").exists():
        # Worktrees use a .git file, ordinary repositories use a directory.
        raise HistoryPrivacyError("repository root does not contain Git metadata")
    return evaluate(_git_objects(resolved))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--require-clean", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = inspect(args.repo)
    except (HistoryPrivacyError, OSError) as exc:
        report = {"schema": 1, "ok": False, "clean": False, "error": str(exc)}

    success = bool(report.get("ok")) and (
        not args.require_clean or bool(report.get("clean"))
    )
    report["require_clean"] = args.require_clean
    report["passed"] = success
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    elif success:
        if report.get("clean"):
            print("Git history privacy: clean")
        else:
            print(
                "Git history privacy: known debt only "
                f"({report['known_debt_count']} object(s)); release remains blocked"
            )
    else:
        print("Git history privacy: FAILED", file=sys.stderr)
        if report.get("error"):
            print(report["error"], file=sys.stderr)
        elif report.get("unexpected"):
            for row in report["unexpected"]:
                print(
                    f"unexpected sensitive object {row['object_id']} {row['path']}",
                    file=sys.stderr,
                )
        elif args.require_clean:
            print(
                f"{report.get('known_debt_count', 0)} known sensitive object(s) "
                "remain reachable",
                file=sys.stderr,
            )
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
