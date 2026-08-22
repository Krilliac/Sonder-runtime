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
        self._crashes: dict[object, int] = {}
        self._decisions: dict[str, QuarantineDecision] = {}

    @staticmethod
    def _installation_key(extension_id: str, installation_key: object | None) -> object:
        return extension_id if installation_key is None else installation_key

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

    def record_crash(self, manifest: ExtensionManifest, *, installation_key: object | None = None) -> QuarantineDecision:
        extension_id = manifest.extension_id
        key = self._installation_key(extension_id, installation_key)
        count = self._crashes.get(key, 0) + 1
        self._crashes[key] = count
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

    def crash_count(self, extension_id: str, *, installation_key: object | None = None) -> int:
        return self._crashes.get(self._installation_key(extension_id, installation_key), 0)

    def restore_crash_count(self, extension_id: str, count: int, *, installation_key: object | None = None) -> None:
        """Restore durable crash evidence without weakening its monotonicity."""
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError("crash count must be a non-negative integer")
        key = self._installation_key(extension_id, installation_key)
        self._crashes[key] = max(self._crashes.get(key, 0), count)
