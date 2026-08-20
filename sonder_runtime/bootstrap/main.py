"""SPEC-5 CLI entry point with startup capability parsing.

Flags are parsed here, frozen immediately, and never re-read.
"""
from __future__ import annotations

import argparse
import os
import sys

from ..adapters import runtime_capabilities as caps
from ..adapters.runtime_capabilities import RuntimeCapabilities
from .container import RuntimeConfig, build_runtime


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sonder",
        description="Sonder Runtime — private-first AI orchestration",
    )
    parser.add_argument(
        "--unrestricted-tools",
        action="store_true",
        default=False,
        help="Disable model-tool authorization gates; enable host execution",
    )
    parser.add_argument(
        "--unrestricted-selfmod",
        action="store_true",
        default=False,
        help="Disable selfmod path/approval/isolation/test restrictions",
    )
    parser.add_argument(
        "--profile",
        choices=("workstation-local", "server-private"),
        default="workstation-local",
    )
    return parser.parse_args(argv)


def build_config_from_env(profile: str) -> RuntimeConfig:
    return RuntimeConfig(
        profile=profile,
        model_backend=os.environ.get("SONDER_MODEL_BACKEND", "ollama").strip().lower(),
        sonder_home=os.environ.get("SONDER_HOME", ""),
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    runtime_caps = caps.freeze(RuntimeCapabilities(
        unrestricted_tools=args.unrestricted_tools,
        unrestricted_selfmod=args.unrestricted_selfmod,
    ))

    if runtime_caps.full_autonomy:
        print("FULL AUTONOMY MODE", file=sys.stderr)
    elif runtime_caps.unrestricted_tools:
        print("Unrestricted tools mode", file=sys.stderr)
    elif runtime_caps.unrestricted_selfmod:
        print("Unrestricted self-modification mode", file=sys.stderr)

    config = build_config_from_env(args.profile)
    _runtime = build_runtime(config, runtime_caps)
    return 0
