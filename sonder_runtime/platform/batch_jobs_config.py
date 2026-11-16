"""Bounded, local-only configuration for durable batch jobs (``[batch_jobs]``)."""
from dataclasses import dataclass
from typing import Protocol

ORDERS = ("stage_major", "item_major")


@dataclass(frozen=True)
class BatchJobsConfig:
    enabled: bool = True
    max_items_per_job: int = 5000
    max_stages: int = 8
    max_items_per_add: int = 500
    # The sealed envelope (system + rendered prompt) must fit the fanout
    # sealed-prompt cap of 64000 characters after vault expansion.
    max_item_input_chars: int = 32000
    max_rendered_prompt_chars: int = 32000
    answer_max_chars: int = 64000
    default_item_timeout_seconds: int = 300
    max_item_timeout_seconds: int = 900
    max_active_jobs: int = 4
    max_consecutive_failures: int = 5
    eta_window: int = 20
    eta_min_samples: int = 3
    order: str = "stage_major"
    retention_ttl_seconds: int = 1209600


_INT_RANGES = (
    ("max_items_per_job", 1, 100000), ("max_stages", 1, 16), ("max_items_per_add", 1, 5000),
    ("max_item_input_chars", 256, 40000), ("max_rendered_prompt_chars", 256, 40000),
    ("answer_max_chars", 1024, 1000000),
    ("default_item_timeout_seconds", 5, 3600), ("max_item_timeout_seconds", 5, 3600),
    ("max_active_jobs", 1, 64), ("max_consecutive_failures", 1, 1000),
    ("eta_window", 2, 1000),
)


class _HasBatchJobs(Protocol):
    """The one attribute of the platform config that the validator reads."""

    @property
    def batch_jobs(self) -> BatchJobsConfig: ...


def batch_jobs_errors(config: _HasBatchJobs) -> list[str]:
    section = config.batch_jobs
    errors: list[str] = []
    if type(section.enabled) is not bool:
        errors.append("[batch_jobs].enabled must be a boolean")
    for name, low, high in _INT_RANGES:
        value = getattr(section, name)
        if type(value) is not int or not low <= value <= high:
            errors.append(f"[batch_jobs].{name} must be an integer in {low}..{high}")
    window = section.eta_window
    window_ok = type(window) is int and window >= 2
    samples = section.eta_min_samples
    if type(samples) is not int or not 1 <= samples or (window_ok and samples > window):
        errors.append("[batch_jobs].eta_min_samples must be an integer in 1..eta_window")
    ttl = section.retention_ttl_seconds
    if type(ttl) is not int or not (ttl == 0 or 3600 <= ttl <= 31536000):
        errors.append("[batch_jobs].retention_ttl_seconds must be 0 (keep) or an integer in 3600..31536000")
    if section.order not in ORDERS:
        errors.append("[batch_jobs].order must be stage_major or item_major")
    if (
        type(section.default_item_timeout_seconds) is int and type(section.max_item_timeout_seconds) is int
        and section.default_item_timeout_seconds > section.max_item_timeout_seconds
    ):
        errors.append("[batch_jobs].default_item_timeout_seconds must not exceed max_item_timeout_seconds")
    return errors
