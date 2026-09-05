"""Private host-only immutable terminal output. References are not authority."""

from dataclasses import dataclass
from typing import Protocol

from ..context import OperationContext
from .lane_continuation import ProjectionBinding

MAX_OUTPUT_BYTES = 1024 * 1024
DEFAULT_TOTAL_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_ROWS = 4096


@dataclass(frozen=True)
class TerminalOutputReference:
    sha256: str
    size_bytes: int
    binding_sha256: str

    def __post_init__(self):
        for value in (self.sha256, self.binding_sha256):
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(c not in "0123456789abcdef" for c in value)
            ):
                raise ValueError("terminal output digest is invalid")
        if (
            type(self.size_bytes) is not int
            or not 0 <= self.size_bytes <= MAX_OUTPUT_BYTES
        ):
            raise ValueError("terminal output size is invalid")


class TerminalOutputStore(Protocol):
    """Trusted codec dependency, never exposed as a model/HTTP blob endpoint."""

    def put(
        self, binding: ProjectionBinding, output: str, *, context: OperationContext
    ) -> TerminalOutputReference:
        """Commit immutable exact UTF-8 output before returning its scoped reference."""
        ...

    def get(
        self,
        binding: ProjectionBinding,
        reference: TerminalOutputReference,
        *,
        context: OperationContext
    ) -> str:
        """Return exact original text only after scope and content verification."""
        ...
