"""Bounded, local-only owner playbook configuration."""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class PlaybooksConfig:
    approval: str = "required"
    categories: tuple[str, ...] = (
        "pitfall", "procedure", "environment", "measurement", "decision",
        "tool-guide", "preference",
    )
    max_entry_bytes: int = 8192
    max_topic_bytes: int = 131072
    max_index_bytes: int = 4096
    max_topics: int = 64
    max_topics_per_turn: int = 3
    max_context_bytes: int = 12288
    environment_stale_days: int = 30
    measurement_stale_days: int = 90


def playbooks_errors(config) -> list[str]:
    section = config.playbooks
    errors = []
    if section.approval not in ("required", "owner_corrections_auto", "auto"):
        errors.append("[playbooks].approval must be required, owner_corrections_auto or auto")
    if (
        not isinstance(section.categories, tuple)
        or not 1 <= len(section.categories) <= 32
        or any(not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", value)
               for value in section.categories)
        or len(set(section.categories)) != len(section.categories)
    ):
        errors.append("[playbooks].categories must contain 1..32 distinct category slugs")
    for name, low, high in (
        ("max_entry_bytes", 256, 32768), ("max_topic_bytes", 1024, 524288),
        ("max_index_bytes", 256, 4096), ("max_topics", 1, 128),
        ("max_topics_per_turn", 1, 8), ("max_context_bytes", 256, 32768),
        ("environment_stale_days", 1, 3650), ("measurement_stale_days", 1, 3650),
    ):
        value = getattr(section, name)
        if type(value) is not int or not low <= value <= high:
            errors.append(f"[playbooks].{name} must be an integer in {low}..{high}")
    if (type(section.max_topic_bytes) is int and type(section.max_entry_bytes) is int
            and section.max_topic_bytes < section.max_entry_bytes):
        errors.append("[playbooks].max_topic_bytes must cover max_entry_bytes")
    return errors
