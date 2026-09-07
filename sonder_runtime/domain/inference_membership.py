"""Pure contracts for bounded, externally admitted inference membership.

These values perform no discovery, transport, persistence, scheduling, or
ownership decisions. Signature verification is supplied by the trusted adapter
as a pure callable over the exact canonical signed envelope. The adapter must
also enforce its configured issuer, member-origin/SAN/CIDR and mTLS policies.
A reconciliation is only a proposal: its high-water value must be durably
compare-and-advanced before a future controller applies any roster change.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import re
from typing import Callable


MAX_ADVERTISEMENTS = 4096
MAX_SNAPSHOT_BYTES = 1_048_576
MAX_ROSTER_WORKERS = 256
PROTOCOL_VERSION = 1
_MAX_GENERATION = (1 << 63) - 1
_STATES = frozenset({"probation", "active", "draining", "expired", "unhealthy", "revoked"})
_CONSIDERED_STATES = frozenset({"probation", "active", "unhealthy"})


def _integer(value: int, low: int, high: int, name: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError("%s is outside its bounded integer range" % name)
    return value


def _identity(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
        raise ValueError("membership identity must be a bounded opaque ASCII identifier")
    return value


def _origin(value: str) -> str:
    # Parse an origin only, without resolving DNS or permitting URL credentials,
    # paths, queries, fragments, zone identifiers, or ambiguous bind-all hosts.
    match = re.fullmatch(r"https://(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+):([0-9]{1,5})/?", value) if isinstance(value, str) else None
    if not match or len(value) > 2048:
        raise ValueError("member origin must be an explicit HTTPS origin")
    host, port = match.groups()
    if not 1 <= int(port) <= 65535:
        raise ValueError("member origin has an invalid port")
    host = host.lower().rstrip(".")
    if host == "localhost":
        host = "127.0.0.1"
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        labels = host.split(".")
        if (len(host) > 253 or host.startswith("[") or all(label.isdigit() for label in labels)
                or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)):
            raise ValueError("member origin has an invalid hostname")
    else:
        if address.is_unspecified or (host.startswith("[") and address.version != 6):
            raise ValueError("member origin must name one concrete endpoint")
        host = "[%s]" % address.compressed if address.version == 6 else address.compressed
    return "https://%s:%d" % (host, int(port))


def _aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("%s must be a timezone-aware datetime" % name)
    return value.astimezone(timezone.utc)


def _timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value,
    ):
        raise ValueError("snapshot timestamp must include an explicit timezone")
    return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")), "snapshot timestamp")


def _bounded_tuple(value, maximum: int, name: str) -> tuple:
    # Do not consume arbitrary iterators or silently trim remote records.
    if not isinstance(value, (tuple, list)) or len(value) > maximum:
        raise ValueError("%s exceeds its bounded sequence limit" % name)
    return tuple(value)


def _key(value) -> tuple[str, str, int]:
    return value.worker_id, value.origin, value.member_generation


@dataclass(frozen=True, slots=True)
class WorkerAdvertisement:
    worker_id: str
    origin: str
    member_generation: int
    lifecycle_state: str = "active"
    models: tuple[str, ...] = ()
    advertised_capacity: int = 1

    def __post_init__(self) -> None:
        _identity(self.worker_id)
        object.__setattr__(self, "origin", _origin(self.origin))
        _integer(self.member_generation, 1, _MAX_GENERATION, "member generation")
        if not isinstance(self.lifecycle_state, str) or self.lifecycle_state not in _STATES:
            raise ValueError("invalid advertised lifecycle state")
        models = _bounded_tuple(self.models, 2048, "model keys")
        for model in models:
            if not isinstance(model, str) or len(model) > 256 or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._/-]*(?::[A-Za-z0-9._-]+)?", model,
            ):
                raise ValueError("invalid bounded model key")
        object.__setattr__(self, "models", models)
        _integer(self.advertised_capacity, 1, 64, "advertised capacity")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate signed JSON field")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True, init=False)
class MembershipSnapshot:
    """Fields and digest derived exclusively from one verified signed envelope.

    Version 1 wire format is exactly ``{payload, signature}``. The payload has
    the fields below except digest/canonical_envelope; workers use the exact
    WorkerAdvertisement fields. Timestamps are RFC3339 with a timezone.
    Canonical bytes are ASCII JSON with sorted object keys, compact separators,
    escaped non-ASCII, no duplicate fields, and no non-finite numbers. Array
    order is signed and retained. The signature encoding/algorithm and trusted
    key selection belong to the injected verifier, never to remote input here.
    """

    cluster_id: str
    issuer_id: str
    generation: int
    protocol_version: int
    issued_at: datetime
    expires_at: datetime
    workers: tuple[WorkerAdvertisement, ...]
    digest: str
    canonical_envelope: bytes = field(repr=False)

    def __init__(self) -> None:
        raise TypeError("use MembershipSnapshot.from_signed_envelope")

    @classmethod
    def from_signed_envelope(
        cls, canonical_envelope: bytes, *, verify: Callable[[bytes], bool],
        max_advertisements: int = MAX_ADVERTISEMENTS,
        max_bytes: int = MAX_SNAPSHOT_BYTES,
    ) -> MembershipSnapshot:
        _integer(max_advertisements, 1, MAX_ADVERTISEMENTS, "advertisement limit")
        _integer(max_bytes, 1, MAX_SNAPSHOT_BYTES, "snapshot byte limit")
        if type(canonical_envelope) is not bytes or not 0 < len(canonical_envelope) <= max_bytes:
            raise ValueError("signed snapshot exceeds its byte limit")
        try:
            signed = json.loads(canonical_envelope, object_pairs_hook=_unique_object)
            encoded = json.dumps(signed, sort_keys=True, separators=(",", ":"),
                                 ensure_ascii=True, allow_nan=False).encode("ascii")
        except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
            raise ValueError("malformed signed snapshot") from exc
        if encoded != canonical_envelope:
            raise ValueError("signed snapshot is not canonical")
        if not isinstance(signed, dict) or signed.keys() != {"payload", "signature"}:
            raise ValueError("invalid signed envelope fields")
        if not isinstance(signed["signature"], str) or not 0 < len(signed["signature"]) <= 8192:
            raise ValueError("invalid bounded signature")
        payload = signed["payload"]
        if not isinstance(payload, dict) or payload.keys() != {
            "cluster_id", "issuer_id", "generation", "protocol_version", "issued_at", "expires_at", "workers",
        }:
            raise ValueError("invalid membership payload fields")
        rows = _bounded_tuple(payload["workers"], max_advertisements, "advertisements")
        try:
            verified = verify(canonical_envelope)
        except Exception as exc:
            raise ValueError("snapshot verification failed") from exc
        if verified is not True:
            raise ValueError("snapshot verification failed")
        cluster = _identity(payload["cluster_id"])
        issuer = _identity(payload["issuer_id"])
        generation = _integer(payload["generation"], 1, _MAX_GENERATION, "snapshot generation")
        _integer(payload["protocol_version"], PROTOCOL_VERSION, PROTOCOL_VERSION, "protocol version")
        issued_at, expires_at = _timestamp(payload["issued_at"]), _timestamp(payload["expires_at"])
        if issued_at >= expires_at:
            raise ValueError("snapshot expiry must follow issue time")
        workers, identities, origins = [], set(), set()
        for row in rows:
            if not isinstance(row, dict) or row.keys() != {
                "worker_id", "origin", "member_generation", "lifecycle_state", "models", "advertised_capacity",
            }:
                raise ValueError("invalid advertisement fields")
            worker = WorkerAdvertisement(**row)
            if worker.worker_id in identities or worker.origin in origins:
                raise ValueError("duplicate member identity or canonical origin")
            identities.add(worker.worker_id)
            origins.add(worker.origin)
            workers.append(worker)
        result = object.__new__(cls)
        for name, value in (
            ("cluster_id", cluster), ("issuer_id", issuer), ("generation", generation),
            ("protocol_version", PROTOCOL_VERSION), ("issued_at", issued_at), ("expires_at", expires_at),
            ("workers", tuple(workers)), ("digest", hashlib.sha256(canonical_envelope).hexdigest()),
            ("canonical_envelope", canonical_envelope),
        ):
            object.__setattr__(result, name, value)
        return result


@dataclass(frozen=True, slots=True)
class MembershipHighWater:
    """Exact durable comparison record; this value performs no persistence."""

    cluster_id: str
    issuer_id: str
    generation: int
    digest: str

    def __post_init__(self) -> None:
        _identity(self.cluster_id)
        _identity(self.issuer_id)
        _integer(self.generation, 1, _MAX_GENERATION, "high-water generation")
        if not isinstance(self.digest, str) or not re.fullmatch(r"[0-9a-f]{64}", self.digest):
            raise ValueError("high-water digest must be a SHA-256 hex digest")


def _authority(value, cluster_id: str, issuer_id: str) -> None:
    if value.cluster_id != cluster_id or value.issuer_id != issuer_id:
        raise ValueError("membership authority does not match configured cluster and issuer")


def _water(snapshot: MembershipSnapshot) -> MembershipHighWater:
    return MembershipHighWater(snapshot.cluster_id, snapshot.issuer_id, snapshot.generation, snapshot.digest)


def validate_high_water(
    snapshot: MembershipSnapshot, previous: MembershipHighWater | None, *,
    cluster_id: str, issuer_id: str, clock: Callable[[], datetime],
) -> MembershipHighWater:
    """Return a proposed high-water record, never reset or persist one.

    A matching unexpired equal generation is an idempotent reread. A future
    issue time, expired lease, rollback, authority change, or equal-generation
    digest conflict fails closed. The caller must persist before applying.
    """
    _identity(cluster_id)
    _identity(issuer_id)
    now = _aware(clock(), "clock")
    if not isinstance(snapshot, MembershipSnapshot):
        raise ValueError("a verified membership snapshot is required")
    _authority(snapshot, cluster_id, issuer_id)
    if not snapshot.issued_at <= now < snapshot.expires_at:
        raise ValueError("snapshot is expired or issued in the future")
    if previous is not None:
        if not isinstance(previous, MembershipHighWater):
            raise ValueError("invalid high-water record")
        _authority(previous, cluster_id, issuer_id)
        if snapshot.generation < previous.generation:
            raise ValueError("snapshot generation rollback")
        if snapshot.generation == previous.generation and snapshot.digest != previous.digest:
            raise ValueError("same-generation snapshot digest conflict")
    return _water(snapshot)


@dataclass(frozen=True, slots=True)
class CapabilityEvidence:
    """Trusted adapter's successful probe for one exact member incarnation."""

    worker_id: str
    origin: str
    member_generation: int
    checked_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        _identity(self.worker_id)
        object.__setattr__(self, "origin", _origin(self.origin))
        _integer(self.member_generation, 1, _MAX_GENERATION, "member generation")
        object.__setattr__(self, "checked_at", _aware(self.checked_at, "capability issue time"))
        object.__setattr__(self, "expires_at", _aware(self.expires_at, "capability expiry"))
        if not 0 < (self.expires_at - self.checked_at).total_seconds() <= 86400:
            raise ValueError("capability expiry must follow its check time within the 86400-second TTL ceiling")


