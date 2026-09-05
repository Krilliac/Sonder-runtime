"""Immutable host observations accompanying the actual outward terminal text."""
from dataclasses import dataclass


@dataclass(frozen=True)
class HostFinalFacts:
    tools: tuple[str, ...]
    project_scope: str
    mutation_observed: bool
    validation_attempted: bool
    validation_passed: bool
    terminal_class: str
    blockers: tuple[str, ...] = ()
    certificate_id: str = ''
    certificate_generation: int = 0
    certificate_code: str = ''
    # None preserves legacy evidence as unknown, never as no delegated work.
    delegated_work: bool | None = None

    def __post_init__(self):
        if self.delegated_work is not None and type(self.delegated_work) is not bool:
            raise ValueError('explicit delegated work observation required')
        if any(type(getattr(self, key)) is not bool for key in (
            'mutation_observed', 'validation_attempted', 'validation_passed',
        )):
            raise ValueError('exact host validation flags required')
        if self.terminal_class not in (
            'NORMAL', 'ERROR', 'EVIDENCE_REQUIRED', 'VALIDATION_FAILED', 'CANCELLED', 'UNVERIFIED',
        ):
            raise ValueError('known host terminal class required')
        for values in (self.tools, self.blockers):
            if type(values) is not tuple or len(values) > 256 or any(
                not isinstance(value, str) or not 1 <= len(value.encode()) <= 256
                for value in values
            ):
                raise ValueError('bounded immutable host facts required')
        for value, maximum in ((self.project_scope, 4096),
                               (self.certificate_id, 256), (self.certificate_code, 256)):
            if not isinstance(value, str) or len(value.encode()) > maximum:
                raise ValueError('bounded host identity required')
        if type(self.certificate_generation) is not int or not 0 <= self.certificate_generation < 2**63:
            raise ValueError('bounded certificate generation required')
