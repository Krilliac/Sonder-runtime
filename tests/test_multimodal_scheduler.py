import pytest

from sonder_runtime.domain.multimodal_scheduler import ModalityJob, schedule_jobs


def test_scheduler_prioritizes_jobs_and_never_overcommits_vram():
    jobs = [
        ModalityJob("embedding", "embedding", priority=1, vram_gb=1.0),
        ModalityJob("vision", "vision", priority=5, vram_gb=3.0),
        ModalityJob("ocr", "ocr", priority=4, vram_gb=2.0),
    ]

    result = schedule_jobs(jobs, free_vram_gb=3.5)

    assert [(row.job_id, row.execution) for row in result] == [
        ("vision", "gpu"), ("ocr", "cpu-fallback"), ("embedding", "cpu-fallback"),
    ]


def test_scheduler_unknown_vram_falls_back_to_local_cpu():
    result = schedule_jobs(
        [ModalityJob("stt", "stt", vram_gb=2.0)], free_vram_gb=None,
    )

    assert result[0].execution == "cpu-fallback"
    assert "unknown" in result[0].reason


def test_scheduler_refuses_nonlocal_job_without_explicit_cloud_opt_in():
    result = schedule_jobs(
        [ModalityJob("tts", "tts", local_only=False)], free_vram_gb=4.0,
    )

    assert result[0].execution == "refused"
    assert "local-first" in result[0].reason


def test_scheduler_can_defer_when_cpu_fallback_is_disabled():
    result = schedule_jobs(
        [ModalityJob("vision", "vision", vram_gb=8.0, allow_cpu_fallback=False)],
        free_vram_gb=2.0,
    )

    assert result[0].execution == "deferred"


def test_scheduler_rejects_unknown_modality_and_negative_budget():
    with pytest.raises(ValueError):
        ModalityJob("bad", "audio")
    with pytest.raises(ValueError):
        schedule_jobs([], free_vram_gb=-1.0)
