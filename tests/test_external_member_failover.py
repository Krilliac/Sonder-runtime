"""External members: pre-response failover and shared-transport concurrency.

* A pre-response transport failure to an external member must fail over for
  idempotent work and count toward that member's circuit breaker, while an
  endpoint-policy refusal stays non-retryable.
* Concurrent callers on the one pinned transport must share DNS resolution
  instead of failing fast because another caller is resolving.
"""
import socket
import threading
import time
from urllib.request import Request

import pytest

from sonder_runtime.adapters.inference.external_membership import (
    ExternalMembershipSource,
    MembershipSourceError,
)
from sonder_runtime.adapters.inference.ollama_pool import OllamaWorkerPool

from tests.test_external_inference_membership import (  # noqa: F401 - autouse fixture
    NOW as EXTERNAL_NOW,
    ORIGIN as EXTERNAL_ORIGIN,
    configuration,
    credentials,
    private_authority_material,
)
from tests.test_inference_membership_controller import LOCAL

WORKER_A = "https://worker-a.example:11434"
WORKER_B = "https://worker-b.example:11434"


def _pinned_failure(tmp_path, monkeypatch, *, connect_error=None, answer="10.77.0.2"):
    """Drive the real pinned transport to one failure and return that error."""
    from sonder_runtime.adapters.inference import external_membership as module

    source = ExternalMembershipSource(configuration(tmp_path), credentials(tmp_path),
                                      clock=lambda: EXTERNAL_NOW)

    def resolve(_host, port, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (answer, port))]

    class Channel:
        def settimeout(self, _value): pass
        def connect(self, _address):
            if connect_error is not None:
                raise connect_error
        def close(self): pass

    class Context:
        check_hostname = True
        verify_mode = None
        def load_verify_locations(self, **_kwargs): pass
        def load_cert_chain(self, **_kwargs): pass
        def wrap_socket(self, channel, *, server_hostname):
            raise AssertionError("connect must fail before the TLS handshake")

    monkeypatch.setattr(module.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(module.socket, "socket", lambda *_a, **_kw: Channel())
    monkeypatch.setattr(module.ssl, "SSLContext", lambda *_a: Context())
    try:
        with pytest.raises(MembershipSourceError) as caught:
            source.open_worker_url(Request(EXTERNAL_ORIGIN + "/api/tags"), timeout=1)
        return caught.value
    finally:
        assert source.close(timeout=2)


@pytest.mark.parametrize("error", [ConnectionRefusedError(), TimeoutError(), OSError("network unreachable")])
def test_pre_response_transport_failure_is_retryable_and_stays_redacted(tmp_path, monkeypatch, error):
    failure = _pinned_failure(tmp_path, monkeypatch, connect_error=error)
    assert str(failure) == "external membership unavailable"
    assert failure.__cause__ is None and failure.__suppress_context__
    assert OllamaWorkerPool._retryable(failure)


def test_endpoint_policy_rejection_is_not_reclassified_as_transport_failure(tmp_path, monkeypatch):
    # An out-of-policy DNS answer is a security refusal, never a connectivity blip.
    failure = _pinned_failure(tmp_path, monkeypatch, answer="203.0.113.9")
    assert str(failure) == "external membership unavailable"
    assert not OllamaWorkerPool._retryable(failure)


def test_unreachable_external_member_fails_over_and_counts_toward_its_circuit(tmp_path, monkeypatch):
    failure = _pinned_failure(tmp_path, monkeypatch, connect_error=ConnectionRefusedError())
    pool = OllamaWorkerPool(LOCAL, (WORKER_A, WORKER_B), allow_remote=True,
                            failure_threshold=2)
    calls = []

    def send(origin):
        calls.append(origin)
        if len(calls) == 1:
            raise failure
        return origin

    result = pool.request(send, idempotent=True)
    assert len(calls) == 2 and result == calls[1] != calls[0]
    failed = next(item for item in pool.snapshots() if item.origin == calls[0])
    assert failed.consecutive_failures == 1


def test_unreachable_member_probe_is_a_transport_failure_not_incompatibility(tmp_path, monkeypatch):
    failure = _pinned_failure(tmp_path, monkeypatch, connect_error=ConnectionRefusedError())

    def prober(origin):
        if origin == WORKER_A:
            raise failure
        return {"models": ["code"]}

    pool = OllamaWorkerPool(LOCAL, (WORKER_A,), allow_remote=True, capability_prober=prober)
    pool.refresh_capabilities()
    record = next(item for item in pool.status()["workers"] if item["origin"] == WORKER_A)
    assert record["consecutive_failures"] == 1
    assert record["state"] != "incompatible"


def test_concurrent_callers_share_resolution_instead_of_failing_fast(tmp_path, monkeypatch):
    from sonder_runtime.adapters.inference import external_membership as module

    lookups = []

    def slow(host, port, **_kwargs):
        lookups.append(host)
        time.sleep(0.05)
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.77.0.2", port))]

    monkeypatch.setattr(module.socket, "getaddrinfo", slow)
    source = ExternalMembershipSource(configuration(tmp_path), credentials(tmp_path),
                                      clock=lambda: EXTERNAL_NOW)
    hosts = ("registry.example", "registry.example", "worker.example", "worker.example")
    barrier, outcomes = threading.Barrier(len(hosts)), [None] * len(hosts)

    def call(index):
        barrier.wait()
        try:
            source._transport._resolve(hosts[index], 443, time.monotonic() + 2)
            outcomes[index] = "ok"
        except Exception as error:  # pragma: no cover - reported by the assertion
            outcomes[index] = type(error).__name__

    threads = [threading.Thread(target=call, args=(index,)) for index in range(len(hosts))]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        assert outcomes == ["ok"] * len(hosts)
        # Same-host callers joined one lookup; distinct hosts each had their own.
        assert sorted(set(lookups)) == ["registry.example", "worker.example"]
        assert len(lookups) <= len(hosts)
    finally:
        assert source.close(timeout=2)
