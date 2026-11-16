"""Inference adapters must not put URL credentials in raw log records."""
from __future__ import annotations

import contextlib
import io
import json
import logging
import random
import re
import socket
import urllib.error
import urllib.request

import pytest

from sonder_runtime.adapters.inference import gpu_admission, ollama_endpoint, ollama_vision
from sonder_runtime.adapters.inference.log_safe import redact_url, safe_origin
from sonder_runtime.adapters.inference.openai_compat_gateway import (
    OpenAICompatibleConfig,
    OpenAICompatibleGateway,
)
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_target import ModelTarget
from sonder_runtime.application.ports.vision_gateway import VisionRequest
from sonder_runtime.domain.common.errors import (DependencyUnavailable, Forbidden, InvalidInput,
                                                  SonderError)
from sonder_runtime.domain.gpu_admission import HostMemoryReading
from sonder_runtime.platform.gpu_admission_config import GpuAdmissionConfig

SECRET = "s3cret"
CRED_ORIGIN = "http://user:s3cret@127.0.0.1:11434"
CRED_URL = "http://user:s3cret@127.0.0.1:11434/v1?api_key=s3cret#s3cret"


def _assert_clean(caplog) -> None:
    assert caplog.records, "expected log records to be captured"
    formatter = logging.Formatter()
    for record in caplog.records:
        assert SECRET not in record.getMessage()
        assert SECRET not in str(record.args)
        if record.exc_info:
            assert SECRET not in formatter.formatException(record.exc_info)


def _assert_emitted(caplog, fragment: str) -> None:
    assert any(fragment in r.getMessage() for r in caplog.records), (
        "expected a log record containing %r" % fragment
    )


def test_redact_url_strips_userinfo_query_fragment():
    assert redact_url(CRED_URL) == "http://127.0.0.1:11434/v1"
    assert redact_url("HTTPS://[::1]:8080/a/b?x=1") == "https://[::1]:8080/a/b"


@pytest.mark.parametrize(
    "value, expected",
    [
        # An "@" after "/", "?" or "#" cannot be told from path or query text.
        ("http://user:p/s3cret@127.0.0.1:11434/v1", "http://<redacted>"),
        ("http://user:p?s3cret@127.0.0.1:11434", "http://<redacted>"),
        ("http://user:p#s3cret@127.0.0.1:11434", "http://<redacted>"),
        # Every "@" before the first delimiter: userinfo up to the last one is dropped.
        ("http://s3cret@127.0.0.1:11434", "http://127.0.0.1:11434"),
        ("http://user:a@b:s3cret@127.0.0.1:11434", "http://127.0.0.1:11434"),
    ],
)
def test_redact_url_drops_userinfo_with_unescaped_delimiters(value, expected):
    assert redact_url(value) == expected


@pytest.mark.parametrize(
    "origin",
    [
        "http://u:s3cret@remote.example.com:bad",
        "http://u:s3cret/@remote.example.com",
        "http://u:s3cret@127.0.0.1:65536",
        "http://u:s3cret@",
    ],
)
def test_unparsable_credentialed_origin_is_never_loopback(origin):
    from sonder_runtime.domain import ollama_policy

    reduced = gpu_admission._origin_without_userinfo(origin)
    assert SECRET not in reduced
    assert ollama_policy.is_loopback(reduced) is False


def test_admission_keeps_a_credentialed_loopback_origin_local():
    from sonder_runtime.domain import ollama_policy

    reduced = gpu_admission._origin_without_userinfo(" http://u:s3cret@127.0.0.1:11434 ")
    assert reduced == "http://127.0.0.1:11434"
    assert ollama_policy.is_loopback(reduced) is True


