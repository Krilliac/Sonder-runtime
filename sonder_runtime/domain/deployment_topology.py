"""Honest deployment capability reporting for the currently integrated backends.

A pooling profile is not cluster ownership. Preference never promotes a node.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DeploymentStatus:
    profile: str = 'single-host'
    local_node: str = 'local'
    peers: tuple[str, ...] = ()
    preferred_primary: str = ''
    allow_remote_compute: bool = False

    def as_dict(self) -> dict:
        prerequisites = (
            'Independent old-owner fencing, acknowledged durable-state replication, '
            'and worker ownership-epoch enforcement are not integrated.'
        )
        remote = self.allow_remote_compute and bool(self.peers)
        return {
            'profile': self.profile,
            'local_node': self.local_node,
            'configured_members': [self.local_node, *self.peers],
            'preferred_primary': self.preferred_primary or self.local_node,
            'control_state_scope': 'local-instance',
            'preference_confers_authority': False,
            'partition_policy': 'no_promotion_without_fencing_and_acknowledged_data',
            'capabilities': {
                'local_sqlite_state': {'available': True, 'reason': 'Local durable state is supported.'},
                'private_compute': {
                    'available': remote,
                    'reason': ('Configured private-node compute is enabled; live eligibility is checked per job.'
                               if remote else 'Private compute requires remote consent and configured peers.'),
                },
                'automatic_takeover': {'available': False, 'reason': prerequisites},
                'automatic_failback': {'available': False, 'reason': prerequisites},
                'explicit_promotion': {'available': False, 'reason': prerequisites},
                'acknowledged_state_replication': {
                    'available': False, 'reason': 'No replicated durable-data acknowledgement backend is integrated.',
                },
                'worker_epoch_fencing': {
                    'available': False, 'reason': 'Existing per-job claims and effect fences are not cluster ownership epochs.',
                },
                'quorum': {
                    'available': False, 'reason': 'No established quorum provider is integrated; no witness is required for local or pooled compute.',
                },
            },
        }
