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
from collections.abc import Iterable, Iterator
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Protocol


# Bounds total streamed inventory against a runaway or hostile repository, not
# normal growth.  The full raw inventory of origin/main was 12.6 MiB on
# 2026-11-26 (3,501 commits, ~98k raw entries) and a developer clone's
# `log --all` across 1,388 refs was 18.9 MiB (6,418 commits, ~148k raw entries).
# Because the inventory is streamed in bounded chunks directly into evaluation,
# raising the total stream ceiling to 256 MiB provides years of headroom while
# keeping Python heap usage O(1).
MAX_OUTPUT_BYTES = 256 * 1024 * 1024
STREAM_CHUNK_BYTES = 64 * 1024
# Maximum bytes buffered for a single NUL-delimited header or path token.
# Prevents a hostile repository omitting NUL separators from growing RAM.
MAX_RECORD_BYTES = 64 * 1024
# Bounds captured stderr temp-file growth on a failing or hostile git process.
MAX_STDERR_BYTES = 1024 * 1024
GIT_TIMEOUT_SECONDS = 30
# How much of git's own stderr may be quoted back in an error. Bounded so a
# failing git cannot pump unbounded text into an operator-facing report.
GIT_STDERR_QUOTE_BYTES = 400
# The exact object/path baseline was pinned by this commit. It is an audit
# anchor for the debt-aware gate: the seven entries below may disappear as
# history is cleaned, but a new entry must never be added silently.
HISTORY_PRIVACY_GATE_REVISION = (
    "655b75ec9950e03c0a0c4701a18f8e04b0dea51c"
)
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


class _ErrorStream(Protocol):
    def seek(self, offset: int, whence: int = 0, /) -> int: ...
    def read(self, size: int = -1, /) -> bytes: ...


def _is_sensitive_path(path: str) -> bool:
    normalized = path.replace("\\", "/").strip("/")
    lowered = normalized.casefold()
    basename = lowered.rsplit("/", 1)[-1]
    if basename in _SENSITIVE_BASENAMES:
        return True
    return "personal-lora/" in lowered and lowered.endswith(".safetensors")


def _iter_raw_changes(
    chunks: Iterable[bytes],
    *,
    max_record_bytes: int = MAX_RECORD_BYTES,
) -> Iterator[tuple[str, str]]:
    """Incrementally parse NUL-delimited ``git log --raw -m -z`` chunks."""
    pending = b""
    pending_match: re.Match[bytes] | None = None
    for chunk in chunks:
        if not chunk:
            continue
        buf = pending + chunk
        pos = 0
        while True:
            nul = buf.find(b"\0", pos)
            if nul == -1:
                break
            field = buf[pos:nul]
            pos = nul + 1
            if len(field) > max_record_bytes:
                raise HistoryPrivacyError(
                    "Git history inventory record exceeds the safety limit"
                )
            if pending_match is None:
                header = field.lstrip(b"\r\n")
                if not header:
                    continue
                match = _RAW_CHANGE.fullmatch(header)
                if match is None:
                    raise HistoryPrivacyError("Git history inventory is malformed")
                pending_match = match
            else:
                try:
                    path = field.decode("utf-8", "strict")
                except UnicodeDecodeError as exc:
                    raise HistoryPrivacyError("Git history path is not UTF-8") from exc
                if not path or "\0" in path:
                    raise HistoryPrivacyError("Git history path is malformed")
                zero = b"0" * len(pending_match.group(3))
                for object_id in (pending_match.group(3), pending_match.group(4)):
                    if object_id != zero:
                        yield (object_id.decode("ascii"), path)
                pending_match = None
        pending = buf[pos:]
        if len(pending) > max_record_bytes:
            raise HistoryPrivacyError(
                "Git history inventory record exceeds the safety limit"
            )
    if pending_match is not None or pending.lstrip(b"\r\n"):
        raise HistoryPrivacyError("Git history inventory is malformed")


def _parse_raw_changes(output: bytes) -> list[tuple[str, str]]:
    return list(_iter_raw_changes((output,)))


