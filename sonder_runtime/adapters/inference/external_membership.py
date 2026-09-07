"""Configured private mTLS transport and Ed25519 membership authority.

No proxy, redirect, enrollment, remote policy input, or implicit refresh.
One bounded resolver task is owned until completion, including after timeout.
"""
import base64
import http.client
import io
import ipaddress
import json
import math
import os
from pathlib import Path
import socket
import ssl
import tempfile
from threading import Event, Lock
from time import monotonic
from types import MappingProxyType
from urllib.error import HTTPError
from urllib.parse import urlsplit
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ...application.ports.inference_membership import MembershipSourceLimits
from ...domain.inference_membership import MembershipSnapshot, _aware, _origin, _timestamp
from ...platform.config import MembershipEndpointPolicy, validate_membership_config
from ...platform.runtime_threads import Thread
from ..model_transport import ModelCallError
from ...application.compute_fabric.artifact_spool import PrivateDirectoryAnchor
from .membership_high_water import _identity


class MembershipSourceError(RuntimeError):
    pass


def _capture_authority(path, maximum):
    """Read once beneath a private no-link anchor, binding handle and bytes.

    The trusted local owner provisions files before construction. Later edits
    are never adopted: credential/CA/signing rotation requires a new source.
    """
    path = Path(path)
    if os.name == "nt":
        import ctypes
        drive_type = ctypes.WinDLL("kernel32", use_last_error=True).GetDriveTypeW
        drive_type.argtypes, drive_type.restype = [ctypes.c_wchar_p], ctypes.c_uint
        if drive_type(path.anchor) not in (2, 3, 6):
            raise ValueError
    with PrivateDirectoryAnchor(path.parent) as anchor:
        # Check before open as well, so a special file cannot block the read.
        import stat
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise ValueError
        with anchor.open_read(path.name) as stream:
            _identity(anchor, path.name, stream)
            before = os.fstat(stream.fileno())
            if not os.path.samestat(metadata, before):
                raise ValueError
            raw = stream.read(maximum + 1)
            _identity(anchor, path.name, stream)
            after = os.fstat(stream.fileno())
            if (not raw or len(raw) > maximum or
                    (before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                    (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise ValueError
            return raw


def _remaining(deadline):
    value = deadline - monotonic()
    if value <= 0:
        raise TimeoutError
    return value


class _ResponseReader:
    def __init__(self, stream, channel, deadline):
        self._stream, self._channel, self._deadline = stream, channel, deadline
        self.started = False
        self._line_bytes = 0

    def readline(self, size=-1):
        data = bytearray()
        for _ in range(min(8193, size) if size >= 0 else 8193):
            chunk = self.read(1)
            if not chunk:
                break
            data.extend(chunk)
            if chunk == b"\n":
                break
        self._line_bytes += len(data)
        if len(data) > 8192 or self._line_bytes > 32768:
            raise ValueError
        return bytes(data)

    def read(self, size=-1):
        if not 0 <= size <= 1048577:
            raise ValueError
        data = bytearray()
        while len(data) < size:
            self._channel.settimeout(_remaining(self._deadline))
            # read1 performs at most one underlying read. Buffered read/readline
            # can otherwise reset a socket timeout repeatedly on trickled data.
            chunk = self._stream.read1(size - len(data))
            self.started |= bool(chunk)
            _remaining(self._deadline)
            if not chunk:
                break
            data.extend(chunk)
        return bytes(data)

    def close(self):
        self._stream.close()

    def flush(self):
        self._stream.flush()


class _PinnedTransport:
    def __init__(self, config, secrets):
        self._config, self._secrets = config, secrets
        self._authority = tuple(_capture_authority(path, 1048576) for path in (
            config.trust_anchor_file, secrets.membership_client_cert_file, secrets.membership_client_key_file))
        self._context = None
        self._context_lock = Lock()
        self._lock = Lock()
        self._resolver_thread = None
        self._closed = False

    def _tls_context(self):
        with self._context_lock:
            if self._context is not None:
                return self._context
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False  # exact SAN check; never CN/wildcard fallback
            context.verify_mode = ssl.CERT_REQUIRED
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_verify_locations(cadata=self._authority[0].decode("ascii"))
            # SSLContext accepts client chains only by path. Materialize the
            # captured bytes in a new private directory, never reopen authority
            # input paths. Delete these temporary copies before connecting.
            root = Path(tempfile.gettempdir()) / ("sonder-membership-" + uuid4().hex)
            names = []
            anchor = PrivateDirectoryAnchor(root, create=True, require_new=True)
            try:
                with anchor:
                    try:
                        for raw in self._authority[1:]:
                            fd, name = anchor.create_temporary()
                            names.append(name)
                            with os.fdopen(fd, "wb") as stream:
                                stream.write(raw)
                                stream.flush()
                                _identity(anchor, name, stream)
                        context.load_cert_chain(certfile=str(root / names[0]), keyfile=str(root / names[1]))
                    finally:
                        for name in names:
                            anchor.unlink(name)
            finally:
                root.rmdir()
            self._context = context
            return context

    def _resolve(self, host, port, deadline):
        with self._lock:
            if self._closed or (self._resolver_thread is not None and self._resolver_thread.is_alive()):
                raise TimeoutError
            done, result = Event(), []
            def resolve():
                try:
                    result.append(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP))
                except Exception:
                    result.append(None)
                finally:
                    done.set()
            self._resolver_thread = Thread(target=resolve, name="sonder-membership-resolver", daemon=True)
            self._resolver_thread.start()
        if not done.wait(_remaining(deadline)) or not result or not result[0]:
            raise TimeoutError
        answers = result[0]
        if type(answers) is not list or len(answers) > 64:
            raise ValueError
        return answers

    def retrieve(self, policy, *, path, timeout, max_bytes, method="GET", data=None):
        channel = response = reader = None
        version_not_found = False
        try:
            if (type(policy) is not MembershipEndpointPolicy or type(max_bytes) is not int
                    or not 1 <= max_bytes <= 1048576 or type(timeout) not in (int, float)
                    or not math.isfinite(timeout) or not 0 < timeout <= 300):
                raise ValueError
            deadline = monotonic() + timeout
            with self._lock:
                if self._closed:
                    raise ValueError
            parsed = urlsplit(policy.origin)
            addresses = self._resolve(parsed.hostname, parsed.port, deadline)
            networks = tuple(ipaddress.ip_network(value, strict=True) for value in policy.allowed_cidrs)
            for family, kind, protocol, _, address in addresses:
                ip = ipaddress.ip_address(address[0])
                if (family not in (socket.AF_INET, socket.AF_INET6) or kind != socket.SOCK_STREAM
                        or protocol != socket.IPPROTO_TCP or address[1] != parsed.port
                        or ip.is_unspecified or ip.is_multicast
                        or not any(ip in network for network in networks)):
                    raise ValueError
            context = self._tls_context()
            family, _, _, _, address = addresses[0]
            channel = socket.socket(family, socket.SOCK_STREAM)
            channel.settimeout(_remaining(deadline))
            channel.connect(address)  # the validated numeric sockaddr; no second DNS lookup
            channel.settimeout(_remaining(deadline))
            channel = context.wrap_socket(channel, server_hostname=policy.tls_server_name)
            expected = policy.tls_server_name
            try:
                ip = ipaddress.ip_address(expected)
            except ValueError:
                matched = any(kind == "DNS" and name == expected
                              for kind, name in channel.getpeercert().get("subjectAltName", ()))
            else:
                matched = any(kind == "IP Address" and ipaddress.ip_address(name) == ip
                              for kind, name in channel.getpeercert().get("subjectAltName", ()))
            if not matched:
                raise ValueError
            if method not in ("GET", "POST") or type(path) is not str or not path.startswith("/") or any(c in path for c in "\r\n?#"):
                raise ValueError
            if data is not None and (type(data) is not bytes or len(data) > 1048576):
                raise ValueError
            body = data or b""
            headers = (f"{method} {path} HTTP/1.1\r\nHost: {parsed.netloc}\r\nConnection: close\r\n"
                       f"Accept: application/json\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n").encode("ascii")
            channel.settimeout(_remaining(deadline))
            channel.sendall(headers + body)
            reader = _ResponseReader(channel.makefile("rb"), channel, deadline)
            class ResponseSocket:
                def makefile(self, *_a, **_kw): return reader
            response = http.client.HTTPResponse(ResponseSocket())
            response.begin()
            version_not_found = (response.status == 404 and method == "GET"
                and path == "/api/version" and data is None
                and response.getheader("Content-Encoding", "identity") == "identity"
                and response.headers.get_all("Content-Length") == ["0"]
                and response.getheader("Transfer-Encoding") is None
                # HTTPResponse.read() trusts Content-Length: 0 and would hide
                # an extra body. Require actual EOF on the bounded raw reader.
                and reader.read(1) == b"")
            if response.status != 200 or response.getheader("Content-Encoding", "identity") != "identity":
                raise ValueError
            length = response.getheader("Content-Length")
            if length is not None and (not length.isdecimal() or int(length) > max_bytes):
                raise ValueError
            chunks, size = [], 0
            while True:
                chunk = response.read(min(65536, max_bytes - size + 1))
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError
                chunks.append(chunk)
            _remaining(deadline)
            if length is not None and size != int(length):
                raise ValueError
            return b"".join(chunks)
        except Exception:
            if version_not_found:
                # Only the informational version endpoint has this historical
                # compatibility meaning. Do not attach URL, headers or body.
                raise HTTPError("", 404, "private worker version unavailable", None, None) from None
            # Even a partial status/header/body is response-bearing: never
            # reclassify it as a retryable pre-response connection failure.
            if reader is not None and reader.started:
                raise ModelCallError("protocol", "private worker response rejected", status=0) from None
            raise MembershipSourceError("external membership unavailable") from None
        finally:
            try:
                if response is not None:
                    response.close()
                elif reader is not None:
                    reader.close()
                if channel is not None:
                    channel.close()
            except Exception:
                raise MembershipSourceError("external membership unavailable") from None

    def close(self, *, timeout):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 <= timeout <= 30:
            raise ValueError("close timeout must be within 0..30 seconds")
        with self._lock:
            self._closed = True
            thread = self._resolver_thread
        if thread is not None:
            thread.join(timeout)
        return thread is None or not thread.is_alive()


class ExternalMembershipSource:
    def __init__(self, config, secrets, *, clock):
        validate_membership_config(config, secrets)
        if config.mode != "external":
            raise ValueError("external membership must be explicitly configured")
        _aware(clock(), "membership clock")
        self._config, self._secrets, self._clock = config, secrets, clock
        self.cluster_id, self.issuer_id = config.cluster_id, config.issuer_id
        self._source_policy = MembershipEndpointPolicy("source", config.source_origin,
            config.source_tls_server_name, config.source_allowed_cidrs)
        self._policies = MappingProxyType({policy.member_id: policy for policy in config.member_policies})
        self._origins = MappingProxyType({policy.origin: policy for policy in config.member_policies})
        try:
            self._signing_key = serialization.load_pem_public_key(_capture_authority(config.signature_public_key_file, 16384))
            if not isinstance(self._signing_key, Ed25519PublicKey):
                raise ValueError
            self._transport = _PinnedTransport(config, secrets)
            # Registration waits for default operations before activating
            # ownership; a busy/reentrant composition fails without publication.
            from .ollama_endpoint import _restrict_for_external_membership
            _restrict_for_external_membership(self)
        except Exception:
            raise MembershipSourceError("external membership unavailable") from None

    def read_snapshot(self, *, limits):
        try:
            if type(limits) is not MembershipSourceLimits:
                raise ValueError
            deadline = monotonic() + limits.timeout_seconds
            maximum = min(limits.max_bytes, self._config.snapshot_max_bytes)
            raw = self._transport.retrieve(self._source_policy, path="/v1/membership",
                timeout=limits.timeout_seconds, max_bytes=maximum)
            if type(raw) is not bytes or len(raw) > maximum:
                raise ValueError
            key = self._signing_key
            def verify(envelope):
                signed = json.loads(envelope)
                payload = signed["payload"]
                key.verify(base64.b64decode(signed["signature"], validate=True),
                    json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii"))
                if (payload["cluster_id"] != self.cluster_id or payload["issuer_id"] != self.issuer_id
                        or type(payload["protocol_version"]) is not int or payload["protocol_version"] != self._config.protocol_version
                        or not _timestamp(payload["issued_at"]) <= _aware(self._clock(), "membership clock") < _timestamp(payload["expires_at"])):
                    return False
                for row in payload["workers"]:
                    policy = self._policies.get(row["worker_id"])
                    if policy is None or _origin(row["origin"]) != policy.origin:
                        return False
                return True
            snapshot = MembershipSnapshot.from_signed_envelope(raw, verify=verify,
                max_bytes=maximum, max_advertisements=min(limits.max_advertisements, self._config.snapshot_max_advertisements))
            _remaining(deadline)
            return snapshot
        except Exception:
            raise MembershipSourceError("external membership unavailable") from None

    def open_worker_url(self, request, *, timeout):
        try:
            parsed = urlsplit(request.full_url)
            origin = _origin(f"{parsed.scheme}://{parsed.netloc}")
            policy = self._origins.get(origin)
            if policy is None or parsed.query or parsed.fragment:
                raise ValueError
            raw = self._transport.retrieve(policy, path=parsed.path, timeout=min(timeout, 300),
                max_bytes=1048576, method=request.get_method(), data=request.data)
            return io.BytesIO(raw)
        except (MembershipSourceError, ModelCallError, HTTPError):
            raise
        except Exception:
            raise MembershipSourceError("external membership unavailable") from None

    def close(self, *, timeout=5):
        return self._transport.close(timeout=timeout)
