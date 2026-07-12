import base64
import gzip
import json
import urllib.parse
import zlib

import pytest

import web_tools


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setenv("SONDER_WEB_TOOLS", "1")
    monkeypatch.setattr(
        web_tools.socket,
        "getaddrinfo",
        lambda host, port, *args, **kwargs: [
            (web_tools.socket.AF_INET, web_tools.socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))
        ],
    )


class FakeResponse:
    def __init__(self, body, content_type="text/html"):
        self._body = body if isinstance(body, bytes) else body.encode("utf-8")
        self.headers = {"Content-Type": content_type}

    def read(self, n=-1):
        return self._body if n == -1 else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_web_search_parses_duckduckgo_results(monkeypatch):
    html = """
    <html><body>
      <a class="result__a" href="/l/?uddg=https%3A%2F%2Fexample.com%2Fa">Example A</a>
      <a class="result__a" href="https://example.com/b">Example B</a>
    </body></html>
    """
    monkeypatch.setattr(web_tools, "_urlopen", lambda req, timeout=10: FakeResponse(html))
    results = web_tools.web_search("example", limit=2)
    assert results[0]["title"] == "Example A"
    assert results[0]["url"] == "https://example.com/a"
    assert results[1]["url"] == "https://example.com/b"


def test_web_search_falls_back_to_mojeek(monkeypatch):
    target = "https://open-meteo.com/en/docs"
    pages = [
        b'<form id="challenge-form"></form>',
        b'<div class="results"><p>No results</p></div>',
        (
            '<ul class="results-standard"><li><h2>'
            '<a class="title" href="%s">Open-Meteo Weather Forecast API</a>'
            '</h2></li></ul>' % target
        ).encode(),
    ]
    requested = []

    def fake_request(url, timeout=10):
        requested.append(url)
        return pages.pop(0), "text/html"

    monkeypatch.setattr(web_tools, "_request", fake_request)

    results = web_tools.web_search("Open-Meteo documentation")

    assert len(requested) == 3
    assert "duckduckgo.com" in requested[0]
    assert "mojeek.com" in requested[1]
    assert "mojeek.com" in requested[2]
    assert "q=Open-Meteo" in requested[2]
    assert results == [{
        "title": "Open-Meteo Weather Forecast API",
        "url": target,
        "snippet": "",
    }]


def test_bing_redirect_decoder_recovers_direct_result_url():
    target = "https://open-meteo.com/en/docs"
    encoded = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")

    assert web_tools._clean_bing_result_url(
        "https://www.bing.com/ck/a?u=a1%s" % encoded
    ) == target


def test_search_rss_parser_returns_direct_links_and_plain_snippets():
    raw = b'''<?xml version="1.0"?><rss><channel><item>
      <title>Open-Meteo Docs</title>
      <link>https://open-meteo.com/en/docs</link>
      <description><![CDATA[<b>Forecast</b> API reference]]></description>
    </item></channel></rss>'''

    assert web_tools._search_rss_rows(raw, 5) == [{
        "title": "Open-Meteo Docs",
        "url": "https://open-meteo.com/en/docs",
        "snippet": "Forecast API reference",
    }]


def test_web_search_retries_distinctive_query_after_blocks_and_irrelevant_rows(
    monkeypatch,
):
    target = "https://open-meteo.com/en/docs"
    target_encoded = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    dictionary = "https://example.com/dictionary/official"
    dictionary_encoded = (
        base64.urlsafe_b64encode(dictionary.encode()).decode().rstrip("=")
    )
    requested = []

    def fake_request(url, timeout=10):
        requested.append(url)
        if "duckduckgo.com" in url:
            return b'<form id="challenge-form"></form>', "text/html"
        if "mojeek.com" in url:
            return b'<title>403 - Forbidden</title> automated queries', "text/html"
        encoded = target_encoded if "q=Open-Meteo" in url else dictionary_encoded
        title = "Open-Meteo Docs" if encoded == target_encoded else "Official Definition"
        page = (
            '<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?u=a1%s">'
            '%s</a></h2></li>' % (encoded, title)
        )
        return page.encode(), "text/html"

    monkeypatch.setattr(web_tools, "_request", fake_request)

    results = web_tools.web_search(
        "official Open-Meteo weather API documentation", limit=3,
    )

    assert len(requested) == 4
    assert "q=Open-Meteo" in requested[-1]
    assert results[0]["url"] == target


def test_web_fetch_strips_html(monkeypatch):
    monkeypatch.setattr(
        web_tools,
        "_urlopen",
        lambda req, timeout=10: FakeResponse("<html><body><h1>Title</h1><script>x</script><p>Hello</p></body></html>"),
    )
    text = web_tools.web_fetch("https://example.com/page")
    assert "Title" in text
    assert "Hello" in text
    assert "script" not in text


