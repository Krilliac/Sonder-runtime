"""Offline authority, pinned transport, and external membership boundaries."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import base64
import io
import json
import socket

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sonder_runtime.adapters.inference.external_membership import ExternalMembershipSource, MembershipSourceError
from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.application.ports.inference_membership import MembershipSourceLimits
from sonder_runtime.platform.config import MembershipConfig, MembershipEndpointPolicy, Secrets

NOW = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
ORIGIN = "https://worker.example:11434"


@pytest.mark.parametrize("field", ["trust_anchor_file", "signature_public_key_file", "membership_client_cert_file", "membership_client_key_file"])
def test_authority_paths_must_be_absolute_local(tmp_path, field):
    from sonder_runtime.platform.config import validate_membership_config
    config = configuration(tmp_path)
    secrets = Secrets(membership_client_cert_file=str(tmp_path / "client.pem"),
                      membership_client_key_file=str(tmp_path / "key.pem"))
    for bad in ("relative.pem", "../relative.pem", r"\\server\share\authority.pem", "file:///private/key.pem"):
        candidate, credentials = config, secrets
        if field.startswith("membership_"): credentials = replace(secrets, **{field: bad})
        else: candidate = replace(config, **{field: bad})
        with pytest.raises(ValueError): validate_membership_config(candidate, credentials)


@pytest.mark.parametrize("field", ["trust_anchor_file", "signature_public_key_file", "membership_client_cert_file", "membership_client_key_file"])
@pytest.mark.parametrize("mutation", ["missing", "directory", "hardlink", "symlink", "nonprivate"])
def test_authority_files_must_be_private_regular_at_construction(tmp_path, field, mutation):
    from pathlib import Path
    import os
    import subprocess
    config = configuration(tmp_path)
    secrets = credentials(tmp_path)
    path = Path(getattr(secrets if field.startswith("membership_") else config, field))
    if mutation in ("missing", "directory", "symlink"):
        path.unlink()
    if mutation == "directory": path.mkdir()
    elif mutation == "hardlink": os.link(path, path.with_suffix(".link"))
    elif mutation == "symlink":
        try: path.symlink_to(path.parent / "other.pem")
        except OSError: pytest.skip("host cannot create a symlink")
    elif mutation == "nonprivate":
        if os.name == "nt":
            subprocess.run(["icacls", str(path), "/grant", "*S-1-1-0:(R)"], check=True, capture_output=True)
        else: path.chmod(0o644)
    with pytest.raises(MembershipSourceError, match="^external membership unavailable$") as caught:
        ExternalMembershipSource(config, secrets, clock=lambda: NOW)
    assert str(path) not in repr(caught.value)


@pytest.fixture(autouse=True)
def private_authority_material(tmp_path):
    from sonder_runtime.application.compute_fabric.artifact_spool import PrivateDirectoryAnchor
    import os
    with PrivateDirectoryAnchor(tmp_path / "authority", create=True, require_new=True) as anchor:
        for name in ("ca.pem", "client.pem", "key.pem", "signer.pem"):
            raw = (Ed25519PrivateKey.generate().public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
                if name == "signer.pem" else b"test-only material")
            fd, temporary = anchor.create_temporary()
            with os.fdopen(fd, "wb") as stream: stream.write(raw)
            anchor.publish(temporary, name)
            if os.name == "posix":
                os.chmod(anchor.path / name, 0o600)


def credentials(tmp_path):
    return Secrets(membership_client_cert_file=str(tmp_path / "authority" / "client.pem"),
                   membership_client_key_file=str(tmp_path / "authority" / "key.pem"))


def configuration(tmp_path):
    return MembershipConfig(mode="external", cluster_id="cluster", issuer_id="issuer", protocol_version=1,
        source_origin="https://registry.example:443", source_tls_server_name="registry.example",
        source_allowed_cidrs=("10.77.0.0/24",), trust_anchor_file=str(tmp_path / "authority" / "ca.pem"),
        signature_public_key_file=str(tmp_path / "authority" / "signer.pem"), refresh_interval_seconds=30,
        snapshot_max_advertisements=16, snapshot_max_bytes=1048576, local_fallback=False,
        member_policies=(MembershipEndpointPolicy("worker", ORIGIN, "worker.example", ("10.77.0.0/24",)),))


def envelope(key, **changes):
    payload = dict(cluster_id="cluster", issuer_id="issuer", generation=1, protocol_version=1,
        issued_at=NOW.isoformat(), expires_at=(NOW + timedelta(seconds=60)).isoformat(),
        workers=[dict(worker_id="worker", origin=ORIGIN, member_generation=1,
                      lifecycle_state="active", models=[], advertised_capacity=1)])
    payload.update(changes)
    canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")
    signature = base64.b64encode(key.sign(canonical(payload))).decode("ascii")
    return canonical(dict(payload=payload, signature=signature))


@pytest.fixture
def source(tmp_path, monkeypatch):
    key = Ed25519PrivateKey.generate()
    config = configuration(tmp_path)
    (tmp_path / "authority" / "signer.pem").write_bytes(key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    secrets = credentials(tmp_path)
    value = ExternalMembershipSource(config, secrets, clock=lambda: NOW)
    calls = []
    def retrieve(policy, **kwargs):
        calls.append((policy, kwargs))
        return envelope(key)
    monkeypatch.setattr(value._transport, "retrieve", retrieve)
    yield value, key, calls
    assert value.close(timeout=2)


def test_external_source_reads_only_configured_authority(source):
    value, key, calls = source
    snapshot = value.read_snapshot(limits=MembershipSourceLimits())
    assert snapshot.cluster_id == "cluster" and snapshot.workers[0].origin == ORIGIN
    assert calls[0][0].origin == "https://registry.example:443"
    assert calls[0][1]["path"] == "/v1/membership"
    with pytest.raises(TypeError):
        value.read_snapshot(limits=MembershipSourceLimits(), origin=ORIGIN)


def test_signing_authority_is_captured_for_source_lifetime(source, monkeypatch):
    from pathlib import Path
    value, key, calls = source
    replacement = Ed25519PrivateKey.generate()
    Path(value._config.signature_public_key_file).write_bytes(replacement.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    assert value.read_snapshot(limits=MembershipSourceLimits()).cluster_id == "cluster"
    monkeypatch.setattr(value._transport, "retrieve", lambda *_a, **_kw: envelope(replacement))
    with pytest.raises(MembershipSourceError): value.read_snapshot(limits=MembershipSourceLimits())


def test_tls_authority_is_captured_and_temporary_credentials_are_removed(source, monkeypatch):
    from pathlib import Path
    from sonder_runtime.adapters.inference import external_membership as module
    value, _, _ = source
    inputs = (value._config.trust_anchor_file, value._secrets.membership_client_cert_file,
              value._secrets.membership_client_key_file)
    expected = tuple(Path(path).read_bytes() for path in inputs)
    for path in inputs: Path(path).write_bytes(b"replacement must never be adopted")
    loaded, temporary, contexts = [], [], []
    class Context:
        def load_verify_locations(self, *, cadata): loaded.append(cadata.encode("ascii"))
        def load_cert_chain(self, *, certfile, keyfile):
            temporary.extend((Path(certfile), Path(keyfile)))
            loaded.extend(path.read_bytes() for path in temporary)
    def create(*_):
        contexts.append(Context())
        return contexts[-1]
    monkeypatch.setattr(module.ssl, "SSLContext", create)
    assert value._transport._tls_context() is value._transport._tls_context()
    assert len(contexts) == 1 and tuple(loaded) == expected
    assert all(not path.exists() and not path.parent.exists() for path in temporary)


def test_external_default_fence_survives_source_close_and_static_composition(source, monkeypatch):
    from sonder_runtime.bootstrap import app as bootstrap
    from sonder_runtime.platform.config import SonderConfig
    from sonder_runtime.adapters import embeddings
    from sonder_runtime.application.context import local_owner_context
    from sonder_runtime.application.ports.specialized_lifecycle import EmbeddingRequest
    value, _, _ = source
    custom_calls = []
    def custom(request, context):
        custom_calls.extend(request.texts)
        return [[1.0] for _ in request.texts]
    application = bootstrap.build_application(config=SonderConfig(), embedding_provider=custom)
    monkeypatch.setattr(embeddings.ollama_endpoint._OPENER, "open", lambda *_a, **_kw: pytest.fail("generic embedding escape"))
    try:
        assert value.close(timeout=1)
        assert embeddings.embed("private text") is None
        provider = application.specialized_providers.registrations[0].provider
        assert len(provider.embed(EmbeddingRequest(("custom text",), "custom"),
                   local_owner_context(correlation_id="custom-embedding")).embeddings) == 1
        assert custom_calls == ["custom text"]
        assert embeddings.serving_model_revision() == ""
    finally:
        application.close_providers(timeout=1)


def test_external_membership_status_reports_pinned_admission_without_origins(
    source, monkeypatch,
):
    """The cached status must not describe an external source as static."""
    from sonder_runtime.adapters.inference.ollama_pool import OllamaWorkerPool
    from sonder_runtime.application.inference_membership.controller import (
        MembershipController,
    )

    value, _, _ = source
    now = [NOW]
    pool = OllamaWorkerPool("http://127.0.0.1:11434", max_workers=4)
    pool.configure_external_source(value)
    controller = MembershipController(
        value,
        pool,
        clock=lambda: now[0],
        cluster_id="cluster",
        issuer_id="issuer",
        refresh_interval_seconds=30,
        source_limits=MembershipSourceLimits(
            max_advertisements=16,
            max_bytes=1_048_576,
        ),
    )
    pool._capability_prober = lambda _origin: {"models": ["code"]}
    try:
        unrefreshed = pool.summary()
        assert unrefreshed["membership_mode"] == "external"
        assert unrefreshed["membership_state"] == "unrefreshed"
        assert unrefreshed["refresh_state"] == "not_refreshed"
        before = pool.status(page_size=1)
        assert before["tls_verification"] == "pinned-ca-exact-san"
        assert before["remote_tls_required"] is True

        controller.refresh(timeout_seconds=2)
        current = pool.summary()
        assert current["membership_mode"] == "external"
        assert current["membership_state"] == "current"
        assert current["refresh_state"] == "current"
        assert ORIGIN not in json.dumps(current)

        def unavailable(*_args, **_kwargs):
            raise TimeoutError

        monkeypatch.setattr(value._transport, "retrieve", unavailable)
        now[0] = NOW + timedelta(seconds=61)
        controller.refresh(timeout_seconds=2)
        stale = pool.summary()
        assert stale["membership_mode"] == "external"
        assert stale["membership_state"] == "stale_or_partial"
        assert stale["refresh_state"] == "stale_or_partial"
    finally:
        assert controller.close(timeout=2)


@pytest.mark.parametrize("module_name", ["embeddings", "ollama_endpoint"])
def test_external_embedding_fence_survives_real_staged_live_reload(source, monkeypatch, module_name):
    import sys
    import types
    from sonder_runtime.adapters import embeddings
    from sonder_runtime.adapters.web import live_reload
    original = embeddings if module_name == "embeddings" else embeddings.ollama_endpoint
    original_policy = embeddings.ollama_endpoint
    # Restore all aliases the real staged reloader updates when this test ends.
    for module in tuple(sys.modules.values()):
        if isinstance(module, types.ModuleType):
            for name, value in tuple(vars(module).items()):
                if value is original: monkeypatch.setattr(module, name, original)
    for name, module in tuple(sys.modules.items()):
        if module is original: monkeypatch.setitem(sys.modules, name, original)
    candidate = live_reload._stage_module_reload(original)
    adapter = candidate if module_name == "embeddings" else embeddings
    policy = adapter.ollama_endpoint
    monkeypatch.setattr(policy._OPENER, "open", lambda *_a, **_kw: pytest.fail("reload opened generic embedding transport"))
    assert candidate is not original
    assert policy._external_membership_owners is original_policy._external_membership_owners
    assert policy._embedding_policy_lock is original_policy._embedding_policy_lock
    assert policy._embedding_policy_condition is original_policy._embedding_policy_condition
    assert policy._embedding_operations is original_policy._embedding_operations
    assert policy._embedding_operation_local is original_policy._embedding_operation_local
    assert adapter.embed("sensitive text after reload") is None


@pytest.mark.parametrize("compose_during_embed", [False, True])
def test_default_embedding_rejects_reentrant_registration_without_partial_ownership(tmp_path, monkeypatch, compose_during_embed):
    from sonder_runtime.adapters import embeddings
    monkeypatch.setenv("SONDER_EMBED_REVISION", "static-test-revision")
    monkeypatch.setenv("SONDER_EMBED_DIM", "1")
    monkeypatch.setattr(embeddings, "_npu_prefer_active", lambda: False)
    monkeypatch.setattr(embeddings, "_npu_shadow_embed", lambda *_a: None)
    owners, calls, serialized = [], [], []
    original_dumps = json.dumps
    def dumps(value, *args, **kwargs):
        if isinstance(value, dict) and set(value) == {"model", "prompt"}:
            serialized.append(embeddings.ollama_endpoint._default_embeddings_disabled())
        return original_dumps(value, *args, **kwargs)
    monkeypatch.setattr(json, "dumps", dumps)
    def accelerate(*_a):
        if compose_during_embed:
            with pytest.raises(MembershipSourceError, match="^external membership unavailable$"):
                ExternalMembershipSource(configuration(tmp_path), credentials(tmp_path), clock=lambda: NOW)
            assert not embeddings.ollama_endpoint._default_embeddings_disabled()
        return None
    monkeypatch.setattr(embeddings, "_accelerated_embed", accelerate)
    def opened(request, **_kw):
        calls.append(request.data)
        return io.BytesIO(b'{"embedding":[1.0]}')
    monkeypatch.setattr(embeddings.ollama_endpoint._OPENER, "open", opened)
    try:
        result = embeddings.embed("dispatch-private-text", base="http://127.0.0.1:11434", model="test-embed")
        assert result == [1.0]  # refused registration never became an external owner
        assert not any(serialized)
        assert len(calls) == 1
        if calls: assert json.loads(calls[0])["prompt"] == "dispatch-private-text"
    finally:
        for owner in owners: owner.close(timeout=1)


@pytest.mark.parametrize("operation", ["revision", "current", "refresh", "embed"])
def test_default_policy_registration_cannot_overtake_active_operation(tmp_path, monkeypatch, operation):
    import threading
    from sonder_runtime.adapters import embeddings
    endpoint = embeddings.ollama_endpoint
    entered, release, registering, registered = (threading.Event() for _ in range(4))
    owners, calls, results, failures, serialized = [], [], [], [], []
    original_dumps = json.dumps
    def dumps(value, *args, **kwargs):
        if isinstance(value, dict) and set(value) == {"model", "prompt"}:
            serialized.append(endpoint._default_embeddings_disabled())
        return original_dumps(value, *args, **kwargs)
    monkeypatch.setattr(json, "dumps", dumps)
    monkeypatch.setenv("SONDER_ALLOW_REMOTE_OLLAMA", "1")
    monkeypatch.delenv("SONDER_EMBED_REVISION", raising=False)
    monkeypatch.setattr(embeddings, "BASE", ORIGIN)
    monkeypatch.setattr(embeddings, "_accelerated_embed", lambda *_a: None)
    monkeypatch.setattr(embeddings, "_npu_prefer_active", lambda: False)
    monkeypatch.setattr(embeddings, "_npu_shadow_embed", lambda *_a: None)
    original_origin = endpoint.configured_origin
    def origin(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            assert release.wait(3)
        return original_origin(*args, **kwargs)
    monkeypatch.setattr(endpoint, "configured_origin", origin)
    def opened(request, **kwargs):
        assert not endpoint._embedding_policy_lock._is_owned()
        calls.append((getattr(request, "full_url", request), endpoint._default_embeddings_disabled()))
        return io.BytesIO(b'{"models":[],"embedding":[1.0]}')
    monkeypatch.setattr(endpoint._OPENER, "open", opened)
    def run():
        try:
            call = {"revision": lambda: embeddings.serving_model_revision(),
                    "current": lambda: embeddings.current_revision(),
                    "refresh": lambda: embeddings.refresh_runtime_revision(),
                    "embed": lambda: embeddings.embed("private race text")}[operation]
            results.append(call())
        except BaseException as error: failures.append(error)
    def register():
        try:
            registering.set()
            owners.append(ExternalMembershipSource(configuration(tmp_path), credentials(tmp_path), clock=lambda: NOW))
            registered.set()
        except BaseException as error: failures.append(error)
    worker, registration = threading.Thread(target=run), threading.Thread(target=register)
    worker.start()
    try:
        assert entered.wait(3)
        registration.start()
        assert registering.wait(3)
        with endpoint._embedding_policy_condition:
            assert endpoint._embedding_policy_condition.wait_for(
                lambda: endpoint._embedding_operations["pending"] == 1, timeout=2)
        assert not registered.is_set(), "ownership overtook an active default operation"
        # A waiting source fences new work while letting existing nested
        # provenance work finish. No policy lock is held during transport.
        before, before_serialized = list(calls), list(serialized)
        assert embeddings.embed("pending private text") is None
        assert embeddings.serving_model_revision() == ""
        assert embeddings.current_revision() == ""
        assert embeddings.refresh_runtime_revision() == ""
        assert calls == before and serialized == before_serialized
    finally:
        release.set()
        worker.join(3)
        if registration.ident is not None: registration.join(3)
        for owner in owners: owner.close(timeout=1)
    assert not worker.is_alive() and not registration.is_alive()
    assert failures == [] and results and registered.is_set()
    assert calls and not any(active for _, active in calls)
    assert not any(serialized)


def test_registration_timeout_never_publishes_or_leaves_pending_restriction(tmp_path):
    import threading
    from sonder_runtime.adapters.inference import ollama_endpoint as endpoint
    entered, release = threading.Event(), threading.Event()
    def reader():
        with endpoint._default_embedding_operation() as allowed:
            assert allowed
            entered.set()
            release.wait(10)
    thread = threading.Thread(target=reader)
    thread.start()
    try:
        assert entered.wait(2)
        with pytest.raises(MembershipSourceError, match="^external membership unavailable$"):
            ExternalMembershipSource(configuration(tmp_path), credentials(tmp_path), clock=lambda: NOW)
        assert not endpoint._default_embeddings_disabled()
        assert endpoint._embedding_operations == {"active": 1, "pending": 0}
        with endpoint._default_embedding_operation() as allowed:
            assert allowed  # a failed registration does not disable static-only work
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive() and endpoint._embedding_operations == {"active": 0, "pending": 0}


def test_static_default_operations_remain_concurrent_without_holding_policy_lock(monkeypatch):
    import threading
    from sonder_runtime.adapters import embeddings
    endpoint = embeddings.ollama_endpoint
    entered = threading.Barrier(3)
    release = threading.Event()
    errors, results = [], []
    def opened(*_a, **_kw):
        assert not endpoint._embedding_policy_lock._is_owned()
        entered.wait(3)
        assert release.wait(3)
        return io.BytesIO(b'{"models":[]}')
    monkeypatch.setattr(endpoint._OPENER, "open", opened)
    def run():
        try: results.append(embeddings.serving_model_revision(base="http://127.0.0.1:11434"))
        except BaseException as error: errors.append(error)
    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads: thread.start()
    try:
        entered.wait(3)
        assert endpoint._embedding_operations["active"] == 2
    finally:
        release.set()
        for thread in threads: thread.join(3)
    assert errors == [] and results == ["", ""]
    assert all(not thread.is_alive() for thread in threads)


@pytest.mark.parametrize("failure", ["signature", "cluster", "issuer", "protocol", "expired", "future",
    "origin", "identity", "trust_root", "san_policy", "cidr_policy", "credentials", "oversized", "over_items", "noncanonical"])
def test_external_authority_cannot_change_configured_trust(source, monkeypatch, failure):
    value, key, calls = source
    changes = {}
    if failure in ("cluster", "issuer"):
        changes[failure + "_id"] = "other-secret-identity"
    elif failure == "protocol":
        changes["protocol_version"] = 2
    elif failure == "expired":
        changes["expires_at"] = NOW.isoformat()
    elif failure == "future":
        changes["issued_at"] = (NOW + timedelta(seconds=1)).isoformat()
    elif failure in ("origin", "identity", "trust_root", "san_policy", "cidr_policy", "credentials", "over_items"):
        workers = json.loads(envelope(key))["payload"]["workers"]
        if failure == "origin": workers[0]["origin"] = "https://other.example:11434"
        elif failure == "identity": workers[0]["worker_id"] = "unknown"
        elif failure == "over_items": workers *= 17
        else: workers[0][failure] = "secret-override"
        changes["workers"] = workers
    raw = envelope(Ed25519PrivateKey.generate() if failure == "signature" else key, **changes)
    if failure == "oversized": raw = b"x" * 1048577
    if failure == "noncanonical": raw += b"\n"
    monkeypatch.setattr(value._transport, "retrieve", lambda *_a, **_kw: raw)
    with pytest.raises(MembershipSourceError) as caught:
        value.read_snapshot(limits=MembershipSourceLimits())
    assert str(caught.value) == "external membership unavailable"


@pytest.mark.parametrize("failure", ["mixed_dns", "out_of_cidr", "empty_dns", "wrong_san", "wildcard_san", "redirect",
                                      "client_cert", "server_cert", "oversized", "partial_status", "partial_body", "server_error",
                                      "version_missing", "post_version_missing", "inference_missing", "tags_missing",
                                      "version_body", "version_unknown_length", "version_chunked", "version_truncated",
                                      "version_oversized", "version_hidden_body", "version_compressed", "version_duplicate_length"])
def test_pinned_transport_rejects_boundary_failures(tmp_path, monkeypatch, failure):
    from sonder_runtime.adapters.inference import external_membership as module
    config = configuration(tmp_path)
    source = ExternalMembershipSource(config, credentials(tmp_path), clock=lambda: NOW)
    resolved, connected, identities = [], [], []
    answers = ["10.77.0.2"]
    if failure == "mixed_dns": answers += ["203.0.113.9"]
    if failure == "out_of_cidr": answers = ["203.0.113.9"]
    if failure == "empty_dns": answers = []
    def resolve(host, port, **kwargs):
        resolved.append((host, port))
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port)) for ip in answers]
    class Channel:
        def settimeout(self, value): pass
        def connect(self, address): connected.append(address)
        def sendall(self, data): self.request = data
        def close(self): pass
        def getpeercert(self):
            name = "other.example" if failure == "wrong_san" else "*.example" if failure == "wildcard_san" else "registry.example"
            if failure == "version_missing": name = "worker.example"
            return {"subjectAltName": (("DNS", name),)}
        def makefile(self, *_args, **_kwargs):
            if failure == "partial_status": return io.BytesIO(b"HTTP/1")
            if failure == "partial_body": return io.BytesIO(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n{}")
            status = b"302 Found" if failure == "redirect" else b"503 Unavailable" if failure == "server_error" else b"200 OK"
            body = b"x" * (1048577 if failure == "oversized" else 1)
            if failure.endswith("missing"):
                status = b"200 OK" if failure == "version_missing" and b"GET /api/tags " in self.request else b"404 Not Found"
                body = b'{"models":[{"name":"code"}]}'
                if failure == "version_missing" and status.startswith(b"404"): body = b""
            if failure in ("version_body", "version_unknown_length", "version_chunked", "version_truncated",
                           "version_oversized", "version_hidden_body", "version_compressed", "version_duplicate_length"):
                headers, body = {
                    "version_body": (b"Content-Length: 7\r\n", b"private"),
                    "version_unknown_length": (b"", b""),
                    "version_chunked": (b"Transfer-Encoding: chunked\r\nContent-Length: 0\r\n", b"0\r\n\r\n"),
                    "version_truncated": (b"Content-Length: 7\r\n", b""),
                    "version_oversized": (b"Content-Length: 1048577\r\n", b"private"),
                    "version_hidden_body": (b"Content-Length: 0\r\n", b"private"),
                    "version_compressed": (b"Content-Length: 0\r\nContent-Encoding: gzip\r\n", b""),
                    "version_duplicate_length": (b"Content-Length: 0\r\nContent-Length: 7\r\n", b"private"),
                }[failure]
                return io.BytesIO(b"HTTP/1.1 404 Not Found\r\n" + headers + b"\r\n" + body)
            return io.BytesIO(b"HTTP/1.1 " + status + b"\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
    class Context:
        check_hostname = True
        verify_mode = None
        def load_verify_locations(self, **kwargs): pass
        def load_cert_chain(self, **kwargs):
            if failure == "client_cert": raise OSError("secret client certificate")
        def wrap_socket(self, channel, *, server_hostname):
            identities.append(server_hostname)
            if failure == "server_cert": raise OSError("secret certificate chain")
            return channel
    monkeypatch.setattr(module.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(module.socket, "socket", lambda *_a, **_kw: Channel())
    monkeypatch.setattr(module.ssl, "SSLContext", lambda *_a: Context())
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    try:
        if failure == "version_missing":
            from sonder_runtime.adapters.inference.ollama_pool import _default_capability_prober
            def opened(request, **kwargs):
                return source.open_worker_url(request, timeout=kwargs["timeout"])
            prober = _default_capability_prober(timeout=1, allow_remote=True, open_url=opened)
            assert prober(ORIGIN)["models"] == ("code",)
            assert len(connected) == 2
            return
        with pytest.raises((MembershipSourceError, ModelCallError)) as caught:
            path = {"post_version_missing":"/api/version", "inference_missing":"/api/generate", "tags_missing":"/api/tags"}.get(failure, "/v1/membership")
            if failure.startswith("version_"): path = "/api/version"
            source._transport.retrieve(source._source_policy, path=path, timeout=1, max_bytes=1048576,
                method="POST" if failure == "post_version_missing" else "GET")
        for private in ("registry.example", "secret client certificate", "secret certificate chain", str(tmp_path), "private.pem"):
            assert private not in str(caught.value) and private not in repr(caught.value)
        if failure in ("redirect", "oversized", "partial_status", "partial_body", "server_error", "post_version_missing", "inference_missing", "tags_missing") or failure.startswith("version_"):
            from sonder_runtime.adapters.inference.ollama_pool import OllamaWorkerPool
            assert type(caught.value) is ModelCallError
            assert not OllamaWorkerPool._retryable(caught.value)
        assert len(resolved) <= 1
        assert all(address[0] == "10.77.0.2" for address in connected)
        assert all(name == "registry.example" for name in identities)
        if failure in ("mixed_dns", "out_of_cidr", "empty_dns"):
            assert connected == []
    finally:
        assert source.close(timeout=2)


@pytest.mark.parametrize("failure", ["commit", "contention", "replacement"])
def test_external_composition_persists_before_apply_and_pins_worker_policy(tmp_path, monkeypatch, failure):
    from sonder_runtime.bootstrap import app as bootstrap, legacy_root
    from sonder_runtime.adapters.inference import ollama_pool
    from sonder_runtime.platform.config import SonderConfig, OllamaConfig, StateConfig
    key = Ed25519PrivateKey.generate()
    membership = configuration(tmp_path)
    (tmp_path / "authority" / "signer.pem").write_bytes(key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    config = SonderConfig(state=StateConfig(home=str(tmp_path)), membership=membership,
        ollama=OllamaConfig(allow_remote=True),
        secrets=credentials(tmp_path))
    application = bootstrap.build_application(config=config)
    control, pool = application.inference_membership, application.inference_pool
    assert type(control._source) is ExternalMembershipSource
    assert control._thread is None
    clock = lambda: NOW
    control._clock = control._source._clock = control._high_water_store._clock = pool._membership_clock = clock
    generation, calls = [1], []
    def retrieve(policy, **kwargs):
        assert not pool._condition._is_owned()
        assert not control._condition._is_owned()
        calls.append((policy, kwargs["path"]))
        if kwargs["path"] == "/v1/membership":
            return envelope(key, generation=generation[0])
        assert policy is membership.member_policies[0]
        return b'{"models":[{"name":"code"}],"version":"1","ok":true}'
    monkeypatch.setattr(control._source._transport, "retrieve", retrieve)
    original_apply = pool.apply_membership
    def apply(result):
        assert control._high_water_store.read() == result.high_water
        original_apply(result)
    monkeypatch.setattr(pool, "apply_membership", apply)
    try:
        assert legacy_root.require_inference_application(application) is pool
        with pytest.raises(ollama_pool.WorkerPoolError):
            pool.request(lambda _: pytest.fail("remote admitted without evidence"), model="code")
        assert calls == []
        control.refresh(timeout_seconds=2, probe=False)
        with pytest.raises(ollama_pool.WorkerPoolError):
            pool.request(lambda _: pytest.fail("probation admitted"), model="code")
        control.refresh(timeout_seconds=2)
        import urllib.request
        def send(origin):
            with pool.open_url(urllib.request.Request(origin + "/api/generate", data=b"{}"), timeout=1) as response:
                return json.loads(response.read())
        assert pool.request(send, model="code")["ok"] is True
        assert calls[-1] == (membership.member_policies[0], "/api/generate")
        import server
        monkeypatch.setattr(server, "OLLAMA_POOL", pool)
        before = len(calls)
        public = json.dumps(pool.summary()) + server.status()
        assert len(calls) == before
        for private in ("worker.example", "registry.example", str(tmp_path), "client.pem", "key.pem"):
            assert private not in public
        old_result = control._result
        generation[0] = 2
        from contextlib import nullcontext
        water_store = control._high_water_store
        def fail(publish):
            assert not pool._condition._is_owned()
            assert not control._condition._is_owned()
            if failure == "replacement":
                water_store._path.write_bytes(b"partial private record")
                publish()
            raise OSError("private fsync failure")
        if failure != "contention": monkeypatch.setattr(water_store, "_commit", fail)
        with water_store._session() if failure == "contention" else nullcontext():
            with pytest.raises(RuntimeError): control.refresh(timeout_seconds=2)
        assert control._result is old_result
        assert pool.request(send, model="code")["ok"] is True
    finally:
        application.close_providers(timeout=2)
        ollama_pool.reset_typed_workers()


@pytest.mark.parametrize("mode", ["dns", "ip", "wrong_dns_san", "wrong_ip_san", "foreign_client", "missing_client", "foreign_server"])
def test_real_local_tls_requires_private_chain_client_certificate_and_exact_san(tmp_path, monkeypatch, mode):
    import ipaddress
    import ssl
    import threading
    from cryptography import x509
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID

    tmp_path = tmp_path / "authority"
    now = datetime.now(timezone.utc)
    ca_key = Ed25519PrivateKey.generate()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "private-test-ca")])
    def certificate(key, name, *, ca=False, signer=ca_key, issuer=ca_name, san=None, usage=None):
        builder = (x509.CertificateBuilder().subject_name(name).issuer_name(issuer).public_key(key.public_key())
                   .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1))
                   .not_valid_after(now+timedelta(hours=1)).add_extension(x509.BasicConstraints(ca=ca, path_length=None), True))
        if san is not None: builder = builder.add_extension(x509.SubjectAlternativeName([san]), False)
        if usage is not None: builder = builder.add_extension(x509.ExtendedKeyUsage([usage]), False)
        return builder.sign(signer, algorithm=None)
    ca = certificate(ca_key, ca_name, ca=True)
    (tmp_path / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    foreign = Ed25519PrivateKey.generate()
    for identity in ("server", "client"):
        key = Ed25519PrivateKey.generate()
        signer = foreign if mode == "foreign_" + identity else ca_key
        san = (x509.IPAddress(ipaddress.ip_address("127.0.0.1")) if mode == "ip" else
               x509.IPAddress(ipaddress.ip_address("127.0.0.2")) if mode == "wrong_ip_san" else
               x509.DNSName("wrong.example" if mode == "wrong_dns_san" else "registry.example"))
        cert = certificate(key, x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "registry.example")]),
            signer=signer, san=san if identity == "server" else None,
            usage=ExtendedKeyUsageOID.SERVER_AUTH if identity == "server" else ExtendedKeyUsageOID.CLIENT_AUTH)
        (tmp_path / (identity + ".pem")).write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (tmp_path / (identity + ".key")).write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    import os
    for path in tmp_path.iterdir():
        if path.is_file(): os.chmod(path, 0o600)
    signing = Ed25519PrivateKey.generate()
    (tmp_path / "signer.pem").write_bytes(signing.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(tmp_path / "server.pem"), str(tmp_path / "server.key"))
    context.load_verify_locations(cafile=str(tmp_path / "ca.pem"))
    context.verify_mode = ssl.CERT_REQUIRED
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(1)
    port = listener.getsockname()[1]
    requests = []
    def serve():
        try:
            raw, _ = listener.accept()
            raw.settimeout(1)
            with raw:
                with context.wrap_socket(raw, server_side=True) as stream:
                    requests.append(stream.recv(4096))
                    body = envelope(signing)
                    stream.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
        except (ssl.SSLError, OSError):
            pass  # expected rejected client/closed peer; assertions are below
        finally:
            listener.close()
    thread = threading.Thread(target=serve)
    thread.start()
    name = "127.0.0.1" if mode in ("ip", "wrong_ip_san") else "registry.example"
    config = replace(configuration(tmp_path.parent), source_origin=f"https://{name}:{port}",
                     source_tls_server_name=name, source_allowed_cidrs=("127.0.0.1/32",))
    credentials = Secrets(membership_client_cert_file=str(tmp_path / ("missing.pem" if mode == "missing_client" else "client.pem")),
                          membership_client_key_file=str(tmp_path / "client.key"))
    if mode == "missing_client":
        with pytest.raises(MembershipSourceError):
            ExternalMembershipSource(config, credentials, clock=lambda: NOW)
        listener.close()
        thread.join(2)
        return
    source = ExternalMembershipSource(config, credentials, clock=lambda: NOW)
    resolves = []
    def resolve(host, resolved_port, **kwargs):
        resolves.append((host, resolved_port))
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", resolved_port))]
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setenv("HTTPS_PROXY", "http://untrusted-proxy.invalid:443")
    try:
        if mode in ("dns", "ip"):
            assert source.read_snapshot(limits=MembershipSourceLimits(timeout_seconds=1)).workers[0].origin == ORIGIN
        else:
            with pytest.raises(MembershipSourceError, match="^external membership unavailable$"):
                source.read_snapshot(limits=MembershipSourceLimits(timeout_seconds=1))
        assert resolves == [(name, port)]
    finally:
        assert source.close(timeout=2)
        thread.join(2)
    assert not thread.is_alive()
    if mode in ("dns", "ip"):
        assert len(requests) == 1 and b"GET /v1/membership HTTP/1.1" in requests[0]


@pytest.mark.parametrize("kind", ["config", "policy", "origin", "cidr", "secrets", "tuple"])
def test_external_configuration_rejects_hostile_subclasses_before_io(tmp_path, kind):
    config = configuration(tmp_path)
    secrets = credentials(tmp_path)
    def hostile(value):
        def equality(*_): pytest.fail("hostile equality called")
        cls = type("Hostile", (type(value),), {"__eq__": equality, "__hash__": type(value).__hash__})
        if isinstance(value, (str, tuple)): return cls(value)
        copy = object.__new__(cls)
        copy.__dict__.update(value.__dict__)
        return copy
    if kind == "config": config = hostile(config)
    elif kind == "secrets": secrets = hostile(secrets)
    elif kind == "tuple": config = replace(config, member_policies=hostile(config.member_policies))
    else:
        policy = config.member_policies[0]
        policy = (hostile(policy) if kind == "policy" else replace(policy, origin=hostile(policy.origin))
                  if kind == "origin" else replace(policy, allowed_cidrs=(hostile("10.77.0.0/24"),)))
        config = replace(config, member_policies=(policy,))
    with pytest.raises(ValueError): ExternalMembershipSource(config, secrets, clock=lambda: NOW)


@pytest.mark.parametrize("fallback", [False, True])
def test_external_local_fallback_cannot_authorize_a_remote_or_cross_lane_origin(tmp_path, monkeypatch, fallback):
    import urllib.request
    from sonder_runtime.bootstrap import app as bootstrap
    from sonder_runtime.adapters.inference import ollama_pool, ollama_endpoint
    from sonder_runtime.platform.config import SonderConfig, OllamaConfig, StateConfig
    config = SonderConfig(state=StateConfig(home=str(tmp_path)), membership=replace(configuration(tmp_path), local_fallback=fallback),
        ollama=OllamaConfig(allow_remote=True), secrets=credentials(tmp_path))
    application = bootstrap.build_application(config=config)
    pool, control = application.inference_pool, application.inference_membership
    calls = []
    monkeypatch.setattr(ollama_endpoint, "open_url", lambda request, **_k: calls.append(request.full_url) or io.BytesIO(b"{}"))
    try:
        request = urllib.request.Request("http://127.0.0.1:11434/api/generate", data=b"{}")
        if fallback:
            with pool.open_url(request, timeout=1) as response: assert response.read() == b"{}"
        else:
            with pytest.raises(ollama_pool.WorkerPoolUnavailable): pool.open_url(request, timeout=1)
        with pytest.raises(ollama_pool.WorkerPoolError):
            pool.request(lambda _: pytest.fail("unadmitted remote dispatched"), model="remote-only")
        with pytest.raises(ollama_pool.WorkerPoolUnavailable):
            pool.open_url(urllib.request.Request("http://127.0.0.2:11434/api/generate"), timeout=1)
        assert calls == ([request.full_url, "http://127.0.0.1:11434/api/version", "http://127.0.0.1:11434/api/tags"] if fallback else [])
        assert control._thread is None
    finally:
        application.close_providers(timeout=2)
        ollama_pool.reset_typed_workers()


def test_resolver_timeout_retains_one_owned_task_without_backlog(tmp_path, monkeypatch):
    import threading
    from time import monotonic
    from sonder_runtime.adapters.inference import external_membership as module
    release, entered = threading.Event(), threading.Event()
    calls = []
    def blocked(*_a, **_kw):
        calls.append(1)
        entered.set()
        release.wait(2)
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.77.0.2", 443))]
    monkeypatch.setattr(module.socket, "getaddrinfo", blocked)
    value = ExternalMembershipSource(configuration(tmp_path),
        credentials(tmp_path), clock=lambda: NOW)
    try:
        with pytest.raises(TimeoutError): value._transport._resolve("registry.example", 443, monotonic() + .02)
        assert entered.is_set()
        thread = value._transport._resolver_thread
        for _ in range(4):
            with pytest.raises(TimeoutError): value._transport._resolve("registry.example", 443, monotonic() + .02)
            assert value._transport._resolver_thread is thread
        assert calls == [1]
        for invalid in (None, True, -1, float("nan"), float("inf"), 31):
            with pytest.raises(ValueError): value.close(timeout=invalid)
        assert value.close(timeout=0) is False
    finally:
        release.set()
        assert value.close(timeout=2)


def test_external_policy_ceiling_does_not_bypass_pool_admission_bound(tmp_path, monkeypatch):
    from sonder_runtime.bootstrap import app as bootstrap, legacy_root
    from sonder_runtime.adapters.inference import ollama_pool
    from sonder_runtime.platform.config import SonderConfig, OllamaConfig, StateConfig, validate_membership_config
    policies = tuple(MembershipEndpointPolicy(f"w{i:04}", f"https://w{i:04}.example:11434",
        f"w{i:04}.example", ("10.77.0.0/24",)) for i in range(4096))
    config = replace(configuration(tmp_path), snapshot_max_advertisements=4096, member_policies=policies)
    secrets = credentials(tmp_path)
    validate_membership_config(config, secrets)
    with pytest.raises(ValueError): validate_membership_config(replace(config, member_policies=policies + policies[:1]), secrets)
    key = Ed25519PrivateKey.generate()
    (tmp_path / "authority" / "signer.pem").write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo))
    raw = envelope(key, workers=[dict(worker_id=p.member_id, origin=p.origin, member_generation=1,
        lifecycle_state="active", models=[], advertised_capacity=1) for p in policies])
    assert len(raw) <= 1048576
    application = bootstrap.build_application(config=SonderConfig(membership=config, secrets=secrets,
        state=StateConfig(home=str(tmp_path)), ollama=OllamaConfig(allow_remote=True, worker_pool_max_workers=4)))
    control, pool = application.inference_membership, application.inference_pool
    clock = lambda: NOW
    control._clock = control._source._clock = control._high_water_store._clock = pool._membership_clock = clock
    probed = []
    def retrieve(policy, **kwargs):
        assert not pool._condition._is_owned()
        if kwargs["path"] == "/v1/membership": return raw
        probed.append(policy.origin)
        return b'{"version":"1","models":[{"name":"code"}]}'
    monkeypatch.setattr(control._source._transport, "retrieve", retrieve)
    try:
        control._refresh_once(2, True)
        assert pool.membership_limit == 3
        assert len(control._result.roster.members) == 3
        assert pool.summary()["membership_omitted_worker_count"] == 4093
        assert len(pool._states) == 4  # reserved static loopback plus three remote
        admitted = {p.origin for p in policies[:3]}
        assert set(probed) == admitted and len(probed) == 6
        dispatched = []
        for _ in range(8): pool.request(lambda origin: dispatched.append(origin), model="code")
        assert set(dispatched) <= admitted
        assert legacy_root.require_inference_application(application) is pool
    finally:
        application.close_providers(timeout=2)
        ollama_pool.reset_typed_workers()


@pytest.mark.parametrize("mutation", ["source_policy", "policies", "origins", "source_subclass"])
def test_external_binding_refuses_substituted_configured_authority(tmp_path, mutation):
    from sonder_runtime.bootstrap import app as bootstrap, legacy_root
    from sonder_runtime.adapters.inference import ollama_pool
    from sonder_runtime.platform.config import SonderConfig, OllamaConfig, StateConfig
    application = bootstrap.build_application(config=SonderConfig(membership=configuration(tmp_path),
        secrets=credentials(tmp_path),
        state=StateConfig(home=str(tmp_path)), ollama=OllamaConfig(allow_remote=True)))
    source = application.inference_membership._source
    try:
        assert legacy_root.require_inference_application(application) is application.inference_pool
        if mutation == "source_policy": source._source_policy = replace(source._source_policy, origin="https://other.example:443")
        elif mutation == "policies": source._policies = {}
        elif mutation == "origins": source._origins = {}
        else:
            class Hostile(ExternalMembershipSource):
                def __eq__(self, other): pytest.fail("hostile equality")
            source.__class__ = Hostile
        with pytest.raises(ValueError): legacy_root.require_inference_application(application)
    finally:
        application.close_providers(timeout=2)
        ollama_pool.reset_typed_workers()


@pytest.mark.parametrize("operation", ["read", "readline"])
def test_trickled_headers_and_body_cannot_extend_the_total_deadline(monkeypatch, operation):
    from sonder_runtime.adapters.inference import external_membership as module
    clock, reads = [0.0], []
    class Trickle:
        def read(self, size): clock[0] += 5; return b"x" * size
        def readline(self, size): clock[0] += 5; return b"private header\r\n"
        def read1(self, size):
            reads.append(size)
            clock[0] += .4
            return b"x"
    class Channel:
        def settimeout(self, timeout): assert 0 < timeout <= 1
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    reader = module._ResponseReader(Trickle(), Channel(), 1.0)
    with pytest.raises(TimeoutError): getattr(reader, operation)(50)
    assert len(reads) <= 3


@pytest.mark.parametrize("command", ["serve", "mcp", "repl", "bound_direct"])
@pytest.mark.parametrize("primary_remote", [False, True])
@pytest.mark.parametrize("fallback", [False, True])
def test_real_entrypoints_keep_external_admission_transport_and_local_choice(
    tmp_path, monkeypatch, command, primary_remote, fallback,
):
    from types import SimpleNamespace
    import urllib.request
    import server
    import sonder_runtime.__main__ as entrypoint
    from sonder_runtime.adapters.application_lifecycle import ApplicationLifecycle
    from sonder_runtime.adapters.inference import ollama_endpoint, ollama_pool
    from sonder_runtime.adapters.persistence import migrations, operations_store
    from sonder_runtime.adapters.persistence.sqlite import bridge_migration
    from sonder_runtime.bootstrap import app as bootstrap, legacy_root
    from sonder_runtime.interfaces.http import serve
    from sonder_runtime.interfaces.repl import repl
    from sonder_runtime.platform.config import SonderConfig, StateConfig, OllamaConfig
    key = Ed25519PrivateKey.generate()
    (tmp_path / "authority" / "signer.pem").write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo))
    local = "http://127.0.0.1:11434"
    config = SonderConfig(state=StateConfig(home=str(tmp_path)), membership=replace(configuration(tmp_path), local_fallback=fallback),
        secrets=credentials(tmp_path),
        ollama=OllamaConfig(url=ORIGIN if primary_remote else local, allow_remote=True))
    monkeypatch.setattr(server, "OLLAMA_POOL", ollama_pool.OllamaWorkerPool(local))
    monkeypatch.setattr(server, "BASE", local)
    monkeypatch.setattr(server, "_APP_GRAPH", None)
    monkeypatch.setattr(legacy_root, "_owned_application", None)
    monkeypatch.setattr(bootstrap, "_application_lifecycle", ApplicationLifecycle(bootstrap._build_default_application))
    for name in ("_default_config", "_default_compute_close", "_default_delegation_close", "_default_inference_close"):
        monkeypatch.setattr(bootstrap, name, None)
    monkeypatch.setattr(entrypoint, "_load_config", lambda _: config)
    monkeypatch.setattr(entrypoint, "_export_runtime_environment", lambda *_a, **_kw: None)
    monkeypatch.setenv("SONDER_ALLOW_REMOTE_OLLAMA", "1")  # matches typed consent; the membership fence must still win
    monkeypatch.setattr(bridge_migration, "require_epoch_2", lambda _: None)
    monkeypatch.setattr(migrations, "migrate_all", lambda **_: None)
    monkeypatch.setattr(operations_store, "OperationsStore", lambda: SimpleNamespace(prune_events=lambda _: 0))
    monkeypatch.setattr(server, "dispatch_provider", lambda _provider, _path, _payload, send: send())
    # Exercise real legacy operations, retaining the embedding adapter and
    # final generic opener. Isolate persistence and the unrelated generation.
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "legacy-memory.db"))
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server, "_capture_preferences", lambda *_a, **_kw: None)
    monkeypatch.setattr(server, "_make_generate", lambda *_a, **_kw: lambda *_a, **_kw: "answer")
    monkeypatch.setattr(server.orchestrator, "run_with_learning", lambda *_a, **_kw: ("answer", "interaction"))
    monkeypatch.setattr(server, "_gateway_generate_text", lambda *_a, **_kw: "Use pathlib.Path for path joins.")
    monkeypatch.setattr(server, "_record_outcome_and_maybe_distill", lambda *_a, **_kw:
                        {"lesson_id": None, "distillation_deferred": True})
    real_repl_main = repl.main
    monkeypatch.setattr(repl, "_startup_banner", lambda *_a: "")
    monkeypatch.setattr(repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(repl, "_named_command_gate", lambda *_a: (True, ""))
    local_calls, remote_calls, source_calls, checked = [], [], [], []
    embedding_active, generic_embedding_requests = [False], []
    serialized_embedding_bodies = []
    original_dumps = json.dumps
    def watched_dumps(value, *args, **kwargs):
        if isinstance(value, dict) and set(value) == {"model", "prompt"}:
            serialized_embedding_bodies.append(value)
        return original_dumps(value, *args, **kwargs)
    monkeypatch.setattr(json, "dumps", watched_dumps)
    def local_transport(request, **kwargs):
        if embedding_active[0]:
            generic_embedding_requests.append((getattr(request, "full_url", request), getattr(request, "data", None)))
            return io.BytesIO(b'{"embedding":[1.0],"models":[]}')
        assert fallback and not primary_remote
        assert request.full_url.startswith(local + "/")
        local_calls.append(request.full_url)
        return io.BytesIO(b'{"version":"1","models":[{"name":"local-model"}],"ok":true}')
    monkeypatch.setattr(ollama_endpoint._OPENER, "open", local_transport)
    def run_interface(**_):
        application = bootstrap.default_app()
        pool, control = application.inference_pool, application.inference_membership
        assert server.OLLAMA_POOL is pool and server._application() is application
        assert control._thread is None and control._source._transport._resolver_thread is None
        clock = [NOW]
        aware = lambda: clock[0]
        control._clock = control._source._clock = control._high_water_store._clock = pool._membership_clock = aware
        def retrieve(policy, **kwargs):
            assert not pool._condition._is_owned()
            if kwargs["path"] == "/v1/membership":
                source_calls.append(1)
                return envelope(key)
            assert policy is config.membership.member_policies[0]
            remote_calls.append(kwargs["path"])
            return b'{"version":"1","models":[{"name":"remote-model"}],"ok":true}'
        monkeypatch.setattr(control._source._transport, "retrieve", retrieve)
        from sonder_runtime.application.context import local_owner_context
        from sonder_runtime.application.ports.specialized_lifecycle import EmbeddingRequest
        embedding = application.specialized_providers.registrations[0].provider
        def embedding_closed():
            before = (list(local_calls), list(remote_calls), list(source_calls))
            embedding_active[0] = True
            try:
                with pytest.raises(RuntimeError, match="^default embeddings unavailable in external membership mode$"):
                    embedding.embed(EmbeddingRequest(("sensitive embedding text",), "embed-test"),
                                    local_owner_context(correlation_id="external-embedding"))
                assert "fact" in server.sonder_remember_fact("sensitive embedding text", project="external-test")
                assert generic_embedding_requests == []
                assert "Example recorded" in server.learn_from_example("sensitive embedding text", "use pathlib")
                assert server._answer(None, "sensitive embedding text", "code", "", 0.2, 32,
                    2048, "session", None, [], augment=False)[0] == "answer"
                candidate = server._prepare_lesson_candidate_bounded(
                    {"task": "sensitive embedding text", "response": "use pathlib"}, "tests_passed")
                assert candidate["embedding"] is None
                mcp_fact = server.mcp._tool_manager.get_tool("sonder_remember_fact").fn
                assert "fact" in mcp_fact("sensitive embedding text", project="external-mcp")
                http_result = serve._dispatch_catalogued_tool(
                    '/sonder_remember_fact text="sensitive embedding text" project=external-http',
                    SimpleNamespace(token=""), context={"mode": "local-open"})
                assert "fact" in http_result
                lines = iter(("/fact sensitive embedding text", "/exit"))
                monkeypatch.setattr(repl, "_read_input", lambda *_a, **_kw: next(lines))
                real_repl_main()
                # Explicit endpoint/model arguments are still the default
                # adapter, and cannot serve as an escape hatch.
                assert server.embeddings.embed("sensitive embedding text", base=local, model="embed-test") is None
                assert server.embeddings.embed_result("sensitive embedding text") is None
                assert server.embeddings.serving_model_revision() == ""
                assert server.embeddings.current_revision() == ""
                assert server.embeddings.refresh_runtime_revision() == ""
                assert isinstance(server.memory_embedding_backfill(limit=1, apply=True), str)
            finally:
                embedding_active[0] = False
            assert generic_embedding_requests == []
            assert serialized_embedding_bodies == []
            assert (local_calls, remote_calls, source_calls) == before
        embedding_closed()  # before any signed membership or high-water evidence
        with pytest.raises(ollama_pool.WorkerPoolError): server._post("/api/generate", {"model":"remote-model"})
        assert remote_calls == [] and source_calls == []
        if primary_remote or not fallback:
            with pytest.raises(ollama_pool.WorkerPoolError): server._post("/api/generate", {}, local_only=True)
            assert local_calls == []
        else:
            assert server._post("/api/generate", {}, local_only=True)["ok"]
        control.refresh(timeout_seconds=2)
        assert source_calls == [1]
        assert server._post("/api/generate", {"model":"remote-model"})["ok"]
        assert remote_calls.count("/api/generate") == 1
        embedding_closed()  # deliberately unavailable even for an admitted member
        clock[0] += timedelta(seconds=61)
        before = list(remote_calls)
        with pytest.raises(ollama_pool.WorkerPoolError): server._post("/api/generate", {"model":"remote-model"})
        if primary_remote:
            with pytest.raises(ollama_pool.WorkerPoolError): server._get("/api/tags")
        assert remote_calls == before and source_calls == [1]
        embedding_closed()  # expired signed membership
        clock[0] = NOW
        monkeypatch.setattr(control._source._transport, "retrieve", lambda *_a, **_kw: envelope(key, generation=2, workers=[]))
        control.refresh(timeout_seconds=2)
        embedding_closed()  # revoked membership
        assert legacy_root.require_inference_application(application) is pool
        checked.append(True)
    monkeypatch.setattr(serve, "main", run_interface)
    monkeypatch.setattr(server.mcp, "run", run_interface)
    monkeypatch.setattr(server, "require_mcp_startup_safety", lambda: None)
    monkeypatch.setattr(repl, "main", run_interface)
    try:
        if command == "bound_direct":
            legacy_root.configure_application(bootstrap.default_app(config=config))
            server.run_mcp()
        else:
            assert getattr(entrypoint, "cmd_" + command)(SimpleNamespace(skip_preflight=True, native=False, json=False)) == 0
        assert checked == [True]
    finally:
        bootstrap.close_default_runtime_resources(timeout=2)
        ollama_pool.reset_typed_workers()


@pytest.mark.parametrize("diagnostic", ["preflight_primary", "preflight_workers", "doctor_primary", "doctor_workers", "doctor_residency"])
@pytest.mark.parametrize("primary_remote", [False, True])
def test_external_standalone_diagnostics_defer_without_network_or_private_details(tmp_path, monkeypatch, diagnostic, primary_remote):
    import urllib.request
    import sonder_doctor
    from sonder_runtime.adapters import preflight
    from sonder_runtime.adapters.inference import ollama_endpoint
    from sonder_runtime.platform.config import SonderConfig, OllamaConfig
    config = SonderConfig(membership=configuration(tmp_path),
        secrets=credentials(tmp_path),
        ollama=OllamaConfig(url=ORIGIN if primary_remote else "http://127.0.0.1:11434",
                            workers=() if primary_remote else (ORIGIN,), allow_remote=True))
    def forbidden(*_a, **_kw): pytest.fail("standalone external diagnostic attempted transport")
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(ollama_endpoint, "open_url", forbidden)
    monkeypatch.setattr(sonder_doctor, "_load_config_or_none", lambda: config)
    if diagnostic.startswith("preflight"):
        results = ([preflight._check_ollama(config)] if diagnostic == "preflight_primary" else preflight._check_ollama_workers(config))
        assert results and all(not item.ok and not item.required for item in results)
        rendered = json.dumps([item.as_dict() for item in results])
    else:
        result = {"doctor_primary":sonder_doctor._check_ollama, "doctor_workers":sonder_doctor._check_ollama_workers,
                  "doctor_residency":sonder_doctor._check_ollama_residency}[diagnostic]()
        assert result["status"] == sonder_doctor.STATUS_SKIPPED
        rendered = json.dumps(result)
    assert "deferred" in rendered
    for private in ("worker.example", "registry.example", "private-client.pem", "private-key.pem", str(tmp_path)):
        assert private not in rendered
