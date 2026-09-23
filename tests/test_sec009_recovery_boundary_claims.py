"""SEC-009: recovery and audit files are never claimed as a security boundary.

The requirement is a truthfulness boundary: same-user recovery state, backups,
and audit files can be rewritten together by a process that was explicitly
granted ``--unrestricted-selfmod``, so neither the runtime nor its documents
may present them as protection against that process.

Three layers are proven here:

* the typed contract cannot be made to claim a boundary, including through
  forged or replaced dataclass state;
* the durable evidence repository carries the startup unrestricted-selfmod
  disclosure on *every* record it returns, including later verification;
* a repository-wide claim ratchet rejects affirmative assurance language about
  recovery/backup/audit material in tracked documentation and runtime strings,
  while requiring the operator-facing disclosure to stay present.
"""
from __future__ import annotations

import dataclasses
import io
import re
import subprocess
import tokenize
from pathlib import Path

import pytest

from sonder_runtime.adapters.security.recovery_evidence import (
    FilesystemRecoveryEvidenceRepository,
)
from sonder_runtime.application.security.recovery_artifacts import (
    RecoveryArtifactService,
)
from sonder_runtime.application.security.recovery_boundary import (
    RecoveryBoundary,
    RecoveryBoundaryAssessment,
)
from sonder_runtime.application.security.recovery_evidence import (
    RecoveryEvidenceError,
    RecoveryEvidenceRecord,
)

ROOT = Path(__file__).resolve().parents[1]
UNRESTRICTED_NOTICE = "alter recovery state and audit files"


# ---------------------------------------------------------------------------
# Typed contract
# ---------------------------------------------------------------------------

def test_assessment_refuses_a_declared_boundary_for_every_actor_shape():
    for actor, owner in (("u", "u"), ("u", "other")):
        for unrestricted in (False, True):
            with pytest.raises(ValueError, match="cannot be declared"):
                RecoveryBoundaryAssessment(
                    actor=actor,
                    resource_owner=owner,
                    kind=RecoveryBoundary.assess(actor=actor, resource_owner=owner).kind,
                    unrestricted_selfmod=unrestricted,
                    security_boundary=True,
                    limitations=("claimed",),
                )


def test_replace_cannot_smuggle_a_boundary_claim():
    assessment = RecoveryBoundary.assess(
        actor="u", resource_owner="u", unrestricted_selfmod=True,
    )
    with pytest.raises(ValueError):
        dataclasses.replace(assessment, security_boundary=True)
    with pytest.raises(ValueError):
        dataclasses.replace(assessment, limitations=())


def test_forged_assessment_still_yields_no_claim_and_no_evidence_record(tmp_path):
    assessment = RecoveryBoundary.assess(actor="u", resource_owner="u")
    object.__setattr__(assessment, "security_boundary", True)
    # The decision function does not trust the field.
    assert RecoveryBoundary.can_claim_security_boundary(assessment) is False
    service = RecoveryArtifactService((tmp_path / "r").resolve(), owner="u")
    artifact = service.write("run-1", actor="u", kind="rollback", payload={})
    with pytest.raises(RecoveryEvidenceError, match="security boundary"):
        RecoveryEvidenceRecord(
            artifact=artifact,
            path=(tmp_path / "r" / "run-1.json").resolve(),
            boundary=assessment,
        )
    with pytest.raises(RecoveryEvidenceError, match="tamper-evident"):
        RecoveryEvidenceRecord(
            artifact=artifact,
            path=(tmp_path / "r" / "run-1.json").resolve(),
            boundary=RecoveryBoundary.assess(actor="u", resource_owner="u"),
            tamper_evident_only=False,
        )


def test_non_bool_unrestricted_flag_is_rejected_not_coerced():
    for value in ("false", 0, 1, None):
        with pytest.raises(TypeError):
            RecoveryBoundary.assess(
                actor="u", resource_owner="u", unrestricted_selfmod=value,
            )


# ---------------------------------------------------------------------------
# Durable evidence repository
# ---------------------------------------------------------------------------

def _repository(tmp_path: Path, **kwargs) -> FilesystemRecoveryEvidenceRepository:
    service = RecoveryArtifactService((tmp_path / "recovery").resolve(), owner="same-user")
    return FilesystemRecoveryEvidenceRepository(service, **kwargs)


