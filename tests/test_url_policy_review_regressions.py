"""Regressions for review findings on the URL / origin / host policy fixes."""
from __future__ import annotations

import urllib.parse

import pytest

import web_tools
from sonder_runtime.adapters import web_search
from sonder_runtime.adapters.inference import ollama_endpoint, ollama_pool
from sonder_runtime.domain import ollama_policy
from sonder_runtime.domain.security import credential_egress as egress
from sonder_runtime.platform.config import load_config


@pytest.mark.parametrize("value", [
    "https://ollama.example.com:8443 /",
    "https://ollama.example.com:8443\xa0/",
    "http://localhost:11434\x1f/",
    "http://localhost:11434 /",
    "http://localhost:11434/ /",
    "http://localhost:11434" + "/ " * 17,
    "http://localhost:11434" + "/ " * 18,
    "http://127.0.0.1:11434" + "/ " * 40,
    "http://a" + chr(92) + "@127.0.0.1:11434",
])
def test_ollama_policy_error_rejects_interior_whitespace_and_backslash(value):
    assert ollama_policy.policy_error(value, allow_remote=True) != ""
    assert ollama_policy.policy_error(value, allow_remote=False) != ""


def test_ollama_policy_error_still_accepts_padded_origins():
    for value in ("127.0.0.1:11434/ ", " http://localhost:11434// ", "https://ollama.example.com:443/"):
        assert ollama_policy.policy_error(value, allow_remote=True) == ""


def test_ollama_policy_error_names_the_backslash():
    for value in ("http://a" + chr(92) + "@127.0.0.1:11434", "http://127.0.0.1:11434" + chr(92)):
        assert ollama_policy.policy_error(value, allow_remote=True) == "OLLAMA_HOST must not contain a backslash"
    assert "whitespace or control" in ollama_policy.policy_error("http://127.0.0.1 :11434")


@pytest.mark.parametrize("value", [
    "http://127.0.0.1:11434" + "/ " * 40,
    "http://a/ /",
    ":[::]://",
    "/",
    "http://" + "/ " * 5000,
    "http:// /#",
    "http://\u3000://",
    "%\x1c\n:",
    "http://%\x1c",
    "http://.:80",
    "http://h:1/?",
    "x:///",
])
def test_ollama_normalize_is_a_fixed_point(value):
    once = ollama_policy.normalize(value)
    assert ollama_policy.normalize(once) == once


def test_ollama_normalize_fixed_point_survives_mutation_fuzz():
    import random

    rng = random.Random(20261116)
    pieces = ["/", " ", "\t", "\n", "%", "@", ":", "[", "]", ".", "#", "?", "//", "://", "http://", "\u3000",
              "\u2028", "\xa0", "\x1c", "\x85", "\x7f", "0.0.0.0", "localhost", "::", "11434", "a"]
    for _ in range(20000):
        value = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 8)))
        once = ollama_policy.normalize(value)
        assert ollama_policy.normalize(once) == once, (value, once)


@pytest.mark.parametrize("value", [
    "http://loc\nalhost:11434",
    "http://127.0.0.1\t:11434",
    "http://localhost:11434 /",
    "http://localhost:11434/ /",
    "http://localhost:11\r\n434",
    "http://0.0.0.0:11434\x1c//",
    "https://\u202810.0.0.5:11434 /",
])
def test_ollama_normalize_never_launders_interior_whitespace(value):
    # A caller that normalizes first and gates second must still be refused.
    once = ollama_policy.normalize(value)
    assert once == value
    assert ollama_policy.policy_error(once, allow_remote=True) != ""