def test_sonder_inference_invalid_base_url_message_has_no_credentials():
    from sonder_runtime.adapters.inference.sonder_inference_gateway import normalize_base_url
    from sonder_runtime.domain.common.errors import InvalidInput

    with pytest.raises(InvalidInput) as caught:
        normalize_base_url("https://user:s3cret@127.0.0.1:bad")
    assert SECRET not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize(
    "base_url",
    ["http://user:s3cret@127.0.0.1/v1", "http://user:s3cret@127.0.0.1:8080/v1", "https://s3cret@api.example.com/v1"],
)
def test_openai_compat_refuses_credentials_in_base_url_before_sending(base_url):
    from sonder_runtime.domain.common.errors import InvalidInput

    sent = []
    gateway = OpenAICompatibleGateway(
        OpenAICompatibleConfig(base_url=base_url, model="m"),
        transport=lambda url, payload, headers, timeout: sent.append(url) or {"ok": True},
    )
    with pytest.raises(InvalidInput) as caught:
        gateway._post("/embeddings", {}, OpenAICompatibleConfig(base_url=base_url, model="m"), 5.0)
    assert sent == []
    text = str(caught.value)
    cause = caught.value.__cause__
    while cause is not None:
        text += " " + str(cause)
        cause = cause.__cause__
    assert SECRET not in text


@pytest.mark.parametrize(
    "value",
    [
        "http://127.0.0.1:8080/v1?user=me@example.com&api_key=SECRETKEY",
        "http://127.0.0.1:8080/v1#frag@SECRETFRAG",
        "http://h/p@s3cret",
    ],
)
def test_redact_url_at_sign_after_delimiter_is_not_userinfo(value):
    rendered = redact_url(value)
    assert rendered == "http://<redacted>"
    for leaked in ("SECRETKEY", "SECRETFRAG", "s3cret", "example.com"):
        assert leaked not in rendered


def test_redact_url_userinfo_before_delimiters_versus_after():
    assert redact_url("HTTP://u:p@127.0.0.1:1/a?b=c@d") == "http://<redacted>"
    assert redact_url("http://u:p@127.0.0.1:1/a") == "http://127.0.0.1:1/a"


@pytest.mark.parametrize("value", ["", None, 42, "not a url", "http://", "http://[bad"])
def test_redact_url_never_raises(value):
    assert redact_url(value) == "<invalid url>"


def test_configure_typed_endpoint_does_not_log_credentials(caplog):
    caplog.set_level(logging.DEBUG)
    try:
        ollama_endpoint.configure_typed_endpoint(CRED_URL)
    finally:
        ollama_endpoint.reset_typed_endpoint()
    _assert_emitted(caplog, "configure_typed_endpoint: value=")
    _assert_clean(caplog)


def test_configured_origin_failure_does_not_log_credentials(caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ValueError):
        ollama_endpoint.configured_origin(CRED_ORIGIN)
    assert any("policy error" in r.getMessage() for r in caplog.records)
    _assert_clean(caplog)


