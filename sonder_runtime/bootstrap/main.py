"""SPEC-5 CLI entry point with startup capability parsing.

Flags are parsed here, frozen immediately, and never re-read.
"""
from __future__ import annotations

import os
import sys

from ..adapters import runtime_capabilities as caps
from ..adapters.cli_options import parse_args
from ..adapters.runtime_capabilities import RuntimeCapabilities
from .container import RuntimeConfig, build_runtime


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
