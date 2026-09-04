"""Validated worker reservation values; budgets are not free-RAM telemetry."""
from dataclasses import dataclass
import re


def bounded_positive(value: int, name: str, maximum: int = 1 << 50) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f'{name} must be within 1..{maximum}')


@dataclass(frozen=True, slots=True)
class WorkerBudget:
    host_id: str
    memory_bytes: int
    max_jobs: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.host_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', self.host_id):
            raise ValueError('worker host_id must be a bounded stable identity')
        if isinstance(self.memory_bytes, bool) or not isinstance(self.memory_bytes, int) or not 0 <= self.memory_bytes <= 1 << 50:
            raise ValueError('worker memory budget must be within 0..2^50')
        bounded_positive(self.max_jobs, 'worker max_jobs', 1024)


@dataclass(frozen=True, slots=True)
class CapacityReservation:
    job_id: str
    token: str
    memory_bytes: int
    expires_at: str
