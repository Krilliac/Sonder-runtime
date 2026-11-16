"""``python -m sonder_runtime anthropic status`` -- read-only Anthropic status.

Reports whether the provider is configured, which API key slots (1 and 2) are
set (booleans only, never a value), whether the optional ``anthropic`` SDK is
installed, the model each tier would use, and today's local spend against
``SONDER_ANTHROPIC_DAILY_USD``.  It reads the process environment and the local
spend ledger only: it makes no network call, sends no prompt, spends nothing,
and never touches the paid API.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ..adapters.inference.anthropic_gateway import PROVIDER_ID, AnthropicGateway
from ..domain.common.errors import SonderError


def format_status(entry: dict[str, Any]) -> str:
    slots = entry.get("key_slots") or {}
    lines = [
        "Anthropic provider: %s" % (entry.get("state") or "unavailable"),
        "  cloud opt-in (SONDER_ALLOW_CLOUD): %s" % ("on" if entry.get("cloud_enabled") else "off"),
        "  key slot 1: %s | key slot 2: %s" % (
            "set" if slots.get("1") else "not set", "set" if slots.get("2") else "not set",
        ),
        "  anthropic SDK installed: %s" % ("yes" if entry.get("sdk_installed") else "no (pip install anthropic)"),
        "  default model: %s" % entry.get("default_model"),
    ]
    if entry.get("spent_today_usd") is not None:
        lines.append("  spend today (UTC %s): $%s of $%s cap%s | calls: %s (slot 1: %s, slot 2: %s)" % (
            entry.get("day_utc"), entry["spent_today_usd"], entry.get("daily_cap_usd"),
            " -- CAP REACHED, new calls are refused" if entry.get("cap_reached") else "",
            entry.get("calls_today"), (entry.get("key_slot_calls_today") or {}).get("1"),
            (entry.get("key_slot_calls_today") or {}).get("2"),
        ))
    if entry.get("detail"):
        lines.append("  note: %s" % entry["detail"])
    return "\n".join(lines)


def cmd_anthropic(args: argparse.Namespace) -> int:
    try:
        entry = dict(AnthropicGateway().provider_status()[PROVIDER_ID])
        print(json.dumps(entry, indent=2, sort_keys=True) if args.json else format_status(entry))
    except (SonderError, ValueError, RuntimeError, OSError) as exc:
        print("anthropic: %s" % exc, file=sys.stderr)
        return 2
    return 0


def add_parser(sub: Any) -> None:
    parser = sub.add_parser("anthropic", help="Anthropic provider status and today's spend")
    commands = parser.add_subparsers(dest="anthropic_command", required=True)
    status = commands.add_parser("status", help="configuration, key slots and spend vs cap (read-only)")
    status.add_argument("--json", action="store_true")
    parser.set_defaults(func=cmd_anthropic)


__all__ = ["add_parser", "cmd_anthropic", "format_status"]