# Text base refused, or accepted only because urlparse and int() forgive
# whitespace; each must be refused at every production entry point.
_HOSTILE_ORIGINS = [
    "http://127.0.0.1:11434" + "/ " * 40,
    "http://localhost:11434/ /",
    "http://localhost:11434 /",
    "http://localhost:11434\x1f/",
    "http://0.0.0.0:11434\x1c//",
    "https://w.example.com:8443/ / /",
    "https://w.example.com:8443 /",
    "https://w.example.com:8443\xa0/",
    "https://w.example.com:8443\u3000/",
    "https://w.example.com:8443\u2028/",
    "https://\u202810.0.0.5:11434 /",
    "http://loc\nalhost:11434",
    "http://127.0.0.1\t:11434",
    "http://127.0.0.1:11434" + chr(92),
    "http://a" + chr(92) + "@127.0.0.1:11434",
    "http://[::1%25lo]:11434",
    "http://[::1%lo]:11434",
]

# (configured text, canonical origin) pairs that main accepts and must keep.
_LEGIT_LOCAL = [
    ("127.0.0.1:11434", "http://127.0.0.1:11434"),
    ("127.0.0.1:11434\n", "http://127.0.0.1:11434"),
    ("http://127.0.0.1:11434\r\n", "http://127.0.0.1:11434"),
    ("  http://localhost:11434  ", "http://127.0.0.1:11434"),
    ("[::1]:11434", "http://[::1]:11434"),
    ("http://localhost:11434/", "http://127.0.0.1:11434"),
    ("http://localhost:11434//", "http://127.0.0.1:11434"),
    ("HTTP://LOCALHOST:11434", "http://127.0.0.1:11434"),
    ("http://0.0.0.0:11434", "http://127.0.0.1:11434"),
]
_LEGIT_REMOTE = [
    ("https://b\u00fccher.de:443", "https://b\u00fccher.de:443"),
    ("https://Ollama.Example.COM:8443/", "https://ollama.example.com:8443"),
    ("https://w.example.com:8443//\n", "https://w.example.com:8443"),
]


@pytest.mark.parametrize("value", _HOSTILE_ORIGINS)
@pytest.mark.parametrize("allow_remote", [False, True])
def test_configured_origin_and_client_environment_refuse_hostile_text(value, allow_remote):
    with pytest.raises(ValueError):
        ollama_endpoint.configured_origin(value, allow_remote=allow_remote)
    environment = {"OLLAMA_HOST": value}
    if allow_remote:
        environment["SONDER_ALLOW_REMOTE_OLLAMA"] = "1"
    with pytest.raises(ValueError):
        ollama_endpoint.client_environment(environment)


@pytest.mark.parametrize("value", _HOSTILE_ORIGINS)
@pytest.mark.parametrize("allow_remote", [False, True])
def test_worker_entry_points_refuse_hostile_text(value, allow_remote):
    with pytest.raises(ValueError):
        ollama_pool.validate_worker_origin(value, allow_remote=allow_remote)
    environment = {"SONDER_OLLAMA_WORKERS": value}
    if allow_remote:
        environment["SONDER_ALLOW_REMOTE_OLLAMA"] = "1"
    with pytest.raises(ValueError):
        ollama_pool.from_environment("http://127.0.0.1:11434", environment)
    with pytest.raises(ValueError):
        ollama_pool.from_environment(value, dict(environment, SONDER_OLLAMA_WORKERS=""))


def test_worker_origin_errors_name_the_reason():
    with pytest.raises(ValueError, match="whitespace or control"):
        ollama_pool.validate_worker_origin("https://w.example.com:8443 /", allow_remote=True)
    with pytest.raises(ValueError, match="backslash"):
        ollama_pool.validate_worker_origin("http://127.0.0.1:11434" + chr(92), allow_remote=True)
    with pytest.raises(ValueError, match="zone id or percent"):
        ollama_pool.validate_worker_origin("http://[::1%25lo]:11434", allow_remote=True)


