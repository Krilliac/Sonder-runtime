"""Private, lazy local operator composition for fixed-peer artifact copies.

No HTTP/MCP/REPL surface receives this binding or a source port. The optional
source binding is an exact, host-owned typed object, never a plugin callback.
A default graph has no trusted producer and therefore cannot send or resume.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import re
from threading import RLock
import time

from .artifact_mobility_source import ArtifactMobilitySourceBinding
from ..application.artifacts.mobility import (
    ArtifactMobilityDispatchService, ArtifactMobilityJournal,
    MobilityDispatchContext, MobilityJournalError,
)
from ..application.artifacts.mobility_source import MobilitySourceError
from ..application.artifacts.transfer import TransferError
from ..platform.artifact_mobility_config import artifact_mobility_errors
from ..platform.artifact_mobility_source_config import source_scope_id
from ..platform.config import SonderConfig, validate_deployment


_PUBLIC_ERRORS = frozenset({
    'UNAVAILABLE', 'SOURCE_UNAVAILABLE', 'NOT_FOUND', 'INVALID_REQUEST',
    'CONFIRMATION_REQUIRED', 'BUSY', 'TERMINAL', 'LEASE_LOST',
    'IMMUTABLE_FENCE', 'CAPACITY', 'INTEGRITY', 'RESTART_REQUIRED',
})


def mobility_error_projection(error: Exception) -> dict:
    """Only constrained local codes cross the operator boundary."""
    code = 'UNAVAILABLE'
    if isinstance(error, (MobilityJournalError, MobilitySourceError, TransferError)):
        if len(error.args) == 1 and isinstance(error.args[0], str) and error.args[0] in _PUBLIC_ERRORS:
            code = error.args[0]
    return {'outcome_code': code}


class _OpenDispatchRepository:
    """Stop local lease work after close without releasing a durable lease."""

    def __init__(self, binding, repository):
        self._binding = binding
        self._repository = repository

    def __getattr__(self, name):
        method = getattr(self._repository, name)
        if not callable(method):
            return method

        def checked(*args, **kwargs):
            with self._binding._lock:
                self._binding._ensure_open()
                return method(*args, **kwargs)
        return checked


class ArtifactMobilityBinding:
    """One local journal and source authority; transport stays dispatch-only."""

    def __init__(self, config_provider, *, source_binding: ArtifactMobilitySourceBinding | None = None):
        if not callable(config_provider):
            raise TypeError('config_provider must be callable')
        if source_binding is not None and type(source_binding) is not ArtifactMobilitySourceBinding:
            raise TypeError('source binding must be host-owned')
        self._config_provider = config_provider
        self._source = source_binding
        self._repository = None
        self._settings = None
        self._closed = False
        self._lock = RLock()
        self._config()

    def __repr__(self):
        return 'ArtifactMobilityBinding(private=True)'

    def _ensure_open(self):
        if self._closed:
            raise MobilityJournalError('UNAVAILABLE')

    def _config(self):
        self._ensure_open()
        try:
            config = self._config_provider()
            if type(config) is not SonderConfig:
                raise ValueError
            validate_deployment(config)
            if not config.artifact_mobility_source.enabled or not config.artifact_mobility.enabled:
                raise ValueError
            ArtifactMobilitySourceBinding._check_store_roots(config)
            return config
        except Exception:
            raise MobilityJournalError('UNAVAILABLE') from None

    def _source_for_dispatch(self, *, resuming=False):
        config = self._config()
        with self._lock:
            if self._source is None:
                # Reader-only default; only an existing trusted host-owned
                # publisher binding can authorize operator dispatch.
                self._source = ArtifactMobilitySourceBinding(
                    self._config_provider, reader_capability=object())
            source = self._source
        if (source is None or source._publisher_capability is None
                or source._reader_capability is None):
            raise MobilityJournalError('SOURCE_UNAVAILABLE')
        # Send admits only the current exact source binding. Resume must reach
        # the existing leased immutable fence with the original source reader:
        # a scope change becomes terminal before any peer request, rather than
        # leaving an old operation reusable after configuration is rolled back.
        if not resuming and source._proof(source._current()) != source._proof(config):
            raise MobilityJournalError('SOURCE_UNAVAILABLE')
        return source

    def _repository_for_read(self):
        config = self._config()
        # The journal shares the already validated private source authority root,
        # never the general state/workspace or destination receiver store.
        root = Path(config.artifact_mobility_source.store_dir).absolute() / 'outbound-journal'
        settings = (root, config.artifact_mobility.max_live_operations)
        with self._lock:
            self._ensure_open()
            if self._repository is None:
                from ..adapters.persistence.artifact_mobility import SQLiteArtifactMobilityJournal
                self._repository = SQLiteArtifactMobilityJournal(root,
                    max_live_operations=config.artifact_mobility.max_live_operations)
                self._settings = settings
            elif settings != self._settings:
                raise MobilityJournalError('RESTART_REQUIRED')
            return self._repository

    def _context(self):
        config = self._config()
        source, destination = config.artifact_mobility_source, config.artifact_mobility
        key = config.secrets.artifact_mobility_peer_key
        canonical = json.dumps({
            'protocol_version': 'mobility-v1',
            'origin': destination.destination_origin,
            'certificate': destination.destination_tls_certificate_sha256,
            'attestation': destination.expected_recipient_attestation_sha256,
            'credential_id': destination.destination_credential_id,
            'label': destination.destination_label,
            'max_object_bytes': destination.max_object_bytes,
        }, sort_keys=True, separators=(',', ':')).encode('ascii')
        return MobilityDispatchContext(
            source_owner_id=source.source_owner_id,
            source_scope_id=source_scope_id(source),
            destination_label=destination.destination_label,
            destination_scope_id=hashlib.sha256(canonical).hexdigest(),
            credential_generation=hashlib.sha256(destination.destination_credential_id.encode('ascii')).hexdigest(),
            destination_binding_hmac=hmac.new(key.encode('ascii'), canonical, hashlib.sha256).hexdigest(),
            credential_material=key,
            attempt_lease_seconds=destination.attempt_lease_seconds,
            receipt_ttl_seconds=destination.receipt_ttl_seconds,
        )

    def _dispatch_service(self, *, resuming=False):
        source = self._source_for_dispatch(resuming=resuming)
        reader = source.reader_for(source._reader_capability)
        config = self._config()
        from ..adapters.compute_fabric.artifact_mobility import ConfiguredArtifactMobilityPeer
        peer = ConfiguredArtifactMobilityPeer(config,
            credential_provider=lambda credential_id: self._credential(credential_id))
        repository = _OpenDispatchRepository(self, self._repository_for_read())
        journal = ArtifactMobilityJournal(repository)
        return ArtifactMobilityDispatchService(source_reader=reader, peer=peer,
            repository=repository, journal=journal, current_context=self._context)

    def _credential(self, credential_id):
        config = self._config()
        if credential_id != config.artifact_mobility.destination_credential_id:
            raise MobilityJournalError('IMMUTABLE_FENCE')
        return config.secrets.artifact_mobility_peer_key

    def send(self, source_artifact_id, *, confirm_destination):
        source = self._source_for_dispatch()
        context = self._context()
        if (not isinstance(confirm_destination, str) or not confirm_destination.isascii()
                or not hmac.compare_digest(confirm_destination, context.destination_label)):
            raise MobilityJournalError('CONFIRMATION_REQUIRED')
        if not isinstance(source_artifact_id, str) or re.fullmatch('[0-9a-f]{32}', source_artifact_id) is None:
            raise MobilityJournalError('INVALID_REQUEST')
        source.reader_for(source._reader_capability).inspect_sealed(source_artifact_id)
        return self._dispatch_service().send(source_artifact_id,
            confirm_destination=confirm_destination).public_status()

    def resume(self, operation_id):
        if not isinstance(operation_id, str) or re.fullmatch('[0-9a-f]{32}', operation_id) is None:
            raise MobilityJournalError('NOT_FOUND')
        self._source_for_dispatch(resuming=True)
        repository = self._repository_for_read()
        repository.load_operation_for_fencing(operation_id)
        # Local recovery only; an explicit resume remains the sole dispatcher.
        repository.recover_expired_leases(now=time.time())
        return self._dispatch_service(resuming=True).resume(operation_id).public_status()

    def status(self, operation_id):
        config = self._config()
        return self._repository_for_read().public_status(operation_id,
            config.artifact_mobility_source.source_owner_id)

    def list(self):
        config = self._config()
        return self._repository_for_read().list_public_status(
            config.artifact_mobility_source.source_owner_id)

    def close(self):
        with self._lock:
            self._closed = True
            repository, source = self._repository, self._source
        if repository is not None:
            repository.close()
        if source is not None:
            source.close()


def compose_artifact_mobility(config_provider, *, source_binding: ArtifactMobilitySourceBinding | None = None):
    """Application-owned lazy closure; never a caller-supplied factory/port."""
    if source_binding is not None and type(source_binding) is not ArtifactMobilitySourceBinding:
        raise TypeError('source binding must be host-owned')
    binding = None
    closed = False
    lock = RLock()

    def get_binding():
        nonlocal binding
        with lock:
            if closed:
                raise MobilityJournalError('UNAVAILABLE')
            if binding is None:
                binding = ArtifactMobilityBinding(config_provider, source_binding=source_binding)
            return binding

    def status(operation_id):
        try:
            return get_binding().status(operation_id)
        except Exception as error:
            return mobility_error_projection(error)

    def listing():
        try:
            return {'operations': list(get_binding().list())}
        except Exception as error:
            return mobility_error_projection(error)

    def available():
        if closed or source_binding is None:
            return False
        try:
            config = config_provider()
            validate_deployment(config)
            return bool(config.artifact_mobility_source.enabled and config.artifact_mobility.enabled
                and not artifact_mobility_errors(config)
                and source_binding._publisher_capability is not None
                and source_binding._reader_capability is not None
                and not source_binding._closed
                and source_binding._proof(source_binding._config_provider()) == source_binding._proof(config))
        except Exception:
            return False

    def close():
        nonlocal closed
        with lock:
            closed = True
            existing = binding
        if existing is not None:
            existing.close()
        elif source_binding is not None:
            source_binding.close()

    return get_binding, status, listing, available, close
