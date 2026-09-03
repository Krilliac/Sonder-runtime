"""Typed filesystem provider over the packaged guarded filesystem adapter.

This provider is deliberately thin: containment, sensitive-path checks, and
bounded reads remain implemented by :mod:`file_ops`.  It translates the
request into that existing guarded call and returns the SEAM-003 value object,
so migrating a caller does not create a second security policy.
"""
from __future__ import annotations

from pathlib import Path

from ...application.context import OperationContext
from ...application.ports.filesystem import (
    FileSystem,
    FileSystemObservation,
    FileSystemOperation,
    FileSystemReadResult,
    FileSystemRequest,
    FileSystemResource,
    PolicyEffect,
    validate_request,
)
from . import file_ops


class GuardedFileSystemAdapter:
    """Concrete SEAM-003 provider for the existing guarded file operations.

    Only ``read`` is migrated in this bounded slice.  Other mutations remain
    on their existing paths until each has an independently reviewed contract.
    """

    def read(
        self, request: FileSystemRequest, context: OperationContext
    ) -> FileSystemReadResult:
        validate_request(request)
        if request.operation is not FileSystemOperation.READ:
            raise ValueError("this bounded provider only supports filesystem reads")

        result = file_ops.read_file(
            str(request.resource.path),
            max_bytes=request.max_bytes,
            extra_roots=request.extra_roots,
            bypass=request.bypass,
            developer_authorized=request.developer_authorized,
        )
        resource = FileSystemResource(
            Path(result["path"]), kind=request.resource.kind,
            resource_id=request.resource.resource_id,
        )
        content = result["text"].encode("utf-8")
        observation = FileSystemObservation(
            operation=FileSystemOperation.READ,
            resource=resource,
            effect=PolicyEffect.ALLOW,
            succeeded=True,
            bytes_read=len(content),
        )
        return FileSystemReadResult(
            content=content, observation=observation,
            truncated=bool(result.get("truncated")),
        )


__all__ = ["GuardedFileSystemAdapter"]
