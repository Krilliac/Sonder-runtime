"""Compare an exact base and candidate with Sonder's promotion SQL suite.

This command is evaluation-only: it never copies/removes model aliases or
changes runtime policy. The deployment lifecycle consumes the same bounded,
execution-grounded report before it can promote a trained adapter.
"""

from __future__ import annotations

import argparse
import json
import secrets

import sonder_runtime.adapters.evaluation_history_store as eval_history
import promotion_eval


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", nargs="?", default="qwen2.5-coder:1.5b")
    parser.add_argument("candidate", nargs="?", default="sonder-personal:latest")
    parser.add_argument("--challenge", default="")
    parser.add_argument(
        "--record-history", action="store_true",
        help="explicitly record aggregate results after verifying model digests",
    )
    parser.add_argument("--history-path", default=None)
    args = parser.parse_args(argv)

    challenge = args.challenge or secrets.token_hex(16)
    history_error = ""
    before_digests = {}
    if args.record_history:
        try:
            before_digests = {
                args.base: promotion_eval.local_model_digest(args.base),
                args.candidate: promotion_eval.local_model_digest(args.candidate),
            }
        except (OSError, ValueError) as exc:
            history_error = "pre-evaluation model digest failed: %s" % exc
    report = promotion_eval.evaluate_pair(
        args.base, args.candidate, challenge=challenge,
    )
    accepted, reason = promotion_eval.promotion_decision(
        report,
        expected_base=args.base,
        expected_candidate=args.candidate,
        expected_challenge=challenge,
    )
    history_records = []
    if args.record_history and not history_error:
        try:
            after_digests = {
                args.base: promotion_eval.local_model_digest(args.base),
                args.candidate: promotion_eval.local_model_digest(args.candidate),
            }
            if after_digests != before_digests:
                raise ValueError("model digest changed during evaluation")
            requested_models = {
                "base": args.base,
                "candidate": args.candidate,
            }
            for label, requested_model in requested_models.items():
                model_report = report[label]
                valid, validation_reason = promotion_eval.validate_model_report(
                    model_report,
                    expected_model=requested_model,
                    challenge=challenge,
                )
                if not valid:
                    raise ValueError(
                        "%s report is not recordable: %s"
                        % (label, validation_reason)
                    )
            for label, requested_model in requested_models.items():
                model_report = report[label]
                history_records.append(eval_history.record_result(
                    args.history_path,
                    model=requested_model,
                    model_digest=after_digests[requested_model],
                    suite="promotion-sql",
                    suite_version=report["suite_version"],
                    suite_digest=report["suite_hash"],
                    passed=model_report["score"],
                    total=model_report["total"],
                    source="eval_models:%s" % label,
                ))
        except (eval_history.HistoryError, OSError, TimeoutError, ValueError) as exc:
            history_error = str(exc)
    payload = {
        "accepted": accepted,
        "reason": reason,
        "report": report,
    }
    if args.record_history:
        payload["history"] = {
            "recorded": len(history_records),
            "records": history_records,
            "error": history_error or None,
        }
    print(json.dumps(payload, indent=2, sort_keys=True))
    if history_error:
        return 2
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
