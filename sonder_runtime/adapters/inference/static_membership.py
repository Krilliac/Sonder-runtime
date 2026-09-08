"""Locally authenticated snapshots of exact, typed static remote configuration.

Loopback workers remain exclusively in the static-local pool lane. The private
per-source MAC binds canonical bytes produced here; it is not an external
registry trust key, persistent authority, or a remote membership transport.
"""
from dataclasses import asdict
from datetime import timedelta
import hashlib
import hmac
import json
import secrets
from threading import Lock
from time import monotonic

from ...application.ports.inference_membership import MembershipSourceLimits
from ...domain.inference_membership import MembershipSnapshot, WorkerAdvertisement
from ...platform.config import OllamaConfig
from .ollama_pool import _is_loopback, validate_worker_origin


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def configured_worker_origins(config: OllamaConfig) -> tuple[str, ...]:
    """Validate bounded static configuration without source, pool, or I/O work."""
    if type(config) is not OllamaConfig:
        raise ValueError("exact typed Ollama configuration required")
    if type(config.allow_remote) is not bool:
        raise ValueError("remote consent must be an exact boolean")
    for value, ceiling in ((config.worker_pool_max_workers, 256),
                           (config.worker_max_inflight, 64),
                           (config.worker_capability_ttl_seconds, 86400)):
        if type(value) is not int or not 1 <= value <= ceiling:
            raise ValueError("static membership configuration exceeds its bound")
    if type(config.workers) is not tuple or len(config.workers) >= config.worker_pool_max_workers:
        raise ValueError("static membership roster exceeds its configured bound")
    if any(type(origin) is not str for origin in (config.url, *config.workers)):
        raise ValueError("static origins must be exact strings")
    origins = tuple(validate_worker_origin(origin, allow_remote=config.allow_remote,
                                           trusted_origins=config.trusted_origins)
                    for origin in (config.url, *config.workers))
    if len(set(origins)) != len(origins):
        raise ValueError("duplicate canonical static origin")
    return origins


class StaticMembershipSource:
    cluster_id = "static-config"
    issuer_id = "local-config"

    def __init__(self, config: OllamaConfig, *, clock):
        origins = configured_worker_origins(config)
        self._configured_origins = origins
        self._configuration = config
        self.local_origins = tuple(origin for origin in origins if _is_loopback(origin))
        self._workers = tuple(WorkerAdvertisement(
            worker_id="static-" + hashlib.sha256(origin.encode("ascii")).hexdigest(),
            origin=origin, member_generation=1, advertised_capacity=config.worker_max_inflight,
        ) for origin in origins if not _is_loopback(origin))
        self._ttl = config.worker_capability_ttl_seconds
        self._clock = clock
        self._key = secrets.token_bytes(32)
        self._generation = 0
        self._lock = Lock()

    @property
    def configured_origins(self) -> tuple[str, ...]:
        return self._configured_origins

    def read_snapshot(self, *, limits: MembershipSourceLimits) -> MembershipSnapshot:
        if type(limits) is not MembershipSourceLimits:
            raise ValueError("exact bounded source limits required")
        started = monotonic()
        if not self._lock.acquire(timeout=limits.timeout_seconds):
            raise TimeoutError("static source is busy")
        try:
            if len(self._workers) > limits.max_advertisements:
                raise ValueError("static snapshot exceeds advertisement limit")
            now = self._clock()
            payload = dict(cluster_id=self.cluster_id, issuer_id=self.issuer_id,
                           generation=self._generation + 1, protocol_version=1,
                           issued_at=now.isoformat(), expires_at=(now + timedelta(seconds=self._ttl)).isoformat(),
                           workers=[asdict(worker) for worker in self._workers])
            signature = hmac.new(self._key, _canonical(payload), hashlib.sha256).hexdigest()
            raw = _canonical(dict(payload=payload, signature=signature))

            def verify(envelope):
                signed = json.loads(envelope)
                expected = hmac.new(self._key, _canonical(signed["payload"]), hashlib.sha256).hexdigest()
                return hmac.compare_digest(expected, signed["signature"])

            result = MembershipSnapshot.from_signed_envelope(raw, verify=verify,
                max_advertisements=limits.max_advertisements, max_bytes=limits.max_bytes)
            if monotonic() - started >= limits.timeout_seconds:
                raise TimeoutError("static snapshot deadline elapsed")
            self._generation = result.generation
            return result
        finally:
            self._lock.release()
