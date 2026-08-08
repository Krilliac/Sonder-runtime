"""Run the selfmod lifecycle continuously for a bounded window.

Each pass proposes one improvement, isolates it in a Git worktree, runs ruff
and the whole test suite there, and on green commits it to its own
``selfmod/<run-id>`` branch. The main working tree is never written, so a live
session keeps its checkout and its uncommitted work; every result is a branch
you can read, cherry-pick, or delete.

    python scripts/selfmod_forever.py --hours 4

Bounded on purpose. An unbounded self-modifying loop has no natural stopping
point and this one shares a 16 GB box with everything else; --hours is the
contract, and it is checked before each pass rather than mid-pass so a run is
never abandoned half-tested.

Consecutive failures stop it early. If the model is down, or every candidate
is being rejected, further passes only burn GPU and fill the run table with
noise -- and a loop that cannot tell "no improvements left" from "backend
broken" should stop rather than keep trying.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for candidate in (str(REPO), str(REPO / "scripts")):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

import nightly_selfmod  # noqa: E402
import selfmod  # noqa: E402

# Stop after this many consecutive passes that produced no commit. Distinguishes
# "nothing left to improve" and "the backend died" from a healthy loop, without
# needing to tell them apart.
MAX_BARREN = 6


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, default=4.0)
    parser.add_argument("--test-timeout", type=int, default=1800)
    args = parser.parse_args()

    import server

    def log(message):
        print("%s %s" % (time.strftime("%H:%M:%S"), message), flush=True)

    deadline = time.time() + args.hours * 3600.0
    committed, barren, passes = [], 0, 0

    log("=== selfmod continuous start: %.1fh, mode=%s, branch-commit ==="
        % (args.hours, selfmod.settings().get("mode")))

    while time.time() < deadline and barren < MAX_BARREN:
        passes += 1
        log("--- pass %d (%.1fh left) ---" % (passes, (deadline - time.time()) / 3600.0))
        try:
            result = nightly_selfmod.run(
                server, log, test_timeout=args.test_timeout, branch=True)
        except Exception as exc:
            result = "pass failed: %s" % str(exc)[:200]
        log(result)

        if result.startswith("COMMITTED"):
            committed.append(result)
            barren = 0
        else:
            barren += 1
            if result.startswith("working tree dirty"):
                # Not the loop's to fix, and it will not clear on its own.
                log("stopping: the tree must be clean for a run to be committable")
                break

    log("=== done: %d pass(es), %d commit(s) ===" % (passes, len(committed)))
    for line in committed:
        log("  " + line)
    if barren >= MAX_BARREN:
        log("stopped early: %d consecutive passes produced no commit" % barren)
    if committed:
        log("review with: git log --oneline --all --grep='^selfmod:'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
