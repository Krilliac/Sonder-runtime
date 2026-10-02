"""Linux diagnostic supervisor: exact checkout, finite owned descendant tree."""
from __future__ import annotations

import argparse
import ctypes
import errno
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

WATCHDOG_SECONDS = 18 * 60
INTERRUPT_SECONDS = 5
TERM_SECONDS = 5
KILL_SECONDS = 5
MIN_AVAILABLE_MIB = 3000
_DISCOVERED = 0


def process_table() -> dict[int, tuple[int, int]]:
    result = {}
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
            result[int(path.name)] = (int(fields[1]), int(fields[19]))
        except OSError as error:
            if error.errno not in {errno.ENOENT, errno.ESRCH}:
                raise
        except (ValueError, IndexError) as error:
            raise RuntimeError("Invalid process metadata prevents owned-tree proof") from error
    return result


def capture_descendants(owned: dict[int, tuple[int, int]]) -> dict[int, tuple[int, int]]:
    global _DISCOVERED
    table = process_table()
    for pid, (token, descriptor) in list(owned.items()):
        if table.get(pid, (0, -1))[1] != token:
            os.close(descriptor)
            del owned[pid]
    parents = {os.getpid()}
    parents.update(owned)
    changed = True
    while changed:
        changed = False
        for pid, (parent, token) in table.items():
            if parent in parents and pid not in parents:
                descriptor = None
                try:
                    descriptor = os.pidfd_open(pid, 0)
                    fields = (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
                    if int(fields[19]) != token or int(fields[1]) not in parents:
                        os.close(descriptor)
                        continue
                except OSError as error:
                    if descriptor is not None:
                        os.close(descriptor)
                    if error.errno not in {errno.ENOENT, errno.ESRCH}:
                        raise
                    continue
                except (ValueError, IndexError) as error:
                    if descriptor is not None:
                        os.close(descriptor)
                    raise RuntimeError("Invalid child identity prevents owned-tree proof") from error
                parents.add(pid)
                owned[pid] = (token, descriptor)
                _DISCOVERED += 1
                changed = True
    return table


def alive(owned: dict[int, tuple[int, int]], table: dict[int, tuple[int, int]]) -> list[int]:
    return [pid for pid, (token, _) in owned.items() if table.get(pid, (0, -1))[1] == token]


def signal_owned(owned: dict[int, tuple[int, int]], value: int) -> None:
    table = capture_descendants(owned)
    for pid in alive(owned, table):
        try:
            # The retained pidfd identifies the verified instance, even after PID reuse.
            signal.pidfd_send_signal(owned[pid][1], value, None, 0)
        except ProcessLookupError:
            continue


def reap() -> None:
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def cleanup(owned: dict[int, tuple[int, int]]) -> list[int]:
    """Subreaper adoption catches descendants that made their own session."""
    for sig, duration in (
        (signal.SIGINT, INTERRUPT_SECONDS),
        (signal.SIGTERM, TERM_SECONDS),
        (signal.SIGKILL, KILL_SECONDS),
    ):
        signal_owned(owned, sig)
        end = time.monotonic() + duration
        while time.monotonic() < end:
            reap()
            if not alive(owned, capture_descendants(owned)):
                return []
            time.sleep(0.1)
    reap()
    return alive(owned, capture_descendants(owned))


def close_owned(owned: dict[int, tuple[int, int]]) -> None:
    for _, descriptor in owned.values():
        os.close(descriptor)
    owned.clear()


def write(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--label", choices=("passing-prior", "current-candidate"), required=True)
    args = parser.parse_args()
    args.evidence.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable, "-u", "-m", "pytest", "-v", "-n", "auto", "--dist", "load",
        "--maxfail=5", "--tb=short", "--durations=25",
        "-p", "tls_ci_diagnostic_plugin",
        "--junitxml=" + str(args.evidence / "pytest-report.xml"),
    ]
    receipt = {
        "label": args.label, "expected_merge_sha": args.expected_sha,
        "harness_dispatch_sha": os.environ.get("GITHUB_SHA"),
        "harness_event_name": os.environ.get("GITHUB_EVENT_NAME"),
        "harness_workspace": os.environ.get("GITHUB_WORKSPACE"),
        "python": sys.version, "command": command, "watchdog_seconds": WATCHDOG_SECONDS,
        "cleanup_bound_seconds": INTERRUPT_SECONDS + TERM_SECONDS + KILL_SECONDS + 3,
        "completed_case_cap": 5000, "maxfail": 5, "diagnostic_only": True, "stage": "preflight",
        "start_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    write(args.evidence / "header.json", receipt)
    owned: dict[int, tuple[int, int]] = {}
    child = None
    code = None
    reader = None
    forward_errors = []
    started = time.monotonic()
    timed_out = False
    output = None
    infrastructure_error = None
    cleanup_error = None

    def forward(pipe) -> None:
        try:
            for line in pipe:
                output.write(line)
                output.flush()
                _, marker, payload = line.partition("TLS_CI_DIAGNOSTIC ")
                if marker:
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    # Keep arbitrary traceback/stdout in the artifact; stream diagnostic JSON only.
                    allowed = {
                        "kind", "worker", "pid", "elapsed", "cpu_user", "cpu_system",
                        "nodeid", "phase", "outcome", "named_failures", "completed", "limit",
                        "target_worker", "collected", "xdist_numprocesses", "exitstatus", "diagnostic_only",
                        "started", "unfinished_started", "observed_workers",
                    }
                    kinds = {"failure", "collection_failure", "cap", "session_start", "session_finish", "worker_collection"}
                    if isinstance(event, dict) and event.get("kind") in kinds:
                        print("TLS_CI_DIAGNOSTIC " + json.dumps(
                            {key: value for key, value in event.items() if key in allowed},
                            sort_keys=True), flush=True)
        except (OSError, ValueError) as error:
            forward_errors.append(type(error).__name__)

    try:
        import hashlib
        from importlib.metadata import PackageNotFoundError, version

        source = args.source.resolve(strict=True)
        head = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        if head != args.expected_sha:
            raise RuntimeError("Diagnostic checkout SHA mismatch")
        blob = subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD:tests/test_ollama_endpoint_tls.py"], text=True
        ).strip()
        available = next(
            int(line.split()[1]) // 1024
            for line in Path("/proc/meminfo").read_text().splitlines()
            if line.startswith("MemAvailable:")
        )
        if available < MIN_AVAILABLE_MIB:
            raise RuntimeError("Fresh hosted available-memory guard failed")
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            raise RuntimeError("Stable owned-process handles unavailable")
        descriptor = os.pidfd_open(os.getpid(), 0)
        try:
            signal.pidfd_send_signal(descriptor, 0, None, 0)
        finally:
            os.close(descriptor)
        # Process-local ownership, not a host trust, privilege or security change.
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
            raise OSError(ctypes.get_errno(), "Cannot own orphaned diagnostic descendants")
        child_env = dict(os.environ)
        child_env["PYTHONUNBUFFERED"] = "1"
        child_env["TLS_CI_DIAGNOSTIC_DIR"] = str(args.evidence)
        child_env["PYTHONPATH"] = str(Path(__file__).resolve().parent)
        child_env["GITHUB_SHA"] = head
        child_env["GITHUB_WORKSPACE"] = str(source)
        package_versions = {}
        for package in ("pytest", "pytest-xdist", "mcp", "cryptography"):
            try:
                package_versions[package] = version(package)
            except PackageNotFoundError:
                package_versions[package] = None
        receipt.update(
            checkout_sha=head, tls_blob_sha=blob,
            tls_raw_sha256=hashlib.sha256((source / "tests/test_ollama_endpoint_tls.py").read_bytes()).hexdigest(),
            available_mib=available, cpu_count=os.cpu_count(),
            affinity_count=len(os.sched_getaffinity(0)), load_average=list(os.getloadavg()),
            child_context_overrides={"GITHUB_SHA": head, "GITHUB_WORKSPACE": str(source)},
            package_versions=package_versions,
            stage="running",
        )
        write(args.evidence / "header.json", receipt)
        print("TLS_CI_DIAGNOSTIC_HEADER " + json.dumps(receipt, sort_keys=True), flush=True)
        output = (args.evidence / "pytest-output.txt").open("w", encoding="utf-8")
        started = time.monotonic()
        child = subprocess.Popen(command, cwd=source, env=child_env, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                 errors="replace", bufsize=1, start_new_session=True)
        reader = threading.Thread(target=forward, args=(child.stdout,), daemon=True)
        reader.start()
        while child.poll() is None:
            capture_descendants(owned)
            if time.monotonic() - started >= WATCHDOG_SECONDS:
                timed_out = True
                break
            time.sleep(0.2)
        code = child.returncode
    except BaseException as error:
        infrastructure_error = type(error).__name__
    finally:
        try:
            leftovers = cleanup(owned)
        except BaseException as error:
            cleanup_error = type(error).__name__
            leftovers = list(owned)
        if child is not None:
            try:
                child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                leftovers.append(child.pid)
        if reader is not None:
            reader.join(timeout=2)
        reader_alive = reader is not None and reader.is_alive()
        if not reader_alive and output is not None:
            output.close()
        receipt.update(
            timed_out=timed_out, elapsed_seconds=round(time.monotonic() - started, 3),
            pytest_exit_code=code if child is not None else None,
            owned_processes_discovered=_DISCOVERED, leftover_owned_pids=leftovers,
            output_forwarding_errors=forward_errors, output_reader_alive=reader_alive,
            infrastructure_error=infrastructure_error, cleanup_error=cleanup_error, stage="finished",
            finish_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        print("TLS_CI_DIAGNOSTIC_RESULT " + json.dumps(receipt, sort_keys=True), flush=True)
        close_owned(owned)
        write(args.evidence / "supervisor-result.json", receipt)
    if leftovers or reader_alive or forward_errors or infrastructure_error or cleanup_error:
        return 125
    return 124 if timed_out else (4 if code == 0 else int(code or 1))


if __name__ == "__main__":
    raise SystemExit(main())