def _iter_git_objects(repo: Path) -> Iterator[tuple[str, str]]:
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

    def _git_reason(errors: _ErrorStream) -> str:
        """A trimmed, single-line quote of git's own complaint, or ''.

        Bounded and newline-free on purpose: this string is surfaced in a JSON
        report an operator reads, and git can emit multi-paragraph advice.
        """
        try:
            errors.seek(0)
            raw = errors.read(GIT_STDERR_QUOTE_BYTES)
        except OSError:
            return ""
        text = raw.decode("utf-8", "replace").strip()
        if not text:
            return ""
        first = text.splitlines()[0].strip()
        return first[:GIT_STDERR_QUOTE_BYTES]

    def run_git(*arguments: str) -> bytes:
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            try:
                process = subprocess.Popen(
                    [
                        executable, "-c", "safe.directory=%s" % repo,
                        "--no-pager", "--no-replace-objects", "-C",
                        str(repo), *arguments,
                    ],
                    cwd=repo,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=errors,
                )
            except OSError as exc:
                raise HistoryPrivacyError(
                    "Git history inspection failed: %s" % exc
                ) from exc
            while process.poll() is None:
                if time.monotonic() >= inspection_deadline:
                    process.kill()
                    process.wait()
                    raise HistoryPrivacyError("Git history inspection timed out")
                # Measure the size, never the position. Git writes through a
                # handle that shares this file's position, and on Windows a
                # tell() racing git's writes can rewind it: the next chunk
                # overwrites the last and the inventory loses 4 KiB. Measured
                # on this repository: 12 of 80 captures damaged, 4 of which
                # still parsed, so the gate passed on a partial inventory.
                if os.fstat(output.fileno()).st_size > MAX_OUTPUT_BYTES:
                    process.kill()
                    process.wait()
                    raise HistoryPrivacyError(
                        "Git history inventory exceeds the safety limit"
                    )
                time.sleep(0.01)
            if process.returncode != 0:
                reason = _git_reason(errors)
                raise HistoryPrivacyError(
                    "Git history inspection failed (git %s exited %d)%s"
                    % (
                        arguments[0],
                        process.returncode,
                        ": " + reason if reason else "",
                    )
                )
            if time.monotonic() >= inspection_deadline:
                raise HistoryPrivacyError("Git history inspection timed out")
            size = os.fstat(output.fileno()).st_size
            if size > MAX_OUTPUT_BYTES:
                raise HistoryPrivacyError(
                    "Git history inventory exceeds the safety limit"
                )
            output.seek(0)
            return output.read()

    def stream_git_chunks(*arguments: str) -> Iterator[bytes]:
        remaining = inspection_deadline - time.monotonic()
        if remaining <= 0:
            raise HistoryPrivacyError("Git history inspection timed out")
        git_subcommand = "log" if "log" in arguments else arguments[0]
        with tempfile.TemporaryFile() as errors:
            try:
                process = subprocess.Popen(
                    [
                        executable, "-c", "safe.directory=%s" % repo,
                        "--no-pager", "--no-replace-objects", "-C",
                        str(repo), *arguments,
                    ],
                    cwd=repo,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=errors,
                )
            except OSError as exc:
                raise HistoryPrivacyError(
                    "Git history inspection failed: %s" % exc
                ) from exc

            timed_out = threading.Event()

            def _kill_on_timeout() -> None:
                timed_out.set()
                try:
                    process.kill()
                except OSError:
                    pass

            timer = threading.Timer(remaining, _kill_on_timeout)
            timer.daemon = True
            timer.start()
            aborted = True
            total_bytes = 0
            try:
                stdout = process.stdout
                if stdout is None:
                    raise HistoryPrivacyError("Git history stream is unavailable")
                read_chunk = getattr(stdout, "read1", stdout.read)
                chunk_size = min(STREAM_CHUNK_BYTES, max(1, MAX_OUTPUT_BYTES + 1))
                while True:
                    chunk = read_chunk(chunk_size)
                    if timed_out.is_set() or time.monotonic() >= inspection_deadline:
                        raise HistoryPrivacyError("Git history inspection timed out")
                    if os.fstat(errors.fileno()).st_size > MAX_STDERR_BYTES:
                        raise HistoryPrivacyError(
                            "Git history stderr exceeds the safety limit"
                        )
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    if total_bytes > MAX_OUTPUT_BYTES:
                        raise HistoryPrivacyError(
                            "Git history inventory exceeds the safety limit"
                        )
                    yield chunk
                aborted = False
            finally:
                try:
                    if process.stdout is not None:
                        try:
                            process.stdout.close()
                        except OSError:
                            pass
                    if aborted:
                        if process.returncode is None:
                            try:
                                process.kill()
                            except OSError:
                                pass
                        process.wait()
                    else:
                        wait_remaining = max(0.0, inspection_deadline - time.monotonic())
                        try:
                            process.wait(timeout=wait_remaining)
                        except subprocess.TimeoutExpired:
                            timed_out.set()
                            try:
                                process.kill()
                            except OSError:
                                pass
                            process.wait()
                finally:
                    timer.cancel()

            if timed_out.is_set() or time.monotonic() >= inspection_deadline:
                raise HistoryPrivacyError("Git history inspection timed out")
            if process.returncode != 0:
                reason = _git_reason(errors)
                raise HistoryPrivacyError(
                    "Git history inspection failed (git %s exited %d)%s"
                    % (
                        git_subcommand,
                        process.returncode,
                        ": " + reason if reason else "",
                    )
                )

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

    return _iter_raw_changes(
        stream_git_chunks(
            "-c", "core.quotePath=false", "log", "--all", "--raw",
            "-m", "--format=format:", "--no-renames", "--no-abbrev", "-z",
        )
    )


def _git_objects(repo: Path) -> list[tuple[str, str]]:
    return list(_iter_git_objects(repo))


def evaluate(objects: Iterable[tuple[str, str]]) -> dict[str, object]:
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
        "baseline_revision": HISTORY_PRIVACY_GATE_REVISION,
        "baseline_entry_count": len(KNOWN_HISTORY_PRIVACY_DEBT),
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
    return evaluate(_iter_git_objects(resolved))


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
        unexpected_rows = report.get("unexpected")
        if report.get("error"):
            print(report["error"], file=sys.stderr)
        elif isinstance(unexpected_rows, list) and unexpected_rows:
            for row in unexpected_rows:
                print(
                    "unexpected sensitive object %s %s"
                    % (row["object_id"], json.dumps(row["path"])),
                    file=sys.stderr,
                )
        elif args.require_clean:
            print(
                f"{report.get('known_debt_count', 0)} known sensitive "
                "object/path pair(s) "
                "remain reachable; release is blocked until the separately "
                "authorized history rewrite and remote-ref cleanup is complete",
                file=sys.stderr,
            )
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