@pytest.mark.parametrize(("body", "content_type", "expected"), [
    ("café".encode("utf-8"), "text/plain; charset=UTF-8", "café"),
    (b"\xef\xbb\xbfhello", "text/plain", "hello"),
    ("hello ☃".encode("utf-16"), "text/plain", "hello ☃"),
    ("hello ☃".encode("utf-16-le"), "text/plain; charset=utf-16le", "hello ☃"),
    (b"caf\xe9", "text/plain; charset=iso-8859-1", "café"),
    (b"price \x8010", "text/plain; charset=windows-1252", "price €10"),
    (b'{"release":"3.14"}', "application/problem+json", '"release":"3.14"'),
    (
        b'<?xml version="1.0"?><feed><title>Release 3.14</title></feed>',
        "application/atom+xml",
        "Release 3.14",
    ),
])
def test_web_fetch_decodes_allowlisted_text_formats(
    monkeypatch, body, content_type, expected,
):
    monkeypatch.setattr(
        web_tools, "_request", lambda *_args, **_kwargs: (body, content_type),
    )

    assert expected in web_tools.web_fetch("https://example.com/page")


def test_web_fetch_uses_allowlisted_html_meta_charset(monkeypatch):
    body = b'<html><head><meta charset="windows-1252"></head><body>\x93Hi\x94</body></html>'
    monkeypatch.setattr(
        web_tools, "_request", lambda *_args, **_kwargs: (body, "text/html"),
    )

    assert web_tools.web_fetch("https://example.com/page") == "“Hi”"


@pytest.mark.parametrize("content_type", [
    "application/pdf", "image/png", "application/octet-stream", "audio/mpeg",
])
def test_web_fetch_rejects_non_text_media_types(monkeypatch, content_type):
    monkeypatch.setattr(
        web_tools,
        "_request",
        lambda *_args, **_kwargs: (b"not readable text", content_type),
    )

    with pytest.raises(ValueError, match="non-text HTTP media type"):
        web_tools.web_fetch("https://example.com/file")


def test_web_fetch_rejects_binary_signature_even_if_mislabeled(monkeypatch):
    monkeypatch.setattr(
        web_tools,
        "_request",
        lambda *_args, **_kwargs: (b"%PDF-1.7 binary", "text/plain"),
    )

    with pytest.raises(ValueError, match="binary web content"):
        web_tools.web_fetch("https://example.com/file")


def test_web_fetch_rejects_binary_controls_even_if_mislabeled(monkeypatch):
    monkeypatch.setattr(
        web_tools,
        "_request",
        lambda *_args, **_kwargs: (
            b"readable prefix\x01\x02binary suffix",
            "text/plain; charset=latin-1",
        ),
    )

    with pytest.raises(ValueError, match="binary control bytes"):
        web_tools.web_fetch("https://example.com/file")


@pytest.mark.parametrize(("body", "content_type", "message"), [
    (b"hello", "text/plain; charset=shift_jis", "unsupported HTTP text charset"),
    (b"hello", "", "missing Content-Type"),
    (b"\xef\xbb\xbfhello", "text/plain; charset=latin-1", "BOM conflicts"),
    (b"\xff\xfe\x00\x00x", "text/plain", "UTF-32"),
])
def test_web_fetch_fails_closed_on_unsafe_encoding_metadata(
    monkeypatch, body, content_type, message,
):
    monkeypatch.setattr(
        web_tools,
        "_request",
        lambda *_args, **_kwargs: (body, content_type),
    )

    with pytest.raises(ValueError, match=message):
        web_tools.web_fetch("https://example.com/page")


@pytest.mark.parametrize(("body", "content_type"), [
    (b"   \r\n\t", "text/plain; charset=utf-8"),
    (b"<html><script>only_code()</script></html>", "text/html"),
])
def test_web_fetch_rejects_pages_without_readable_text(
    monkeypatch, body, content_type,
):
    monkeypatch.setattr(
        web_tools, "_request", lambda *_args, **_kwargs: (body, content_type),
    )

    with pytest.raises(ValueError, match="no readable text"):
        web_tools.web_fetch("https://example.com/empty")


def test_http_content_encoding_decodes_gzip_and_deflate():
    payload = b"<html><body>official release 3.14</body></html>"

    assert web_tools._decode_content_encoding(gzip.compress(payload), "gzip") == payload
    assert web_tools._decode_content_encoding(zlib.compress(payload), "deflate") == payload


def test_http_content_encoding_rejects_expansion_bomb():
    compressed = gzip.compress(b"x" * (web_tools.MAX_DECOMPRESSED_BYTES + 1))

    with pytest.raises(ValueError, match="safety limit"):
        web_tools._decode_content_encoding(compressed, "gzip")


def test_web_fetch_rejects_localhost():
    with pytest.raises(ValueError):
        web_tools.web_fetch("http://127.0.0.1/private")