def test_verification_under_unrestricted_startup_keeps_the_disclosure(tmp_path):
    # A record verified while --unrestricted-selfmod is active must disclose
    # that the verifying process could have rewritten artifact and audit
    # chain together; "verified" is not evidence against that process.
    repository = _repository(tmp_path, unrestricted_selfmod=True)
    recorded = repository.record(
        "run-1", actor="same-user", kind="rollback", payload={"release": "r1"},
    )
    verified = repository.verify("run-1", actor="same-user")
    for record in (recorded, verified):
        assert record.boundary.unrestricted_selfmod is True
        assert record.boundary.security_boundary is False
        assert UNRESTRICTED_NOTICE in RecoveryBoundary.recovery_notice(record.boundary)


def test_per_call_flag_cannot_downgrade_the_startup_disclosure(tmp_path):
    repository = _repository(tmp_path, unrestricted_selfmod=True)
    record = repository.record(
        "run-1", actor="same-user", kind="rollback", payload={},
        unrestricted_selfmod=False,
    )
    assert record.boundary.unrestricted_selfmod is True
    assert UNRESTRICTED_NOTICE in RecoveryBoundary.recovery_notice(record.boundary)


def test_restricted_repository_still_discloses_same_user_and_audit_limits(tmp_path):
    repository = _repository(tmp_path)
    repository.record("run-1", actor="same-user", kind="rollback", payload={})
    verified = repository.verify("run-1", actor="same-user")
    notice = RecoveryBoundary.recovery_notice(verified.boundary)
    assert verified.boundary.unrestricted_selfmod is False
    assert "not a security boundary" in notice
    assert "not tamper-resistant" in notice
    assert verified.tamper_evident_only is True


def test_repository_rejects_a_non_bool_startup_capability(tmp_path):
    service = RecoveryArtifactService((tmp_path / "recovery").resolve(), owner="u")
    with pytest.raises(TypeError):
        FilesystemRecoveryEvidenceRepository(service, unrestricted_selfmod="yes")


def test_consistent_same_user_rewrite_is_not_reported_as_detected(tmp_path):
    # The honest limit: a same-user actor that rewrites the artifact and
    # the whole audit chain consistently is indistinguishable from history.
    # The repository must say "verified" *and* carry the limitation, never
    # present the verification as proof against that actor.
    root = (tmp_path / "recovery").resolve()
    repository = _repository(tmp_path, unrestricted_selfmod=True)
    repository.record("run-1", actor="same-user", kind="rollback", payload={"v": 1})
    # Rebuild the entire store from scratch with different content, as an
    # unrestricted self-mod process could.
    for path in sorted(root.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    forged = _repository(tmp_path, unrestricted_selfmod=True)
    forged.record("run-1", actor="same-user", kind="rollback", payload={"v": 2})
    result = forged.verify("run-1", actor="same-user")
    assert result.verified is True
    assert result.artifact.payload == {"v": 2}
    assert UNRESTRICTED_NOTICE in RecoveryBoundary.recovery_notice(result.boundary)


# ---------------------------------------------------------------------------
# Repository-wide claim ratchet
# ---------------------------------------------------------------------------

SUBJECT = re.compile(
    r"\b(recover(?:y|ies|ed)?|rollback|backups?|audit(?:s|ed)?|"
    r"selfmod_events|manifest\.sha256)\b",
    re.IGNORECASE,
)
# "immutable" alone usually describes a frozen value object, so it counts
# only when it qualifies stored recovery/backup/audit material directly.
ASSURANCE = re.compile(
    r"\b(immutable (?:backups?|backup bundles?|audit\w*|recovery (?:files?|state|"
    r"artifacts?|bundles?))|(?:backups?|audit (?:files?|logs?|trail)) (?:is|are) "
    r"immutable|tamper[- ]?proof|tamper[- ]?resistant|"
    r"security boundary|cannot be (?:altered|tampered|forged|rewritten|modified)|"
    r"unforgeable|guaranteed? (?:integrity|authenticity))\b",
    re.IGNORECASE,
)
LIMITED = re.compile(
    r"\b(not|never|no|cannot claim|isn't|aren't|without|only|"
    r"tamper[- ]evident|evidence only|must not|does not|do not)\b",
    re.IGNORECASE,
)
SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+|\n\s*[-*|]\s*|\n{2,}")
# Documents whose job is to quote the forbidden phrasing (the requirement
# text itself) rather than make the claim.
EXEMPT = {
    "docs/architecture/SONDER-MASTER-IMPLEMENTATION-SPEC.md",
    "docs/architecture/SPEC-5-End-State-Architecture.md",
}


def _tracked(patterns: tuple[str, ...]) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", *patterns],
        cwd=ROOT, capture_output=True, check=True,
    )
    return [ROOT / raw.decode("utf-8") for raw in result.stdout.split(b"\0") if raw]


