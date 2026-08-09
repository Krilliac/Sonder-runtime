"""Fail closed when sensitive Git-history debt grows.

The repository has a small, explicitly pinned set of already-public historical
object/path pairs that require a coordinated history rewrite. Normal CI permits
only that exact set so the debt can shrink but cannot grow. Tagged release jobs
use ``--require-clean`` and remain blocked until every flagged pair is
unreachable.

This checker examines names and object identities only.  It never reads or
prints blob contents.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


MAX_OUTPUT_BYTES = 16 * 1024 * 1024
GIT_TIMEOUT_SECONDS = 30
_RAW_CHANGE = re.compile(
    rb"^:([0-7]{6}) ([0-7]{6}) ([0-9a-f]{40,64}) "
    rb"([0-9a-f]{40,64}) ([A-Z][0-9]*)$"
)
_FORMER_PRODUCT_NAME = "trilo" + "bite"

# Exact object/path pairs already reachable before this gate was introduced.
# Deleting any entry is allowed. Adding or changing an object is not.
KNOWN_HISTORY_PRIVACY_DEBT = frozenset({
    (
        "a47e45360ed2eb3e11e1a2700a505cc511b53017",
        "combined_personal.jsonl",
    ),
    (
        "d0ea07e097451999c6b6093ffde826b82dab7b5c",
        "sonder-personal-lora/checkpoints/checkpoint-58/adapter_model.safetensors",
    ),
    (
        "d0ea07e097451999c6b6093ffde826b82dab7b5c",
        f"{_FORMER_PRODUCT_NAME}-personal-lora/checkpoints/checkpoint-58/adapter_model.safetensors",
    ),
    (
        "f6ed8c56f5670e642a64040df3f47fe98577cf73",
        "sonder-personal-lora/adapter_model.safetensors",
    ),
    (
        "f6ed8c56f5670e642a64040df3f47fe98577cf73",
        "sonder-personal-lora/checkpoints/checkpoint-116/adapter_model.safetensors",
    ),
    (
        "f6ed8c56f5670e642a64040df3f47fe98577cf73",
        f"{_FORMER_PRODUCT_NAME}-personal-lora/adapter_model.safetensors",
    ),
    (
        "f6ed8c56f5670e642a64040df3f47fe98577cf73",
        f"{_FORMER_PRODUCT_NAME}-personal-lora/checkpoints/checkpoint-116/adapter_model.safetensors",
    ),
})
_KNOWN_DEBT_OBJECT_IDS = frozenset(
    object_id for object_id, _path in KNOWN_HISTORY_PRIVACY_DEBT
)

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


def _is_sensitive_path(path: str) -> bool:
    normalized = path.replace("\\", "/").strip("/")
    lowered = normalized.casefold()
    basename = lowered.rsplit("/", 1)[-1]
    if basename in _SENSITIVE_BASENAMES:
        return True
    return "personal-lora/" in lowered and lowered.endswith(".safetensors")


def _parse_raw_changes(output: bytes) -> list[tuple[str, str]]:
    objects: list[tuple[str, str]] = []
    fields = output.split(b"\0")
    index = 0
    while index < len(fields):
        header = fields[index].lstrip(b"\r\n")
        index += 1
        if not header:
            continue
        match = _RAW_CHANGE.fullmatch(header)
        if match is None or index >= len(fields):
            raise HistoryPrivacyError("Git history inventory is malformed")
        raw_path = fields[index]
        index += 1
        try:
            path = raw_path.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise HistoryPrivacyError("Git history path is not UTF-8") from exc
        if not path or "\0" in path:
            raise HistoryPrivacyError("Git history path is malformed")
        zero = b"0" * len(match.group(3))
        for object_id in (match.group(3), match.group(4)):
            if object_id != zero:
                objects.append((object_id.decode("ascii"), path))
    return objects


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
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "LC_ALL": "C",
        "LANG": "C",
    })
    inspection_deadline = time.monotonic() + GIT_TIMEOUT_SECONDS

    def run_git(*arguments: str) -> bytes:
        with tempfile.TemporaryFile() as output:
            try:
                process = subprocess.Popen(
                [
                    executable, "--no-pager", "--no-replace-objects", "-C",
                    str(repo), *arguments,
                ],
                cwd=repo,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.DEVNULL,
            )
            except OSError as exc:
                raise HistoryPrivacyError("Git history inspection failed") from exc
            while process.poll() is None:
                if time.monotonic() >= inspection_deadline:
                    process.kill()
                    process.wait()
                    raise HistoryPrivacyError("Git history inspection timed out")
                if output.tell() > MAX_OUTPUT_BYTES:
                    process.kill()
                    process.wait()
                    raise HistoryPrivacyError(
                        "Git history inventory exceeds the safety limit"
                    )
                time.sleep(0.01)
            if process.returncode != 0:
                raise HistoryPrivacyError("Git history inspection failed")
            if time.monotonic() >= inspection_deadline:
                raise HistoryPrivacyError("Git history inspection timed out")
            size = output.tell()
            if size > MAX_OUTPUT_BYTES:
                raise HistoryPrivacyError(
                    "Git history inventory exceeds the safety limit"
                )
            output.seek(0)
            return output.read()

    top_level_raw = run_git("rev-parse", "--show-toplevel")
    try:
        top_level = Path(top_level_raw.decode("utf-8", "strict").strip()).resolve(
            strict=True
        )
    except (UnicodeDecodeError, OSError) as exc:
        raise HistoryPrivacyError("Git repository identity is invalid") from exc
    if os.path.normcase(str(top_level)) != os.path.normcase(str(repo)):
        raise HistoryPrivacyError("repository root is not the exact Git top level")

    shallow = run_git("rev-parse", "--is-shallow-repository")
    if shallow.strip() != b"false":
        raise HistoryPrivacyError("complete Git history is required")

    graft_path_raw = run_git("rev-parse", "--git-path", "info/grafts")
    try:
        graft_path_text = graft_path_raw.decode("utf-8", "strict").strip()
    except UnicodeDecodeError as exc:
        raise HistoryPrivacyError("Git metadata path is invalid") from exc
    if not graft_path_text or "\0" in graft_path_text:
        raise HistoryPrivacyError("Git metadata path is invalid")
    graft_path = Path(graft_path_text)
    if not graft_path.is_absolute():
        graft_path = repo / graft_path
    try:
        if graft_path.is_file() and graft_path.stat().st_size:
            raise HistoryPrivacyError("Git grafts may not replace history")
    except OSError as exc:
        raise HistoryPrivacyError("Git graft state could not be inspected") from exc

    output = run_git(
        "-c", "core.quotePath=false", "log", "--all", "--raw",
        "--format=format:", "--no-renames", "--no-abbrev", "-z",
    )
    return _parse_raw_changes(output)


def evaluate(objects: list[tuple[str, str]]) -> dict[str, object]:
    flagged = {
        (object_id, path)
        for object_id, path in objects
        if _is_sensitive_path(path) or object_id in _KNOWN_DEBT_OBJECT_IDS
    }
    known = flagged & KNOWN_HISTORY_PRIVACY_DEBT
    unexpected = flagged - KNOWN_HISTORY_PRIVACY_DEBT
    removed = KNOWN_HISTORY_PRIVACY_DEBT - flagged
    return {
        "schema": 1,
        "ok": not unexpected,
        "clean": not flagged,
        "known_debt_count": len(known),
        "unexpected_count": len(unexpected),
        "removed_from_baseline_count": len(removed),
        "known_object_ids": sorted({
            object_id[:12] for object_id, _path in known
        }),
        "unexpected": [
            {"object_id": object_id[:12], "path": path}
            for object_id, path in sorted(unexpected)
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
                f"({report['known_debt_count']} object/path pair(s)); "
                "release remains blocked"
            )
    else:
        print("Git history privacy: FAILED", file=sys.stderr)
        if report.get("error"):
            print(report["error"], file=sys.stderr)
        elif report.get("unexpected"):
            for row in report["unexpected"]:
                print(
                    "unexpected sensitive object %s %s"
                    % (row["object_id"], json.dumps(row["path"])),
                    file=sys.stderr,
                )
        elif args.require_clean:
            print(
                f"{report.get('known_debt_count', 0)} known sensitive "
                "object/path pair(s) "
                "remain reachable",
                file=sys.stderr,
            )
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
