"""Process identity adapter implementing the application process-probe port."""
from __future__ import annotations

from ..application.ports.process_probe import ProbeResult, ProcessIdentity


class ProcessProbeAdapter:
    """Expose guarded process-liveness checks through the application port."""

    def identity(self, pid: int) -> ProcessIdentity | None:
        import sonder_runtime.adapters.process_liveness as process_liveness

        state, fingerprint = process_liveness.probe_process(pid)
        if state == process_liveness.PROCESS_DEAD or not fingerprint:
            return None
        return ProcessIdentity(pid=int(pid), started_at=0.0, fingerprint=str(fingerprint))

    def is_same_live_process(self, identity: ProcessIdentity) -> ProbeResult:
        import sonder_runtime.adapters.process_liveness as process_liveness

        state, _observed = process_liveness.probe_process(
            identity.pid, expected_identity=identity.fingerprint or None
        )
        if state == process_liveness.PROCESS_ALIVE:
            return ProbeResult.ALIVE
        if state == process_liveness.PROCESS_DEAD:
            return ProbeResult.DEAD
        return ProbeResult.UNKNOWN
