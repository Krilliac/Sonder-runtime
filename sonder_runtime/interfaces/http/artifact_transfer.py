"""Bounded production wire boundary for the explicitly configured receiver."""
import ipaddress
import re
import ssl
import threading

from sonder_runtime.application.errors import DependencyUnavailable, Unauthenticated

RAW_LIMIT = 1024 * 1024
CONTROL_LIMIT = 32 * 1024
_ID = r"[0-9a-f]{32}"
# Admission precedes body allocation and lasts through delivery. This does not
# bound the listener's connection threads or non-transfer HTTP operations.
_REQUEST_SLOTS = threading.BoundedSemaphore(8)


class BindingError(RuntimeError):
    """Internal wire rejection; host failures use application error taxonomy."""

    def __init__(self, status_code, code):
        super().__init__(code)
        self.status_code = status_code


def is_artifact_route(path):
    return isinstance(path, str) and path.startswith(("/v1/artifact-transfers", "/v1/artifacts"))


def transport_allowed(connection, peer, config):
    """Forwarded request headers are deliberately not inputs to this decision."""
    if isinstance(connection, ssl.SSLSocket):
        return True
    try:
        address = ipaddress.ip_address(peer)
        if address.is_loopback:
            return True
        return config.server.tls_terminated_by_proxy and any(
            address in ipaddress.ip_network(cidr, strict=False)
            for cidr in config.server.trusted_proxy_cidrs
        )
    except (ValueError, TypeError):
        return False


def _route(method, target):
    if len(target) > 4096 or any(ord(char) < 33 or ord(char) > 126 for char in target) or "%" in target:
        raise BindingError(400, "INVALID_ROUTE")
    path, separator, query = target.partition("?")
    if method == "POST" and path == "/v1/artifact-transfers" and not separator:
        return "begin", {}
    match = re.fullmatch(r"/v1/artifact-transfers/(" + _ID + r")(?:/(seal|abort|chunks/([0-9]{1,20})))?", path)
    if match and not separator:
        identity, suffix, offset = match.groups()
        if suffix is None and method == "GET":
            return "inspect", {"transfer_id": identity}
        if suffix in ("seal", "abort") and method == "POST":
            return suffix, {"transfer_id": identity}
        if offset is not None and method == "PUT":
            value = int(offset)
            if value > 64 * 1024**3:
                raise BindingError(400, "INVALID_BOUND")
            return "append", {"transfer_id": identity, "offset": value}
        raise BindingError(405, "METHOD_NOT_ALLOWED")
    match = re.fullmatch(r"/v1/artifacts/(" + _ID + r")(/bytes)?", path)
    if match and method == "GET":
        identity, suffix = match.groups()
        if suffix is None and not separator:
            return "artifact", {"artifact_id": identity}
        if suffix and separator:
            pairs = query.split("&")
            parsed = {}
            for pair in pairs:
                name, equals, value = pair.partition("=")
                if name not in ("offset", "length") or name in parsed or not equals or not re.fullmatch(r"[0-9]{1,20}", value):
                    raise BindingError(400, "INVALID_QUERY")
                parsed[name] = int(value)
            if set(parsed) == {"offset", "length"} and 0 <= parsed["offset"] <= 64 * 1024**3 and 1 <= parsed["length"] <= RAW_LIMIT:
                return "range", {"artifact_id": identity, **parsed}
    raise BindingError(400, "INVALID_ROUTE")


def _single_header(handler, name, *, required=False):
    values = handler.headers.get_all(name) or ()
    if len(values) > 1 or (required and not values):
        raise BindingError(400, "INVALID_HEADERS")
    return values[0] if values else ""


def _length(handler, limit, *, required):
    raw = _single_header(handler, "Content-Length", required=required)
    if not raw and not required:
        return 0
    if not re.fullmatch(r"[0-9]{1,20}", raw):
        raise BindingError(400, "INVALID_LENGTH")
    length = int(raw)
    if length > limit:
        raise BindingError(413, "BODY_TOO_LARGE")
    return length