@dataclass(frozen=True, slots=True)
class RosterMember:
    advertisement: WorkerAdvertisement
    lifecycle_state: str
    evidence: CapabilityEvidence | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.advertisement, WorkerAdvertisement):
            raise ValueError("invalid roster advertisement")
        if self.lifecycle_state not in {"probation", "active", "unhealthy", "expired"}:
            raise ValueError("invalid admitted lifecycle state")
        if self.evidence is not None and (
            not isinstance(self.evidence, CapabilityEvidence) or _key(self.evidence) != _key(self.advertisement)
        ):
            raise ValueError("capability evidence does not bind this member")
        if self.lifecycle_state == "active" and self.evidence is None:
            raise ValueError("active membership requires capability evidence")


@dataclass(frozen=True, slots=True)
class MembershipRoster:
    snapshot: MembershipSnapshot
    members: tuple[RosterMember, ...]
    generation: int

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, MembershipSnapshot):
            raise ValueError("roster requires a verified snapshot")
        _integer(self.generation, 1, _MAX_GENERATION, "roster generation")
        members = _bounded_tuple(self.members, MAX_ROSTER_WORKERS, "roster members")
        advertisements = {worker.worker_id: worker for worker in self.snapshot.workers}
        seen = set()
        for member in members:
            if not isinstance(member, RosterMember):
                raise ValueError("invalid roster member")
            worker = member.advertisement
            if worker.worker_id in seen or advertisements.get(worker.worker_id) != worker:
                raise ValueError("roster member is duplicated or absent from verified snapshot")
            if worker.lifecycle_state not in _CONSIDERED_STATES:
                raise ValueError("roster cannot admit a revoked, draining, or expired advertisement")
            seen.add(worker.worker_id)
        object.__setattr__(self, "members", members)


