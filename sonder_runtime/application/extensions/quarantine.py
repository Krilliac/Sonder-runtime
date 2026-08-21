"""Deterministic extension quarantine decisions; no process or filesystem effects."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sonder_runtime.domain.extensions.manifest import ExtensionManifest


class QuarantineReason(StrEnum):
    INCOMPATIBLE = "incompatible"
    REPEATED_CRASH = "repeated-crash"


@dataclass(frozen=True)
class QuarantineDecision:
    extension_id: str
    quarantined: bool
    reasons: tuple[str, ...] = ()
    cleanup_action: str = "disable"
    retain_state: bool = True


class QuarantineRegistry:
    def __init__(self) -> None:
        self._crashes: dict[str, int] = {}
        self._decisions: dict[str, QuarantineDecision] = {}

    def evaluate(
        self, manifest: ExtensionManifest, *, protocol: str,
        available_dependencies: set[str], granted_permissions: set[str],
    ) -> QuarantineDecision:
        reasons = manifest.compatibility_reasons(
            protocol=protocol, available_dependencies=available_dependencies,
            granted_permissions=granted_permissions,
        )
        decision = QuarantineDecision(
            manifest.extension_id, bool(reasons), reasons,
            manifest.cleanup.on_quarantine, manifest.cleanup.retain_state,
        )
        self._decisions[manifest.extension_id] = decision
        return decision

    def record_crash(self, manifest: ExtensionManifest) -> QuarantineDecision:
        extension_id = manifest.extension_id
        count = self._crashes.get(extension_id, 0) + 1
        self._crashes[extension_id] = count
        quarantined = count >= manifest.health.crash_limit
        reasons = (QuarantineReason.REPEATED_CRASH.value,) if quarantined else ()
        decision = QuarantineDecision(
            extension_id, quarantined, reasons,
            manifest.cleanup.on_quarantine, manifest.cleanup.retain_state,
        )
        self._decisions[extension_id] = decision
        return decision

    def decision(self, extension_id: str) -> QuarantineDecision | None:
        return self._decisions.get(extension_id)

    def crash_count(self, extension_id: str) -> int:
        return self._crashes.get(extension_id, 0)

    def restore_crash_count(self, extension_id: str, count: int) -> None:
        """Restore durable crash evidence without weakening its monotonicity."""
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError("crash count must be a non-negative integer")
        self._crashes[extension_id] = max(self._crashes.get(extension_id, 0), count)
