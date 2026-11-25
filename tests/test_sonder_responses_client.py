"""Comprehensive unit and loopback HTTP/HTTPS tests for ``sonder_responses_client``."""
from __future__ import annotations

import base64
import datetime as dt
import io
import ipaddress
import json
import ssl
import threading
import time
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest

from sonder_runtime.adapters.inference import exchange_transport
from sonder_runtime.adapters.inference.sonder_responses_client import (
    API_VERSION,
    MAX_IMAGE_BASE64_CHARS,
    MAX_IMAGES_PER_REQUEST,
    PreviousResponseNotFound,
    ResponsesCorrelationHeaders,
    ResponsesFunctionCallInput,
    ResponsesFunctionCallOutputInput,
    ResponsesImagePart,
    ResponsesMessageInput,
    ResponsesNamedToolChoice,
    ResponsesRequest,
    ResponsesStreamEvent,
    ResponsesTextFormat,
    ResponsesTextPart,
    ResponsesToolDefinition,
    SonderResponsesClient,
    SonderResponsesConfig,
    build_request_headers,
    build_request_payload,
    is_loopback_url,
    iter_sse_events,
    normalize_responses_base_url,
    parse_response_document,
    validate_data_image_uri,
    validate_endpoint_policy,
    validate_private_worker_url,
)
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.domain.common.errors import (
    Cancelled,
    CapacityExceeded,
    DeadlineExceeded,
    DependencyUnavailable,
    Forbidden,
    InvalidInput,
    NotFound,
)

TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)
TINY_PNG_URI = f"data:image/png;base64,{TINY_PNG_B64}"


def _sample_response_doc(
    *,
    resp_id: str = "resp_req-1",
    model: str = "qwen3:8b",
    status: str = "completed",
    previous_response_id: str | None = None,
    output_text: str = "Hello back.",
    reasoning: str = "",
    function_calls: list[dict[str, object]] | None = None,
    session_key: str = "sess-key-1",
    input_tokens: int = 14,
    output_tokens: int = 6,
    cached_tokens: int | None = None,
    api_version: int = API_VERSION,
    logprobs: list[dict[str, object]] | None = None,
    top_level_logprobs: object = None,
    warnings: list[str] | None = None,
) -> dict[str, object]:
    output: list[dict[str, object]] = []
    if reasoning:
        output.append({
            "id": f"rs_{resp_id}",
            "type": "reasoning",
            "status": "completed",
            "summary": [{"type": "summary_text", "text": reasoning}],
        })
    if output_text or not function_calls:
        text_block: dict[str, object] = {
            "type": "output_text",
            "text": output_text,
            "annotations": [],
        }
        if logprobs is not None:
            text_block["logprobs"] = logprobs
        output.append({
            "id": f"msg_{resp_id}",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [text_block],
        })
    for idx, fc in enumerate(function_calls or []):
        output.append({
            "id": str(fc.get("id") or f"fc_{resp_id}_{idx}"),
            "type": "function_call",
            "status": "completed",
            "call_id": str(fc["call_id"]),
            "name": str(fc["name"]),
            "arguments": str(fc.get("arguments", "{}")),
        })
    usage: dict[str, object] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    if cached_tokens is not None:
        usage["input_tokens_details"] = {"cached_tokens": cached_tokens}
    sonder: dict[str, object] = {
        "api_version": api_version,
        "request_id": "req-1",
        "session_id": "sess-1",
        "session_key": session_key,
        "backend": "llamacpp",
        "synthetic": False,
        "token_counts_from_backend": True,
    }
    if warnings:
        sonder["warnings"] = warnings
    doc: dict[str, object] = {
        "id": resp_id,
        "object": "response",
        "created_at": 1_790_000_000,
        "status": status,
        "model": model,
        "previous_response_id": previous_response_id,
        "output": output,
        "output_text": output_text,
        "usage": usage,
        "sonder": sonder,
    }
    if top_level_logprobs is not None:
        doc["logprobs"] = top_level_logprobs
    return doc


