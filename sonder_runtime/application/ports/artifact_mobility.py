"""In-process ports for a private sealed artifact source.

These ports are deliberately not HTTP, CLI, REPL, MCP, path-staging, or
destination-selection contracts.  Host composition injects the concrete
capability only into a trusted local producer or future outbound dispatcher.
"""
from typing import Protocol

from ..artifacts.mobility_source import SourceArtifactRange


class ArtifactMobilityPublisher(Protocol):
    def publish_sealed(self, stream, immutable_spec: dict, trusted_provenance: object) -> dict: ...


class ArtifactMobilityReader(Protocol):
    def inspect_sealed(self, source_artifact_id: str) -> dict: ...

    def read_range(
        self, source_artifact_id: str, offset: int, length: int
    ) -> SourceArtifactRange: ...
