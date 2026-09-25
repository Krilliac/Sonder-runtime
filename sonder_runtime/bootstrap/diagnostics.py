"""Compose the output digest service (lazy: nothing reads or probes here)."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..adapters.diagnostics.sources import GuardedFileWindowSource, RegistryJobOutputReader
from ..application.diagnostics.ports import JobOutputReader
from ..application.diagnostics.service import OutputDigestService
from ..platform.logging import Redactor


def job_output_reader(job_registry: Callable[[], Any]) -> JobOutputReader:
    """A bounded reader over the durable job registry the getter returns."""
    return RegistryJobOutputReader(job_registry)


def compose_output_digest_service(
    job_registry: Callable[[], Any] | None,
    *,
    redactor: Redactor | None = None,
) -> OutputDigestService:
    """File, job and text digests, redacted by the runtime redactor."""
    active = redactor if redactor is not None else Redactor()
    return OutputDigestService(
        GuardedFileWindowSource(),
        job_output_reader(job_registry) if job_registry is not None else None,
        redact=active.redact,
    )


__all__ = ["compose_output_digest_service", "job_output_reader"]
