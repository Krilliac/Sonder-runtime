"""Explicit composition for the disposable control-state rehearsal command.

This module is intentionally absent from normal application composition.  It
only creates a provider after the explicit command has loaded a typed config
and this factory has repeated the control-state and deployment checks for a
directly constructed ``SonderConfig``.
"""
from __future__ import annotations

from ..adapters.cluster.http_control_state import HttpsControlStateProvider
from ..application.control_state import ExternalControlStateCoordinator
from ..domain.cluster_availability import (
    CONTROL_STATE_PROTOCOL_VERSION,
    PartitionState,
    ReplicatedControlStateCapabilities,
    validate_availability_profile,
)
from ..platform.config import (
    ComputeConfig,
    ComputeNodeConfig,
    DeploymentConfig,
    Secrets,
    SonderConfig,
    deployment_errors,
)
from ..platform.control_state_rehearsal_config import (
    ControlStateRehearsalConfig,
    control_state_rehearsal_errors,
)


_INVALID_CONFIGURATION = "control-state rehearsal configuration is invalid"


def _require_typed_sections(config: SonderConfig) -> None:
    """Reject malformed direct construction before any adapter exists."""
    if (
        type(config.control_state_rehearsal) is not ControlStateRehearsalConfig
        or type(config.compute) is not ComputeConfig
        or type(config.deployment) is not DeploymentConfig
        or type(config.secrets) is not Secrets
    ):
        raise ValueError(_INVALID_CONFIGURATION)


def _validated_data_replica_ids(config: SonderConfig) -> tuple[str, str]:
    """Return the exact local-plus-one-peer order required by the rehearsal."""
    nodes = config.compute.nodes
    if type(nodes) is not tuple or len(nodes) != 1:
        raise ValueError(_INVALID_CONFIGURATION)
    peer = nodes[0]
    if type(peer) is not ComputeNodeConfig:
        raise ValueError(_INVALID_CONFIGURATION)
    local_id = config.compute.node_id
    peer_id = peer.node_id
    if (
        not isinstance(local_id, str)
        or not isinstance(peer_id, str)
        or local_id == peer_id
    ):
        raise ValueError(_INVALID_CONFIGURATION)
    return local_id, peer_id


def _validate_config(config: SonderConfig) -> tuple[str, str]:
    """Apply loader-equivalent rehearsal/topology validation without I/O."""
    _require_typed_sections(config)
    section = config.control_state_rehearsal
    if section.enabled is False:
        raise ValueError("control-state rehearsal is disabled")

    try:
        errors = [
            *control_state_rehearsal_errors(config),
            *deployment_errors(config),
        ]
    except (AttributeError, TypeError, ValueError):
        raise ValueError(_INVALID_CONFIGURATION) from None
    if errors or config.compute.allow_remote is not True:
        raise ValueError(_INVALID_CONFIGURATION)

    data_replica_ids = _validated_data_replica_ids(config)
    if (
        section.node_id != data_replica_ids[0]
        or section.witness_id in data_replica_ids
        or config.deployment.automatic_takeover is not False
        or config.deployment.automatic_failback is not False
        or not config.secrets.control_state_rehearsal_key
    ):
        raise ValueError(_INVALID_CONFIGURATION)
    return data_replica_ids


def build_control_state_rehearsal(
    config: SonderConfig,
) -> ExternalControlStateCoordinator:
    """Construct one rehearsal-only provider; never construct an owner.

    Capability declarations establish only the shape required to accept future
    provider receipts.  They do not create an owner, start a network operation,
    mutate a lease, or establish automatic failover/failback.
    """
    if type(config) is not SonderConfig:
        raise TypeError("config must be a SonderConfig")

    data_replica_ids = _validate_config(config)
    section = config.control_state_rehearsal
    try:
        capabilities = ReplicatedControlStateCapabilities(
            provider_id=section.provider_id,
            protocol_version=CONTROL_STATE_PROTOCOL_VERSION,
            data_replica_ids=data_replica_ids,
            witness_ids=(section.witness_id,),
            durable_acknowledgements=True,
            external_fencing=True,
            partition_policy=PartitionState.SAFE,
        )
        validate_availability_profile(
            config.deployment.profile,
            data_replica_ids,
            local_node_id=config.compute.node_id,
            preferred_primary=config.deployment.preferred_primary,
            provider=capabilities,
            protocol_version=CONTROL_STATE_PROTOCOL_VERSION,
            require_provider_contract=True,
        )
    except (TypeError, ValueError):
        raise ValueError(_INVALID_CONFIGURATION) from None

    try:
        provider = HttpsControlStateProvider(
            origin=section.origin,
            api_key=config.secrets.control_state_rehearsal_key,
            capabilities=capabilities,
            timeout_seconds=section.timeout_seconds,
            allow_insecure_loopback=section.allow_insecure_loopback,
        )
    except (TypeError, ValueError):
        raise ValueError(_INVALID_CONFIGURATION) from None
    return ExternalControlStateCoordinator(
        provider,
        capabilities,
        minimum_data_replicas=2,
    )


__all__ = ["build_control_state_rehearsal"]
