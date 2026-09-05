"""Non-authoritative links to retained host-turn evidence."""

from dataclasses import dataclass, field
from .app_control import text, digest, positive
from .host_final import HostFinalFacts


@dataclass(frozen=True)
class ManagedHostTurnLink:
    continuation_id: str
    parent_session_id: str
    host_conversation_id: str
    principal_id: str
    run_id: str
    ordinal: int

    def __post_init__(self):
        for value in (
            self.continuation_id,
            self.parent_session_id,
            self.host_conversation_id,
            self.principal_id,
            self.run_id,
        ):
            text(value, maximum=256)
        positive(self.ordinal, maximum=32)


@dataclass(frozen=True)
class ManagedHostTerminalLink:
    turn: ManagedHostTurnLink
    original_projection_id: str
    original_projection_digest: str
    final_projection_id: str
    final_projection_digest: str
    receipt_digest: str
    output_digest: str

    def __post_init__(self):
        if type(self.turn) is not ManagedHostTurnLink:
            raise ValueError("typed host turn link required")
        self.turn.__post_init__()
        for value in (self.original_projection_id, self.final_projection_id):
            text(value, maximum=256)
        for value in (
            self.original_projection_digest,
            self.final_projection_digest,
            self.receipt_digest,
            self.output_digest,
        ):
            digest(value)


@dataclass(frozen=True)
class FinalizedHostResult:
    output: str = field(repr=False)
    receipt: ManagedHostTerminalLink


@dataclass(frozen=True)
class ManagedHostFinalEvidence:
    """Validated retained facts and text; this is not terminal eligibility."""

    result: FinalizedHostResult
    facts: HostFinalFacts
