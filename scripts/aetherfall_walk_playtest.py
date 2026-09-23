#!/usr/bin/env python3
"""Run Aetherfall's deterministic walk-only playtest and emit a small verdict."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sonder_runtime.adapters.playtester.process import ProcessAdapter  # noqa: E402


def _verdict(report: object) -> tuple[bool, list[str]]:
    if not isinstance(report, dict):
        return False, ["playtest output was not an object"]
    required = {"concordSpawn", "concordToHollow", "pactSpawn", "hollowWalk", "rim"}
    missing = sorted(required - set(report))
    errors = [f"missing report: {name}" for name in missing]
    for name in sorted(required - {"rim"}):
        if name in report and not isinstance(report[name], dict):
            errors.append(f"{name} report is not an object")
    rim = report.get("rim")
    if not isinstance(rim, dict):
        errors.append("rim report is missing")
    else:
        for direction in ("east", "west", "north", "south"):
            value = rim.get(direction)
            if not isinstance(value, dict):
                errors.append(f"rim/{direction} report is missing")
                continue
            if value.get("pastExtent") is not False:
                errors.append(f"rim/{direction} crossed the world extent")
            if value.get("nan") is not False:
                errors.append(f"rim/{direction} produced non-finite coordinates")
            distance = value.get("distLeft")
            if (isinstance(distance, bool) or not isinstance(distance, (int, float))
                    or not math.isfinite(distance) or distance > 8 or distance < 0):
                errors.append(f"rim/{direction} did not reach the rim")
            if value.get("returned") is not True:
                errors.append(f"rim/{direction} did not return to the origin")
    return not errors, errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="operator-trusted Aetherfall checkout")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args(argv)
    if not 0 < args.timeout <= 300 or not math.isfinite(args.timeout):
        parser.error("--timeout must be between 0 and 300 seconds")
    script = (args.root / "tools" / "walk-playtest.mjs").resolve()
    if not args.root.is_dir() or not script.is_file():
        print(json.dumps({"scenario": "aetherfall-walk", "passed": False, "errors": ["Aetherfall checkout or playtest script is missing"]}))
        return 1
    completed = ProcessAdapter().run(
        ("node", str(script)), cwd=args.root, timeout_seconds=args.timeout,
    )
    if completed.error_type:
        print(json.dumps({"scenario": "aetherfall-walk", "passed": False, "errors": [completed.error_type]}))
        return 1
    if completed.returncode:
        print(json.dumps({"scenario": "aetherfall-walk", "passed": False, "errors": ["Aetherfall playtest exited with an error"]}))
        return 1
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError:
        print(json.dumps({"scenario": "aetherfall-walk", "passed": False, "errors": ["Aetherfall playtest returned invalid JSON"]}))
        return 1
    passed, errors = _verdict(report)
    print(json.dumps({"scenario": "aetherfall-walk", "passed": passed, "errors": errors, "source": "tools/walk-playtest.mjs"}, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
