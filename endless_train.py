"""Endless grounded training loop for trilobite.

Runs bounded campaign rounds until Ctrl+C or until a round records no useful
progress. Configuration is via environment variables so the .cmd launcher stays
simple.
"""
import os
import re
import time

import server


_SUMMARY_RE = re.compile(
    r"campaign generate/compile/execute/record: "
    r"(?P<passed>\d+)/(?P<total>\d+) passed, (?P<recorded>\d+) recorded"
    r"(?:, (?P<failed_recorded>\d+) failed-recorded)?"
)


def _env_int(name, default, lo=1, hi=10_000):
    raw = os.environ.get(name, "")
    try:
        value = int(raw)
    except ValueError:
        value = default
    return max(lo, min(value, hi))


def _env_float(name, default, lo=0.0, hi=3600.0):
    raw = os.environ.get(name, "")
    try:
        value = float(raw)
    except ValueError:
        value = default
    return max(lo, min(value, hi))


def _round_config():
    return {
        "total": _env_int("TRILOBITE_ENDLESS_TOTAL", 30, 1, 120),
        "languages": os.environ.get(
            "TRILOBITE_ENDLESS_LANGUAGES",
            "python,javascript,powershell,cpp,csharp",
        ),
        "tier": os.environ.get("TRILOBITE_ENDLESS_TIER", "fast"),
        "max_workers": _env_int("TRILOBITE_ENDLESS_WORKERS", 4, 1, 12),
        "timeout": _env_int("TRILOBITE_ENDLESS_TIMEOUT", 10, 1, 120),
        "repair_rounds": _env_int("TRILOBITE_ENDLESS_REPAIRS", 2, 0, 3),
    }


def _parse_summary(text):
    match = _SUMMARY_RE.search(text or "")
    if not match:
        return None
    return {
        key: int(value or 0)
        for key, value in match.groupdict().items()
    }


def main():
    sleep_seconds = _env_float("TRILOBITE_ENDLESS_SLEEP", 2.0, 0.0, 3600.0)
    stop_after_no_progress = _env_int(
        "TRILOBITE_ENDLESS_STOP_AFTER_NO_PROGRESS", 1, 1, 100
    )
    no_progress = 0
    round_no = 0

    print("trilobite endless training")
    print("Press Ctrl+C to stop after the current in-flight process is interrupted.")
    print("Config: %r" % _round_config())
    print()

    while True:
        round_no += 1
        cfg = _round_config()
        print("=== endless training round %d ===" % round_no)
        started = time.time()
        try:
            output = server.campaign_generate_compile_execute_record(**cfg)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print("ERROR: campaign crashed: %s" % e)
            print("Stopping: no more work can be done until the error is fixed.")
            return 2

        print(output)
        summary = _parse_summary(output)
        elapsed = time.time() - started

        if summary is None:
            print("Stopping: campaign did not return a usable progress summary.")
            return 2

        print(
            "round %d summary: %d/%d passed, %d recorded, %d failed-recorded, %.1fs"
            % (
                round_no,
                summary["passed"],
                summary["total"],
                summary["recorded"],
                summary["failed_recorded"],
                elapsed,
            )
        )
        print()

        if summary["recorded"] <= 0:
            no_progress += 1
            print(
                "No new passing outcomes recorded (%d/%d no-progress rounds)."
                % (no_progress, stop_after_no_progress)
            )
            if no_progress >= stop_after_no_progress:
                print("Stopping: no more work can be done right now.")
                return 0
        else:
            no_progress = 0

        if sleep_seconds:
            time.sleep(sleep_seconds)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print()
        print("Stopped by Ctrl+C.")
        raise SystemExit(130)
