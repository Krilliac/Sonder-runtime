"""Packaged command boundary for the headless supervisor CLI.

The root ``sonder_headless`` module supplies the process supervisor callbacks
for compatibility.  This interface owns only argument parsing and command
sequencing, so the packaged boundary has no dependency on root modules.
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from typing import Callable, TextIO


@dataclass(frozen=True)
class HeadlessCliOperations:
    """Supervisor operations injected by the legacy-compatible root module."""

    status: Callable[[str, int], str]
    stop_pid: Callable[..., str]
    stop_succeeded: Callable[[str], bool]
    start_succeeded: Callable[[str], bool]
    start_ollama: Callable[[], str]
    ensure_sonder_alias: Callable[[], tuple[bool, str]]
    validate_start: Callable[[str, dict[str, str]], str | None]
    start_sonder: Callable[[str, int, dict[str, str]], str]
    managed_listener_pid: Callable[[str, int], int | None]
    wait_until: Callable[[Callable[[], bool], float], bool]


def run(
    argv: list[str] | None,
    operations: HeadlessCliOperations,
    *,
    default_host: str = "127.0.0.1",
    default_port: int = 11435,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Parse and execute one headless command through injected operations."""
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    parser = argparse.ArgumentParser(description="Run Sonder Runtime and Ollama headlessly.")
    parser.add_argument(
        "command", nargs="?", default="start",
        choices=["start", "engine", "status", "stop", "restart"],
    )
    parser.add_argument("--host", default=default_host)
    parser.add_argument("--port", type=int, default=default_port)
    parser.add_argument("--stop-ollama", action="store_true", help="Also stop Ollama when stopping.")
    parser.add_argument("--context-size", default=os.environ.get("SONDER_CONTEXT_SIZE", ""))
    parser.add_argument("--allow-hosted", action="store_true")
    args = parser.parse_args(argv)

    env: dict[str, str] = {}
    if args.context_size:
        env["SONDER_CONTEXT_SIZE"] = args.context_size
    if args.allow_hosted:
        env["SONDER_ALLOW_CLOUD"] = "1"

    if args.command == "status":
        print(operations.status(args.host, args.port), file=out)
        return 0
    if args.command == "stop":
        messages = [operations.stop_pid("sonder_serve", args.host, args.port)]
        print(messages[0], file=out)
        if args.stop_ollama:
            messages.append(operations.stop_pid("ollama"))
            print(messages[-1], file=out)
        return 0 if all(operations.stop_succeeded(message) for message in messages) else 1
    if args.command == "restart":
        stopped = operations.stop_pid("sonder_serve", args.host, args.port)
        print(stopped, file=out)
        if not operations.stop_succeeded(stopped):
            return 1

    error = operations.validate_start(args.host, env)
    if error:
        print("ERROR: %s" % error, file=err)
        return 2
    print(operations.start_ollama(), file=out)
    alias_ok, alias_message = operations.ensure_sonder_alias()
    print(alias_message, file=out)
    if not alias_ok:
        return 2
    if args.command == "engine":
        return 0

    started = operations.start_sonder(args.host, args.port, env)
    print(started, file=out)
    print(operations.status(args.host, args.port), file=out)
    managed = operations.managed_listener_pid(args.host, args.port)
    if not managed:
        observed: int | None = None

        def listener_identity_visible() -> bool:
            nonlocal observed
            observed = operations.managed_listener_pid(args.host, args.port)
            return observed is not None

        operations.wait_until(listener_identity_visible, 2)
        managed = observed
    return 0 if operations.start_succeeded(started) or managed else 1
