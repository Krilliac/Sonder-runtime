"""Process-boundary rehearsal, NOT real TLS pinning or independent-host availability.

Only this test explicitly injects a numeric-loopback HTTP connection factory
with a synthetic certificate. Production configuration still requires HTTPS.
"""

from dataclasses import replace
import hashlib
import http.client
from http.server import ThreadingHTTPServer
import io
import json
import multiprocessing
import threading
import time

import pytest

from sonder_runtime.adapters.compute_fabric.artifact_mobility import ConfiguredArtifactMobilityPeer
from sonder_runtime.application.artifacts.mobility import (
    ArtifactMobilityDispatchService, ArtifactMobilityJournal, MobilityJournalError,
)
from sonder_runtime.bootstrap.artifact_mobility import ArtifactMobilityBinding
from sonder_runtime.bootstrap.artifact_mobility_source import ArtifactMobilitySourceBinding
from tests.test_artifact_mobility_binding import config_for


_SYNTHETIC_CERTIFICATE = b'test-only synthetic leaf: no TLS handshake'


def _receiver_process(root, identity, control):
    """Production Handler and receiver store in a separate spawned process."""
    from sonder_runtime.interfaces.http import serve
    from tests.test_artifact_transfer_mobility_protocol import _receiver

    binding, context = _receiver(root, can_read=False, receiver_identity_id=identity)
    serve._ARTIFACT_TRANSFER_BINDING = binding

    class QuietHandler(serve.Handler):
        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    service = binding.service()
    try:
        control.send((server.server_address[1], binding.mobility_attestation(context)['sha256']))
        while True:
            command = control.recv()
            if command == 'stop':
                break
            if command == 'finish-verification':
                # Synchronize the receiver's existing verifier, not a sender retry.
                service._workers.shutdown(wait=True)
                control.send('finished')
                continue
            assert command == 'snapshot'
            with service.store._connection() as connection:
                rows = connection.execute('SELECT * FROM artifact_uploads').fetchall()
            snapshots = []
            for row in rows:
                snapshot = {'state': row['state'], 'offset': row['offset'], 'command': row['command']}
                if row['state'] == 'sealed':
                    spec = json.loads(row['spec'])
                    # Private test inspection, never a write-only receiver read API.
                    with service.store._directories(row) as (_, stage):
                        data = stage.read_bytes(spec['sha256'], max_bytes=spec['size_bytes'])
                    snapshot.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
                snapshots.append(snapshot)
            control.send(snapshots)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        binding.close()
        control.close()


class _Receiver:
    def __init__(self, root, identity):
        context = multiprocessing.get_context('spawn')
        self.control, child = context.Pipe()
        self.process = context.Process(target=_receiver_process, args=(root, identity, child))
        self.process.start()
        child.close()
        assert self.control.poll(30), 'receiver did not start'
        self.port, self.attestation = self.control.recv()

    def query(self, command='snapshot'):
        self.control.send(command)
        assert self.control.poll(30), 'receiver control timeout'
        return self.control.recv()

    def close(self):
        if self.process.is_alive():
            self.control.send('stop')
            self.process.join(timeout=10)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=5)
        self.control.close()
        assert self.process.exitcode == 0


class _SyntheticSocket:
    def __init__(self, sock):
        self.sock = sock

    def getpeercert(self, *, binary_form):
        assert binary_form
        return _SYNTHETIC_CERTIFICATE

    def __getattr__(self, name):
        return getattr(self.sock, name)


def _numeric_loopback_peer_factory(ports, requests, *, interrupt=False):
    """Explicit test injection only; no production fallback to plain HTTP."""
    class Connection(http.client.HTTPConnection):
        def connect(self):
            super().connect()
            self.sock = _SyntheticSocket(self.sock)

        def request(self, method, url, body=None, headers=None, **kwargs):
            requests.append((self.port, method, url, len(body or b'')))
            return super().request(method, url, body=body, headers=headers or {}, **kwargs)

    def connection_factory(host, port, timeout, context):
        assert host == '127.0.0.1' and port in ports
        return Connection(host, port, timeout=timeout)

    class InterruptedPeer(ConfiguredArtifactMobilityPeer):
        def append(self, *args):
            result = super().append(*args)
            assert result['next_offset'] > 0
            raise _InterruptedAfterDurableAppend()

    def factory(config):
        peer_type = InterruptedPeer if interrupt else ConfiguredArtifactMobilityPeer
        return peer_type(config, credential_provider=lambda _: config.secrets.artifact_mobility_peer_key,
            connection_factory=connection_factory)
    return factory


class _InterruptedAfterDurableAppend(BaseException):
    """Leave the durable lease live as an abruptly interrupted sender would."""


def _compose(config, clock, *, peer_factory):
    publisher, reader = object(), object()
    source = ArtifactMobilitySourceBinding(lambda: config,
        publisher_capability=publisher, reader_capability=reader)
    binding = ArtifactMobilityBinding(lambda: config, source_binding=source)
    repository = binding._repository_for_dispatch()
    journal = ArtifactMobilityJournal(repository, clock=clock)
    service = ArtifactMobilityDispatchService(source_reader=source.reader_for(reader),
        peer=peer_factory(config), repository=repository, journal=journal,
        current_context=binding._context, clock=clock)
    return binding, source.publisher_for(publisher), publisher, repository, service