def test_configured_origin_success_does_not_log_credentials(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(ollama_endpoint, "normalize", lambda value=None: CRED_ORIGIN)
    monkeypatch.setattr(ollama_endpoint, "policy_error", lambda *a, **k: "")
    assert ollama_endpoint.configured_origin(CRED_ORIGIN) == CRED_ORIGIN
    assert any("resolved to" in r.getMessage() for r in caplog.records)
    _assert_clean(caplog)


def test_open_url_does_not_log_credentials(caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ValueError):
        ollama_endpoint.open_url(urllib.request.Request(CRED_URL))
    assert any("open_url" in r.getMessage() for r in caplog.records)
    _assert_clean(caplog)


def test_vision_analyze_does_not_log_credentials(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(ollama_vision.ollama_endpoint, "normalize", lambda: CRED_URL)
    monkeypatch.setattr(ollama_vision.ollama_endpoint, "is_loopback", lambda value: True)
    gateway = ollama_vision.OllamaVisionGateway(
        target_resolver=lambda tier, strict: ModelTarget("llava-local", False, "vision"),
        transport=lambda url, payload, timeout: {"message": {"content": "ok"}},
    )
    gateway.analyze(
        VisionRequest("describe", b"pixels", "image/png"),
        local_owner_context(correlation_id="redact"),
    )
    assert any("posting to" in r.getMessage() for r in caplog.records)
    _assert_clean(caplog)


def test_vision_post_failure_does_not_log_credentials(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)

    def boom():
        raise RuntimeError("admission unavailable")

    monkeypatch.setattr(ollama_vision.gpu_admission, "process_service", boom)
    with pytest.raises(DependencyUnavailable):
        ollama_vision.OllamaVisionGateway._post(CRED_URL, {}, 1.0)
    _assert_emitted(caplog, "Ollama vision request failed")
    _assert_emitted(caplog, "Ollama vision inference request failed")
    _assert_clean(caplog)


def _gateway(transport) -> tuple[OpenAICompatibleGateway, OpenAICompatibleConfig]:
    cfg = OpenAICompatibleConfig(
        # Credentials in the base URL are refused outright; the query still carries one.
        base_url="http://127.0.0.1:8080/v1?api_key=s3cret",
        model="m",
    )
    return OpenAICompatibleGateway(cfg, transport=transport), cfg


def test_openai_compat_post_success_does_not_log_credentials(caplog):
    caplog.set_level(logging.DEBUG)
    gateway, cfg = _gateway(lambda url, payload, headers, timeout: {"ok": True})
    assert gateway._post("/embeddings", {}, cfg, 5.0) == {"ok": True}
    assert any("_post" in r.getMessage() for r in caplog.records)
    _assert_clean(caplog)


def _http_error(code):
    return urllib.error.HTTPError("http://x", code, "err", {}, None)


@pytest.mark.parametrize(
    "error, fragment",
    [
        (_http_error(401), "authentication failed"),
        (_http_error(429), "rate limited"),
        (_http_error(500), "returned server error"),
        (socket.timeout("t"), "timed out: url="),
        (urllib.error.URLError(socket.timeout("t")), "timed out (URLError)"),
        (urllib.error.URLError("refused"), "endpoint unreachable: url="),
        (OSError("down"), "unreachable (OSError)"),
    ],
    ids=["401", "429", "500", "timeout", "urlerror-timeout", "urlerror", "oserror"],
)
def test_openai_compat_post_failures_do_not_log_credentials(caplog, error, fragment):
    caplog.set_level(logging.DEBUG)

    def transport(url, payload, headers, timeout):
        raise error

    gateway, cfg = _gateway(transport)
    with pytest.raises(SonderError):
        gateway._post("/embeddings", {}, cfg, 5.0)
    _assert_emitted(caplog, fragment)
    _assert_clean(caplog)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://user:s3cret@api.example.com/v1?api_key=s3cret",
        "http://user:s3cret@:8080/v1",
    ],
)
def test_openai_compat_forbidden_messages_do_not_carry_credentials(base_url):
    gateway = OpenAICompatibleGateway(OpenAICompatibleConfig(base_url=base_url, model="m"))
    cfg = gateway._resolved_config()
    with pytest.raises(Forbidden) as consent:
        gateway._enforce_consent(cfg, local_owner_context(correlation_id="redact"))
    assert SECRET not in str(consent.value)
    with pytest.raises((Forbidden, InvalidInput)) as transport:
        gateway._require_secure_transport(
            OpenAICompatibleConfig(base_url=base_url.replace("https", "http"), model="m")
        )
    assert SECRET not in str(transport.value)