def test_web_tools_can_be_disabled(monkeypatch):
    monkeypatch.setenv("SONDER_WEB_TOOLS", "0")
    with pytest.raises(RuntimeError):
        web_tools.web_search("x")


def test_format_search_results_empty():
    assert web_tools.format_search_results([]) == "(no results)"


def test_weather_lookup_geocodes_and_formats_imperial_us_forecast(monkeypatch):
    requested = []

    def fake_request(url, timeout=10):
        requested.append(url)
        if "geocoding-api" in url:
            payload = {
                "results": [{
                    "name": "Chicago", "admin1": "Illinois",
                    "country": "United States", "country_code": "US",
                    "latitude": 41.85, "longitude": -87.65,
                }],
            }
        else:
            payload = {
                "timezone": "America/Chicago",
                "current": {
                    "time": "2026-07-11T01:00", "temperature_2m": 72,
                    "apparent_temperature": 73, "relative_humidity_2m": 64,
                    "precipitation": 0, "weather_code": 1,
                    "wind_speed_10m": 8, "wind_direction_10m": 270,
                },
                "current_units": {
                    "temperature_2m": "°F", "wind_speed_10m": "mph",
                    "precipitation": "inch",
                },
                "daily": {
                    "time": ["2026-07-11"], "weather_code": [2],
                    "temperature_2m_max": [81], "temperature_2m_min": [65],
                    "precipitation_probability_max": [20],
                    "precipitation_sum": [0.02], "wind_speed_10m_max": [14],
                },
                "daily_units": {
                    "temperature_2m_max": "°F", "temperature_2m_min": "°F",
                    "precipitation_sum": "inch", "wind_speed_10m_max": "mph",
                },
            }
        return json.dumps(payload).encode(), "application/json"

    monkeypatch.setattr(web_tools, "_request", fake_request)

    result = web_tools.weather_lookup("Chicago, IL", forecast_days=2)
    output = web_tools.format_weather(result)

    query = urllib.parse.parse_qs(urllib.parse.urlparse(requested[1]).query)
    assert query["temperature_unit"] == ["fahrenheit"]
    assert query["forecast_days"] == ["2"]
    assert "Weather for Chicago, Illinois, United States" in output
    assert "Mainly clear, 72°F" in output
    assert "wind 8 mph W" in output
    assert "Source: Open-Meteo" in output


def test_weather_lookup_retries_city_component_and_ranks_region(monkeypatch):
    requested = []

    def fake_json(url, timeout=10):
        requested.append(url)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        if "geocoding-api" in url and query["name"] == ["Springfield, IL"]:
            return {"results": []}
        if "geocoding-api" in url:
            return {"results": [
                {
                    "name": "Springfield", "admin1": "Missouri",
                    "country": "United States", "country_code": "US",
                    "latitude": 37.2, "longitude": -93.3,
                },
                {
                    "name": "Springfield", "admin1": "Illinois",
                    "country": "United States", "country_code": "US",
                    "latitude": 39.8, "longitude": -89.6,
                },
            ]}
        return {
            "timezone": "America/Chicago",
            "current": {"weather_code": 0},
            "current_units": {},
            "daily": {"time": []},
            "daily_units": {},
        }

    monkeypatch.setattr(web_tools, "_json_request", fake_json)

    result = web_tools.weather_lookup("Springfield, IL")

    assert result["place"]["admin1"] == "Illinois"
    assert len(requested) == 3
    assert "name=Springfield%2C+IL" in requested[0]
    assert "name=Springfield" in requested[1]


def test_approximate_location_discards_raw_ip_and_reports_accuracy(monkeypatch):
    monkeypatch.setattr(
        web_tools,
        "_json_request",
        lambda *_args, **_kwargs: {
            "success": True, "ip": "203.0.113.99", "city": "Chicago",
            "region": "Illinois", "country": "United States",
            "country_code": "US", "latitude": 41.8, "longitude": -87.6,
            "timezone": {"id": "America/Chicago", "abbr": "CDT"},
        },
    )

    location = web_tools.approximate_location_lookup()
    output = web_tools.format_approximate_location(location)

    assert "ip" not in location
    assert "203.0.113.99" not in output
    assert "Approximate location: Chicago, Illinois, United States" in output
    assert "Raw IP: not retained or displayed" in output
    assert location["timezone"] == "America/Chicago"


def test_location_hint_requires_a_place_and_discards_coordinates():
    with pytest.raises(ValueError, match="did not return a place"):
        web_tools.normalize_location_hint({"success": True, "ip": "203.0.113.1"})
    location = web_tools.normalize_location_hint({
        "city": "Nowhere", "latitude": 41.0, "longitude": -87.0,
    })
    assert "latitude" not in location
    assert "longitude" not in location
