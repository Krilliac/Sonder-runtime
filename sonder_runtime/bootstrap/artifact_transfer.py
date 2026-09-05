"""Host-owned artifact authority. Incoming fields never select a principal."""
from dataclasses import dataclass, field, fields
import hashlib
import hmac
import threading
from pathlib import Path

from sonder_runtime.application.context import OperationContext, local_owner_context
from sonder_runtime.application.errors import DependencyUnavailable, Unauthenticated
from sonder_runtime.platform.artifact_transfer_config import (
    artifact_transfer_errors, private_store_path,
)
from sonder_runtime.platform.config import ConfigError


@dataclass(frozen=True, kw_only=True)
class _ArtifactContext(OperationContext):
    _issuer: object = field(repr=False, compare=False)
    _proof: tuple = field(repr=False)


class ArtifactTransferBinding:
    """One receiver, live grant configuration, immutable authenticated calls.

    The injected provider must return the host's currently applied typed config.
    Mutating a TOML file does not imply that a host has applied that configuration.
    """

    def __init__(self, config_provider):
        self._config_provider = config_provider
        self._issuer = object()
        self._lock = threading.RLock()
        self._service = None
        self._service_settings = None
        self._closed = False
        errors = artifact_transfer_errors(config_provider())
        if errors:
            raise ConfigError(errors)

    def _current(self):
        config = self._config_provider()
        if self._closed or not config.artifact_transfer.enabled:
            raise DependencyUnavailable("UNAVAILABLE")
        if artifact_transfer_errors(config):
            raise DependencyUnavailable("UNAVAILABLE")
        return config

    @staticmethod
    def _check_store_roots(config):
        root = private_store_path(config).resolve()
        # Typed configuration can precede application of compatibility roots.
        # Check these too; the store additionally checks every live file root.
        roots = list(config.state.workspace_roots)
        if config.state.home:
            roots.append(config.state.home)
        for value in roots:
            other = Path(value).resolve()
            if root.is_relative_to(other) or other.is_relative_to(root):
                raise PermissionError("artifact store overlaps a configured writable root")

    def current_config(self):
        return self._current()

    @staticmethod
    def _proof(config):
        # Only a private digest, never a bearer, survives request authentication.
        return (config.artifact_transfer, str(private_store_path(config)), config.state,
                hashlib.sha256(config.secrets.artifact_transfer_key.encode("ascii")).digest())

    def authenticate(self, authorization, *, correlation_id):
        config = self._current()
        expected = "Bearer " + config.secrets.artifact_transfer_key
        if not isinstance(authorization, str) or len(authorization) > 520 or not hmac.compare_digest(
            authorization.encode("utf-8"), expected.encode("ascii")
        ):
            raise Unauthenticated("UNAUTHORIZED")
        base = local_owner_context(correlation_id=correlation_id, source="http",
                                   auth_level="user", timeout_seconds=300)
        values = {item.name: getattr(base, item.name) for item in fields(base)}
        values["principal_id"] = config.artifact_transfer.principal_id
        return _ArtifactContext(**values, _issuer=self._issuer, _proof=self._proof(config))

    def validate_context(self, context):
        try:
            config = self._current()
        except DependencyUnavailable:
            raise PermissionError("artifact grant is unavailable") from None
        if not isinstance(context, _ArtifactContext) or context._issuer is not self._issuer or (
            context._proof != self._proof(config) or context.expired or context.cancellation.cancelled
            or context.principal_id != config.artifact_transfer.principal_id
        ):
            raise PermissionError("artifact grant is no longer current")
        self._check_store_roots(config)
        return config

    def authorize(self, context, action):
        from sonder_runtime.application.artifacts.transfer import TransferGrant
        config = self.validate_context(context)
        section = config.artifact_transfer
        if action not in ("read", "write") or not getattr(section, "can_" + action):
            raise PermissionError("artifact operation is not granted")
        return TransferGrant(section.principal_id, section.project_id, section.peer_node_id,
                             section.grant_id, section.grant_revision, section.expires_at,
                             section.can_read, section.can_write, section.max_object_bytes,
                             section.quota_bytes)

    def service(self):
        config = self._current()
        self._check_store_roots(config)
        settings = (str(private_store_path(config)), config.artifact_transfer.max_object_bytes,
                    config.artifact_transfer.total_bytes, config.artifact_transfer.ttl_seconds)
        with self._lock:
            if self._service is not None:
                if settings != self._service_settings:
                    raise DependencyUnavailable("RESTART_REQUIRED")
                return self._service
            from sonder_runtime.adapters.persistence.artifact_transfer import SQLiteArtifactTransferStore
            from sonder_runtime.application.artifacts.transfer import ArtifactTransferService, TransferLimits
            self._service = ArtifactTransferService(
                SQLiteArtifactTransferStore(private_store_path(config)), authorizer=self.authorize,
                limits=TransferLimits(max_object_bytes=settings[1], total_bytes=settings[2],
                                      ttl_seconds=settings[3]),
            )
            self._service_settings = settings
            return self._service

    def start(self):
        if self._config_provider().artifact_transfer.enabled:
            self.service()  # Anchor/ACL/root-overlap validation precedes the listener.

    def close(self):
        with self._lock:
            self._closed = True
            service = self._service
        if service is not None:
            service.close()
