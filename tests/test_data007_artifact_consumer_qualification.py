"""DATA-007 producer-to-consumer tamper qualification."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from sonder_runtime.adapters.execution.durable_output import (
    DurableExecutionOutput,
    DurableSpillIntegrityError,
    SQLiteSpillStore,
)
from sonder_runtime.adapters.updates.service import BundleManifest, build_bundle
from sonder_runtime.application.execution.world_control import SpillReference
from sonder_runtime.application.ports.artifact_store import SpillSpec
from sonder_runtime.application.ports.training import ManifestEvidence
from sonder_runtime.application.training.attended_execution import (
    AttendedTrainingExecutionService,
    AttendedTrainingRequest,
)
from sonder_runtime.application.updates.activation import (
    SignedManifest,
    UpdateActivation,
)
from sonder_runtime.domain.common.errors import Forbidden
from sonder_runtime.domain.training.reproducible import (
    BaseModelManifest,
    DatasetManifest,
    DependencyManifest,
    EvaluationManifest,
    Provenance,
    ReproducibleTrainingManifest,
)


def _training_manifest() -> ReproducibleTrainingManifest:
    provenance = Provenance("qualification", "source-revision")
    dataset = DatasetManifest("dataset", "d" * 64, 1, "v1", provenance)
    return ReproducibleTrainingManifest(
        dataset=dataset,
        base_model=BaseModelManifest("model", "revision", "m" * 64, "t" * 64, provenance),
        dependencies=(DependencyManifest("trainer", "1.0", "lock", "p" * 64),),
        evaluation=EvaluationManifest.from_mapping(
            "smoke", "1", "d" * 64, {"quality": 1.0}, provenance
        ),
    )


def _manifest_from_disk(payload: dict) -> ReproducibleTrainingManifest:
    def provenance(value):
        return Provenance.from_mapping(
            value["source"], value["revision"],
            artifact_digest=value["artifact_digest"], metadata=value["metadata"],
        )

    dataset = DatasetManifest(
        payload["dataset"]["dataset_id"], payload["dataset"]["snapshot_digest"],
        payload["dataset"]["row_count"], payload["dataset"]["schema_version"],
        provenance(payload["dataset"]["provenance"]),
    )
    base = BaseModelManifest(
        payload["base_model"]["model_id"], payload["base_model"]["revision"],
        payload["base_model"]["artifact_digest"], payload["base_model"]["tokenizer_digest"],
        provenance(payload["base_model"]["provenance"]),
    )
    dependencies = tuple(DependencyManifest(**item) for item in payload["dependencies"])
    evaluation = EvaluationManifest.from_mapping(
        payload["evaluation"]["suite_id"], payload["evaluation"]["suite_version"],
        payload["evaluation"]["dataset_digest"], payload["evaluation"]["metrics"],
        provenance(payload["evaluation"]["provenance"]),
    )
    return ReproducibleTrainingManifest(dataset, base, dependencies, evaluation)


class _Verifier:
    def __init__(self, expected_digest: str):
        self.expected_digest = expected_digest

    def verify(self, evidence: ManifestEvidence) -> bool:
        return evidence.signature == self.expected_digest and evidence.manifest_digest == self.expected_digest


class _NoEffect:
    def __init__(self):
        self.calls = []

    def launch(self, request):
        self.calls.append(request)
        raise AssertionError("tampered manifest reached launch")


def test_real_bundle_manifest_reopen_refuses_same_path_byte_tamper(tmp_path: Path):
    source = tmp_path / "source"
    output = tmp_path / "bundle"
    source.mkdir()
    target = source / "generated-deliverable.txt"
    target.write_bytes(b"original generated deliverable\n")
    built = build_bundle(source, output, version="data007")

    reopened = BundleManifest.load(built["manifest"])
    assert reopened.verify_tree(source) == []
    target.write_bytes(b"tampered generated deliverable\n")
    reopened_after_tamper = BundleManifest.load(built["manifest"])
    assert any("hash mismatch" in problem for problem in reopened_after_tamper.verify_tree(source))


def test_reopened_training_manifest_tamper_is_rejected_before_launch(tmp_path: Path):
    produced = _training_manifest()
    manifest_path = tmp_path / "training-manifest.json"
    manifest_path.write_text(json.dumps(produced.as_dict(), sort_keys=True), encoding="utf-8")
    original_digest = produced.digest

    persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
    persisted["base_model"]["artifact_digest"] = "x" * 64
    manifest_path.write_text(json.dumps(persisted, sort_keys=True), encoding="utf-8")
    reopened = _manifest_from_disk(json.loads(manifest_path.read_text(encoding="utf-8")))
    assert reopened.digest != original_digest

    effects = _NoEffect()
    service = AttendedTrainingExecutionService(
        process=effects,
        lock=type("Lock", (), {"acquire": lambda self, run_id: __import__("contextlib").nullcontext()})(),
        verifier=_Verifier(original_digest),
        journal=type("Journal", (), {"append": lambda self, event: None})(),
        policy=type("Policy", (), {})(),
        deployment=type("Deployment", (), {})(),
    )
    with pytest.raises(Forbidden):
        service.execute(AttendedTrainingRequest("run", ("train",), reopened, original_digest, True))
    assert effects.calls == []


def test_reopened_selfmod_deploy_refuses_tampered_candidate_before_copy(tmp_path: Path, monkeypatch):
    import selfmod

    state = tmp_path / "selfmod-state"
    database = state / "selfmod.db"
    root = tmp_path / "repository"
    root.mkdir()
    (root / "target.txt").write_text("original\n", encoding="utf-8")
    monkeypatch.setenv("SONDER_SELFMOD_HOME", str(state))
    monkeypatch.setenv("SONDER_SELFMOD_DB", str(database))
    run = selfmod.create_plan(
        "bind candidate bytes", root, evidence=["tamper qualification"],
        files=["target.txt"], criteria=["candidate bytes are immutable after test"],
    )
    selfmod.create_backup(run["id"])
    selfmod.prepare_workspace(run["id"])
    selfmod.apply_candidate_changes(run["id"], {"target.txt": "candidate\n"})
    selfmod.begin_testing(run["id"])
    selfmod.record_test(run["id"], "smoke", [sys.executable, "-c", "pass"])
    selfmod.review(run["id"], require_kinds=("smoke",))
    selfmod.approve(run["id"], approver="user")
    candidate = Path(selfmod.get_run(run["id"])["workspace_path"])
    (candidate / "target.txt").write_text("tampered after review\n", encoding="utf-8")

    child_env = dict(os.environ)
    child_env["SONDER_SELFMOD_HOME"] = str(state)
    child_env["SONDER_SELFMOD_DB"] = str(database)
    child_env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + os.pathsep + child_env.get("PYTHONPATH", "")
    child = subprocess.run(
        [sys.executable, "-c", f"import selfmod; selfmod.deploy({run['id']!r}, commit=False)"],
        cwd=str(root), env=child_env, capture_output=True, text=True, timeout=20, check=False,
    )
    assert child.returncode != 0
    assert "candidate bytes differ from tested bytes" in (child.stdout + child.stderr)
    assert (root / "target.txt").read_text(encoding="utf-8") == "original\n"


def test_reopened_spill_consumer_refuses_same_record_byte_tamper(tmp_path: Path):
    database = tmp_path / "spill.sqlite"
    producer = SQLiteSpillStore(database)
    handle = producer.begin(SpillSpec(128, media_type="text/plain", name="session-attachment"))
    handle.write(b"persisted session attachment")
    artifact = handle.commit()
    handle.close()
    reference = SpillReference(artifact.sha256, "persisted session attachment", artifact.size_bytes, "text/plain", "session")

    reopened = SQLiteSpillStore(database)
    assert reopened.read(artifact, max_bytes=128) == b"persisted session attachment"
    # Tamper the durable payload in place, preserving the path/row identity.
    import sqlite3
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE execution_spill SET payload=?,size_bytes=? WHERE spill_id=?",
            (b"tampered session attachment", len(b"tampered session attachment"), artifact.artifact_id),
        )
    with pytest.raises(DurableSpillIntegrityError):
        SQLiteSpillStore(database).read(artifact, max_bytes=128)
    with pytest.raises(DurableSpillIntegrityError):
        DurableExecutionOutput(SQLiteSpillStore(database)).read(reference, max_bytes=128)


def test_ephemeral_signed_manifest_reopen_and_tamper_refuse_before_activation(tmp_path: Path):
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    public = key.public_key()
    signer = "qualification-ephemeral-ed25519"
    artifact_path = tmp_path / "release.bin"
    artifact_path.write_bytes(b"release-bytes-v1")
    digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    unsigned = SignedManifest("1.0.0", digest, signer, "pending")
    signature = base64.b64encode(key.sign(unsigned.signing_bytes())).decode("ascii")
    manifest_path = tmp_path / "signed-manifest.json"
    manifest_path.write_text(json.dumps({
        "version": unsigned.version, "artifact_digest": unsigned.artifact_digest,
        "signer": unsigned.signer, "signature": signature,
    }, sort_keys=True), encoding="utf-8")

    def reopened_manifest() -> SignedManifest:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        return SignedManifest(raw["version"], raw["artifact_digest"], raw["signer"], raw["signature"])

    def verify(message: bytes, encoded: str, identity: str) -> bool:
        if identity != signer:
            return False
        try:
            public.verify(base64.b64decode(encoded), message)
            return True
        except (InvalidSignature, ValueError):
            return False

    admitted = []
    activation = UpdateActivation(verify, lambda manifest: admitted.append(manifest.version) or True)
    reopened = reopened_manifest()
    record = activation.activate(reopened, artifact_path.read_bytes())
    assert record.artifact_digest == digest and admitted == ["1.0.0"]

    # A same-path artifact byte change is rejected before the health/activation callback.
    artifact_path.write_bytes(b"release-bytes-tampered")
    with pytest.raises(ValueError, match="artifact digest"):
        activation.activate(reopened_manifest(), artifact_path.read_bytes())
    assert admitted == ["1.0.0"]

    # A persisted manifest-byte change invalidates the real Ed25519 signature.
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["version"] = "9.9.9"
    manifest_path.write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")
    with pytest.raises(ValueError, match="signature"):
        activation.activate(reopened_manifest(), b"release-bytes-v1")
    assert admitted == ["1.0.0"]

    # A persisted signature-byte change is also rejected before activation.
    raw["version"] = "1.0.0"
    raw["signature"] = base64.b64encode(b"tampered-signature").decode("ascii")
    manifest_path.write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")
    with pytest.raises(ValueError, match="signature"):
        activation.activate(reopened_manifest(), b"release-bytes-v1")
    assert admitted == ["1.0.0"]