def handle_artifact_transfer(handler, method, binding, *, max_request_bytes=None):
    handler._artifact_transfer_request = is_artifact_route(handler.path)
    if not handler._artifact_transfer_request:
        return False
    slots = _REQUEST_SLOTS
    if not slots.acquire(blocking=False):
        handler._send_json_payload({"error": {"code": "BUSY"}}, status=429,
                                   headers={"Cache-Control": "no-store"})
        return True
    try:
        return _handle_admitted(handler, method, binding, max_request_bytes=max_request_bytes)
    finally:
        slots.release()


def _handle_admitted(handler, method, binding, *, max_request_bytes):
    # The generic response path must close without draining a rejected body.
    try:
        if binding is None:
            raise BindingError(503, "UNAVAILABLE")
        config = binding.current_config()
        if not transport_allowed(handler.connection, handler.client_address[0], config):
            raise BindingError(403, "HTTPS_REQUIRED")
        origin = _single_header(handler, "Origin")
        if origin and origin not in config.server.cors_origins:
            raise BindingError(403, "ORIGIN_FORBIDDEN")
        authorization = _single_header(handler, "Authorization")
        context = binding.authenticate(authorization, correlation_id=handler._correlation_id or "artifact-http")
        handler._validate_request_framing()
        action, payload = _route(method, handler.path)
        body = b""
        if action == "append":
            host_limit = config.server.max_request_bytes if max_request_bytes is None else max_request_bytes
            length = _length(handler, min(RAW_LIMIT, host_limit), required=True)
            if _single_header(handler, "Content-Type").lower() != "application/octet-stream":
                raise BindingError(415, "INVALID_MEDIA_TYPE")
            digest = _single_header(handler, "X-Sonder-Chunk-Sha256", required=True)
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise BindingError(400, "INVALID_DIGEST")
            payload["chunk_sha256"] = digest
            body = handler.rfile.read(length)
            if len(body) != length:
                raise BindingError(400, "INCOMPLETE_BODY")
            handler._request_body_consumed = True
        elif action in ("begin", "seal", "abort"):
            _length(handler, CONTROL_LIMIT, required=True)
            _single_header(handler, "Content-Type", required=True)
            supplied = handler._read_json(max_bytes=CONTROL_LIMIT)
            # Route-selected identity cannot be overwritten by body fields.
            if set(supplied) & set(payload):
                raise BindingError(400, "INVALID_REQUEST")
            payload = {**payload, **supplied}
        elif _length(handler, 0, required=False):
            raise BindingError(400, "UNEXPECTED_BODY")
        binding.validate_context(context)  # Recheck after a potentially slow body read.
        from sonder_runtime.application.artifacts.transfer import ArtifactRange, TransferError
        from sonder_runtime.interfaces.http.facades.artifact_transfer import dispatch_artifact_transfer, transfer_error_status
        try:
            result = dispatch_artifact_transfer(binding.service(), action, payload, context, body=body)
        except TransferError as error:
            raise BindingError(transfer_error_status(error), str(error)) from None
        if isinstance(result.body, ArtifactRange):
            item = result.body
            handler._send_binary_payload(item.body, content_type="application/octet-stream", digest=item.sha256,
                status=result.status_code, headers={"X-Sonder-Offset": str(item.offset),
                    "X-Sonder-Size": str(item.size_bytes), "X-Sonder-Chunk-Sha256": item.chunk_sha256})
        else:
            handler._send_json_payload(result.body, status=result.status_code, headers={"Cache-Control": "no-store"})
    except BindingError as error:
        handler._send_json_payload({"error": {"code": str(error)}}, status=error.status_code,
                                   headers={"Cache-Control": "no-store"})
    except Unauthenticated:
        handler._send_json_payload({"error": {"code": "UNAUTHORIZED"}}, status=401)
    except DependencyUnavailable as error:
        code = "RESTART_REQUIRED" if str(error) == "RESTART_REQUIRED" else "UNAVAILABLE"
        handler._send_json_payload({"error": {"code": code}}, status=503)
    except PermissionError:
        handler._send_json_payload({"error": {"code": "FORBIDDEN"}}, status=403)
    except Exception as error:
        # Existing framing/parser errors carry a public status; no raw exception,
        # request URL, body, header or proof is ever included in transfer logs.
        status = getattr(error, "status", 503)
        if type(status) is not int or status not in (400, 411, 413, 415):
            status = 503
        handler._send_json_payload({"error": {"code": "INVALID_REQUEST" if status != 503 else "UNAVAILABLE"}}, status=status)
    return True
