"""SPEC-5 CLI entry point with startup capability parsing.

Flags are parsed here, frozen immediately, and never re-read.
"""
from __future__ import annotations

import logging
import sys

from ..adapters import runtime_capabilities as caps
from ..adapters.cli_options import parse_args
from ..adapters.runtime_configuration import build_config_from_env
from ..adapters.runtime_capabilities import RuntimeCapabilities
from .container import build_runtime

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    logger.info("sonder runtime starting")
    logger.debug(f"main entry, argv={argv!r}")
    args = parse_args(argv)

    runtime_caps = caps.freeze(RuntimeCapabilities(
        unrestricted_tools=args.unrestricted_tools,
        unrestricted_selfmod=args.unrestricted_selfmod,
    ))
    logger.info(f"capabilities frozen, unrestricted_tools={runtime_caps.unrestricted_tools}, unrestricted_selfmod={runtime_caps.unrestricted_selfmod}")
    logger.debug(f"capabilities frozen: unrestricted_tools={runtime_caps.unrestricted_tools}, unrestricted_selfmod={runtime_caps.unrestricted_selfmod}, full_autonomy={runtime_caps.full_autonomy}")

    if runtime_caps.full_autonomy:
        logger.warning("runtime starting in FULL AUTONOMY mode, all safety restrictions bypassed")
        print("FULL AUTONOMY MODE", file=sys.stderr)
    elif runtime_caps.unrestricted_tools:
        logger.warning("runtime starting with unrestricted tools, tool safety guards bypassed")
        print("Unrestricted tools mode", file=sys.stderr)
    elif runtime_caps.unrestricted_selfmod:
        logger.warning("runtime starting with unrestricted self-modification, selfmod guards bypassed")
        print("Unrestricted self-modification mode", file=sys.stderr)

    logger.debug(f"building config from env, profile={args.profile!r}")
    config = build_config_from_env(args.profile)
    logger.debug("building runtime container")
    _runtime = build_runtime(config, runtime_caps)
    logger.info("runtime container built successfully")
    logger.debug("runtime container built successfully")
    return 0