@dataclass(frozen=True, slots=True)
class MembershipReconciliation:
    """Immutable proposal; drains reference original endpoints, never requests."""

    roster: MembershipRoster | None
    high_water: MembershipHighWater | None
    additions: tuple[WorkerAdvertisement, ...] = ()
    activations: tuple[WorkerAdvertisement, ...] = ()
    drains: tuple[WorkerAdvertisement, ...] = ()
    expirations: tuple[WorkerAdvertisement, ...] = ()
    omitted_worker_count: int = 0

    def __post_init__(self) -> None:
        if self.roster is not None and not isinstance(self.roster, MembershipRoster):
            raise ValueError("reconciliation requires an immutable membership roster")
        if self.high_water is not None and not isinstance(self.high_water, MembershipHighWater):
            raise ValueError("reconciliation requires an immutable high-water record")
        for name in ("additions", "activations", "drains", "expirations"):
            values = _bounded_tuple(getattr(self, name), MAX_ROSTER_WORKERS, name)
            if any(not isinstance(value, WorkerAdvertisement) for value in values):
                raise ValueError("invalid reconciliation advertisement")
            object.__setattr__(self, name, values)
        _integer(self.omitted_worker_count, 0, MAX_ADVERTISEMENTS, "omitted worker count")

    @property
    def roster_generation(self) -> int:
        return self.roster.generation if self.roster is not None else 0


