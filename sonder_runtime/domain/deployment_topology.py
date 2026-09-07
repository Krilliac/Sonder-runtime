"""Honest deployment capability reporting for the currently integrated backends.

A pooling profile is not cluster ownership. Preference never promotes a node.
"""
from __future__ import annotations

from dataclasses import dataclass

from .cluster_availability import AvailabilityProfile


@dataclass(frozen=True, slots=True)
class RecoveryPosture:
    """Read-only recovery limits shared by status consumers.

    This is deliberately a projection of the supported deployment contract.
    It does not inspect peers, elect an owner, or accept a fence receipt.
    """

    mode: str
    automatic_takeover_available: bool
    automatic_failback_available: bool
    independent_witness_required: bool
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            'mode': self.mode,
            'automatic_takeover_available': self.automatic_takeover_available,
            'automatic_failback_available': self.automatic_failback_available,
            'independent_witness_required': self.independent_witness_required,
            'reason': self.reason,
        }


@dataclass(frozen=True, slots=True)
class DeploymentStatus:
    profile: str = 'single-host'
    local_node: str = 'local'
    peers: tuple[str, ...] = ()
    preferred_primary: str = ''
    allow_remote_compute: bool = False

    @property
    def canonical_profile(self) -> str:
        """Return the explicit availability-profile name for status consumers."""
        aliases = {
            'single-host': AvailabilityProfile.SINGLE_PC.value,
            'pooled-pair': AvailabilityProfile.TWO_PC.value,
        }
        return aliases.get(self.profile, self.profile)

    @property
    def recovery_posture(self) -> RecoveryPosture:
        """Return the same fail-closed recovery boundary for every profile.

        A single host and a pooled pair may run their normal local or private
        compute workloads.  Neither has the external authority needed to
        transition control-state ownership automatically.
        """
        return RecoveryPosture(
            mode='external-authority-required',
            automatic_takeover_available=False,
            automatic_failback_available=False,
            independent_witness_required=True,
            reason=(
                'Automatic takeover and failback are unavailable: an independent '
                'witness, external old-owner fencing, acknowledged durable-state '
                'replication, and worker ownership-epoch enforcement are required '
                'before an automatic owner transition.'
            ),
        )

    def as_dict(self) -> dict:
        posture = self.recovery_posture
        manual_promotion_prerequisites = (
            'Independent old-owner fencing, acknowledged durable-state replication, '
            'and worker ownership-epoch enforcement are not integrated.'
        )
        remote = self.allow_remote_compute and bool(self.peers)
        return {
            'profile': self.profile,
            'profile_id': self.canonical_profile,
            'local_node': self.local_node,
            'configured_members': [self.local_node, *self.peers],
            'preferred_primary': self.preferred_primary or self.local_node,
            'control_state_scope': 'local-instance',
            'preference_confers_authority': False,
            'partition_policy': 'no_promotion_without_fencing_and_acknowledged_data',
            'recovery_posture': posture.as_dict(),
            'capabilities': {
                'local_sqlite_state': {'available': True, 'reason': 'Local durable state is supported.'},
                'private_compute': {
                    'available': remote,
                    'reason': ('Configured private-node compute is enabled; live eligibility is checked per job.'
                               if remote else 'Private compute requires remote consent and configured peers.'),
                },
                'automatic_takeover': {
                    'available': posture.automatic_takeover_available,
                    'reason': posture.reason,
                },
                'automatic_failback': {
                    'available': posture.automatic_failback_available,
                    'reason': posture.reason,
                },
                'explicit_promotion': {
                    'available': False,
                    'reason': manual_promotion_prerequisites,
                },
                'acknowledged_state_replication': {
                    'available': False, 'reason': 'No replicated durable-data acknowledgement backend is integrated.',
                },
                'worker_epoch_fencing': {
                    'available': False, 'reason': 'Existing per-job claims and effect fences are not cluster ownership epochs.',
                },
                'quorum': {
                    'available': False,
                    'reason': (
                        'No established quorum provider is integrated. An independent '
                        'witness is not required for local or pooled compute, but is '
                        'required before an automatic owner transition.'
                    ),
                },
            },
        }
