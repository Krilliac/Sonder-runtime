"""Reference endpoint for the opt-in selfmod evaluator broker.

The nightly selfmod lane (``scripts/nightly_selfmod.py``) can submit each
candidate's held-out request to an evaluator that runs under a separate,
pre-provisioned Windows account through a Scheduled Task.  This program is
that task's entry point.  Deploy it outside the editable checkout and
configure the task's *run-as* account to differ from the nightly worker.  A
direct invocation under the worker account fails the identity check; matching
hashes are tamper evidence only and never establish the boundary.

The task account needs read access to the request, the held-out snapshot, the
candidate checkout and this deployment directory, write access to the outbox,
and read access to the Ed25519 private key.  It must not be able to write the
checkout, the worker's state directory, the snapshot or the deployment.

This reference broker fails closed: it validates the request and refuses to
sign anything until a third Scheduled Task that runs candidate code under a
distinct lower-privilege identity is provisioned.  It never runs candidate
code while holding the signing key.

Usage::

    selfmod_evaluator_broker.py REQUEST.json
    selfmod_evaluator_broker.py --once INBOX OUTBOX

Every outcome is one ``sonder.selfmod.evaluator-receipt/v1`` JSON line on
stdout; only a passing evaluation carries a signature.  Exit status 0 means a
signed passing receipt, 1 a refusal or failure, 2 a usage error.

Single-threaded; holds no state between invocations.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any


SCHEMA = "sonder.selfmod.evaluator-request/v1"
RECEIPT = "sonder.selfmod.evaluator-receipt/v1"
ISOLATION = "windows-scheduled-task"
PRIVATE_KEY_ENV = "SONDER_SELFMOD_EVALUATOR_PRIVATE_KEY"
CANDIDATE_IDENTITY_ENV = "SONDER_SELFMOD_CANDIDATE_IDENTITY"
CANDIDATE_TASK_ENV = "SONDER_SELFMOD_CANDIDATE_TASK"


def _identity() -> str:
    """The account this process runs as (``DOMAIN\\user`` on Windows)."""
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["whoami", "/user", "/fo", "csv", "/nh"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                check=False, timeout=5,
            )
            line = (result.stdout or "").strip().splitlines()[0]
            fields = [item.strip().strip('"') for item in line.split(",")]
            if fields and fields[0]:
                return fields[0]
        except (OSError, IndexError, subprocess.SubprocessError):
            pass
    return "%s:%s" % (
        os.environ.get("USERNAME") or os.environ.get("USER") or "unknown",
        getattr(os, "getuid", lambda: "none")(),
    )


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _manifest(payload: dict[str, Any]) -> dict[str, Any]:
    """The digest-bound held-out manifest; must match nightly's construction."""
    return {
        "target_path": payload["target_path"],
        "target_sha256": payload["target_sha256"],
        "suites": [str(item["path"]) for item in payload["suites"]],
        "files": payload["files"],
    }


def _verify_snapshot(payload: dict[str, Any]) -> None:
    for item in payload["files"]:
        path = Path(item["path"])
        if path.is_symlink() or not path.is_file() or _sha(path) != item["sha256"]:
            raise RuntimeError("truth snapshot changed: %s" % path)


def _static_request_checks(payload: dict[str, Any]) -> None:
    """Refuse a request whose roots, target, manifest or snapshot do not bind."""
    root = Path(payload["root"]).resolve()
    suite_root = Path(payload["suite_root"]).resolve()
    if root == suite_root or root in suite_root.parents or suite_root in root.parents:
        raise RuntimeError("candidate and truth roots are not separated")
    target = root / str(payload.get("target_path") or "")
    if target.is_symlink() or not target.is_file() or root not in target.resolve().parents:
        raise RuntimeError("candidate target is not a regular file inside candidate root")
    if _sha(target) != payload.get("target_sha256"):
        raise RuntimeError("candidate target digest changed before evaluation")
    if any(root in Path(item["path"]).resolve().parents for item in payload["files"]):
        raise RuntimeError("truth snapshot overlaps candidate root")
    if hashlib.sha256(_canonical(_manifest(payload))).hexdigest() != payload.get("manifest_digest"):
        raise RuntimeError("held-out manifest digest mismatch")
    unsigned = dict(payload)
    request_digest = unsigned.pop("request_digest", None)
    if hashlib.sha256(_canonical(unsigned)).hexdigest() != request_digest:
        raise RuntimeError("evaluator request digest mismatch")
    _verify_snapshot(payload)


