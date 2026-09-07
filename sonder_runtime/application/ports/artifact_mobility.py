"""Cooperative trusted-in-process ports for a private sealed artifact source.

These protocols describe host-owned internal APIs; they are not Python
object-capability security boundaries. Runtime composition must inject a
publisher or reader only into approved local workload code. No model, MCP,
HTTP, CLI, REPL, or public-plugin path receives either port. Any untrusted
extension or model code must be process-isolated before a source authority is
injected, because code already able to reflect over this interpreter is outside
this boundary.

The binding still verifies its issued context, source scope, role, and trusted
publisher provenance on every operation. Those checks prevent accidental
cross-role wiring and forged ordinary call inputs; they do not sandbox code
that already controls the same interpreter.
"""
from typing import Protocol

from ..artifacts.mobility_source import SourceArtifactRange


class ArtifactMobilityPublisher(Protocol):
    """Trusted host-injected local source publisher."""

    def publish_sealed(self, stream, immutable_spec: dict, trusted_provenance: object) -> dict: ...


class ArtifactMobilityReader(Protocol):
    """Trusted host-injected local source reader for later dispatch composition."""

    def inspect_sealed(self, source_artifact_id: str) -> dict: ...

    def read_range(
        self, source_artifact_id: str, offset: int, length: int
    ) -> SourceArtifactRange: ...