def _python_strings(text: str) -> str:
    chunks = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.STRING:
                chunks.append(token.string)
            elif token.type == tokenize.COMMENT:
                chunks.append(token.string.lstrip("#"))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return text
    return "\n\n".join(chunks)


def overclaims(text: str) -> list[str]:
    """Return sentences that assert recovery/audit assurance without a limit."""
    flattened = re.sub(r"(?<!\n)\n(?![\n\-*|])", " ", text)
    findings = []
    for sentence in SENTENCE_SPLIT.split(flattened):
        if SUBJECT.search(sentence) and ASSURANCE.search(sentence):
            if not LIMITED.search(sentence):
                findings.append(" ".join(sentence.split())[:240])
    return findings


@pytest.mark.parametrize("text", [
    "`selfmod.py` owns SQLite state, immutable backups, hashes, and budgets.",
    "The audit log is tamper-proof.",
    "Recovery files form a security boundary against selfmod.",
    "Backups cannot be altered by the candidate once written.",
    "- recovery bundle: immutable audit trail",
])
def test_ratchet_detects_affirmative_assurance(text):
    assert overclaims(text)


@pytest.mark.parametrize("text", [
    "Audit files are evidence only and are not tamper-resistant.",
    "Same-user recovery is not a security boundary.",
    "This is tamper-evident evidence, not tamper-resistant storage.",
    "Backups are hash-verified before restoration.",
    "Sonder must not claim that its audit database is a security boundary.",
])
def test_ratchet_accepts_limited_or_neutral_statements(text):
    assert overclaims(text) == []


# Known remaining overclaims, pinned to exact counts so the list can only
# shrink.  Both files describe backups as "immutable" and are owned by the
# open low-integrity self-mod change (PR #519), so this lane may not edit
# them.  SEC-009 stays unverified until these entries are gone.
KNOWN_DEBT = {
    "selfmod.py": 3,
    "scripts/nightly_selfmod.py": 2,
}


def _repository_overclaims() -> dict[str, list[str]]:
    findings: dict[str, list[str]] = {}
    for path in _tracked(("*.md",)):
        relative = path.relative_to(ROOT).as_posix()
        if relative in EXEMPT or not path.is_file():
            continue
        found = overclaims(path.read_text(encoding="utf-8", errors="replace"))
        if found:
            findings[relative] = found
    for path in _tracked(("sonder_runtime/*.py", "selfmod*.py", "scripts/*selfmod*.py")):
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT).as_posix()
        text = _python_strings(path.read_text(encoding="utf-8", errors="replace"))
        found = overclaims(text)
        if found:
            findings[relative] = found
    return findings


def test_no_tracked_document_or_runtime_string_overclaims_recovery_or_audit():
    findings = _repository_overclaims()
    unexpected = {
        path: sentences for path, sentences in findings.items()
        if path not in KNOWN_DEBT
    }
    assert unexpected == {}
    # The ratchet is exact: fixing a known overclaim must also shrink the
    # pinned debt, and no pinned file may gain a new one.
    assert {path: len(findings.get(path, [])) for path in KNOWN_DEBT} == KNOWN_DEBT


def test_scan_actually_covers_the_operator_documents_and_runtime():
    # Guard against a vacuous pass: the scan set must include the files
    # that describe self-modification and recovery.
    markdown = {p.relative_to(ROOT).as_posix() for p in _tracked(("*.md",))}
    python = {
        p.relative_to(ROOT).as_posix()
        for p in _tracked(("sonder_runtime/*.py", "selfmod*.py", "scripts/*selfmod*.py"))
    }
    assert {"SELFMOD.md", "SECURITY.md", "README.md"} <= markdown
    assert {
        "selfmod.py",
        "selfmod_recover.py",
        "sonder_runtime/application/security/recovery_boundary.py",
        "sonder_runtime/application/selfmod/selfmod_service.py",
    } <= python


def test_operator_documents_disclose_the_unrestricted_recovery_limit():
    selfmod = (ROOT / "SELFMOD.md").read_text(encoding="utf-8")
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    for text in (selfmod, security):
        flat = " ".join(text.split())
        assert "--unrestricted-selfmod" in flat
        assert re.search(
            r"not a security boundary against (?:a process granted )?"
            r"(?:explicitly )?`?--unrestricted-selfmod`?", flat,
        ), "unrestricted recovery/audit disclosure missing"