class FakeResponsesServer:
    """Scriptable local HTTP/HTTPS stand-in for ``sonder-infer serve`` /v1/responses."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str], dict[str, object]]] = []
        self.history: dict[str, tuple[str, str, list[dict[str, object]]]] = {}
        self.counter = 0
        self.force_status: int | None = None
        self.force_error_body: dict[str, object] | None = None
        self.redirect_location: str | None = None
        self.raw_response_bytes: bytes | None = None
        self.raw_content_type: str = "application/json"
        self.non_sse_on_stream: bool = False
        self.stream_tokens: list[str] = ["Hello", " ", "stream!"]
        self.stream_function_call: tuple[str, str, list[str]] | None = None
        self.stream_error_after: int | None = None
        self.stream_truncate_before_completed: bool = False
        self.stream_incomplete_status: bool = False
        self.stream_logprobs: list[dict[str, object]] | None = None
        self.response_reasoning: str = ""
        self.response_api_version: int = API_VERSION
        self.response_warnings: list[str] = []
        self.delay_before_headers: float = 0.0
        self.hold_after_first_delta: threading.Event | None = None
        self.first_delta_sent = threading.Event()
        self.client_disconnected = threading.Event()
        self.lock = threading.Lock()


@contextmanager
def _serve_fake(
    fake: FakeResponsesServer,
    *,
    ssl_context: ssl.SSLContext | None = None,
) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:
            pass

        def _send_json(self, status: int, doc: dict[str, object]) -> None:
            raw = json.dumps(doc).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _send_sse(self, event: str, payload: dict[str, object]) -> None:
            frame = f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode("utf-8")
            self.wfile.write(frame)
            self.wfile.flush()

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length)
            payload = json.loads(raw_body.decode("utf-8"))
            headers_lower = {k.lower(): v for k, v in self.headers.items()}
            with fake.lock:
                fake.requests.append((self.path, headers_lower, payload))

            if fake.delay_before_headers > 0.0:
                deadline = time.monotonic() + fake.delay_before_headers
                while time.monotonic() < deadline:
                    time.sleep(0.02)

            if fake.redirect_location is not None:
                self.send_response(302)
                self.send_header("Location", fake.redirect_location)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            if fake.raw_response_bytes is not None:
                self.send_response(200)
                self.send_header("Content-Type", fake.raw_content_type)
                self.send_header("Content-Length", str(len(fake.raw_response_bytes)))
                self.end_headers()
                try:
                    self.wfile.write(fake.raw_response_bytes)
                except OSError:
                    fake.client_disconnected.set()
                return

            if fake.force_status is not None:
                err_doc = fake.force_error_body or {
                    "error": {"code": "forced_error", "message": "forced failure", "param": None},
                    "sonder": {"api_version": API_VERSION},
                }
                self._send_json(fake.force_status, err_doc)
                return

            prev_id = payload.get("previous_response_id")
            with fake.lock:
                if prev_id is not None:
                    if prev_id not in fake.history:
                        self._send_json(
                            404,
                            {
                                "error": {
                                    "code": "not_found",
                                    "type": "not_found_error",
                                    "message": f"previous_response_id '{prev_id}' was not found",
                                    "param": "previous_response_id",
                                },
                                "sonder": {"api_version": API_VERSION},
                            },
                        )
                        return
                    session_key, prev_model, prev_turns = fake.history[prev_id]
                    turns = list(prev_turns)
                else:
                    fake.counter += 1
                    session_key = str(payload.get("prompt_cache_key") or f"resp_req-{fake.counter}")
                    prev_model = ""
                    turns = []
                fake.counter += 1
                resp_id = f"resp_req-{fake.counter}"
                model = str(payload.get("model") or "default")
                if model == "default":
                    model = prev_model or "qwen3:8b"
                turns.append({"input": payload["input"]})

            cached = 10 if prev_id else None
            in_tok = 10 * len(turns)
            is_stream = bool(payload.get("stream")) and not fake.non_sse_on_stream

            if not is_stream:
                fcs: list[dict[str, object]] = []
                text = f"turn-{len(turns)}"
                if fake.stream_function_call is not None:
                    call_id, fn_name, arg_parts = fake.stream_function_call
                    fcs = [{"call_id": call_id, "name": fn_name, "arguments": "".join(arg_parts)}]
                    text = ""
                doc = _sample_response_doc(
                    resp_id=resp_id,
                    model=model,
                    status="incomplete" if fake.stream_incomplete_status else "completed",
                    previous_response_id=prev_id,
                    output_text=text,
                    reasoning=fake.response_reasoning,
                    function_calls=fcs or None,
                    session_key=session_key,
                    input_tokens=in_tok,
                    output_tokens=5,
                    cached_tokens=cached,
                    api_version=fake.response_api_version,
                    logprobs=fake.stream_logprobs,
                    warnings=fake.response_warnings or None,
                )
                if payload.get("store", True) and doc["status"] == "completed":
                    with fake.lock:
                        fake.history[resp_id] = (session_key, model, turns)
                self._send_json(200, doc)
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.close_connection = True
            try:
                self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
                created_doc = _sample_response_doc(
                    resp_id=resp_id,
                    model=model,
                    status="in_progress",
                    previous_response_id=prev_id,
                    output_text="",
                    session_key=session_key,
                    input_tokens=0,
                    output_tokens=0,
                    api_version=fake.response_api_version,
                )
                self._send_sse("response.created", {"type": "response.created", "response": created_doc})

                emitted_text: list[str] = []
                for idx, tok in enumerate(fake.stream_tokens):
                    if fake.stream_error_after is not None and idx == fake.stream_error_after:
                        self._send_sse(
                            "error",
                            {
                                "error": {
                                    "code": "backend_unavailable",
                                    "message": "backend crashed mid-stream",
                                    "param": None,
                                },
                                "sonder": {"api_version": API_VERSION},
                            },
                        )
                        return
                    ev_payload: dict[str, object] = {
                        "type": "response.output_text.delta",
                        "item_id": f"msg_{resp_id}",
                        "output_index": 0,
                        "content_index": 0,
                        "delta": tok,
                    }
                    if fake.stream_logprobs is not None and idx < len(fake.stream_logprobs):
                        ev_payload["logprobs"] = [fake.stream_logprobs[idx]]
                    self._send_sse("response.output_text.delta", ev_payload)
                    emitted_text.append(tok)
                    if idx == 0:
                        fake.first_delta_sent.set()
                        if fake.hold_after_first_delta is not None:
                            fake.hold_after_first_delta.wait(timeout=10.0)

                fcs = []
                if fake.stream_function_call is not None:
                    call_id, fn_name, arg_parts = fake.stream_function_call
                    for piece in arg_parts:
                        self._send_sse(
                            "response.function_call_arguments.delta",
                            {
                                "type": "response.function_call_arguments.delta",
                                "item_id": f"fc_{resp_id}_0",
                                "output_index": 1 if emitted_text else 0,
                                "call_id": call_id,
                                "name": fn_name,
                                "delta": piece,
                            },
                        )
                    fcs.append({
                        "call_id": call_id,
                        "name": fn_name,
                        "arguments": "".join(arg_parts),
                    })

                if fake.stream_truncate_before_completed:
                    return

                final_status = "incomplete" if fake.stream_incomplete_status else "completed"
                completed_doc = _sample_response_doc(
                    resp_id=resp_id,
                    model=model,
                    status=final_status,
                    previous_response_id=prev_id,
                    output_text="".join(emitted_text),
                    reasoning=fake.response_reasoning,
                    function_calls=fcs or None,
                    session_key=session_key,
                    input_tokens=in_tok,
                    output_tokens=len(emitted_text),
                    cached_tokens=cached,
                    api_version=fake.response_api_version,
                    warnings=fake.response_warnings or None,
                )
                if payload.get("store", True) and final_status == "completed":
                    with fake.lock:
                        fake.history[resp_id] = (session_key, model, turns)
                self._send_sse(
                    "response.completed",
                    {"type": "response.completed", "response": completed_doc},
                )
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                fake.client_disconnected.set()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    scheme = "http"
    if ssl_context is not None:
        httpd.socket = ssl_context.wrap_socket(httpd.socket, server_side=True)
        scheme = "https"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"{scheme}://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5.0)


def _generate_ca_and_server_certs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """Build a test CA cert, a server cert signed by it for 127.0.0.1, and an unrelated CA."""
    x509 = pytest.importorskip("cryptography.x509")
    hashes = pytest.importorskip("cryptography.hazmat.primitives.hashes")
    serialization = pytest.importorskip("cryptography.hazmat.primitives.serialization")
    rsa = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.rsa")
    oid = pytest.importorskip("cryptography.x509.oid")

    now = dt.datetime.now(dt.timezone.utc)

    def _make_ca(common_name: str) -> tuple[object, object, object]:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(oid.NameOID.COMMON_NAME, common_name)])
        ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=False,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(ski, critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ski),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        return key, cert, ski

    ca_key, ca_cert, ca_ski = _make_ca("Sonder Test Root CA")
    other_key, other_ca_cert, _other_ski = _make_ca("Unrelated Root CA")
    _ = other_key

    srv_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    srv_name = x509.Name([x509.NameAttribute(oid.NameOID.COMMON_NAME, "127.0.0.1")])
    srv_cert = (
        x509.CertificateBuilder()
        .subject_name(srv_name)
        .issuer_name(ca_cert.subject)
        .public_key(srv_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(srv_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski),
            critical=False,
        )
        .add_extension(
            x509.SubjectAlternativeName([
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                x509.DNSName("localhost"),
            ]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    ca_pem = tmp_path / "ca.pem"
    other_ca_pem = tmp_path / "other-ca.pem"
    srv_cert_pem = tmp_path / "server-cert.pem"
    srv_key_pem = tmp_path / "server-key.pem"

    ca_pem.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    other_ca_pem.write_bytes(other_ca_cert.public_bytes(serialization.Encoding.PEM))
    srv_cert_pem.write_bytes(srv_cert.public_bytes(serialization.Encoding.PEM))
    srv_key_pem.write_bytes(
        srv_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return ca_pem, other_ca_pem, srv_cert_pem, srv_key_pem


# -- URL & policy validation --------------------------------------------------


def test_normalize_base_url_loopback_and_bind_rewrites() -> None:
    assert normalize_responses_base_url("http://127.0.0.1:11437/") == "http://127.0.0.1:11437"
    assert normalize_responses_base_url("http://0.0.0.0:11437") == "http://127.0.0.1:11437"
    assert normalize_responses_base_url("http://[::]:11437") == "http://[::1]:11437"
    assert normalize_responses_base_url("http://LOCALHOST:11437/sub/") == "http://localhost:11437/sub"
    assert is_loopback_url("http://127.0.0.1:11437") is True
    assert is_loopback_url("http://localhost:11437") is True
    assert is_loopback_url("http://[::1]:11437") is True
    assert is_loopback_url("https://10.77.0.2:8443") is False


@pytest.mark.parametrize("bad_url", [
    "",
    "ftp://127.0.0.1:11437",
    "file:///etc/passwd",
    "http://user:secret@127.0.0.1:11437",
    "http://127.0.0.1:11437?token=abc",
    "http://127.0.0.1:11437#frag",
    "http://127.0.0.2:11437",
    "http://127.0.0.1%20:11437",
    "http://127.0.0.1:11437/bad\r\npath",
])
def test_normalize_base_url_rejects_unsafe_inputs(bad_url: str) -> None:
    with pytest.raises(InvalidInput):
        normalize_responses_base_url(bad_url)


def test_private_worker_url_and_policy(tmp_path: Path) -> None:
    ca_file = tmp_path / "worker-ca.pem"
    ca_file.write_text("-----BEGIN CERTIFICATE-----\n-----END CERTIFICATE-----\n", encoding="utf-8")

    assert validate_private_worker_url("https://10.77.0.2:8443/") == "https://10.77.0.2:8443"
    assert validate_private_worker_url("https://[fd00::2]:8443") == "https://[fd00::2]:8443"

    for bad_worker in (
        "http://10.77.0.2:8443",
        "https://node1.internal:8443",
        "https://8.8.8.8:8443",
        "https://127.0.0.1:8443",
        "https://10.77.0.2",
        "https://224.0.0.1:8443",
    ):
        with pytest.raises(InvalidInput):
            validate_private_worker_url(bad_worker)

    # Private worker requires absolute existing CA bundle at construction.
    with pytest.raises(InvalidInput, match="ca_bundle"):
        SonderResponsesConfig(
            base_url="https://10.77.0.2:8443",
            private_worker=True,
            allow_remote=True,
            api_key="tok-1234567890abcdef",
            ca_bundle="",
        )

    # Private worker requires allow_remote + api_key, but NOT cloud_allowed.
    cfg_no_optin = SonderResponsesConfig(
        base_url="https://10.77.0.2:8443",
        private_worker=True,
        allow_remote=False,
        api_key="tok-1234567890abcdef",
        ca_bundle=str(ca_file),
    )
    with pytest.raises(Forbidden, match="allow_remote"):
        validate_endpoint_policy(cfg_no_optin)

    cfg_ok = SonderResponsesConfig(
        base_url="https://10.77.0.2:8443",
        private_worker=True,
        allow_remote=True,
        api_key="tok-1234567890abcdef",
        ca_bundle=str(ca_file),
    )
    ctx_no_cloud = local_owner_context(correlation_id="run-1", cloud_allowed=False)
    validate_endpoint_policy(cfg_ok, context=ctx_no_cloud)

    # Deleting the CA file after config construction still fails closed before send.
    ca_file.unlink()
    with pytest.raises(Forbidden, match="CA bundle"):
        validate_endpoint_policy(cfg_ok, context=ctx_no_cloud)


def test_remote_non_worker_requires_https_token_optin_and_cloud_consent() -> None:
    cfg = SonderResponsesConfig(
        base_url="https://10.77.0.9:8443",
        allow_remote=True,
        api_key="secret-token",
    )
    with pytest.raises(Forbidden, match="does not allow prompts to leave"):
        validate_endpoint_policy(cfg, context=None)
    with pytest.raises(Forbidden, match="does not allow prompts to leave"):
        validate_endpoint_policy(
            cfg,
            context=local_owner_context(correlation_id="run-1", cloud_allowed=False),
        )
    validate_endpoint_policy(
        cfg,
        context=local_owner_context(correlation_id="run-1", cloud_allowed=True),
    )

    plain_remote = SonderResponsesConfig(
        base_url="http://10.77.0.9:8443",
        allow_remote=True,
        api_key="secret-token",
    )
    with pytest.raises(Forbidden, match="https://"):
        validate_endpoint_policy(
            plain_remote,
            context=local_owner_context(correlation_id="run-1", cloud_allowed=True),
        )


def test_config_rejects_control_chars_and_invalid_bounds() -> None:
    with pytest.raises(InvalidInput, match="api_key"):
        SonderResponsesConfig(api_key="bad\r\nX-Injected: 1")
    with pytest.raises(InvalidInput, match="timeout_seconds"):
        SonderResponsesConfig(timeout_seconds=0.0)
    with pytest.raises(InvalidInput, match="timeout_seconds"):
        SonderResponsesConfig(timeout_seconds=float("nan"))
    with pytest.raises(InvalidInput, match="max_response_bytes"):
        SonderResponsesConfig(max_response_bytes=128)


# -- Request schema & header contract validation ------------------------------


def test_build_request_payload_full_contract() -> None:
    req = ResponsesRequest(
        model="qwen3:27b",
        instructions="Be concise.",
        previous_response_id="resp_req-0",
        input=[
            ResponsesMessageInput(
                role="user",
                content=[
                    ResponsesTextPart("Inspect this image"),
                    ResponsesImagePart.from_bytes(base64.b64decode(TINY_PNG_B64), "image/png"),
                ],
            ),
            ResponsesFunctionCallInput(call_id="call_1", name="search", arguments='{"q":"kv"}'),
            ResponsesFunctionCallOutputInput(call_id="call_1", output='{"hits":2}'),
        ],
        tools=[
            ResponsesToolDefinition(
                name="search",
                description="Search docs",
                parameters={"type": "object", "properties": {"q": {"type": "string"}}},
            ),
            {
                "type": "function",
                "function": {
                    "name": "calc",
                    "description": "Compute",
                    "parameters": {"type": "object", "properties": {"x": {"type": "integer"}}},
                },
            },
        ],
        tool_choice=ResponsesNamedToolChoice("search"),
        text_format=ResponsesTextFormat(
            format_type="json_schema",
            name="summary",
            schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
            strict=True,
        ),
        reasoning_effort="medium",
        temperature=0.2,
        top_p=0.9,
        max_output_tokens=256,
        seed=42,
        logprobs=True,
        top_logprobs=4,
        stream=True,
        store=False,
        prompt_cache_key="session-abc",
        priority="subagent",
        deadline_ms=15_000,
    )
    payload = build_request_payload(req)
    assert payload["model"] == "qwen3:27b"
    assert payload["instructions"] == "Be concise."
    assert payload["previous_response_id"] == "resp_req-0"
    assert payload["stream"] is True
    assert payload["store"] is False
    assert payload["prompt_cache_key"] == "session-abc"
    assert payload["priority"] == "subagent"
    assert payload["deadline_ms"] == 15_000
    assert payload["reasoning"] == {"effort": "medium"}
    assert payload["temperature"] == 0.2
    assert payload["top_p"] == 0.9
    assert payload["max_output_tokens"] == 256
    assert payload["seed"] == 42
    assert payload["logprobs"] is True
    assert payload["top_logprobs"] == 4
    assert payload["include"] == ["message.output_text.logprobs"]
    assert payload["tool_choice"] == {"type": "function", "name": "search"}
    tools = payload["tools"]
    assert isinstance(tools, list) and len(tools) == 2
    assert tools[0]["name"] == "search"
    assert tools[1]["name"] == "calc" and "function" not in tools[1]
    text_obj = payload["text"]
    assert isinstance(text_obj, dict)
    assert text_obj["format"]["type"] == "json_schema"


def test_request_validation_catches_ssrf_tools_logprobs_and_numeric_priority() -> None:
    # SSRF & bad base64 image rejection
    for bad_uri in (
        "http://127.0.0.1:8080/a.png",
        "https://attacker.example/a.png",
        "file:///C:/Windows/win.ini",
        "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=",
        "data:image/png;base64,!!!invalid!!!",
        "data:image/png;base64,AAB=",  # non-canonical trailing padding bits
    ):
        with pytest.raises(InvalidInput):
            validate_data_image_uri(bad_uri)

    # Exceeding MAX_IMAGES_PER_REQUEST
    too_many_imgs = [ResponsesImagePart(TINY_PNG_URI) for _ in range(MAX_IMAGES_PER_REQUEST + 1)]
    with pytest.raises(InvalidInput, match="maximum"):
        build_request_payload(
            ResponsesRequest(input=[ResponsesMessageInput(role="user", content=too_many_imgs)])
        )

    # Duplicate tools / unknown tool_choice / required with no tools
    with pytest.raises(InvalidInput, match="duplicate"):
        build_request_payload(
            ResponsesRequest(
                input="hi",
                tools=[ResponsesToolDefinition("dup"), ResponsesToolDefinition("dup")],
            )
        )
    with pytest.raises(InvalidInput, match="requires at least one tool"):
        build_request_payload(ResponsesRequest(input="hi", tool_choice="required"))
    with pytest.raises(InvalidInput, match="not defined in tools"):
        build_request_payload(
            ResponsesRequest(
                input="hi",
                tools=[ResponsesToolDefinition("search")],
                tool_choice=ResponsesNamedToolChoice("missing"),
            )
        )

    # Logprobs validation
    with pytest.raises(InvalidInput, match="top_logprobs"):
        build_request_payload(ResponsesRequest(input="hi", top_logprobs=3))
    with pytest.raises(InvalidInput, match="top_logprobs"):
        build_request_payload(ResponsesRequest(input="hi", logprobs=True, top_logprobs=21))
    with pytest.raises(InvalidInput, match="max_output_tokens"):
        build_request_payload(ResponsesRequest(input="hi", max_output_tokens=True))  # type: ignore[arg-type]

    # Numeric priority goes to header ONLY, never into body "priority" (server rejects int body priority)
    num_req = ResponsesRequest(input="hi", priority=-4, workload="critic_verification")
    body = build_request_payload(num_req)
    assert "priority" not in body
    headers = build_request_headers(num_req, config=SonderResponsesConfig())
    assert headers["X-Sonder-Priority"] == "-4"
    assert headers["X-Sonder-Workload"] == "critic_verification"

    # Invalid priority / workload / correlation headers
    with pytest.raises(InvalidInput, match="X-Sonder-Priority"):
        ResponsesCorrelationHeaders(priority=17)
    with pytest.raises(InvalidInput, match="X-Sonder-Priority"):
        ResponsesCorrelationHeaders(priority="urgent")
    with pytest.raises(InvalidInput, match="X-Sonder-Workload"):
        ResponsesCorrelationHeaders(workload="unknown_class")
    with pytest.raises(InvalidInput, match="X-Sonder-Run-Id"):
        ResponsesCorrelationHeaders(run_id="bad id with spaces")


def test_headers_derive_from_operation_context_with_explicit_overrides() -> None:
    cfg = SonderResponsesConfig(api_key="tok-abc")
    worker_ctx = local_owner_context(
        correlation_id="run-99",
        source="worker",
        session_id="worker-sess-1",
    )
    req = ResponsesRequest(input="hi", stream=True)
    headers = build_request_headers(req, config=cfg, context=worker_ctx)
    assert headers["Authorization"] == "Bearer tok-abc"
    assert headers["Accept"] == "text/event-stream"
    assert headers["X-Sonder-Run-Id"] == "run-99"
    assert headers["X-Sonder-Parent-Request-Id"] == "run-99"
    assert headers["X-Sonder-Agent-Id"] == "worker-sess-1"
    assert headers["X-Sonder-Workload"] == "implementation_worker"
    assert headers["X-Sonder-Priority"] == "subagent"

    # Request overrides win over context defaults
    override_req = ResponsesRequest(
        input="hi",
        priority=8,
        workload="owner_orchestrator",
        deadline_ms=5000,
        reasoning_budget=1024,
    )
    headers2 = build_request_headers(override_req, config=cfg, context=worker_ctx)
    assert headers2["X-Sonder-Priority"] == "8"
    assert headers2["X-Sonder-Workload"] == "owner_orchestrator"
    assert headers2["X-Sonder-Deadline-Ms"] == "5000"
    assert headers2["X-Sonder-Reasoning-Budget"] == "1024"


# -- Response & logprobs parsing ----------------------------------------------


def test_parse_response_document_extracts_reasoning_tools_logprobs_and_metadata() -> None:
    lp_list = [
        {
            "token": "OK",
            "logprob": -0.05,
            "bytes": [79, 75],
            "top_logprobs": [
                {"token": "OK", "logprob": -0.05, "bytes": [79, 75]},
                {"token": "Yes", "logprob": -3.1, "bytes": [89, 101, 115]},
            ],
        }
    ]
    doc = _sample_response_doc(
        output_text="OK",
        reasoning="step-by-step thought",
        function_calls=[{"call_id": "call_7", "name": "lookup", "arguments": '{"id": 7}'}],
        cached_tokens=11,
        logprobs=lp_list,
        warnings=["pin_override"],
    )
    result = parse_response_document(doc)
    assert result.id == "resp_req-1"
    assert result.output_text == "OK"
    assert result.reasoning == "step-by-step thought"
    assert result.has_function_calls is True
    assert result.function_calls[0].call_id == "call_7"
    assert result.function_calls[0].parsed_arguments() == {"id": 7}
    assert result.usage.input_tokens == 14
    assert result.usage.output_tokens == 6
    assert result.usage.total_tokens == 20
    assert result.usage.cached_tokens == 11
    assert len(result.logprobs) == 1
    assert result.logprobs[0].token == "OK"
    assert result.logprobs[0].bytes == (79, 75)
    assert len(result.logprobs[0].top_logprobs) == 2
    assert result.sonder.api_version == 1
    assert result.sonder.session_key == "sess-key-1"
    assert result.sonder.warnings == ("pin_override",)

    # Top-level logprobs.content fallback also parses cleanly
    doc_top = _sample_response_doc(top_level_logprobs={"content": lp_list})
    assert parse_response_document(doc_top).logprobs[0].token == "OK"

    # Incompatible sonder.api_version fails loudly
    with pytest.raises(DependencyUnavailable, match="incompatible sonder-inference API version"):
        parse_response_document(_sample_response_doc(api_version=2))


# -- End-to-end loopback HTTP tests -------------------------------------------


def test_stateful_previous_response_id_chaining_and_tool_outputs() -> None:
    fake = FakeResponsesServer()
    fake.response_reasoning = "thinking..."
    with _serve_fake(fake) as base_url:
        client = SonderResponsesClient(SonderResponsesConfig(base_url=base_url))
        session = client.start_session(
            instructions="You are terse.",
            tools=[ResponsesToolDefinition("search", "Search", {"type": "object"})],
            priority="interactive",
            workload="interactive_user",
        )
        # Turn 1: model emits a function call
        fake.stream_function_call = ("call_abc", "search", ['{"q":"bonsai"}'])
        turn1 = session.send("Find bonsai")
        assert turn1.has_function_calls
        assert turn1.function_calls[0].call_id == "call_abc"
        assert turn1.function_calls[0].parsed_arguments() == {"q": "bonsai"}
        assert turn1.reasoning == "thinking..."
        assert session.last_response_id == turn1.id
        assert session.session_key == turn1.sonder.session_key

        # Turn 2: submit function_call_output chained via previous_response_id
        fake.stream_function_call = None
        turn2 = session.submit_tool_outputs([("call_abc", '{"found": true}')])
        assert turn2.previous_response_id == turn1.id
        assert turn2.sonder.session_key == turn1.sonder.session_key
        assert turn2.usage.cached_tokens == 10
        assert session.last_response_id == turn2.id
        assert len(session.history) == 2

    assert len(fake.requests) == 2
    path1, hdrs1, body1 = fake.requests[0]
    path2, hdrs2, body2 = fake.requests[1]
    assert path1 == "/v1/responses" and path2 == "/v1/responses"
    assert hdrs1["x-sonder-priority"] == "interactive"
    assert hdrs1["x-sonder-workload"] == "interactive_user"
    assert body1["instructions"] == "You are terse."
    assert "previous_response_id" not in body1
    assert body2["previous_response_id"] == turn1.id
    assert "instructions" not in body2
    assert body2["input"] == [
        {"type": "function_call_output", "call_id": "call_abc", "output": '{"found": true}'}
    ]


def test_streaming_incremental_deltas_tools_and_logprobs() -> None:
    hold = threading.Event()
    fake = FakeResponsesServer()
    fake.stream_tokens = ["Alpha", " ", "Beta"]
    fake.stream_function_call = ("call_s1", "lookup", ['{"k":', '"v"}'])
    fake.stream_logprobs = [
        {"token": "Alpha", "logprob": -0.1, "bytes": [65], "top_logprobs": []},
        {"token": " ", "logprob": -0.2, "bytes": [32], "top_logprobs": []},
        {"token": "Beta", "logprob": -0.3, "bytes": [66], "top_logprobs": []},
    ]
    fake.hold_after_first_delta = hold

    seen_deltas: list[str] = []
    seen_fc_deltas: list[str] = []
    seen_event_types: list[str] = []

    def on_text(piece: str) -> None:
        seen_deltas.append(piece)
        if len(seen_deltas) == 1:
            # Server is still blocked on `hold` -> proves incremental delivery!
            assert len(seen_deltas) == 1
            hold.set()

    def on_fc(ev: ResponsesStreamEvent) -> None:
        seen_fc_deltas.append(ev.delta)

    with _serve_fake(fake) as base_url:
        client = SonderResponsesClient(SonderResponsesConfig(base_url=base_url))
        result = client.create(
            ResponsesRequest(
                input="Stream please",
                tools=[ResponsesToolDefinition("lookup")],
                logprobs=True,
                top_logprobs=2,
                stream=True,
            ),
            on_event=lambda ev: seen_event_types.append(ev.event_type),
            on_text_delta=on_text,
            on_function_call_delta=on_fc,
        )

    assert seen_deltas == ["Alpha", " ", "Beta"]
    assert seen_fc_deltas == ['{"k":', '"v"}']
    assert seen_event_types[0] == "response.created"
    assert seen_event_types[-1] == "response.completed"
    assert result.output_text == "Alpha Beta"
    assert result.function_calls[0].parsed_arguments() == {"k": "v"}
    # Streamed delta logprobs were preserved onto the returned ResponsesResult
    assert [lp.token for lp in result.logprobs] == ["Alpha", " ", "Beta"]


def test_stream_non_sse_json_fallback_and_incomplete_session_handling() -> None:
    fake = FakeResponsesServer()
    fake.non_sse_on_stream = True
    fake.stream_incomplete_status = True
    with _serve_fake(fake) as base_url:
        client = SonderResponsesClient(SonderResponsesConfig(base_url=base_url))
        session = client.start_session()
        res = session.send("hi", stream=True)
    assert res.is_incomplete is True
    # Incomplete turns must not overwrite last_response_id (server didn't store it)
    assert session.last_response_id is None


# -- Error classification, redirects, bounds, timeouts, cancellation ----------


def test_http_error_classification_and_previous_response_not_found() -> None:
    fake = FakeResponsesServer()
    with _serve_fake(fake) as base_url:
        client = SonderResponsesClient(SonderResponsesConfig(base_url=base_url))

        # 404 unknown previous_response_id -> PreviousResponseNotFound (subclass of NotFound)
        with pytest.raises(PreviousResponseNotFound) as exc_info:
            client.create(ResponsesRequest(input="hi", previous_response_id="resp_missing"))
        assert exc_info.value.previous_response_id == "resp_missing"
        assert isinstance(exc_info.value, NotFound)

        # 404 model_not_found -> NotFound (not PreviousResponseNotFound)
        fake.force_status = 404
        fake.force_error_body = {
            "error": {"code": "model_not_found", "message": "no such model", "param": "model"},
            "sonder": {"api_version": 1},
        }
        with pytest.raises(NotFound) as mf_exc:
            client.create(ResponsesRequest(input="hi", model="ghost"))
        assert not isinstance(mf_exc.value, PreviousResponseNotFound)

        # 404 missing /v1/responses route on older server -> plain NotFound even with previous_response_id
        fake.force_status = 404
        fake.force_error_body = {
            "error": {"code": "not_found", "message": "no route for /v1/responses", "param": None},
            "sonder": {"api_version": 1},
        }
        with pytest.raises(NotFound) as route_exc:
            client.create(ResponsesRequest(input="hi", previous_response_id="resp_1"))
        assert not isinstance(route_exc.value, PreviousResponseNotFound)

        # 401 -> Forbidden
        fake.force_status = 401
        fake.force_error_body = {"error": {"code": "unauthorized", "message": "bad token"}}
        with pytest.raises(Forbidden, match="credentials"):
            client.create(ResponsesRequest(input="hi"))

        # 403 forbidden_host -> Forbidden naming Host
        fake.force_status = 403
        fake.force_error_body = {"error": {"code": "forbidden_host", "message": "bad host"}}
        with pytest.raises(Forbidden, match="Host"):
            client.create(ResponsesRequest(input="hi"))

        # 429 and 503 overloaded -> CapacityExceeded
        fake.force_status = 429
        fake.force_error_body = {"error": {"code": "overloaded", "message": "queue full"}}
        with pytest.raises(CapacityExceeded):
            client.create(ResponsesRequest(input="hi"))
        fake.force_status = 503
        with pytest.raises(CapacityExceeded):
            client.create(ResponsesRequest(input="hi"))

        # 504 deadline_exceeded -> DeadlineExceeded
        fake.force_status = 504
        fake.force_error_body = {"error": {"code": "deadline_exceeded", "message": "expired"}}
        with pytest.raises(DeadlineExceeded):
            client.create(ResponsesRequest(input="hi"))

        # 400 -> InvalidInput
        fake.force_status = 400
        fake.force_error_body = {"error": {"code": "invalid_request", "message": "bad input"}}
        with pytest.raises(InvalidInput):
            client.create(ResponsesRequest(input="hi"))

        # 503 backend_unavailable -> DependencyUnavailable
        fake.force_status = 503
        fake.force_error_body = {"error": {"code": "backend_unavailable", "message": "failed"}}
        with pytest.raises(DependencyUnavailable):
            client.create(ResponsesRequest(input="hi"))


def test_redirects_are_never_followed() -> None:
    fake = FakeResponsesServer()
    fake.redirect_location = "http://127.0.0.1:9/should-never-be-followed"
    with _serve_fake(fake) as base_url:
        client = SonderResponsesClient(SonderResponsesConfig(base_url=base_url))
        with pytest.raises(DependencyUnavailable, match="HTTP 302"):
            client.create(ResponsesRequest(input="hi"))


def test_stream_midstream_error_and_early_eof() -> None:
    fake = FakeResponsesServer()
    fake.stream_error_after = 1
    with _serve_fake(fake) as base_url:
        client = SonderResponsesClient(SonderResponsesConfig(base_url=base_url))
        with pytest.raises(DependencyUnavailable, match="backend_unavailable"):
            client.create(ResponsesRequest(input="hi", stream=True))

    fake2 = FakeResponsesServer()
    fake2.stream_truncate_before_completed = True
    with _serve_fake(fake2) as base_url:
        client = SonderResponsesClient(SonderResponsesConfig(base_url=base_url))
        with pytest.raises(DependencyUnavailable, match="ended before response.completed"):
            client.create(ResponsesRequest(input="hi", stream=True))


def test_bounded_reads_for_json_sse_stream_and_sse_line() -> None:
    # 1. Oversized non-streaming JSON body
    fake = FakeResponsesServer()
    fake.raw_response_bytes = b'{"pad":"' + (b"x" * 4096) + b'"}'
    with _serve_fake(fake) as base_url:
        client = SonderResponsesClient(
            SonderResponsesConfig(base_url=base_url, max_response_bytes=1024)
        )
        with pytest.raises(DependencyUnavailable, match="exceeds 1024 bytes"):
            client.create(ResponsesRequest(input="hi"))

    # 2. Oversized SSE stream total bytes
    sse_blob = (
        b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"'
        + (b"a" * 280)
        + b'"}\n\n'
    )
    with pytest.raises(DependencyUnavailable, match="exceeds 1024 bytes"):
        list(iter_sse_events(io.BytesIO(sse_blob * 6), max_bytes=1024, max_line_bytes=1024))

    # 3. Oversized single SSE line
    long_line = b"data: " + (b"z" * 1200) + b"\n\n"
    with pytest.raises(DependencyUnavailable, match="SSE line exceeds 1024 bytes"):
        list(iter_sse_events(io.BytesIO(long_line), max_bytes=8192, max_line_bytes=1024))


def test_timeout_budget_and_expired_context_raise_deadline_exceeded() -> None:
    fake = FakeResponsesServer()
    fake.delay_before_headers = 1.5
    with _serve_fake(fake) as base_url:
        client = SonderResponsesClient(
            SonderResponsesConfig(base_url=base_url, timeout_seconds=0.15)
        )
        with pytest.raises(DeadlineExceeded):
            client.create(ResponsesRequest(input="slow"))

        # Already-expired OperationContext fails before any socket is opened
        fake.requests.clear()
        expired_ctx = local_owner_context(correlation_id="exp-1", timeout_seconds=0.0)
        with pytest.raises(DeadlineExceeded):
            client.create(ResponsesRequest(input="never-sent"), context=expired_ctx)
        assert fake.requests == []


def test_cancellation_before_send_midstream_and_via_callback() -> None:
    fake = FakeResponsesServer()
    with _serve_fake(fake) as base_url:
        client = SonderResponsesClient(
            SonderResponsesConfig(base_url=base_url, timeout_seconds=30.0)
        )

        # 1. Pre-send cancel sends zero requests
        already_cancelled = threading.Event()
        already_cancelled.set()
        with pytest.raises(Cancelled):
            client.create(ResponsesRequest(input="hi"), cancellation=already_cancelled)
        assert fake.requests == []

        # 2. Mid-stream cancel unblocks held stream promptly without waiting for 30s timeout
        hold = threading.Event()
        fake.hold_after_first_delta = hold
        cancel_ev = threading.Event()

        def trigger_cancel() -> None:
            fake.first_delta_sent.wait(timeout=5.0)
            cancel_ev.set()

        t = threading.Thread(target=trigger_cancel, daemon=True)
        t.start()
        started = time.monotonic()
        try:
            with pytest.raises(Cancelled):
                client.create(ResponsesRequest(input="hi", stream=True), cancellation=cancel_ev)
        finally:
            hold.set()
            t.join(timeout=5.0)
        assert time.monotonic() - started < 5.0

        # 3. Callback returning False cancels cleanly
        fake.hold_after_first_delta = None
        fake.first_delta_sent.clear()
        with pytest.raises(Cancelled, match="callback"):
            client.create(
                ResponsesRequest(input="hi", stream=True),
                on_text_delta=lambda _piece: False,
            )


# -- Real CA-verified HTTPS & trust_scope tests -------------------------------


def _make_server_ssl_context(certfile: Path, keyfile: Path) -> ssl.SSLContext:
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile=str(certfile), keyfile=str(keyfile))
    return ctx


def test_ca_verified_https_succeeds_with_pinned_ca_and_rejects_untrusted(tmp_path: Path) -> None:
    ca_pem, other_ca_pem, srv_cert_pem, srv_key_pem = _generate_ca_and_server_certs(tmp_path)
    server_ctx = _make_server_ssl_context(srv_cert_pem, srv_key_pem)

    fake = FakeResponsesServer()
    with _serve_fake(fake, ssl_context=server_ctx) as https_url:
        # 1. Trusted pinned CA bundle succeeds (both non-streaming and streaming)
        trusted_cfg = SonderResponsesConfig(
            base_url=https_url,
            ca_bundle=str(ca_pem),
            api_key="tls-secret-token",
        )
        trusted_client = SonderResponsesClient(trusted_cfg)
        res = trusted_client.create(ResponsesRequest(input="hello over tls"))
        assert res.output_text == "turn-1"
        res_stream = trusted_client.create(ResponsesRequest(input="stream over tls", stream=True))
        assert res_stream.output_text == "Hello stream!"

        # 2. Wrong CA bundle strictly fails TLS verification (never falls back to system store)
        wrong_ca_client = SonderResponsesClient(
            SonderResponsesConfig(base_url=https_url, ca_bundle=str(other_ca_pem))
        )
        with pytest.raises(DependencyUnavailable, match="TLS verification failed"):
            wrong_ca_client.create(ResponsesRequest(input="should fail"))

        # 3. No CA bundle (system store) rejects the test self-signed CA
        system_ca_client = SonderResponsesClient(SonderResponsesConfig(base_url=https_url))
        with pytest.raises(DependencyUnavailable, match="TLS verification failed"):
            system_ca_client.create(ResponsesRequest(input="should also fail"))

        # 4. Cert chain satisfies OpenSSL 3 VERIFY_X509_STRICT (Python 3.13+ default)
        strict_ctx = ssl.create_default_context(cafile=str(ca_pem))
        strict_ctx.verify_flags |= ssl.VERIFY_X509_STRICT
        strict_client = SonderResponsesClient(
            trusted_cfg,
            opener_factory=lambda _budget: urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                urllib.request.HTTPSHandler(context=strict_ctx),
            ),
        )
        res_strict = strict_client.create(ResponsesRequest(input="strict x509 over tls"))
        assert res_strict.output_text == "turn-1"


def test_private_worker_binds_pinned_ca_trust_scope(tmp_path: Path) -> None:
    ca_file = tmp_path / "node1-ca.pem"
    ca_file.write_text("-----BEGIN CERTIFICATE-----\n-----END CERTIFICATE-----\n", encoding="utf-8")
    observed_ca: list[str] = []

    class _FakeResp:
        status = 200
        headers = {"Content-Type": "application/json"}

        def __init__(self, raw: bytes) -> None:
            self._buf = io.BytesIO(raw)

        def read(self, size: int = -1) -> bytes:
            return self._buf.read(size)

        def readline(self, size: int = -1) -> bytes:
            return self._buf.readline(size)

        def close(self) -> None:
            self._buf.close()

        def __enter__(self) -> "_FakeResp":
            return self

        def __exit__(self, *args: object) -> None:
            self.close()

    class _FakeOpener(urllib.request.OpenerDirector):
        def open(self, fullurl: object, data: object = None, timeout: object = None) -> object:  # type: ignore[override]
            observed_ca.append(str(getattr(exchange_transport._TRUST, "ca_bundle", "")))
            return _FakeResp(json.dumps(_sample_response_doc(output_text="private-ok")).encode("utf-8"))

    cfg = SonderResponsesConfig(
        base_url="https://10.77.0.2:8443",
        private_worker=True,
        allow_remote=True,
        api_key="node1-secret-token",
        ca_bundle=str(ca_file),
    )
    client = SonderResponsesClient(cfg, opener_factory=lambda _budget: _FakeOpener())
    result = client.create(
        ResponsesRequest(input="ping worker", priority="subagent"),
        context=local_owner_context(correlation_id="priv-1", cloud_allowed=False),
    )
    assert result.output_text == "private-ok"
    assert observed_ca == [str(ca_file)]
    # Trust scope restored cleanly after exchange
    assert getattr(exchange_transport._TRUST, "ca_bundle", "") == ""


def test_stream_does_not_leak_trust_scope_across_yield_or_callbacks(tmp_path: Path) -> None:
    ca_pem, _other_ca, srv_cert_pem, srv_key_pem = _generate_ca_and_server_certs(tmp_path)
    server_ctx = _make_server_ssl_context(srv_cert_pem, srv_key_pem)

    fake = FakeResponsesServer()
    fake.stream_tokens = ["one", "two"]
    with _serve_fake(fake, ssl_context=server_ctx) as https_url:
        client = SonderResponsesClient(
            SonderResponsesConfig(base_url=https_url, ca_bundle=str(ca_pem))
        )

        # 1. During generator iteration (between next() calls), _TRUST.ca_bundle must already be restored.
        gen = client.stream(ResponsesRequest(input="hi", stream=True))
        try:
            first_event = next(gen)
            assert first_event.event_type == "response.created"
            assert getattr(exchange_transport._TRUST, "ca_bundle", "") == ""
            second_event = next(gen)
            assert second_event.event_type == "response.output_text.delta"
            assert getattr(exchange_transport._TRUST, "ca_bundle", "") == ""
        finally:
            gen.close()

        # 2. Inside streaming callbacks (on_event / on_text_delta), _TRUST.ca_bundle must be restored.
        callback_trust: list[str] = []

        def on_delta(_piece: str) -> None:
            callback_trust.append(str(getattr(exchange_transport._TRUST, "ca_bundle", "")))

        res = client.create(ResponsesRequest(input="hi", stream=True), on_text_delta=on_delta)
        assert res.output_text == "onetwo"
        assert callback_trust == ["", ""]


def test_validate_data_image_uri_rejects_oversized_base64_before_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("base64.b64decode must not be called on oversized base64 payload")

    monkeypatch.setattr(base64, "b64decode", _fail_if_called)
    oversized_uri = "data:image/png;base64," + ("A" * (MAX_IMAGE_BASE64_CHARS + 4))
    with pytest.raises(InvalidInput, match="maximum encoded size"):
        validate_data_image_uri(oversized_uri)
