"""Host-owned, local-only sealed artifact source authority."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import threading
from weakref import ref

from sonder_runtime.application.artifacts.mobility_source import (
    ArtifactMobilitySourceService,
    MobilitySourceError,
    SourceArtifactLimits,
    SourceAuthority,
)
from sonder_runtime.application.errors import DependencyUnavailable
from sonder_runtime.application.ports.artifact_mobility import (
    ArtifactMobilityPublisher,
    ArtifactMobilityReader,
    _issue_publisher,
    _issue_reader,
    _revoke,
)
from sonder_runtime.platform.artifact_mobility_source_config import (
    artifact_mobility_source_errors,
    source_scope_id,
)
from sonder_runtime.platform.config import ConfigError


@dataclass(frozen=True, eq=False, kw_only=True)
class _SourceContext:
    scope_id: str
    role: str
    _issuer: object = field(repr=False, compare=False)
    _capability: object = field(repr=False, compare=False)
    _proof: tuple = field(repr=False, compare=False)


class ArtifactMobilitySourceBinding:
    """Private source spool with two explicitly injected in-process capabilities.

    ``publisher_capability`` belongs to the trusted local producer and is also
    the provenance proof required by ``publish_sealed``.  ``reader_capability``
    is separate so future outbound dispatch composition can receive read access
    without implicitly granting it to a producer.  Omitting either capability
    leaves that port unavailable.  No listener, bearer, receiver grant, or
    destination is accepted here.
    """

    def __init__(
        self,
        config_provider,
        *,
        publisher_capability: object | None = None,
        reader_capability: object | None = None,
    ) -> None:
        if not callable(config_provider):
            raise TypeError("config_provider must be callable")
        for capability in (publisher_capability, reader_capability):
            if capability is not None and type(capability) is not object:
                raise TypeError("source capabilities must be opaque object instances")
        self._config_provider = config_provider
        self._publisher_capability = publisher_capability
        self._reader_capability = reader_capability
        self._issuer = object()
        self._lock = threading.RLock()
        self._service = None
        self._service_settings = None
        # The binding must not own a port strongly: a holder with only its
        # token must have no reverse object-graph edge into this host state.
        self._publisher_port_ref = None
        self._publisher_proof = None
        self._reader_port_ref = None
        self._reader_proof = None
        self._closed = False
        try:
            config = config_provider()
            errors = artifact_mobility_source_errors(config)
            if errors:
                raise ConfigError(errors)
            if config.artifact_mobility_source.enabled:
                self._check_store_roots(config)
        except ConfigError:
            raise
        except PermissionError as error:
            raise ConfigError(["[artifact_mobility_source].store_dir " + str(error)]) from None
        except Exception:
            raise ConfigError(["[artifact_mobility_source] invalid"]) from None

    @staticmethod
    def _private_root(config) -> Path:
        return Path(config.artifact_mobility_source.store_dir).absolute()

    @staticmethod
    def _paths_overlap(first: Path, second: Path) -> bool:
        try:
            first = first.resolve(strict=False)
            second = second.resolve(strict=False)
            return first == second or first in second.parents or second in first.parents
        except (OSError, RuntimeError, ValueError):
            raise PermissionError("invalid") from None

    @classmethod
    def _check_store_roots(cls, config) -> None:
        root = cls._private_root(config)
        state = config.state
        values = list(getattr(state, "workspace_roots", ()))
        if getattr(state, "home", ""):
            values.append(state.home)
        try:
            from sonder_runtime.adapters.filesystem.file_ops import allowed_roots

            values.extend(str(value) for value in allowed_roots())
        except Exception:
            raise PermissionError("invalid") from None
        for value in values:
            if not isinstance(value, str) or not value:
                continue
            if cls._paths_overlap(root, Path(value)):
                raise PermissionError("overlaps configured writable root")

    @staticmethod
    def _proof(config) -> tuple:
        source = config.artifact_mobility_source
        return (
            source_scope_id(source),
            source,
            config.state.home,
            tuple(config.state.workspace_roots),
        )

    def _current(self):
        try:
            config = self._config_provider()
            if self._closed or not config.artifact_mobility_source.enabled:
                raise DependencyUnavailable("UNAVAILABLE")
            if artifact_mobility_source_errors(config):
                raise DependencyUnavailable("UNAVAILABLE")
            self._check_store_roots(config)
            return config
        except DependencyUnavailable:
            raise
        except PermissionError:
            raise DependencyUnavailable("UNAVAILABLE") from None
        except Exception:
            raise DependencyUnavailable("UNAVAILABLE") from None

    @staticmethod
    def _limits(config) -> SourceArtifactLimits:
        source = config.artifact_mobility_source
        return SourceArtifactLimits(
            max_object_bytes=source.max_object_bytes,
            total_bytes=source.total_bytes,
            ttl_seconds=source.ttl_seconds,
        )

    def _context_for(self, capability: object, role: str) -> _SourceContext:
        expected = (
            self._publisher_capability if role == "publish" else self._reader_capability
        )
        if expected is None:
            raise DependencyUnavailable("UNAVAILABLE")
        if capability is not expected:
            raise PermissionError("FORBIDDEN")
        config = self._current()
        return _SourceContext(
            scope_id=source_scope_id(config.artifact_mobility_source),
            role=role,
            _issuer=self._issuer,
            _capability=capability,
            _proof=self._proof(config),
        )

    def publisher_for(self, capability: object) -> ArtifactMobilityPublisher:
        context = self._context_for(capability, "publish")
        with self._lock:
            existing = (
                self._publisher_port_ref()
                if self._publisher_port_ref is not None
                else None
            )
            if (
                existing is not None
                and self._publisher_proof == context._proof
            ):
                return existing
            _revoke(existing)
            port = _issue_publisher(
                lambda stream, immutable_spec, trusted_provenance: self._publish_from_port(
                    context, stream, immutable_spec, trusted_provenance
                )
            )
            self._publisher_port_ref = ref(port)
            self._publisher_proof = context._proof
            return port

    def reader_for(self, capability: object) -> ArtifactMobilityReader:
        context = self._context_for(capability, "read")
        with self._lock:
            existing = self._reader_port_ref() if self._reader_port_ref is not None else None
            if (
                existing is not None
                and self._reader_proof == context._proof
            ):
                return existing
            _revoke(existing)
            port = _issue_reader(
                lambda source_artifact_id, offset=None, length=None: self._read_from_port(
                    context, source_artifact_id, offset, length
                )
            )
            self._reader_port_ref = ref(port)
            self._reader_proof = context._proof
            return port

    def _publish_from_port(
        self, context: _SourceContext, stream, immutable_spec: dict, trusted_provenance: object
    ) -> dict:
        # The port registry never receives this context or binding through the
        # port object itself.  Validate before lazy store construction.
        try:
            service = self._service_for_port(context, "publish", trusted_provenance)
        except PermissionError:
            raise MobilitySourceError("FORBIDDEN") from None
        except DependencyUnavailable:
            raise MobilitySourceError("UNAVAILABLE") from None
        return service.publish_sealed(stream, immutable_spec, trusted_provenance, context)

    def _read_from_port(
        self, context: _SourceContext, source_artifact_id: str, offset, length):
        try:
            service = self._service_for_port(context, "read")
        except PermissionError:
            raise MobilitySourceError("FORBIDDEN") from None
        except DependencyUnavailable:
            raise MobilitySourceError("UNAVAILABLE") from None
        if offset is None and length is None:
            return service.inspect_sealed(source_artifact_id, context)
        if offset is None or length is None:
            raise MobilitySourceError("INVALID_BOUND")
        return service.read_range(source_artifact_id, offset, length, context)

    def _authorize(self, context: object, action: str, trusted_provenance: object = None) -> SourceAuthority:
        if action not in ("publish", "read") or not isinstance(context, _SourceContext):
            raise PermissionError("FORBIDDEN")
        expected_capability = (
            self._publisher_capability if action == "publish" else self._reader_capability
        )
        if (
            context._issuer is not self._issuer
            or context.role != action
            or expected_capability is None
            or context._capability is not expected_capability
        ):
            raise PermissionError("FORBIDDEN")
        config = self._current()
        if (
            context.scope_id != source_scope_id(config.artifact_mobility_source)
            or context._proof != self._proof(config)
        ):
            raise PermissionError("FORBIDDEN")
        if action == "publish" and trusted_provenance is not expected_capability:
            raise PermissionError("FORBIDDEN")
        return SourceAuthority(
            scope_id=context.scope_id,
            limits=self._limits(config),
        )

    def _service_for_port(
        self, context: object, action: str, trusted_provenance: object = None
    ) -> ArtifactMobilitySourceService:
        # No caller can even initialize the private spool without first
        # presenting a binding-issued context for the requested local port.
        self._authorize(context, action, trusted_provenance)
        config = self._current()
        root = self._private_root(config)
        source = config.artifact_mobility_source
        settings = (
            str(root),
            source.max_object_bytes,
            source.total_bytes,
            source.ttl_seconds,
        )
        with self._lock:
            if self._service is not None:
                if settings != self._service_settings:
                    raise DependencyUnavailable("RESTART_REQUIRED")
                return self._service
            try:
                from sonder_runtime.adapters.persistence.artifact_mobility_source import (
                    SQLiteArtifactMobilitySourceStore,
                )

                self._service = ArtifactMobilitySourceService(
                    SQLiteArtifactMobilitySourceStore(root), authorizer=self._authorize
                )
                self._service_settings = settings
                return self._service
            except MobilitySourceError:
                raise DependencyUnavailable("UNAVAILABLE") from None
            except Exception:
                raise DependencyUnavailable("UNAVAILABLE") from None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            service = self._service
            port_refs = (self._publisher_port_ref, self._reader_port_ref)
            self._publisher_port_ref = None
            self._publisher_proof = None
            self._reader_port_ref = None
            self._reader_proof = None
        for port_ref in port_refs:
            if port_ref is not None:
                _revoke(port_ref())
        if service is not None:
            service.close()
