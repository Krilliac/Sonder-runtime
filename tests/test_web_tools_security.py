import pytest
import web_tools


class Response:
    def __init__(self, status=200, location=""):
        self.status = self.code = status
        self.headers = {"Content-Type": "text/html"}
        if location: self.headers["Location"] = location
    def read(self, n=-1): return b""
    def __enter__(self): return self
    def __exit__(self, *args): return False


@pytest.fixture(autouse=True)
def enable_web(monkeypatch):
    monkeypatch.setenv("TRILOBITE_WEB_TOOLS", "1")


def _dns(address):
    return lambda host, port, *a, **k: [(web_tools.socket.AF_INET, web_tools.socket.SOCK_STREAM, 6, "", (address, port))]


def test_private_dns_is_not_opened(monkeypatch):
    opened = []
    monkeypatch.setattr(web_tools.socket, "getaddrinfo", _dns("10.0.0.7"))
    monkeypatch.setattr(web_tools, "_urlopen", lambda req, timeout=10: opened.append(req.full_url))
    with pytest.raises(ValueError, match="globally routable"):
        web_tools.web_fetch("https://internal.example/x")
    assert opened == []


def test_redirect_target_is_revalidated(monkeypatch):
    opened = []
    def resolve(host, port, *a, **k):
        return _dns("93.184.216.34" if host == "public.example" else "127.0.0.1")(host, port)
    monkeypatch.setattr(web_tools.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(web_tools, "_urlopen", lambda req, timeout=10: opened.append(req.full_url) or Response(302, "http://internal.example/x"))
    with pytest.raises(ValueError, match="globally routable"):
        web_tools.web_fetch("https://public.example/start")
    assert opened == ["https://public.example/start"]


def test_request_pins_first_validated_dns_answer(monkeypatch):
    dns_calls = []
    opened = []

    def resolve(host, port, *args, **kwargs):
        dns_calls.append((host, port))
        address = "93.184.216.34" if len(dns_calls) == 1 else "127.0.0.1"
        return _dns(address)(host, port, *args, **kwargs)

    def fake_open(req, timeout=10):
        opened.append(tuple(req._trilobite_addresses))
        return Response()

    monkeypatch.setattr(web_tools.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(web_tools, "_urlopen", fake_open)

    web_tools.web_fetch("https://public.example/start")

    assert dns_calls == [("public.example", 443)]
    assert opened == [("93.184.216.34",)]


def test_urlopen_connects_to_pinned_address_and_preserves_host(monkeypatch):
    seen = {}

    class Connection:
        def __init__(self, host, port, *, connect_host, timeout):
            seen.update(
                host=host,
                port=port,
                connect_host=connect_host,
                timeout=timeout,
            )

        def request(self, method, target, body=None, headers=None):
            seen.update(method=method, target=target, headers=headers)

        def getresponse(self):
            return Response()

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(web_tools, "_PinnedHTTPSConnection", Connection)
    req = web_tools.urllib.request.Request("https://public.example/a?q=1")
    req._trilobite_addresses = ("93.184.216.34",)

    with web_tools._urlopen(req, timeout=7):
        pass

    assert seen["host"] == "public.example"
    assert seen["connect_host"] == "93.184.216.34"
    assert seen["target"] == "/a?q=1"
    assert seen["headers"]["Host"] == "public.example"


@pytest.mark.parametrize("url", ["http://2130706433/x", "http://0177.0.0.1/x", "http://0x7f000001/x"])
def test_noncanonical_numeric_host_is_rejected(url):
    with pytest.raises(ValueError, match="non-canonical numeric"):
        web_tools.web_fetch(url)
