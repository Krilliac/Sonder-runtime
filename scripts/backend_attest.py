#!/usr/bin/env python3
"""Record diagnostic model probes; independent deployment identity is unbound."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sonder_runtime.adapters.inference.openai_compat_gateway import (
    OpenAICompatibleConfig,
    OpenAICompatibleGateway,
)
from sonder_runtime.adapters.inference.openai_protocol_probe import (
    OpenAICompatibleProtocolProbe,
)
from sonder_runtime.adapters.inference.sonder_inference_gateway import (
    SonderInferenceConfig,
    SonderInferenceGateway,
    config_from_env as sonder_inference_config_from_env,
)
from sonder_runtime.adapters.inference.sonder_inference_probe import (
    IdentityUnavailable,
    SonderInferenceIdentityReader,
    SonderInferenceProtocolProbe,
    SyntheticIdentityRefused,
)
from sonder_runtime.application.routing.backend_conformance import (
    RecentCapabilityEvidence,
    run_gateway_probes,
)
from sonder_runtime.domain.common.errors import SonderError
from sonder_runtime.domain.routing.backend_conformance import (
    BackendIdentity,
)
from sonder_runtime.platform.paths import state_path

BACKENDS = ("openai-compatible", "sonder-inference")


def attest(
    gateway,
    *,
    backend: str,
    model: str,
    evidence_path: str | Path,
    timeout_seconds: float,
    cloud_allowed: bool = False,
    dry_run: bool = False,
    identity: BackendIdentity | None = None,
    protocol_probes: bool = False,
    identity_reader=None,
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
            "protocol_probes": bool(protocol_probes),
        }
    if protocol_probes and backend == "sonder-inference":
        # Sonder Inference measures its own identity; the reader refuses
        # synthetic (mock) identities, so they can never become evidence.
        if (identity is None or identity.model != model
                or identity_reader is None or identity_reader() != identity):
            raise ValueError("protocol probes require an unchanged measured Sonder Inference identity")
        record = SonderInferenceProtocolProbe(
            gateway, identity_reader=identity_reader, cloud_allowed=cloud_allowed,
        ).run(timeout_seconds=timeout_seconds)
    elif protocol_probes:
        if (backend != "openai-compatible" or identity is None
                or identity.backend != backend or identity.model != model
                or identity_reader is None or identity_reader() != identity):
            raise ValueError("protocol probes require an unchanged host-owned OpenAI identity")
        record = OpenAICompatibleProtocolProbe(
            gateway, identity_reader=identity_reader, cloud_allowed=cloud_allowed,
        ).run(timeout_seconds=timeout_seconds)
    else:
        # A measured Sonder Inference identity names the backend it runs on;
        # the record must carry that backend so identity and route agree.
        record_backend = (
            identity.backend
            if backend == "sonder-inference" and identity is not None
            else backend
        )
        record = run_gateway_probes(
            gateway,
            backend=record_backend,
            model=model,
            timeout_seconds=timeout_seconds,
            cloud_allowed=cloud_allowed,
            identity=identity,
            # A supplied identity file and a request-derived ModelResponse.model
            # cannot attest the serving model weights or backend environment.
            synthetic=True,
        )
    RecentCapabilityEvidence(evidence_path).save(record)
    return {
        "backend": record.backend,
        "model": record.model,
        "checked_at": record.checked_at,
        "dry_run": False,
        "passed": sorted(item.capability.value for item in record.results if item.passed is True),
        "failed": sorted(item.capability.value for item in record.results if item.passed is False),
        "unknown": sorted(item.capability.value for item in record.results if item.passed is None),
        "identity_bound": record.identity is not None and not record.synthetic,
        "identity_declared": record.identity is not None,
        "synthetic": record.synthetic,
        "reasons": {item.capability.value: item.reason_code for item in record.results},
        "evidence_path": str(Path(evidence_path).expanduser()),
    }


def _read_host_identity(raw_path: str) -> BackendIdentity:
    path = Path(raw_path)
    if not path.is_absolute() or any(part in (".", "..") for part in path.parts):
        raise ValueError("identity path must be absolute and canonical")
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    for parent in reversed(path.parents):
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or parent.is_symlink()
                or getattr(info, "st_file_attributes", 0) & reparse):
            raise ValueError("identity path has a reparse ancestor")
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or path.is_symlink()
            or getattr(info, "st_file_attributes", 0) & reparse
            or info.st_size > 4096):
        raise ValueError("identity file must be ordinary and under 4 KiB")
    with path.open("rb") as stream:
        data = stream.read(4097)
    if len(data) > 4096:
        raise ValueError("identity file exceeds size bounds")
    return BackendIdentity.from_dict(json.loads(data))


def _default_inference_model(gateway: SonderInferenceGateway) -> str:
    """Resolve Inference's 'default' alias to the concrete served model id."""
    snapshot = gateway.health(refresh=True)
    if snapshot.state != "ready" or snapshot.document is None:
        raise ValueError("sonder-inference is not ready: %s" % snapshot.detail)
    for item in snapshot.document.get("models") or ():
        if isinstance(item, dict) and item.get("default") is True and isinstance(item.get("id"), str):
            return item["id"]
    raise ValueError("sonder-inference health lists no default model; pass --model")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backend", choices=BACKENDS, default="openai-compatible",
        help="provider to attest (sonder-inference reads its identity from the server)",
    )
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--evidence", default=state_path("backend-capabilities.json", "SONDER_BACKEND_EVIDENCE"))
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--allow-cloud", action="store_true", help="explicitly allow a non-loopback OpenAI-compatible endpoint")
    parser.add_argument("--dry-run", action="store_true", help="show the bounded local attestation plan without contacting a provider")
    parser.add_argument("--identity-file", help="host-owned JSON with the current model/backend identity and exact digests")
    parser.add_argument("--protocol-probes", action="store_true", help="run diagnostic JSON-shape checks; unbound tool protocols stay unknown; requires --identity-file")
    args = parser.parse_args(argv)
    if args.backend == "sonder-inference":
        return _main_sonder_inference(parser, args)
    args.base_url = (args.base_url if args.base_url is not None
                     else os.environ.get("SONDER_OPENAI_BASE_URL", "").strip())
    args.model = (args.model if args.model is not None
                  else os.environ.get("SONDER_OPENAI_MODEL", "").strip())
    if not args.base_url or not args.model:
        parser.error("--base-url and --model are required (or set SONDER_OPENAI_BASE_URL and SONDER_OPENAI_MODEL)")
    if not 0.0 < args.timeout <= 300.0:
        parser.error("--timeout must be between 0 and 300 seconds")
    if args.protocol_probes and not args.identity_file and not args.dry_run:
        parser.error("--protocol-probes requires --identity-file")
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
    identity = None
    if args.identity_file:
        try:
            identity = _read_host_identity(args.identity_file)
        except (OSError, ValueError, UnicodeError) as exc:
            parser.error(f"invalid backend identity: {exc}")
        if identity.backend != "openai-compatible" or identity.model != args.model:
            parser.error("backend identity must match the configured route")
    result = attest(
        OpenAICompatibleGateway(config),
        backend="openai-compatible",
        model=args.model,
        evidence_path=args.evidence,
        timeout_seconds=args.timeout,
        cloud_allowed=args.allow_cloud,
        dry_run=args.dry_run,
        identity=identity,
        protocol_probes=args.protocol_probes,
        identity_reader=(lambda: _read_host_identity(args.identity_file)) if args.identity_file else None,
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if args.dry_run or not result.get("failed") else 1


def _main_sonder_inference(parser: argparse.ArgumentParser, args) -> int:
    if not 0.0 < args.timeout <= 300.0:
        parser.error("--timeout must be between 0 and 300 seconds")
    if args.identity_file:
        parser.error("--identity-file does not apply: sonder-inference measures its own identity")
    try:
        settings = sonder_inference_config_from_env()
        settings = SonderInferenceConfig(
            base_url=args.base_url or settings.base_url,
            model=args.model or settings.model,
            api_key=settings.api_key,
            allow_remote=bool(args.allow_cloud),
            timeout_seconds=settings.timeout_seconds,
            health_ttl_seconds=0.0,
        )
    except SonderError as exc:
        parser.error(f"invalid sonder-inference configuration: {exc}")
    loopback = settings.loopback
    if not loopback and not args.allow_cloud:
        parser.error("refusing non-loopback endpoint; pass --allow-cloud for explicit consent")
    if not loopback and not settings.base_url.startswith("https://"):
        parser.error("non-loopback attestation requires HTTPS")
    gateway = SonderInferenceGateway(settings)
    model = settings.model
    if args.dry_run:
        result = attest(
            gateway, backend="sonder-inference", model=model, evidence_path=args.evidence,
            timeout_seconds=args.timeout, cloud_allowed=args.allow_cloud, dry_run=True,
            protocol_probes=args.protocol_probes,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    try:
        if model == "default":
            model = _default_inference_model(gateway)
            gateway = SonderInferenceGateway(SonderInferenceConfig(
                base_url=settings.base_url, model=model, api_key=settings.api_key,
                allow_remote=settings.allow_remote, timeout_seconds=settings.timeout_seconds,
                health_ttl_seconds=0.0,
            ))
        reader = SonderInferenceIdentityReader(gateway, model=model)
        observation = reader.observe()
    except (SonderError, ValueError) as exc:
        parser.error(f"cannot read sonder-inference identity: {exc}")
    identity = None
    if args.protocol_probes:
        try:
            identity = reader()
        except (SyntheticIdentityRefused, IdentityUnavailable) as exc:
            parser.error(str(exc))
    elif not observation.synthetic and observation.identity is not None:
        if observation.identity.model == model:
            identity = observation.identity
        else:
            observation = type(observation)(
                observation.model, None, False,
                "measured identity names model %r, not the served id %r"
                % (observation.identity.model, model),
            )
    result = attest(
        gateway,
        backend="sonder-inference",
        model=model,
        evidence_path=args.evidence,
        timeout_seconds=args.timeout,
        cloud_allowed=args.allow_cloud,
        identity=identity,
        protocol_probes=args.protocol_probes,
        identity_reader=reader if args.protocol_probes else None,
    )
    result["identity_synthetic"] = observation.synthetic
    if observation.identity is None:
        result["identity_reason"] = observation.reason
    print(json.dumps(result, sort_keys=True))
    return 0 if not result.get("failed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
