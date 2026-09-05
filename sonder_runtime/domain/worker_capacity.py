"""Validated worker reservation values; budgets are not free-RAM telemetry.

The worker's logical ``host_id`` is an operator-facing authority label.  It is
not enough to fence a shared database when two physical hosts happen to use the
same label, so the persistence adapter also binds each budget to an opaque
physical-host fingerprint.  The domain only validates that fingerprint; the
platform adapter owns how it is measured.
"""
from dataclasses import dataclass
import re


_PHYSICAL_FINGERPRINT = re.compile(r"[0-9a-f]{64}")


def bounded_positive(value: int, name: str, maximum: int = 1 << 50) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f'{name} must be within 1..{maximum}')


def validate_physical_host_fingerprint(value: str, name: str = "physical host fingerprint") -> None:
    if not isinstance(value, str) or _PHYSICAL_FINGERPRINT.fullmatch(value) is None:
        raise ValueError(f'{name} must be a lowercase SHA-256 fingerprint')


@dataclass(frozen=True, slots=True)
class PhysicalHostIdentity:
    """An opaque, stable identity used to fence one physical worker authority."""

    authority_id: str
    fingerprint: str
    source: str = "machine-id"

    def __post_init__(self) -> None:
        if not isinstance(self.authority_id, str) or not re.fullmatch(
            r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', self.authority_id
        ):
            raise ValueError('physical authority_id must be a bounded stable identity')
        validate_physical_host_fingerprint(self.fingerprint)
        if not isinstance(self.source, str) or not re.fullmatch(
            r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', self.source
        ):
            raise ValueError('physical identity source must be bounded')


@dataclass(frozen=True, slots=True)
class WorkerBudget:
    host_id: str
    memory_bytes: int
    max_jobs: int = 1
    physical_host_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.host_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', self.host_id):
            raise ValueError('worker host_id must be a bounded stable identity')
        if isinstance(self.memory_bytes, bool) or not isinstance(self.memory_bytes, int) or not 0 <= self.memory_bytes <= 1 << 50:
            raise ValueError('worker memory budget must be within 0..2^50')
        bounded_positive(self.max_jobs, 'worker max_jobs', 1024)
        if self.physical_host_fingerprint is not None:
            validate_physical_host_fingerprint(self.physical_host_fingerprint)


@dataclass(frozen=True, slots=True)
class CapacityReservation:
    job_id: str
    token: str
    memory_bytes: int
    expires_at: str
    physical_host_fingerprint: str | None = None
    state: str = "reserved"

    def __post_init__(self) -> None:
        if self.physical_host_fingerprint is not None:
            validate_physical_host_fingerprint(self.physical_host_fingerprint)
        if self.state not in {"reserved", "dispatched", "released"}:
            raise ValueError("capacity reservation state is invalid")


@dataclass(frozen=True, slots=True)
class CapacityReservationView:
    """Redacted durable reservation state; reservation tokens never leave admission."""

    job_id: str
    host_id: str
    request_sha256: str
    memory_bytes: int
    expires_at: str
    state: str
    physical_host_fingerprint: str | None
    release_reason: str = ""

    @property
    def status(self) -> str:
        return "expired" if self.state == "released" and self.release_reason == "expired" else self.state


@dataclass(frozen=True, slots=True)
class CapacityReconciliation:
    """Bounded result of a worker-owned lease reconciliation pass."""

    observed_at: str
    expired: tuple[CapacityReservationView, ...]
    inspected: int

    @property
    def expired_job_ids(self) -> tuple[str, ...]:
        return tuple(item.job_id for item in self.expired)
