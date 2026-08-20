"""Command-line input adapter for the SPEC-5 runtime entry point."""
from __future__ import annotations

import argparse


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse startup flags without constructing or probing the runtime."""
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


__all__ = ["parse_args"]
