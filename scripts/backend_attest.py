#!/usr/bin/env python3
"""Produce recent, non-synthetic capability evidence for one model route."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sonder_runtime.adapters.inference.openai_compat_gateway import (  # noqa: E402
    OpenAICompatibleConfig,
    OpenAICompatibleGateway,
)
from sonder_runtime.application.routing.backend_conformance import (  # noqa: E402
    RecentCapabilityEvidence,
    run_gateway_probes,
)
from sonder_runtime.platform.paths import state_path  # noqa: E402


def attest(
    gateway,
    *,
    backend: str,
    model: str,
    evidence_path: str | Path,
    timeout_seconds: float,
    cloud_allowed: bool = False,
    dry_run: bool = False,
) -> dict[str, object]:
    """Run one bounded probe or return a non-invasive plan without probing."""
    if dry_run:
        return {
            "backend": backend,
            "model": model,
            "evidence_path": str(Path(evidence_path).expanduser()),
            "timeout_seconds": float(timeout_seconds),
            "cloud_allowed": bool(cloud_allowed),
            "dry_run": True,
        }
    record = run_gateway_probes(
        gateway,
        backend=backend,
        model=model,
        timeout_seconds=timeout_seconds,
        cloud_allowed=cloud_allowed,
    )
    RecentCapabilityEvidence(evidence_path).save(record)
    return {
        "backend": record.backend,
        "model": record.model,
        "checked_at": record.checked_at,
        "dry_run": False,
        "passed": sorted(item.capability.value for item in record.results if item.passed),
        "failed": sorted(item.capability.value for item in record.results if not item.passed),
        "reasons": {item.capability.value: item.reason_code for item in record.results},
        "evidence_path": str(Path(evidence_path).expanduser()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("SONDER_OPENAI_BASE_URL", "").strip())
    parser.add_argument("--model", default=os.environ.get("SONDER_OPENAI_MODEL", "").strip())
    parser.add_argument("--evidence", default=state_path("backend-capabilities.json", "SONDER_BACKEND_EVIDENCE"))
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--allow-cloud", action="store_true", help="explicitly allow a non-loopback OpenAI-compatible endpoint")
    parser.add_argument("--dry-run", action="store_true", help="show the bounded local attestation plan without contacting a provider")
    args = parser.parse_args(argv)
    if not args.base_url or not args.model:
        parser.error("--base-url and --model are required (or set SONDER_OPENAI_BASE_URL and SONDER_OPENAI_MODEL)")
    if not 0.0 < args.timeout <= 300.0:
        parser.error("--timeout must be between 0 and 300 seconds")
    try:
        parsed = urlsplit(args.base_url)
        host = parsed.hostname
    except ValueError:
        parser.error("--base-url is not a valid HTTP(S) endpoint")
    if (parsed.scheme not in {"http", "https"} or not host
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        parser.error("--base-url must be an HTTP(S) endpoint without credentials or query data")
    loopback = OpenAICompatibleGateway._is_loopback(args.base_url)
    if host == "0.0.0.0":
        parser.error("0.0.0.0 is a bind address, not an attestation destination")
    if not loopback and not args.allow_cloud:
        parser.error("refusing non-loopback endpoint; pass --allow-cloud for explicit consent")
    if not loopback and parsed.scheme != "https":
        parser.error("non-loopback attestation requires HTTPS")
    config = OpenAICompatibleConfig(
        base_url=args.base_url,
        api_key=os.environ.get("SONDER_OPENAI_API_KEY", "").strip(),
        model=args.model,
    )
    result = attest(
        OpenAICompatibleGateway(config),
        backend="openai-compatible",
        model=args.model,
        evidence_path=args.evidence,
        timeout_seconds=args.timeout,
        cloud_allowed=args.allow_cloud,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if args.dry_run or not result.get("failed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