@pytest.mark.parametrize("value,origin", _LEGIT_LOCAL + _LEGIT_REMOTE)
def test_production_entry_points_keep_accepting_legitimate_origins(value, origin):
    remote = (value, origin) in _LEGIT_REMOTE
    environment = {"SONDER_ALLOW_REMOTE_OLLAMA": "1"} if remote else {}
    assert ollama_endpoint.configured_origin(value, allow_remote=remote) == origin
    assert ollama_endpoint.client_environment(dict(environment, OLLAMA_HOST=value))["OLLAMA_HOST"] == origin
    assert ollama_pool.validate_worker_origin(value, allow_remote=remote) == origin
    assert ollama_pool.from_environment(value, dict(environment)).configured_origins == (origin,)
    if origin != "http://127.0.0.1:11434":
        pool = ollama_pool.from_environment("http://127.0.0.1:11434", dict(environment, SONDER_OLLAMA_WORKERS=value))
        assert pool.configured_origins == ("http://127.0.0.1:11434", origin)
    if remote:
        with pytest.raises(ValueError):
            ollama_endpoint.configured_origin(value, allow_remote=False)
        with pytest.raises(ValueError):
            ollama_pool.validate_worker_origin(value, allow_remote=False)


def test_configured_origin_gates_on_the_configured_text(monkeypatch):
    seen = []
    real = ollama_policy.policy_error

    def spy(value=None, *, allow_remote=False):
        seen.append(value)
        return real(value, allow_remote=allow_remote)

    monkeypatch.setattr(ollama_policy, "policy_error", spy)
    assert ollama_endpoint.configured_origin(" HTTP://LOCALHOST:11434/ ", allow_remote=False) == "http://127.0.0.1:11434"
    assert seen == ["HTTP://LOCALHOST:11434/"]


@pytest.mark.parametrize("host", ["a\u00a8b.com", "a\u00afb.com", "a\u00b4b.com", "a\u02d8b.com"])
def test_egress_normalize_host_rejects_nfkc_space(host):
    with pytest.raises(ValueError):
        egress._normalize_host(host)


@pytest.mark.parametrize("host", [
    "https\uff1a\uff0f\uff0fevil.com",  # NFKC -> https://evil.com
    "a\u2100b.com",  # -> aa/cb.com
    "a\uff20b.com",  # -> a@b.com
    "a\uff1ab.com",  # -> a:b.com
    "\uff1a\uff1a1",  # -> ::1, not an address as written
    "\u2024evil.com",  # -> .evil.com
    "a\u2026b.com",  # -> a...b.com
    "a\uff1fb.com",  # -> a?b.com
    "a?b.com",
    "[ab].example.com",
    "evil.com:443",
    "a b.com",
])
def test_egress_normalize_host_rejects_delimiters_after_idna(host):
    with pytest.raises(ValueError):
        egress._normalize_host(host)


@pytest.mark.parametrize("host,scope", [
    ("example.com", "example.com"),
    ("*.example.com", "*.example.com"),
    ("*", "*"),
    ("Example.COM.", "example.com"),
    (" example.com\n", "example.com"),
    ("b\u00fccher.de", "xn--bcher-kva.de"),
    ("*.B\u00dcCHER.de", "*.xn--bcher-kva.de"),
    ("xn--bcher-kva.de", "xn--bcher-kva.de"),
    ("example.com\u3002", "example.com"),
    ("my_host.internal", "my_host.internal"),
    ("127.0.0.1", "127.0.0.1"),
    ("::1", "::1"),
    ("2001:DB8::1", "2001:db8::1"),
])
def test_egress_normalize_host_keeps_legitimate_scopes(host, scope):
    assert egress._normalize_host(host) == scope
    assert egress._normalize_host(scope) == scope


def test_egress_ipv6_scope_still_matches_its_literal():
    handle = egress.CredentialHandle.mint("issuer", ("::1", "good.com"))
    assert handle.allows("https://[::1]:8443/x")
    assert handle.allows("https://good.com/x")
    assert not handle.allows("https://evil.com/x")


