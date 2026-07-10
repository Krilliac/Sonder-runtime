import pytest

import web_tools


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setenv("TRILOBITE_WEB_TOOLS", "1")
    monkeypatch.setattr(
        web_tools.socket,
        "getaddrinfo",
        lambda host, port, *args, **kwargs: [
            (web_tools.socket.AF_INET, web_tools.socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))
        ],
    )


class FakeResponse:
    def __init__(self, body, content_type="text/html"):
        self._body = body.encode("utf-8")
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


def test_web_fetch_rejects_localhost():
    with pytest.raises(ValueError):
        web_tools.web_fetch("http://127.0.0.1/private")


def test_web_tools_can_be_disabled(monkeypatch):
    monkeypatch.setenv("TRILOBITE_WEB_TOOLS", "0")
    with pytest.raises(RuntimeError):
        web_tools.web_search("x")


def test_format_search_results_empty():
    assert web_tools.format_search_results([]) == "(no results)"