def _run(payload: dict[str, Any]) -> tuple[bool, str]:
    candidate_identity = os.environ.get(CANDIDATE_IDENTITY_ENV, "").strip()
    candidate_task = os.environ.get(CANDIDATE_TASK_ENV, "").strip()
    evaluator_identity = _identity()
    if not candidate_identity or not candidate_task or candidate_identity == evaluator_identity:
        raise RuntimeError(
            "candidate execution task and a distinct lower-privilege identity are required"
        )
    # The evaluator owns the signing key. Candidate code must execute through
    # a third, separately ACLed scheduled task before this broker is allowed
    # to sign a result. This reference broker intentionally fails closed until
    # that task-side handoff is installed; it never runs candidate code while
    # holding the private key.
    raise RuntimeError("candidate execution task handoff is not provisioned")


def _evaluate(request: Path) -> int:
    try:
        payload = json.loads(request.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
            raise RuntimeError("unsupported request schema")
        identity = _identity()
        expected = str(payload.get("expected_evaluator_identity") or "")
        if not expected or identity != expected:
            raise RuntimeError("evaluator identity mismatch: %s" % identity)
        if identity == str(payload.get("worker_identity") or ""):
            raise RuntimeError("evaluator identity is the worker identity")
        if payload.get("isolation") != ISOLATION:
            raise RuntimeError("unsupported isolation attestation")
        _static_request_checks(payload)
        passed, output = _run(payload)
        receipt_payload = {
            "schema": RECEIPT,
            "nonce": payload.get("nonce"),
            "request_digest": payload.get("request_digest"),
            "manifest_digest": payload.get("manifest_digest"),
            "identity": identity,
            "isolation": ISOLATION,
            "passed": bool(passed),
        }
        from sonder_runtime.adapters.inference.external_membership import sign_ed25519_payload
        receipt = {
            "schema": RECEIPT,
            "payload": receipt_payload,
            "signature": sign_ed25519_payload(receipt_payload, os.environ[PRIVATE_KEY_ENV]),
            "output": output,
            "completed_at": time.time(),
        }
        print(json.dumps(receipt, sort_keys=True), flush=True)
        return 0 if passed else 1
    except (OSError, ValueError, KeyError, TypeError, AttributeError,
            RuntimeError, subprocess.SubprocessError) as exc:
        print(json.dumps({
            "schema": RECEIPT,
            "payload": {
                "schema": RECEIPT,
                "identity": _identity(),
                "isolation": ISOLATION,
                "passed": False,
            },
            "error": str(exc)[:500],
        }, sort_keys=True), flush=True)
        return 1


def _once(inbox: Path, outbox: Path) -> int:
    """Answer the oldest unanswered inbox request, writing the outbox atomically."""
    if not inbox.is_dir() or not outbox.is_dir():
        return 1
    for request in sorted(inbox.glob("*.json")):
        response = outbox / request.name
        if response.exists():
            continue
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = _evaluate(request)
        temporary = outbox / (request.name + ".tmp")
        temporary.write_text(captured.getvalue(), encoding="utf-8")
        os.replace(temporary, response)
        return code
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 3 and args[0] == "--once":
        return _once(Path(args[1]), Path(args[2]))
    if len(args) != 1:
        print("usage: selfmod_evaluator_broker.py REQUEST.json | --once INBOX OUTBOX",
              file=sys.stderr)
        return 2
    return _evaluate(Path(args[0]))


if __name__ == "__main__":
    raise SystemExit(main())
