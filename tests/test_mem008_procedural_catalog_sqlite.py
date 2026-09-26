"""MEM-008: durable procedural skill catalog over a real SQLite file."""
from dataclasses import replace
import json
import sqlite3

import pytest

from sonder_runtime.adapters.persistence.sqlite.skill_catalog import (
    CatalogStoreError,
    SQLiteCatalogSnapshotStore,
)
from sonder_runtime.application.promotion.gates import MeasuredPromotionGates
from sonder_runtime.application.skill_refresh import SkillRevision, SkillTrust
from sonder_runtime.application.skills.composition import (
    build_procedural_publication_composition,
)
from sonder_runtime.application.skills.procedural_publication import (
    DurableLastGoodCatalog,
    HeldOutEvidence,
    InMemoryActiveSkillPort,
    PublicationError,
    PublicationState,
    SkillPublication,
)
from sonder_runtime.domain.memory.wp6_typed import (
    Evidence,
    EvidenceKind,
    MemoryLabel,
    TypedMemory,
)
from sonder_runtime.domain.promotion.measured import (
    MeasuredEvidence,
    PromotionArea,
    PromotionPolicy,
)


def _memory() -> TypedMemory:
    return TypedMemory(
        "memory-1", "Use the bounded workflow.", MemoryLabel.PROCEDURAL,
        (Evidence(EvidenceKind.TEST_PASS, "heldout-suite", weight=1.0),), (),
    )


def _evidence(digest: str) -> HeldOutEvidence:
    return HeldOutEvidence("heldout-v1", "suite-sha", digest, "base-sha", True, {"success": 1.0})


def _decision(candidate: str, evidence_digest: str):
    measured = MeasuredEvidence(
        PromotionArea.SKILLS, candidate, "base@sha", {"quality": 1.0}, True,
        ("eval-suite@sha",), "base-sha",
    )
    decision = MeasuredPromotionGates({PromotionArea.SKILLS: PromotionPolicy(
        minimums={"quality": 0.9}, required_provenance=("eval-suite@sha",),
    )}).evaluate(measured)
    return replace(decision, evidence_digest=evidence_digest)


def _publish(graph, skill_id: str, version: str):
    digest = f"{skill_id}-digest-{version}"
    ev = _evidence(digest)
    candidate = SkillPublication(skill_id, version, digest, "Use the bounded workflow.", ev.digest, "memory-1")
    return graph.publish(
        _memory(), candidate, SkillRevision(skill_id, digest, version, SkillTrust.PROJECT),
        ev, _decision(f"{skill_id}@{version}", ev.digest),
        source_interaction_ids=(f"interaction-{skill_id}-{version}",),
    )


def _open(path, active=None):
    store = SQLiteCatalogSnapshotStore(path)
    graph = build_procedural_publication_composition(
        active=active or InMemoryActiveSkillPort(), store=store,
    )
    return store, graph


def _seed(path):
    store, graph = _open(path)
    _publish(graph, "bounded", "1")
    _publish(graph, "bounded", "2")
    _publish(graph, "quarantined", "1")
    graph.disable("quarantined", "operator quarantine")
    return store, graph


def test_publish_restart_restores_active_last_good_and_disabled(tmp_path):
    path = tmp_path / "state" / "skills.sqlite3"
    store, graph = _seed(path)
    assert store.generation() == 4
    before = graph.catalog.snapshot()

    active = InMemoryActiveSkillPort()
    reopened_store, reopened = _open(path, active)

    assert reopened.catalog.snapshot() == before
    assert reopened.catalog.current("bounded").version == "2"
    assert reopened.catalog.last_good("bounded").version == "1"
    assert reopened.catalog.disabled_reason("quarantined") == "operator quarantine"
    assert reopened.catalog.current("quarantined") is None
    restored_active = active.current("bounded")
    assert restored_active is not None and restored_active.version == "2"
    assert restored_active.state is PublicationState.ACTIVE
    assert reopened_store.generation() == 4

    with pytest.raises(PublicationError, match="disabled"):
        _publish(reopened, "quarantined", "2")
    assert reopened_store.generation() == 4