class _FakeAdmissionTransport:
    """Stands in for ``ollama_endpoint.open_url``: control-plane reads fail, chat succeeds."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def __call__(self, request, timeout=None, **_kwargs):
        self.urls.append(request.full_url)
        if request.get_method() == "POST" and request.full_url.endswith("/api/chat"):
            return io.BytesIO(json.dumps({"message": {"content": "ok"}}).encode("utf-8"))
        raise urllib.error.URLError("control plane down: " + request.full_url)


def test_vision_post_through_real_admission_does_not_log_credentials(
    caplog, monkeypatch, tmp_path
):
    caplog.set_level(logging.DEBUG)
    reading = HostMemoryReading(
        available_physical_bytes=64 * 2**30, total_physical_bytes=64 * 2**30,
        commit_limit_bytes=96 * 2**30, commit_available_bytes=64 * 2**30, commit_enforced=True,
    )
    service = gpu_admission.GpuAdmission(
        GpuAdmissionConfig(mode="enforce", store_path=str(tmp_path / "gpu.sqlite3")),
        host_reader=lambda: reading, memory_reader=lambda _key: None,
        show_reader=lambda _origin, _model: None, background=lambda work: work(),
    )
    gpu_admission.set_process_service(service)
    fake = _FakeAdmissionTransport()
    monkeypatch.setattr(ollama_endpoint, "open_url", fake)
    payload = {"model": "llava-local", "messages": [], "options": {"num_ctx": 4096}}
    data = ollama_vision.OllamaVisionGateway._post(
        "http://user:s3cret@127.0.0.1:11434/api/chat", payload, 5.0
    )
    assert data == {"message": {"content": "ok"}}
    assert any(url.endswith("/api/ps") for url in fake.urls), "admission never read /api/ps"
    _assert_emitted(caplog, "gpu admission could not read")
    _assert_clean(caplog)


def test_admission_drops_userinfo_for_every_caller(caplog, monkeypatch, tmp_path):
    """A caller that hands admit() a credentialed origin directly leaks nothing."""
    caplog.set_level(logging.DEBUG)
    reading = HostMemoryReading(
        available_physical_bytes=64 * 2**30, total_physical_bytes=64 * 2**30,
        commit_limit_bytes=96 * 2**30, commit_available_bytes=64 * 2**30, commit_enforced=True,
    )
    service = gpu_admission.GpuAdmission(
        GpuAdmissionConfig(mode="enforce", store_path=str(tmp_path / "gpu.sqlite3")),
        host_reader=lambda: reading, memory_reader=lambda _key: None,
        show_reader=lambda _origin, _model: None, background=lambda work: work(),
    )
    fake = _FakeAdmissionTransport()
    monkeypatch.setattr(ollama_endpoint, "open_url", fake)
    payload = {"model": "llava-local", "messages": [], "options": {"num_ctx": 4096}}
    with service.admit("/api/chat", payload, origin="http://user:s3cret@127.0.0.1:11434"):
        pass
    assert fake.urls, "admission never read the control plane"
    assert all(SECRET not in url for url in fake.urls), fake.urls
    _assert_emitted(caplog, "gpu admission could not read")
    _assert_clean(caplog)


def test_openai_compat_generate_does_not_log_base_url_credentials(caplog):
    caplog.set_level(logging.DEBUG)
    gateway, cfg = _gateway(lambda url, payload, headers, timeout: {"ok": True})
    try:
        gateway.generate(
            type("R", (), {"options": {}, "tier": "t", "prompt": "p", "system": None,
                           "messages": None})(),
            local_owner_context(correlation_id="redact"),
        )
    except Exception:
        pass
    assert any("OpenAICompatibleGateway.generate" in r.getMessage() for r in caplog.records)
    _assert_clean(caplog)


# --- review 3: allowlist rendering, parse-error text, mutation guards -----------

MARK = "Zq9MarkerXy"
_TOKENS = {"<invalid url>", "http://<redacted>", "https://<redacted>"}
# Everything redact_url may return besides a token: lower-case scheme and host,
# decimal port, and a path of allowlisted characters or the path placeholder.
_RENDERED = re.compile(
    r"(?:https?://)?(?:[a-z0-9][a-z0-9._\-]*|\[[0-9a-f:.]+\])(?::[0-9]{1,5})?"
    r"(?:/<redacted>|(?:/[A-Za-z0-9._~%:+,=\-]*)*)"
)
_NOISE = ["", "", "/", "?", "#", "@", ";", ":", "\\", " ", "\t", "\n", "%40", "%2F", "://", "//"]
_NOISE_NO_SLASH = [item for item in _NOISE if "/" not in item]
_NOISE_NO_SLASH_OR_AT = [item for item in _NOISE_NO_SLASH if item != "@"]


def _chain_text(exc: BaseException) -> str:
    """The text of ``exc`` and of everything chained to it, shown or suppressed."""
    seen: set[int] = set()
    pending: list[BaseException | None] = [exc]
    parts: list[str] = []
    while pending:
        item = pending.pop()
        if item is None or id(item) in seen:
            continue
        seen.add(id(item))
        parts.extend((str(item), repr(item)))
        pending.extend((item.__cause__, item.__context__))
    return " | ".join(parts)


def _assert_no_traceback(caplog, fragment: str) -> None:
    matching = [r for r in caplog.records if fragment in r.getMessage()]
    assert matching, "expected a log record containing %r" % fragment
    assert all(not r.exc_info for r in matching), "%r is logged with a traceback" % fragment


def _generated_urls(count: int, seed: int):
    """``(where, url)`` pairs with MARK in userinfo, port, query, fragment or params.

    MARK is never placed where it would be a host or a path segment, so no
    rendering of these values may contain it.
    """
    rng = random.Random(seed)
    schemes = ["http://", "https://", "HTTP://", "hTTps://", "", "", "ftp://", "//", "http:/", "http:"]
    hosts = ["127.0.0.1", "localhost", "[::1]", "api.example.com", "EXAMPLE.com", "h", ""]
    ports = ["", "", ":11434", ":80", ":99999", ":bad", ":"]
    paths = ["", "/", "/v1", "/api/chat", "/a/b/c", "/v1/models/x:generate"]
    tails = ["", "", "?next=http://x/@y", "&cb=http://h/@x", "#http://h/@x", "/@", "@", "/x@y", "?a=b@c"]
    for _ in range(count):
        where = rng.choice(["user", "password", "port", "query", "fragment", "params"])
        scheme, host, port, path = (rng.choice(schemes), rng.choice(hosts), rng.choice(ports),
                                    rng.choice(paths))
        if where in ("user", "password"):
            secret = rng.choice(_NOISE) + MARK + rng.choice(_NOISE)
            userinfo = secret if where == "user" else "u:" + secret
            url = scheme + userinfo + "@" + host + port + path
        elif where == "port":
            url = scheme + host + ":" + rng.choice(_NOISE_NO_SLASH_OR_AT) + MARK + path
        elif where == "query":
            url = scheme + host + port + path + "?k=" + rng.choice(_NOISE) + MARK
        elif where == "fragment":
            url = scheme + host + port + path + "#" + rng.choice(_NOISE) + MARK
        else:
            url = scheme + host + port + (path or "/") + ";p=" + rng.choice(_NOISE_NO_SLASH) + MARK
        url += rng.choice(tails)
        yield where, rng.choice(["", " ", "\t"]) + url + rng.choice(["", " ", "\n"])


def test_redact_url_never_renders_a_marker_from_userinfo_port_query_fragment_or_params():
    checked = 0
    for where, url in _generated_urls(6000, seed=20261116):
        rendered = redact_url(url)
        assert MARK.lower() not in rendered.lower(), (where, url, rendered)
        assert rendered in _TOKENS or _RENDERED.fullmatch(rendered), (where, url, rendered)
        origin = safe_origin(url)
        assert origin is None or MARK.lower() not in origin.lower(), (where, url, origin)
        if where in ("user", "password"):
            reduced = gpu_admission._origin_without_userinfo(url)
            assert MARK.lower() not in reduced.lower(), (where, url, reduced)
            assert "@" not in reduced, (where, url, reduced)
        checked += 1
    assert checked == 6000


def test_redact_url_output_is_a_token_or_allowlisted_text_for_arbitrary_input():
    rng = random.Random(7)
    alphabet = "ab1AZ.:/@?#;[]%\\ \n\t-_~+=,<>'\"(){}|^`*&!$" + chr(0) + chr(127) + chr(233)
    for _ in range(8000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 40)))
        if rng.random() < 0.5:
            text = rng.choice(["http://", "https://", "HTTP://"]) + text
        rendered = redact_url(text)
        assert rendered in _TOKENS or _RENDERED.fullmatch(rendered), (text, rendered)
        origin = safe_origin(text)
        assert origin is None or _RENDERED.fullmatch(origin), (text, origin)


@pytest.mark.parametrize(
    "value, expected",
    [
        # Review 3: "://" late in the value is not a scheme separator.
        ("u:s3cret@127.0.0.1:11434/cb?next=http://x/@y", "<invalid url>"),
        ("127.0.0.1:11434/v1?api_key=s3cret&cb=http://h/@x", "<invalid url>"),
        ("user:s3cret@host://a/b@c", "<invalid url>"),
        ("foo://bar://u:s3cret@h", "<invalid url>"),
        ("s3cret://u@h", "<invalid url>"),
        # ;params are dropped from every segment.
        ("http://h/p;s3cret=1", "http://h/p"),
        ("http://h/a;s3cret=1/b;k=s3cret/c", "http://h/a/b/c"),
        # A port that is not a port is not echoed.
        ("http://h:s3cret/p", "<invalid url>"),
        ("http://[::1]:s3cret/p", "<invalid url>"),
        ("http://u:s3cret@h:99999/p", "<invalid url>"),
        # A path outside the allowlist keeps the origin only.
        ("http://u:s3cret@h/a b", "http://h/<redacted>"),
        ("http://h/a\\s3cret", "http://h/<redacted>"),
        # A bare host:port is the documented OLLAMA_HOST form and renders.
        ("127.0.0.1:11434", "127.0.0.1:11434"),
        ("localhost:11434/api/tags?k=s3cret", "localhost:11434/api/tags"),
        ("[::1]:11434", "[::1]:11434"),
        ("localhost", "<invalid url>"),
        ("HTTP://U:s3cret@LOCALHOST:11434/V1", "http://localhost:11434/V1"),
        ("http://h/" + "a" * 5000, "<invalid url>"),
    ],
)
def test_redact_url_renders_from_the_allowlist_only(value, expected):
    assert redact_url(value) == expected


class _RaisingStr:
    def __str__(self) -> str:
        raise RuntimeError(MARK)

    __repr__ = __str__


class _HostileText(str):
    def strip(self, *args):
        raise RuntimeError(MARK)


@pytest.mark.parametrize(
    "value",
    [_RaisingStr(), _HostileText("http://u:s3cret@h/p"), b"http://u:s3cret@h/p", ["http://h"], 1.5],
    ids=["str-raises", "strip-raises", "bytes", "list", "float"],
)
def test_redact_url_and_safe_origin_never_raise_and_never_echo(value):
    assert redact_url(value) == "<invalid url>"
    assert safe_origin(value) is None


@pytest.mark.parametrize(
    "value",
    [
        "user:s3cret@127.0.0.1:11434/x?cb=http://h/@y",
        "127.0.0.1:11434/?api_key=s3cret&next=http://a/@b",
    ],
)
def test_configured_origin_does_not_log_a_value_with_a_late_scheme_separator(caplog, value):
    """ollama_policy adds "http://" only when "://" is absent anywhere in the value."""
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ValueError) as caught:
        ollama_endpoint.configured_origin(value)
    assert SECRET not in _chain_text(caught.value)
    _assert_emitted(caplog, "policy error")
    _assert_clean(caplog)


@pytest.mark.parametrize(
    "origin",
    [
        "http://user/s3cret@127.0.0.1:11434",
        "http://s3cret?x@127.0.0.1:11434",
        "http://127.0.0.1:11434/s3cret@remote.example.com",
        "ftp://u:s3cret@127.0.0.1:11434",
        "u:s3cret@127.0.0.1:11434",
    ],
)
def test_ambiguous_credentialed_origin_becomes_the_sentinel(origin):
    from sonder_runtime.domain import ollama_policy

    reduced = gpu_admission._origin_without_userinfo(origin)
    assert reduced == gpu_admission._UNPARSABLE_ORIGIN
    assert ollama_policy.is_loopback(reduced) is False
    assert safe_origin(reduced) is None


def _enforcing_service(tmp_path) -> gpu_admission.GpuAdmission:
    reading = HostMemoryReading(
        available_physical_bytes=64 * 2**30, total_physical_bytes=64 * 2**30,
        commit_limit_bytes=96 * 2**30, commit_available_bytes=64 * 2**30, commit_enforced=True,
    )
    return gpu_admission.GpuAdmission(
        GpuAdmissionConfig(mode="enforce", store_path=str(tmp_path / "gpu.sqlite3")),
        host_reader=lambda: reading, memory_reader=lambda _key: None,
        background=lambda work: work(),
    )


def test_read_json_failure_logs_no_credentials_and_no_traceback(caplog, monkeypatch):
    """Direct call: admit() strips userinfo first, so it cannot guard this log."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(ollama_endpoint, "open_url", _FakeAdmissionTransport())
    assert gpu_admission._read_json(CRED_ORIGIN, "/api/ps") is None
    _assert_no_traceback(caplog, "gpu admission could not read")
    _assert_clean(caplog)


