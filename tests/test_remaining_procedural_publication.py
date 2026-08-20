from dataclasses import replace

import pytest

from sonder_runtime.application.skill_refresh import SkillRevision, SkillTrust
from sonder_runtime.application.skills.procedural_publication import (
    DurableLastGoodCatalog,
    HeldOutEvidence,
    PublicationError,
    PublicationState,
    SkillPublication,
)


def evidence(candidate_digest: str, *, passed: bool = True) -> HeldOutEvidence:
    return HeldOutEvidence("heldout-v1", "suite-sha", candidate_digest, "base-sha", passed, {"success": 1.0})


def candidate(skill_id: str, version: str, digest: str, ev: HeldOutEvidence) -> SkillPublication:
    return SkillPublication(skill_id, version, digest, "Use the bounded workflow.", ev.digest, "memory-1")


def revision(skill_id: str, version: str, digest: str) -> SkillRevision:
    return SkillRevision(skill_id, digest, version, SkillTrust.PROJECT)


def test_publish_requires_matching_held_out_evidence_and_trusted_revision():
    ev = evidence("digest-1")
    catalog = DurableLastGoodCatalog()
    published = catalog.publish(candidate("bounded", "1", "digest-1", ev), revision("bounded", "1", "digest-1"), ev)
    assert published.state is PublicationState.ACTIVE
    assert catalog.current("bounded") == published

    with pytest.raises(PublicationError, match="different candidate"):
        catalog.publish(candidate("bounded", "2", "digest-2", ev), revision("bounded", "2", "digest-2"), ev)
    with pytest.raises(PublicationError, match="did not pass"):
        failed = evidence("digest-3", passed=False)
        catalog.publish(candidate("bounded", "3", "digest-3", failed), revision("bounded", "3", "digest-3"), failed)


def test_versioned_publish_retains_last_good_and_rolls_back():
    catalog = DurableLastGoodCatalog()
    first_evidence = evidence("digest-1")
    catalog.publish(candidate("bounded", "1", "digest-1", first_evidence), revision("bounded", "1", "digest-1"), first_evidence)
    second_evidence = evidence("digest-2")
    catalog.publish(candidate("bounded", "2", "digest-2", second_evidence), revision("bounded", "2", "digest-2"), second_evidence)
    assert catalog.last_good("bounded").version == "1"
    assert catalog.rollback("bounded").version == "1"
    assert catalog.current("bounded").version == "1"


def test_disablement_is_explicit_and_survives_snapshot_restore():
    catalog = DurableLastGoodCatalog()
    ev = evidence("digest-1")
    catalog.publish(candidate("bounded", "1", "digest-1", ev), revision("bounded", "1", "digest-1"), ev)
    catalog.disable("bounded", "operator quarantine")
    assert catalog.current("bounded") is None
    assert catalog.disabled_reason("bounded") == "operator quarantine"
    restored = DurableLastGoodCatalog.from_snapshot(catalog.snapshot())
    assert restored.disabled_reason("bounded") == "operator quarantine"
    with pytest.raises(PublicationError, match="disabled"):
        restored.publish(candidate("bounded", "2", "digest-2", evidence("digest-2")), revision("bounded", "2", "digest-2"), evidence("digest-2"))


def test_untrusted_and_tampered_snapshot_are_rejected():
    catalog = DurableLastGoodCatalog()
    ev = evidence("digest-1")
    with pytest.raises(PublicationError, match="trusted"):
        catalog.publish(candidate("bounded", "1", "digest-1", ev), revision("bounded", "1", "digest-1").__class__("bounded", "digest-1", "1", SkillTrust.UNTRUSTED), ev)
    catalog.publish(candidate("bounded", "1", "digest-1", ev), revision("bounded", "1", "digest-1"), ev)
    snapshot = replace(catalog.snapshot(), snapshot_digest="0" * 64)
    with pytest.raises(PublicationError, match="integrity"):
        DurableLastGoodCatalog.from_snapshot(snapshot)