def test_rollback_after_restart_restores_last_good_durably(tmp_path):
    path = tmp_path / "skills.sqlite3"
    _seed(path)

    active = InMemoryActiveSkillPort()
    store, reopened = _open(path, active)
    restored = reopened.rollback("bounded")
    assert restored.version == "1"
    assert active.current("bounded") == restored
    assert store.generation() == 5

    third_active = InMemoryActiveSkillPort()
    _, third = _open(path, third_active)
    assert third.catalog.current("bounded").version == "1"
    assert third.catalog.last_good("bounded").version == "2"
    assert third_active.current("bounded").version == "1"

    third.enable("quarantined")
    _, fourth = _open(path)
    assert fourth.catalog.disabled_reason("quarantined") is None


@pytest.mark.parametrize("tamper", ["payload", "digest", "malformed"])
def test_tampered_store_fails_closed_before_touching_active_port(tmp_path, tamper):
    path = tmp_path / "skills.sqlite3"
    _seed(path)
    connection = sqlite3.connect(path)
    try:
        payload_json, digest = connection.execute(
            "SELECT payload_json,snapshot_digest FROM procedural_skill_catalog"
        ).fetchone()
        if tamper == "payload":
            payload = json.loads(payload_json)
            payload["active"] = [["bounded", "1"]]
            payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        elif tamper == "digest":
            digest = "0" * 64
        else:
            payload_json = payload_json[: len(payload_json) // 2]
        with connection:
            connection.execute(
                "UPDATE procedural_skill_catalog SET payload_json=?,snapshot_digest=?",
                (payload_json, digest),
            )
    finally:
        connection.close()

    active = InMemoryActiveSkillPort()
    with pytest.raises(CatalogStoreError):
        SQLiteCatalogSnapshotStore(path).load()
    with pytest.raises(CatalogStoreError):
        _open(path, active)
    assert active.snapshot() == {}


def test_store_refuses_to_persist_a_snapshot_that_does_not_verify(tmp_path):
    store = SQLiteCatalogSnapshotStore(tmp_path / "skills.sqlite3")
    forged = replace(DurableLastGoodCatalog().snapshot(), active=(("ghost", "1"),))
    with pytest.raises(CatalogStoreError):
        store.save(forged)
    assert store.load() is None
    assert store.generation() == 0


class _FailingStore(SQLiteCatalogSnapshotStore):
    fail = False

    def save(self, snapshot):
        if self.fail:
            raise OSError("disk full")
        super().save(snapshot)


def test_save_failure_rolls_back_catalog_and_active_port(tmp_path):
    path = tmp_path / "skills.sqlite3"
    store = _FailingStore(path)
    active = InMemoryActiveSkillPort()
    graph = build_procedural_publication_composition(active=active, store=store)
    first = _publish(graph, "bounded", "1")
    catalog_before = graph.catalog.snapshot()
    active_before = active.snapshot()

    store.fail = True
    with pytest.raises(PublicationError, match="rolled back"):
        _publish(graph, "bounded", "2")
    assert graph.catalog.snapshot() == catalog_before
    assert active.snapshot() == active_before
    assert active.current("bounded") == first

    with pytest.raises(PublicationError, match="disable failed"):
        graph.disable("bounded", "operator quarantine")
    assert graph.catalog.disabled_reason("bounded") is None
    assert graph.catalog.snapshot() == catalog_before

    store.fail = False
    assert store.generation() == 1
    assert store.load() == catalog_before


def test_catalog_and_store_are_mutually_exclusive(tmp_path):
    with pytest.raises(ValueError, match="either"):
        build_procedural_publication_composition(
            catalog=DurableLastGoodCatalog(), active=InMemoryActiveSkillPort(),
            store=SQLiteCatalogSnapshotStore(tmp_path / "skills.sqlite3"),
        )


def test_second_writer_on_the_same_file_is_refused_and_rolled_back(tmp_path):
    path = tmp_path / "skills.sqlite3"
    first_active = InMemoryActiveSkillPort()
    second_active = InMemoryActiveSkillPort()
    first_store, first = _open(path, first_active)
    second_store, second = _open(path, second_active)

    _publish(first, "alpha", "1")
    with pytest.raises(CatalogStoreError, match="changed since it was loaded"):
        _publish(second, "beta", "1")
    assert second.catalog.current("beta") is None
    assert second_active.current("beta") is None
    assert second_store.generation() == 1

    # A writer that reloads sees the other publication and may then write.
    third_store, third = _open(path)
    assert third.catalog.current("alpha").version == "1"
    _publish(third, "beta", "1")
    assert third_store.generation() == 2
    with pytest.raises(CatalogStoreError, match="changed since it was loaded"):
        _publish(first, "alpha", "2")
    assert first.catalog.current("alpha").version == "1"

    _, reopened = _open(path)
    assert reopened.catalog.current("alpha").version == "1"
    assert reopened.catalog.current("beta").version == "1"
    assert first_active.current("alpha").version == "1"


def test_unloaded_store_refuses_to_overwrite_existing_catalog(tmp_path):
    path = tmp_path / "skills.sqlite3"
    _seed(path)
    stranger = SQLiteCatalogSnapshotStore(path)
    with pytest.raises(CatalogStoreError, match="changed since it was loaded"):
        stranger.save(DurableLastGoodCatalog().snapshot())
    assert stranger.generation() == 4


class _RecordingEvents:
    def __init__(self, fail_on=None):
        self.events = []
        self.fail_on = fail_on

    def emit(self, kind, *, summary, detail):
        self.events.append((kind, summary))
        if kind == self.fail_on:
            raise RuntimeError("event sink unavailable")


def _open_with_events(store, events, active=None):
    return build_procedural_publication_composition(
        active=active or InMemoryActiveSkillPort(), store=store, events=events,
    )


def test_committed_event_is_emitted_only_after_the_durable_save(tmp_path):
    store = _FailingStore(tmp_path / "skills.sqlite3")
    events = _RecordingEvents()
    graph = _open_with_events(store, events)
    _publish(graph, "bounded", "1")
    assert events.events == [
        ("procedural_skill_published", "procedural skill publication committed"),
    ]

    store.fail = True
    events.events.clear()
    with pytest.raises(PublicationError, match="rolled back"):
        _publish(graph, "bounded", "2")
    assert events.events == [
        ("procedural_skill_publication_failed", "procedural skill publication rolled back"),
    ]


def test_failure_after_the_durable_save_writes_the_prior_catalog_back(tmp_path):
    path = tmp_path / "skills.sqlite3"
    store = SQLiteCatalogSnapshotStore(path)
    events = _RecordingEvents()
    active = InMemoryActiveSkillPort()
    graph = _open_with_events(store, events, active)
    first = _publish(graph, "bounded", "1")
    catalog_before = graph.catalog.snapshot()

    events.fail_on = "procedural_skill_published"
    with pytest.raises(PublicationError, match="rolled back"):
        _publish(graph, "bounded", "2")
    assert graph.catalog.snapshot() == catalog_before
    assert active.current("bounded") == first
    # The compensating save moved the generation on but restored the content.
    assert store.generation() == 3
    assert store.load() == catalog_before

    restarted = InMemoryActiveSkillPort()
    _, reopened = _open(path, restarted)
    assert reopened.catalog.current("bounded").version == "1"
    assert restarted.current("bounded").version == "1"


class _SaveOnceStore(SQLiteCatalogSnapshotStore):
    """Accepts the next save, then fails every later one."""

    armed = False

    def save(self, snapshot):
        if self.armed == "failing":
            raise OSError("disk went away")
        super().save(snapshot)
        if self.armed:
            self.armed = "failing"


def test_failed_compensation_is_reported_as_a_durable_divergence(tmp_path):
    store = _SaveOnceStore(tmp_path / "skills.sqlite3")
    events = _RecordingEvents()
    active = InMemoryActiveSkillPort()
    graph = _open_with_events(store, events, active)
    first = _publish(graph, "bounded", "1")
    catalog_before = graph.catalog.snapshot()

    store.armed = True
    events.fail_on = "procedural_skill_published"
    with pytest.raises(PublicationError, match="durable catalog rollback failed") as failed:
        _publish(graph, "bounded", "2")
    assert isinstance(failed.value.__cause__, OSError)
    assert graph.catalog.snapshot() == catalog_before
    assert active.current("bounded") == first
