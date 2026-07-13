"""Compare an exact base and candidate with Sonder's promotion SQL suite.

This command is evaluation-only: it never copies/removes model aliases or
changes runtime policy. The deployment lifecycle consumes the same bounded,
execution-grounded report before it can promote a trained adapter.
"""

from __future__ import annotations

import argparse
import json
import secrets

import promotion_eval


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", nargs="?", default="qwen2.5-coder:1.5b")
    parser.add_argument("candidate", nargs="?", default="sonder-personal:latest")
    parser.add_argument("--challenge", default="")
    args = parser.parse_args(argv)

    challenge = args.challenge or secrets.token_hex(16)
    report = promotion_eval.evaluate_pair(
        args.base, args.candidate, challenge=challenge,
    )
    accepted, reason = promotion_eval.promotion_decision(
        report,
        expected_base=args.base,
        expected_candidate=args.candidate,
        expected_challenge=challenge,
    )
    print(json.dumps({
        "accepted": accepted,
        "reason": reason,
        "report": report,
    }, indent=2, sort_keys=True))
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
