"""Guarded filesystem provider for the application vision service."""
from __future__ import annotations

import hashlib

from ..application.context import OperationContext
from ..application.ports.vision_gateway import VisionInput
from .filesystem import file_ops, workbench


MAX_VISION_PIXELS = 16_000_000
MAX_VISION_EDGE = 8192
_FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "BMP": "image/bmp"}


class FileVisionInputProvider:
    """Load only bounded, digest-stable raster inputs under file guards."""

    def load(self, path: str, context: OperationContext) -> VisionInput:
        del context  # containment and authorization are owned by file_ops.
        resolved = file_ops.require_read_access(path)
        metadata = workbench.image_inspect(str(resolved))
        image_format = str(metadata.get("format") or "").upper()
        media_type = _FORMATS.get(image_format)
        if media_type is None:
            raise ValueError("vision analysis accepts PNG, JPEG, or BMP images")
        width, height = metadata.get("width"), metadata.get("height")
        if (
            not isinstance(width, int) or not isinstance(height, int)
            or width <= 0 or height <= 0
            or width > MAX_VISION_EDGE or height > MAX_VISION_EDGE
            or width * height > MAX_VISION_PIXELS
        ):
            raise ValueError("vision input dimensions exceed bounded limits")
        raw = resolved.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        expected = str(metadata.get("sha256") or "").casefold()
        if len(raw) != int(metadata.get("bytes") or 0) or digest != expected:
            raise ValueError("vision input changed while it was being read")
        return VisionInput(resolved, raw, media_type, digest)


__all__ = ["FileVisionInputProvider"]
