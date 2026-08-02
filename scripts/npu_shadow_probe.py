"""Measure routing shadow agreement on held-out prompts, off the live ledger.

Drives the production pair — ``server._execution_route_model`` (the real
router-tier baseline) and ``npu_service.route_shadow`` — over generated
ambiguous-band prompts, exactly like live serve traffic would, but records
into a sandbox ledger so the production advisory keeps meaning "live user
traffic". Use a --seed different from the distillation seed (20260802) or
the measurement is circular.

Usage (repo root, runtime venv, Ollama serving):

    python scripts/npu_shadow_probe.py --samples 80 --seed 20260803

Prints per-run agreement and the sandbox advisory. The RAM spawn gate can be
bypassed for this probe process only with SONDER_AVAILABLE_RAM_GB (the tiny
routing session is far below the worker RSS cap); the live server's gate is
untouched.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--samples", type=int, default=80)
    parser.add_argument("--seed", type=int, default=20260803)
    parser.add_argument("--ledger", default="",
                        help="sandbox ledger path (default: a temp file)")
    args = parser.parse_args()

    ledger = args.ledger or os.path.join(
        tempfile.gettempdir(), "sonder-shadow-probe-%d.json" % args.seed,
    )
    os.environ["SONDER_NPU_SHADOW_LEDGER"] = ledger
    os.environ.setdefault("SONDER_AVAILABLE_RAM_GB", "4")

    import npu_distill_router
    import npu_service
    import server

    if npu_service._mode("routing") not in ("shadow", "prefer"):
        print("routing accelerator is off; enable shadow first")
        return 1
    prompts = npu_distill_router.filter_decide_band(
        npu_distill_router.generate_corpus(args.samples, args.seed),
    )
    print("driving %d held-out decide-band prompts (seed %d)"
          % (len(prompts), args.seed))
    print("sandbox ledger: %s" % ledger)

    started = time.time()
    driven = 0
    for index, prompt in enumerate(prompts):
        try:
            baseline = server._execution_route_model(prompt)
        except Exception as exc:
            print("  baseline failed at %d: %s" % (index, str(exc)[:120]))
            continue
        npu_service.route_shadow(
            prompt, {"mode": baseline["mode"], "tier": baseline["tier"]},
        )
        driven += 1
        if driven % 20 == 0:
            advisory = npu_service.shadow_advisory()
            print("  %d driven | window=%d agreement=%s" % (
                driven, advisory["window"], advisory["agreement"]))

    advisory = npu_service.shadow_advisory()
    print("done: %d driven in %.0fs" % (driven, time.time() - started))
    print("sandbox advisory: window=%(window)d agreement=%(agreement)s "
          "errors=%(errors)d -> %(advisory)s" % {
              **advisory, "errors": advisory["totals"]["errors"]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
