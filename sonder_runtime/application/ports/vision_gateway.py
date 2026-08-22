"""Typed local-vision transport port.

The application owns guarded image loading and provenance; this port receives
only already-validated bytes and a declared media type.  Implementations must
enforce the operation context and must not turn a vision request into a cloud
or remote-Ollama fallback.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Protocol

from ..context import OperationContext


MAX_VISION_BYTES = 8 * 1024 * 1024
MAX_VISION_PROMPT_CHARS = 4096
VISION_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/bmp"})


@dataclass(frozen=True)
class VisionRequest:
    """One bounded image question after filesystem validation."""

    prompt: str
    image: bytes
    media_type: str
    tier: str = "vision"
    options: Mapping[str, str | int | float | bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise ValueError("vision prompt must not be empty")
        if len(self.prompt) > MAX_VISION_PROMPT_CHARS:
            raise ValueError("vision prompt exceeds bounded length")
        if not isinstance(self.image, bytes) or not self.image:
            raise ValueError("vision image must be non-empty bytes")
        if len(self.image) > MAX_VISION_BYTES:
            raise ValueError("vision image exceeds bounded size")
        if self.media_type not in VISION_MEDIA_TYPES:
            raise ValueError("unsupported vision media type")
        if not isinstance(self.tier, str) or not self.tier.strip():
            raise ValueError("vision tier must not be empty")


@dataclass(frozen=True)
class VisionInput:
    """Guarded image bytes plus the inspection facts that authorized them."""

    path: Path
    image: bytes
    media_type: str
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise ValueError("vision input path must be absolute")
        if len(self.sha256) != 64:
            raise ValueError("vision input digest must be SHA-256")


@dataclass(frozen=True)
class VisionResponse:
    """Provider response with identity retained for provenance."""

    text: str
    model: str
    tier: str


class VisionGateway(Protocol):
    """Local-only multimodal provider boundary."""

    def analyze(
        self, request: VisionRequest, context: OperationContext
    ) -> VisionResponse: ...


class VisionInputProvider(Protocol):
    """Guarded filesystem boundary for loading one image."""

    def load(self, path: str, context: OperationContext) -> VisionInput: ...


def require_vision_text(value: object) -> str:
    """Reject empty provider output before it crosses the application port."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("vision provider returned no usable text")
    return value


__all__ = [
    "MAX_VISION_BYTES", "MAX_VISION_PROMPT_CHARS", "VISION_MEDIA_TYPES",
    "VisionGateway", "VisionInput", "VisionInputProvider", "VisionRequest",
    "VisionResponse", "require_vision_text",
]