def reconcile_membership(
    candidate: MembershipSnapshot | None, *, cluster_id: str, issuer_id: str,
    clock: Callable[[], datetime], previous: MembershipRoster | None = None,
    high_water: MembershipHighWater | None = None, max_workers: int = 16,
    capability_evidence: tuple[CapabilityEvidence, ...] = (),
) -> MembershipReconciliation:
    """Propose a bounded roster from admitted authority, without side effects.

    Eligible advertisements are selected by ascending opaque worker ID; the
    omitted count makes the finite active limit explicit. New/replaced members
    begin in probation even if supplied with a pre-admission probe. An existing
    member activates only with fresh exact-origin/generation evidence. ``None``
    means source outage: retain only the old snapshot's unexpired authority.
    There is no implicit local fallback, request replay, or ownership transfer.
    """
    _identity(cluster_id)
    _identity(issuer_id)
    _integer(max_workers, 1, MAX_ROSTER_WORKERS, "roster worker limit")
    now = _aware(clock(), "clock")
    if high_water is not None:
        if not isinstance(high_water, MembershipHighWater):
            raise ValueError("invalid high-water record")
        _authority(high_water, cluster_id, issuer_id)
    if previous is not None:
        if not isinstance(previous, MembershipRoster) or high_water is None:
            raise ValueError("previous roster requires its retained high-water record")
        _authority(previous.snapshot, cluster_id, issuer_id)
        if (previous.snapshot.generation > high_water.generation or
            (previous.snapshot.generation == high_water.generation and previous.snapshot.digest != high_water.digest)):
            raise ValueError("previous roster does not match retained high-water")
        if previous.snapshot.issued_at > now:
            raise ValueError("previous snapshot is issued in the future")
    if candidate is not None:
        next_water = validate_high_water(candidate, high_water, cluster_id=cluster_id,
                                         issuer_id=issuer_id, clock=lambda: now)
        if previous is not None:
            old_ads = {worker.worker_id: worker for worker in previous.snapshot.workers}
            for worker in candidate.workers:
                old = old_ads.get(worker.worker_id)
                if old is not None and (worker.member_generation < old.member_generation or
                    (worker.origin != old.origin and worker.member_generation <= old.member_generation)):
                    raise ValueError("member generation must advance for endpoint replacement and cannot roll back")
        snapshot = candidate
    elif previous is not None:
        snapshot, next_water = previous.snapshot, high_water
    else:
        return MembershipReconciliation(None, high_water)

    old_members = {member.advertisement.worker_id: member for member in previous.members} if previous else {}
    eligible = sorted((worker for worker in snapshot.workers if worker.lifecycle_state in _CONSIDERED_STATES),
                      key=lambda worker: worker.worker_id)
    if snapshot.expires_at <= now:
        # Only the outage path may retain expired authority, and only to block
        # new admission. Existing requests keep their original pool deadlines.
        members = tuple(replace(member, lifecycle_state="expired", evidence=None)
                        for member in previous.members)
        expirations = tuple(member.advertisement for member in previous.members if member.lifecycle_state != "expired")
        generation = previous.generation + int(members != previous.members)
        return MembershipReconciliation(MembershipRoster(snapshot, members, generation), next_water,
                                        expirations=expirations, omitted_worker_count=max(0, len(eligible) - len(members)))

    proofs = {}
    for proof in _bounded_tuple(capability_evidence, MAX_ROSTER_WORKERS, "capability evidence"):
        if not isinstance(proof, CapabilityEvidence) or _key(proof) in proofs:
            raise ValueError("invalid or duplicate capability evidence")
        proofs[_key(proof)] = proof
    members, additions, activations = [], [], []
    selectable = eligible if candidate is not None else [
        worker for worker in eligible
        if worker.worker_id in old_members and _key(worker) == _key(old_members[worker.worker_id].advertisement)
    ]
    for worker in selectable[:max_workers]:
        old = old_members.get(worker.worker_id)
        same_incarnation = (old is not None and _key(old.advertisement) == _key(worker)
                            and old.lifecycle_state != "expired" and previous.snapshot.expires_at > now)
        proof = proofs.get(_key(worker), old.evidence if same_incarnation else None)
        state = "unhealthy" if worker.lifecycle_state == "unhealthy" else "probation"
        if same_incarnation and proof is not None and proof.checked_at <= now < proof.expires_at and state != "unhealthy":
            state = "active"
        else:
            proof = None
        if not same_incarnation:
            additions.append(worker)
        if state == "active" and old.lifecycle_state != "active":
            activations.append(worker)
        members.append(RosterMember(worker, state, proof))
    selected_keys = {_key(member.advertisement) for member in members}
    drains = tuple(member.advertisement for member in old_members.values()
                   if _key(member.advertisement) not in selected_keys and member.lifecycle_state != "expired")
    generation = (previous.generation + int(tuple(members) != previous.members)) if previous else 1
    roster = MembershipRoster(snapshot, tuple(members), generation)
    return MembershipReconciliation(roster, next_water, tuple(additions), tuple(activations), drains,
                                    omitted_worker_count=max(0, len(eligible) - len(members)))
