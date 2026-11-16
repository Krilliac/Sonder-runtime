"""Regressions for review findings on the URL / origin / host policy fixes."""
from __future__ import annotations

import urllib.parse

import pytest

import web_tools
from sonder_runtime.adapters import web_search
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


@pytest.mark.parametrize("value", [
    "http://127.0.0.1:11434" + "/ " * 40,
    "http://a/ /",
    ":[::]://",
    "/",
    "http://" + "/ " * 5000,
])
def test_ollama_normalize_is_a_fixed_point(value):
    once = ollama_policy.normalize(value)
    assert ollama_policy.normalize(once) == once


@pytest.mark.parametrize("host", ["a\u00a8b.com", "a\u00afb.com", "a\u00b4b.com", "a\u02d8b.com"])
def test_egress_normalize_host_rejects_nfkc_space(host):
    with pytest.raises(ValueError):
        egress._normalize_host(host)


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