def _feed(url):
    return urllib.parse.quote(url, safe="")


def test_search_rows_survive_unparseable_decoded_target():
    page = (
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=%s">Bad</a>'
        '<a class="result__a" href="https://ok.example/page">Good</a>'
    ).replace("%s", _feed("http://["))
    rows = web_tools._search_rows(page, "https://duckduckgo.com/html/", 5)
    assert [row["url"] for row in rows] == ["https://ok.example/page"]


def test_mojeek_rows_drop_control_characters_in_urls():
    page = (
        '<a class="title" href="https://ok.example/&#13;&#10;x">Bad</a>'
        '<a class="title" href="https://ok.example/fine">Good</a>'
    )
    rows = web_tools._search_rows(page, "https://www.mojeek.com/search", 5)
    assert [row["url"] for row in rows] == ["https://ok.example/fine"]


def test_rss_rows_drop_non_http_and_control_character_links():
    raw = (
        b"<?xml version='1.0'?><rss><channel>"
        b"<item><title>A</title><link>http://[</link></item>"
        b"<item><title>B</title><link>https://ok.example/a b</link></item>"
        b"<item><title>C</title><link>https://ok.example/c</link></item>"
        b"</channel></rss>"
    )
    rows = web_tools._search_rss_rows(raw, 5)
    assert [row["url"] for row in rows] == ["https://ok.example/c"]


def test_json_provider_rows_require_plain_http_urls(monkeypatch):
    import json

    payload = json.dumps({"results": [
        {"title": "js", "url": "javascript:alert(1)"},
        {"title": "crlf", "url": "https://ok.example/\r\nx"},
        {"title": "blank", "url": ""},
        {"title": "good", "url": "https://ok.example/good"},
    ]}).encode()
    monkeypatch.setenv("SONDER_SEARCH_URL", "https://search.example/?q={query}")
    monkeypatch.setattr(web_tools, "enabled", lambda: True)
    monkeypatch.setattr(web_tools, "_request", lambda url, timeout=10: (payload, "application/json"))
    monkeypatch.setattr(web_tools, "_search_relevance", lambda query, results: (1, 0))
    rows = web_search.search_raw("good", limit=5)
    assert [row["url"] for row in rows] == ["https://ok.example/good"]


@pytest.mark.parametrize("origin", ["http://:80", "https://:443", "http://:"])
def test_observatory_origins_without_a_host_are_refused_at_load(origin):
    with pytest.raises(Exception) as caught:
        load_config(env={"SONDER_OBSERVATORY_ORIGINS": origin})
    assert "live_export_origins" in str(caught.value)


_LOOPBACK_PARITY_INPUTS = [
    "http://[::1%25lo]:11434",
    "http://[::1%lo]:11434",
    "http://[::1]:11434",
    "http://127.0.0.1:11434",
    "http://localhost:11434",
    "http://example.com:11434",
    "http://[fe80::1%25eth0]:11434",
]


@pytest.mark.parametrize("value", _LOOPBACK_PARITY_INPUTS)
def test_gateway_is_loopback_agrees_with_endpoint_is_loopback(value):
    from sonder_runtime.adapters.inference import ollama_gateway

    assert ollama_gateway._is_loopback(value) == ollama_endpoint.is_loopback(value)
    if "%" in value:
        assert ollama_gateway._is_loopback(value) is False
        assert ollama_endpoint.is_loopback(value) is False


@pytest.mark.parametrize("scope", ["fe80::1%*", "fe80::1%" + chr(252), "fe80::1%eth0"])
def test_host_scope_refuses_ipv6_zone_ids(scope):
    with pytest.raises(ValueError):
        egress._normalize_host(scope)


@pytest.mark.parametrize("scope", ["::1", "fe80::1", "::ffff:1.2.3.4"])
def test_host_scope_still_accepts_bare_ipv6_literals(scope):
    assert egress._normalize_host(scope) == scope
