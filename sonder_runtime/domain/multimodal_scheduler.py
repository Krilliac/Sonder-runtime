"""Pure local-first scheduler for modality work sharing accelerator memory."""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Iterable

logger = logging.getLogger(__name__)


MODALITIES = frozenset({"vision", "ocr", "stt", "tts", "embedding"})


@dataclass(frozen=True)
class ModalityJob:
    """One bounded modality request; payloads never enter this policy."""

    job_id: str
    modality: str
    priority: int = 0
    vram_gb: float = 0.0
    ram_gb: float = 0.0
    local_only: bool = True
    allow_cpu_fallback: bool = True

    def __post_init__(self) -> None:
        if not str(self.job_id).strip():
            raise ValueError("job_id is required")
        if self.modality not in MODALITIES:
            raise ValueError("unsupported modality %r" % self.modality)
        if self.priority < 0 or self.vram_gb < 0 or self.ram_gb < 0:
            raise ValueError("priority and memory estimates must be non-negative")


@dataclass(frozen=True)
class ScheduledJob:
    job_id: str
    modality: str
    execution: str
    reason: str
    reserved_vram_gb: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def schedule_jobs(
    jobs: Iterable[ModalityJob],
    *,
    free_vram_gb: float | None,
    allow_cloud: bool = False,
) -> tuple[ScheduledJob, ...]:
    """Assign jobs deterministically under one measured VRAM budget.

    Higher priority wins; ties are stable by job id. Unknown VRAM is treated as
    unavailable, never as infinite. A local CPU fallback may run when GPU
    capacity is absent. Non-local jobs are refused unless the caller has
    explicitly opted into cloud work; this function still never sends data.
    """
    if free_vram_gb is not None and free_vram_gb < 0:
        raise ValueError("free_vram_gb must be non-negative or None")
    if free_vram_gb is None:
        logger.warning("VRAM budget unknown (free_vram_gb=None), all GPU-requiring jobs will fall back to CPU or be deferred")
    remaining = free_vram_gb
    ordered = sorted(jobs, key=lambda job: (-job.priority, job.job_id))
    logger.debug(f"schedule_jobs: {len(ordered)} jobs, free_vram_gb={free_vram_gb}, allow_cloud={allow_cloud}")
    result: list[ScheduledJob] = []
    for job in ordered:
        if not job.local_only and not allow_cloud:
            logger.error(f"modality job {job.job_id!r} ({job.modality!r}) refused: non-local work disabled by policy, job will not execute")
            logger.warning(f"modality job {job.job_id!r} ({job.modality!r}) refused: non-local work disabled by policy, job will not execute")
            logger.debug(f"schedule_jobs: refusing job {job.job_id!r} ({job.modality!r}): non-local work disabled")
            result.append(ScheduledJob(
                job.job_id, job.modality, "refused",
                "non-local modality work is disabled by the local-first policy",
            ))
            continue
        if remaining is not None and job.vram_gb <= remaining:
            logger.debug(f"schedule_jobs: assigning job {job.job_id!r} ({job.modality!r}) to GPU, vram={job.vram_gb}GB, remaining={remaining}GB")
            result.append(ScheduledJob(
                job.job_id, job.modality, "gpu", "fits measured free VRAM",
                round(job.vram_gb, 2),
            ))
            remaining = round(remaining - job.vram_gb, 2)
            continue
        if job.allow_cpu_fallback:
            reason = (
                "VRAM is unknown; CPU fallback preserves truthful scheduling"
                if remaining is None else
                "insufficient measured free VRAM; CPU fallback preserves progress"
            )
            logger.warning(f"modality job {job.job_id!r} ({job.modality!r}) falling back to CPU: {reason} -- expect slower inference")
            logger.debug(f"schedule_jobs: job {job.job_id!r} ({job.modality!r}) -> cpu-fallback")
            result.append(ScheduledJob(
                job.job_id, job.modality, "cpu-fallback", reason,
            ))
            continue
        logger.error(f"modality job {job.job_id!r} ({job.modality!r}) deferred: insufficient VRAM and CPU fallback disabled, job will not run until resources free up")
        logger.warning(f"modality job {job.job_id!r} ({job.modality!r}) deferred: insufficient VRAM and CPU fallback disabled, job will not run until resources free up")
        logger.debug(f"schedule_jobs: deferring job {job.job_id!r} ({job.modality!r}): insufficient VRAM, no CPU fallback")
        result.append(ScheduledJob(
            job.job_id, job.modality, "deferred",
            "insufficient measured free VRAM and CPU fallback is disabled",
        ))
    gpu_count = sum(1 for r in result if r.execution == "gpu")
    cpu_count = sum(1 for r in result if r.execution == "cpu-fallback")
    deferred_count = sum(1 for r in result if r.execution == "deferred")
    refused_count = sum(1 for r in result if r.execution == "refused")
    if result and gpu_count == 0 and cpu_count == 0:
        logger.critical(f"all {len(result)} modality jobs failed to schedule (deferred={deferred_count}, refused={refused_count}) -- zero multimodal inference capacity available")
    logger.info(
        f"modality scheduling complete: {len(result)} jobs "
        f"(gpu={gpu_count}, cpu_fallback={cpu_count}, deferred={deferred_count}, refused={refused_count})"
    )
    logger.debug(f"schedule_jobs: scheduled {len(result)} jobs, remaining_vram={remaining}")
    return tuple(result)


__all__ = ["MODALITIES", "ModalityJob", "ScheduledJob", "schedule_jobs"]