def test_show_modelfile_failure_logs_no_credentials_and_no_traceback(
    caplog, monkeypatch, tmp_path
):
    caplog.set_level(logging.DEBUG)
    service = _enforcing_service(tmp_path)
    monkeypatch.setattr(ollama_endpoint, "open_url", _FakeAdmissionTransport())
    assert service._show_modelfile(CRED_ORIGIN, "llava-local") is None
    _assert_no_traceback(caplog, "gpu admission could not read /api/show")
    _assert_clean(caplog)


class _RecordingAdmission:
    """Stands in for the process service: records what admit() is handed."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    @contextlib.contextmanager
    def admit(self, path, payload, *, origin, **_kwargs):
        self.calls.append((path, origin))
        yield gpu_admission.passthrough_ticket(payload)


def test_vision_post_hands_admission_an_origin_without_userinfo(monkeypatch):
    """admit() strips userinfo itself, so only the argument shows what vision sent."""
    recorder = _RecordingAdmission()
    monkeypatch.setattr(ollama_vision.gpu_admission, "process_service", lambda: recorder)
    monkeypatch.setattr(ollama_endpoint, "open_url", _FakeAdmissionTransport())
    data = ollama_vision.OllamaVisionGateway._post(
        "http://user:s3cret@127.0.0.1:11434/api/chat", {"model": "m"}, 5.0
    )
    assert data == {"message": {"content": "ok"}}
    assert recorder.calls == [("/api/chat", "http://127.0.0.1:11434")]


@pytest.mark.parametrize(
    "url",
    [
        # urllib reads a password that ends at an unescaped "/" as the port,
        # and quotes it in its ValueError.
        "http://user:s3cret/@127.0.0.1:11434/api/chat",
        "http://127.0.0.1:s3cret/api/chat",
        "http://user:s3cret@127.0.0.1:99999/api/chat",
    ],
)
def test_vision_post_malformed_url_leaks_nothing(caplog, url):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(DependencyUnavailable) as caught:
        ollama_vision.OllamaVisionGateway._post(url, {"model": "m"}, 1.0)
    assert SECRET not in _chain_text(caught.value)
    _assert_no_traceback(caplog, "Ollama vision request failed")
    _assert_clean(caplog)


@pytest.mark.parametrize(
    "url",
    [
        "http://user:s3cret/@127.0.0.1:11434/api/ps",
        "http://127.0.0.1:s3cret/api/ps",
        # http.client refuses a space with an InvalidURL that quotes path and query.
        "http://127.0.0.1:11434/api/ps?key=s3cret&note=a b",
    ],
)
def test_open_url_malformed_url_error_has_no_url_text(caplog, url):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ValueError) as caught:
        ollama_endpoint.open_url(urllib.request.Request(url))
    assert SECRET not in _chain_text(caught.value)
    _assert_clean(caplog)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.0.0.1:s3cret/v1",
        "http://user:s3cret/@127.0.0.1:8080/v1",
        "http://127.0.0.1:8080/v1?api_key=s3cret&note=a b",
        "s3cret://127.0.0.1:8080/v1",
        "http://[s3cret]/v1",
    ],
)
def test_openai_compat_refuses_a_malformed_base_url_before_sending(caplog, base_url):
    caplog.set_level(logging.DEBUG)
    sent = []
    cfg = OpenAICompatibleConfig(base_url=base_url, model="m")
    gateway = OpenAICompatibleGateway(
        cfg, transport=lambda url, payload, headers, timeout: sent.append(url) or {"ok": True},
    )
    with pytest.raises(InvalidInput) as caught:
        gateway._post("/embeddings", {}, cfg, 5.0)
    assert sent == []
    assert SECRET not in _chain_text(caught.value)
    for record in caplog.records:
        assert SECRET not in record.getMessage()


def test_openai_compat_at_sign_in_the_path_is_refused_with_an_accurate_message():
    cfg = OpenAICompatibleConfig(base_url="https://api.example.com/@org/v1", model="m")
    with pytest.raises(InvalidInput) as caught:
        OpenAICompatibleGateway(cfg)._require_secure_transport(cfg)
    assert "%40" in str(caught.value)
    assert "set the API key instead" not in str(caught.value)
    encoded = OpenAICompatibleConfig(base_url="https://api.example.com/%40org/v1", model="m")
    OpenAICompatibleGateway(encoded)._require_secure_transport(encoded)


def test_sonder_inference_invalid_base_url_error_has_no_chained_url_text():
    from sonder_runtime.adapters.inference.sonder_inference_gateway import normalize_base_url

    with pytest.raises(InvalidInput) as caught:
        normalize_base_url("https://user:s3cret/@127.0.0.1")
    assert SECRET not in _chain_text(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