def test_composed_process_receivers_resume_durable_offset_after_local_lease_recovery(tmp_path_factory):
    """Two local processes are not two hosts and the synthetic leaf is not TLS proof."""
    # Content-addressed paths add a scope, artifact ID and digest. Keep the
    # unique fixture root short enough for native Windows file APIs.
    tmp_path = tmp_path_factory.mktemp('mob')
    target = _Receiver(tmp_path / 'a', 'receiver-a')
    other = None
    binding = reopened = wrong = None
    try:
        other = _Receiver(tmp_path / 'b', 'receiver-b')
        assert target.process.pid != other.process.pid
        config = config_for(tmp_path / 's')
        config = replace(config,
            artifact_mobility_source=replace(config.artifact_mobility_source, source_owner_id='source-owner-a'),
            artifact_mobility=replace(config.artifact_mobility, destination_label='node-b',
                destination_origin=f'https://127.0.0.1:{target.port}',
                destination_tls_certificate_sha256=hashlib.sha256(_SYNTHETIC_CERTIFICATE).hexdigest(),
                expected_recipient_attestation_sha256=target.attestation),
            secrets=replace(config.secrets, artifact_mobility_peer_key='transfer-' + 'b' * 32))
        assert not config.artifact_transfer.enabled
        now = [time.time()]
        clock = lambda: now[0]
        requests = []
        ports = {target.port, other.port}
        data = bytes(range(256)) * 10241  # Binary, > two 1 MiB receiver chunks.
        spec = {'sha256': hashlib.sha256(data).hexdigest(), 'size_bytes': len(data),
            'media_type': 'application/octet-stream'}
        capability = object()
        producer = ArtifactMobilitySourceBinding(lambda: config, publisher_capability=capability)
        try:
            artifact = producer.publisher_for(capability).publish_sealed(io.BytesIO(data), spec, capability)
        finally:
            producer.close()
        binding, _, _, repository, service = _compose(config, clock,
            peer_factory=_numeric_loopback_peer_factory(ports, requests, interrupt=True))
        with pytest.raises(_InterruptedAfterDurableAppend):
            service.send(artifact['source_artifact_id'], confirm_destination='node-b')
        receipt = repository.list_public_status('source-owner-a')[0]
        operation_id = receipt['operation_id']
        interrupted = repository.load_operation(operation_id, 'source-owner-a')
        assert interrupted.state == 'dispatching'
        assert interrupted.lease_expires_at > now[0]
        before = target.query()
        assert len(before) == 1 and before[0]['offset'] == 1024 * 1024
        assert other.query() == []
        binding.close()
        count = len(requests)
        reopened, _, _, repository, service = _compose(config, clock,
            peer_factory=_numeric_loopback_peer_factory(ports, requests))
        assert len(requests) == count
        assert repository.recover_expired_leases(now=now[0]) == ()
        with pytest.raises(MobilityJournalError, match='^BUSY$'):
            service.resume(operation_id)
        assert len(requests) == count
        now[0] = interrupted.lease_expires_at + .01
        assert repository.recover_expired_leases(now=now[0]) == (operation_id,)
        assert repository.public_status(operation_id, 'source-owner-a')['state'] == 'resumable'
        assert len(requests) == count  # Reopen/recovery/status did not contact either peer.
        resumed = service.resume(operation_id)
        target.query('finish-verification')
        if resumed.state == 'awaiting_seal':
            resumed = service.resume(operation_id)  # Explicit second operator invocation.
        assert resumed.state == 'sealed'
        after = target.query()
        assert after == [{'state': 'sealed', 'offset': len(data), 'command': before[0]['command'],
            'size': len(data), 'sha256': spec['sha256']}]
        puts = [item for item in requests if item[1] == 'PUT']
        assert [int(item[2].split('/')[-1]) for item in puts] == [0, 1024 * 1024, 2 * 1024 * 1024]
        assert sum(item[3] for item in puts) == len(data)
        # The interruption precedes a local receipt checkpoint. Resume replays
        # begin with the canonical command; the receiver retains one record.
        begins = [item for item in requests if item[1:3] == ('POST', '/v1/artifact-transfers')]
        assert len(begins) == 2 and all(item[0] == target.port for item in begins)
        assert resumed.public_status()['source_artifact_id'] == artifact['source_artifact_id']
        # Same public label/key cannot identify the second, distinct recipient.
        other_config = replace(config, artifact_mobility=replace(config.artifact_mobility,
            destination_origin=f'https://127.0.0.1:{other.port}'))
        wrong, _, _, _, wrong_service = _compose(other_config, clock,
            peer_factory=_numeric_loopback_peer_factory(ports, requests))
        wrong_start = len(requests)
        blocked = wrong_service.send(artifact['source_artifact_id'], confirm_destination='node-b')
        assert blocked.state == 'terminal_blocked'
        assert other.query() == []
        assert requests[wrong_start:] == [(other.port, 'GET', '/v1/artifact-transfers/recipient-attestation', 0)]
        assert all(item[0] == target.port for item in requests if item[1] in ('POST', 'PUT'))
    finally:
        for item in (wrong, reopened, binding):
            if item is not None:
                item.close()
        if other is not None:
            other.close()
        target.close()
