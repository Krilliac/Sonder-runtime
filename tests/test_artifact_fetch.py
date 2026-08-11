"""Offline tests for verified artifact download.

Every case here is a real failure that happened while acquiring a Windows ISO
and four driver installers by hand, replayed against a loopback fixture server:

* HTTP 200 carrying a 10-byte body that was really a 404 (``drivers.amd.com``)
* a zero-byte file written behind a zero exit code (``curl``)
* an Akamai "Access Denied" HTML page handed back as though it were content
* a driver whose version string matched the product we wanted but whose signer
  was a different product line entirely

The network layer is exercised for real -- streaming, redirects, ranges, the
partial-file rename -- and only two seams are stubbed: the SSRF address policy
(so loopback fixtures are reachable without shipping a weaker policy) and the
Windows Authenticode shell-out (so the publisher checks run on any host).
"""
from __future__ import annotations

import http.server
import json
import threading
import urllib.parse
from pathlib import Path

import pytest

import artifact_fetch


pytestmark = pytest.mark.unit


# --- fixture server -------------------------------------------------------


PE_BODY = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 500 + b"PE\x00\x00" + b"\xcc" * 2048
ZIP_BODY = b"PK\x03\x04" + b"\x00" * 600
PNG_BODY = b"\x89PNG\r\n\x1a\n" + b"\x00" * 4096

AKAMAI_BLOCK = (
    b"<!DOCTYPE html><html><head><title>Access Denied</title></head><body>\n"
    b"<h1>Access Denied</h1>\n"
    b"You don't have permission to access \"http://mirror/installer.exe\" on "
    b"this server.<p>Reference #18.4f2c1a.1712000000.abcdef\n"
    b"<p>https://errors.edgesuite.net/18.4f2c1a.1712000000.abcdef\n"
    b"</body></html>\n"
)
CLOUDFLARE_BLOCK = (
    b"<!DOCTYPE html><html><head><title>Attention Required! | Cloudflare</title>"
    b"</head><body><div class=\"cf-browser-verification cf-im-under-attack\">"
    b"Checking your browser before accessing the site.</div></body></html>"
)
PLAIN_HTML = (
    b"<!DOCTYPE html><html><head><title>Downloads</title></head><body>"
    b"<h1>Driver downloads</h1><p>Pick a package from the list below.</p>"
    b"</body></html>"
)


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_GET(self):  # noqa: N802 - stdlib callback name
        route = self.server.routes.get(self.path)
        if route is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        status = route.get("status", 200)
        body = route.get("body", b"")
        headers = dict(route.get("headers", {}))
        content_type = route.get("content_type", "application/octet-stream")

        requested_range = self.headers.get("Range")
        self.server.seen_ranges.append((self.path, requested_range))
        if requested_range and status == 200 and route.get("ranges", True):
            start = int(requested_range.split("=", 1)[1].split("-", 1)[0])
            total = len(body)
            body = body[start:]
            status = 206
            headers["Content-Range"] = "bytes %d-%d/%d" % (
                start, max(total - 1, start), total,
            )

        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, *args):  # noqa: D401 - silence the fixture server
        return


@pytest.fixture()
def fixture_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.routes = {}
    server.seen_ranges = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class _Control:
        base = "http://127.0.0.1:%d" % server.server_address[1]

        @staticmethod
        def route(path, **spec):
            server.routes[path] = spec
            return _Control.base + path

    try:
        yield _Control
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def offline_seams(monkeypatch, tmp_path):
    """Reachable loopback, writable tmp root, and a stubbed signature verifier."""
    monkeypatch.setenv("SONDER_WEB_TOOLS", "1")
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))

    def _loopback(url):
        return urllib.parse.urlparse(url), ("127.0.0.1",)

    monkeypatch.setattr(artifact_fetch, "_validate_target", _loopback)
    monkeypatch.setattr(
        artifact_fetch,
        "_authenticode_signature",
        lambda path, **kwargs: {
            "supported": False,
            "status": "unsupported_platform",
            "publisher": "",
            "thumbprint": "",
            "detail": "stubbed for offline tests",
        },
    )


def _signature(status="Valid", publisher="", supported=True):
    return lambda path, **kwargs: {
        "supported": supported,
        "status": status,
        "publisher": publisher,
        "thumbprint": "AA" * 20,
        "detail": "",
    }


def _failed(result, check):
    return [row for row in result["failures"] if row["check"] == check]


# --- happy path -----------------------------------------------------------


def test_good_binary_download_verifies_and_writes_provenance(
    fixture_server, tmp_path,
):
    url = fixture_server.route("/setup.exe", body=PE_BODY)
    dest = tmp_path / "setup.exe"

    result = artifact_fetch.fetch_artifact(url, str(dest), expect_type="pe")

    assert result["ok"], result["failures"]
    assert result["verdict"] == "verified"
    assert dest.exists()
    assert dest.read_bytes() == PE_BODY
    assert result["bytes"] == len(PE_BODY)
    assert result["detected_type"] == "pe"
    assert result["sha256"] == artifact_fetch.file_sha256(dest)
    assert not artifact_fetch._part_path(dest).exists()

    sidecar = Path(result["provenance_path"])
    assert sidecar.name == "setup.exe.provenance.json"
    record = json.loads(sidecar.read_text(encoding="utf-8"))
    assert record["url"] == url
    assert record["final_url"] == url
    assert record["sha256"] == result["sha256"]
    assert record["bytes"] == len(PE_BODY)
    assert record["http_status"] == 200
    assert record["content_type"] == "application/octet-stream"
    assert record["detected_type"] == "pe"
    assert record["verified"] is True
    assert record["verdict"] == "verified"
    assert "signature_status" in record and "publisher" in record
    assert any(row["check"] == "magic" for row in record["checks"])


def test_formatted_report_names_the_destination_and_digest(
    fixture_server, tmp_path,
):
    url = fixture_server.route("/pkg.zip", body=ZIP_BODY)
    dest = tmp_path / "pkg.zip"

    report = artifact_fetch.format_fetch_result(
        artifact_fetch.fetch_artifact(url, str(dest))
    )

    assert "VERIFIED" in report
    assert str(dest) in report
    assert artifact_fetch.file_sha256(dest) in report


# --- failure #3: a denial served as content -------------------------------


def test_akamai_block_page_is_rejected_and_no_file_is_written(
    fixture_server, tmp_path,
):
    url = fixture_server.route(
        "/chipset.exe", body=AKAMAI_BLOCK, content_type="text/html",
    )
    dest = tmp_path / "chipset.exe"

    result = artifact_fetch.fetch_artifact(url, str(dest), expect_type="pe")

    assert not result["ok"]
    assert result["verdict"] == "rejected"
    assert not dest.exists(), "a denial page must never land at the destination"
    assert not artifact_fetch._part_path(dest).exists()
    assert not (tmp_path / "chipset.exe.provenance.json").exists()

    blocked = _failed(result, "block_page")
    assert blocked, result["failures"]
    assert "edgesuite" in blocked[0]["detail"] or "Akamai" in blocked[0]["detail"]
    assert result["block"]["confidence"] == "strong"


def test_cloudflare_challenge_is_rejected_by_name(fixture_server, tmp_path):
    url = fixture_server.route(
        "/driver.msi", body=CLOUDFLARE_BLOCK, content_type="text/html",
    )
    dest = tmp_path / "driver.msi"

    result = artifact_fetch.fetch_artifact(url, str(dest))

    assert not result["ok"]
    assert not dest.exists()
    assert "Cloudflare" in _failed(result, "block_page")[0]["detail"]


def test_html_served_as_exe_is_rejected_even_without_block_markers(
    fixture_server, tmp_path,
):
    url = fixture_server.route(
        "/tool.exe", body=PLAIN_HTML, content_type="text/html",
    )
    dest = tmp_path / "tool.exe"

    result = artifact_fetch.fetch_artifact(url, str(dest), expect_type="pe")

    assert not result["ok"]
    assert not dest.exists()
    blocked = _failed(result, "block_page")
    assert blocked, result["failures"]
    assert "pe payload was expected" in blocked[0]["detail"]


def test_wrong_magic_bytes_are_rejected(fixture_server, tmp_path):
    """A real binary of the wrong kind: failure #1 without the HTML tell."""
    url = fixture_server.route("/driver.exe", body=PNG_BODY)
    dest = tmp_path / "driver.exe"

    result = artifact_fetch.fetch_artifact(url, str(dest))

    assert not result["ok"]
    assert not dest.exists()
    assert result["detected_type"] == "png"
    magic = _failed(result, "magic")
    assert magic, result["failures"]
    assert "png" in magic[0]["detail"] and "pe" in magic[0]["detail"]
    assert not _failed(result, "block_page")


# --- failures #1 and #2: success statuses over garbage --------------------


def test_zero_byte_body_is_rejected(fixture_server, tmp_path):
    url = fixture_server.route("/empty.exe", body=b"")
    dest = tmp_path / "empty.exe"

    result = artifact_fetch.fetch_artifact(url, str(dest), expect_type="pe")

    assert not result["ok"]
    assert not dest.exists()
    size = _failed(result, "size")
    assert size, result["failures"]
    assert "0 bytes" in size[0]["detail"]


def test_http_200_over_a_ten_byte_body_is_rejected(fixture_server, tmp_path):
    """drivers.amd.com answered a probe 200 while serving a 404-sized body."""
    url = fixture_server.route("/amd_chipset.exe", body=b"Not Found\n")
    dest = tmp_path / "amd_chipset.exe"

    result = artifact_fetch.fetch_artifact(url, str(dest), expect_type="pe")

    assert not result["ok"]
    assert result["status"] == 200, "the status alone claimed success"
    assert not dest.exists()
    assert _failed(result, "size") or _failed(result, "magic")


def test_http_404_is_rejected_with_the_status(fixture_server, tmp_path):
    url = fixture_server.route(
        "/missing.exe", status=404, body=b"nope", content_type="text/plain",
    )
    dest = tmp_path / "missing.exe"

    result = artifact_fetch.fetch_artifact(url, str(dest))

    assert not result["ok"]
    assert not dest.exists()
    assert "404" in _failed(result, "http_status")[0]["detail"]


def test_payload_over_the_size_ceiling_is_rejected(fixture_server, tmp_path):
    url = fixture_server.route("/big.zip", body=b"PK\x03\x04" + b"\x00" * (3 << 20))
    dest = tmp_path / "big.zip"

    result = artifact_fetch.fetch_artifact(url, str(dest), max_mb=1)

    assert not result["ok"]
    assert not dest.exists()
    assert not artifact_fetch._part_path(dest).exists()
    assert _failed(result, "size_limit")


def test_a_lying_content_length_cannot_smuggle_an_oversized_payload(
    fixture_server, tmp_path,
):
    """The ceiling is enforced on bytes written, not on what the header claims."""
    url = fixture_server.route(
        "/sneaky.zip",
        body=b"PK\x03\x04" + b"\x00" * (3 << 20),
        headers={"X-Note": "content-length is set by the fixture handler"},
    )
    dest = tmp_path / "sneaky.zip"

    result = artifact_fetch.fetch_artifact(url, str(dest), max_mb=2)

    assert not result["ok"]
    assert not dest.exists()


# --- digest ---------------------------------------------------------------


def test_sha256_mismatch_is_rejected(fixture_server, tmp_path):
    url = fixture_server.route("/pinned.zip", body=ZIP_BODY)
    dest = tmp_path / "pinned.zip"

    result = artifact_fetch.fetch_artifact(url, str(dest), sha256="ab" * 32)

    assert not result["ok"]
    assert not dest.exists()
    assert "digest mismatch" in _failed(result, "sha256")[0]["detail"]


def test_sha256_match_is_accepted(fixture_server, tmp_path):
    import hashlib

    url = fixture_server.route("/pinned-ok.zip", body=ZIP_BODY)
    dest = tmp_path / "pinned-ok.zip"

    result = artifact_fetch.fetch_artifact(
        url, str(dest), sha256=hashlib.sha256(ZIP_BODY).hexdigest().upper(),
    )

    assert result["ok"], result["failures"]
    assert dest.exists()


# --- failure #4: right version number, wrong vendor -----------------------


def test_expect_publisher_mismatch_is_rejected(
    fixture_server, tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        artifact_fetch,
        "_authenticode_signature",
        _signature(publisher="CN=NVIDIA Corporation, O=NVIDIA Corporation, C=US"),
    )
    url = fixture_server.route("/geforce.exe", body=PE_BODY)
    dest = tmp_path / "geforce.exe"

    result = artifact_fetch.fetch_artifact(
        url, str(dest), expect_type="pe", expect_publisher="Advanced Micro Devices",
    )

    assert not result["ok"]
    assert not dest.exists()
    publisher = _failed(result, "publisher")
    assert publisher, result["failures"]
    assert "wrong vendor" in publisher[0]["detail"]
    assert "NVIDIA" in publisher[0]["detail"]


def test_expect_publisher_match_is_accepted_case_insensitively(
    fixture_server, tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        artifact_fetch,
        "_authenticode_signature",
        _signature(publisher="CN=NVIDIA Corporation, O=NVIDIA Corporation, C=US"),
    )
    url = fixture_server.route("/ok-driver.exe", body=PE_BODY)
    dest = tmp_path / "ok-driver.exe"

    result = artifact_fetch.fetch_artifact(
        url, str(dest), expect_publisher="nvidia corporation",
    )

    assert result["ok"], result["failures"]
    record = json.loads(
        Path(result["provenance_path"]).read_text(encoding="utf-8")
    )
    assert record["publisher_common_name"] == "NVIDIA Corporation"
    assert record["signature_status"] == "Valid"


def test_unsigned_binary_fails_an_expected_publisher(
    fixture_server, tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        artifact_fetch,
        "_authenticode_signature",
        _signature(status="NotSigned", publisher=""),
    )
    url = fixture_server.route("/unsigned.exe", body=PE_BODY)
    dest = tmp_path / "unsigned.exe"

    result = artifact_fetch.fetch_artifact(
        url, str(dest), expect_publisher="Microsoft Corporation",
    )

    assert not result["ok"]
    assert "NotSigned" in _failed(result, "publisher")[0]["detail"]
    assert not dest.exists()


def test_publisher_cannot_be_confirmed_without_a_verifier(
    fixture_server, tmp_path,
):
    """The default stub reports an unusable verifier: that must fail closed."""
    url = fixture_server.route("/unverifiable.exe", body=PE_BODY)
    dest = tmp_path / "unverifiable.exe"

    result = artifact_fetch.fetch_artifact(
        url, str(dest), expect_publisher="Intel Corporation",
    )

    assert not result["ok"]
    assert "cannot confirm publisher" in _failed(result, "publisher")[0]["detail"]


# --- redirects, resume, idempotency --------------------------------------


def test_redirect_chain_is_recorded(fixture_server, tmp_path):
    final = fixture_server.route("/cdn/final.exe", body=PE_BODY)
    middle = fixture_server.route(
        "/mirror.exe", status=302, body=b"", headers={"Location": final},
    )
    start = fixture_server.route(
        "/download.exe", status=301, body=b"", headers={"Location": middle},
    )
    dest = tmp_path / "download.exe"

    result = artifact_fetch.fetch_artifact(start, str(dest), expect_type="pe")

    assert result["ok"], result["failures"]
    assert result["final_url"] == final
    hops = result["redirect_chain"]
    assert [hop["status"] for hop in hops] == [301, 302]
    assert hops[0]["url"] == start
    assert hops[1]["url"] == middle

    record = json.loads(
        Path(result["provenance_path"]).read_text(encoding="utf-8")
    )
    assert record["redirect_chain"] == hops
    assert record["url"] == start
    assert record["final_url"] == final


def test_redirect_without_location_is_rejected(fixture_server, tmp_path):
    url = fixture_server.route("/loopy.exe", status=302, body=b"")
    dest = tmp_path / "loopy.exe"

    result = artifact_fetch.fetch_artifact(url, str(dest))

    assert not result["ok"]
    assert not dest.exists()
    assert _failed(result, "redirect")


def test_resume_appends_to_an_existing_partial(fixture_server, tmp_path):
    url = fixture_server.route("/resume.exe", body=PE_BODY)
    dest = tmp_path / "resume.exe"
    part = artifact_fetch._part_path(dest)
    part.write_bytes(PE_BODY[:1000])

    result = artifact_fetch.fetch_artifact(url, str(dest), expect_type="pe")

    assert result["ok"], result["failures"]
    assert result["resumed_from"] == 1000
    assert dest.read_bytes() == PE_BODY
    assert result["sha256"] == artifact_fetch.file_sha256(dest)


def test_rerunning_a_completed_fetch_is_safe(fixture_server, tmp_path):
    url = fixture_server.route("/idempotent.exe", body=PE_BODY)
    dest = tmp_path / "idempotent.exe"

    first = artifact_fetch.fetch_artifact(url, str(dest), expect_type="pe")
    second = artifact_fetch.fetch_artifact(url, str(dest), expect_type="pe")

    assert first["ok"] and second["ok"]
    assert second["reused"] is True
    assert second["action"] == "reused"
    assert second["sha256"] == first["sha256"]
    assert dest.read_bytes() == PE_BODY
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "idempotent.exe", "idempotent.exe.provenance.json",
    ]


def test_rerun_re_reports_a_destination_that_no_longer_verifies(
    fixture_server, tmp_path,
):
    url = fixture_server.route("/drifted.exe", body=PE_BODY)
    dest = tmp_path / "drifted.exe"
    artifact_fetch.fetch_artifact(url, str(dest), expect_type="pe")
    dest.write_bytes(PLAIN_HTML)

    again = artifact_fetch.fetch_artifact(url, str(dest), expect_type="pe")

    assert not again["ok"]
    assert again["action"] == "reused"
    assert _failed(again, "magic")


def test_overwrite_forces_a_fresh_download(fixture_server, tmp_path):
    url = fixture_server.route("/refresh.exe", body=PE_BODY)
    dest = tmp_path / "refresh.exe"
    dest.write_bytes(b"stale")

    result = artifact_fetch.fetch_artifact(
        url, str(dest), expect_type="pe", overwrite=True,
    )

    assert result["ok"], result["failures"]
    assert dest.read_bytes() == PE_BODY


# --- verify_artifact against files already on disk ------------------------


def test_verify_artifact_accepts_a_good_on_disk_binary(tmp_path):
    target = tmp_path / "already-here.exe"
    target.write_bytes(PE_BODY)

    result = artifact_fetch.verify_artifact(str(target), expect_type="pe")

    assert result["ok"], result["failures"]
    assert result["detected_type"] == "pe"
    assert result["bytes"] == len(PE_BODY)
    assert result["sha256"] == artifact_fetch.file_sha256(target)
    assert "VERIFIED" in artifact_fetch.format_verify_result(result)


def test_verify_artifact_rejects_a_staged_block_page(tmp_path):
    target = tmp_path / "staged.exe"
    target.write_bytes(AKAMAI_BLOCK)

    result = artifact_fetch.verify_artifact(str(target), expect_type="pe")

    assert not result["ok"]
    assert _failed(result, "block_page")
    assert _failed(result, "magic")
    assert target.exists(), "verification inspects, it does not delete"


def test_verify_artifact_rejects_an_empty_file(tmp_path):
    target = tmp_path / "empty.iso"
    target.write_bytes(b"")

    result = artifact_fetch.verify_artifact(str(target))

    assert not result["ok"]
    assert "0 bytes" in _failed(result, "size")[0]["detail"]


def test_verify_artifact_checks_a_declared_digest(tmp_path):
    target = tmp_path / "payload.zip"
    target.write_bytes(ZIP_BODY)

    assert artifact_fetch.verify_artifact(
        str(target), sha256=artifact_fetch.file_sha256(target),
    )["ok"]
    assert not artifact_fetch.verify_artifact(
        str(target), sha256="cd" * 32,
    )["ok"]


def test_verify_artifact_reports_a_missing_file(tmp_path):
    result = artifact_fetch.verify_artifact(str(tmp_path / "nothing.exe"))

    assert not result["ok"]
    assert _failed(result, "exists")


def test_verify_artifact_surfaces_the_recorded_provenance(
    fixture_server, tmp_path,
):
    url = fixture_server.route("/traced.exe", body=PE_BODY)
    dest = tmp_path / "traced.exe"
    artifact_fetch.fetch_artifact(url, str(dest), expect_type="pe")

    result = artifact_fetch.verify_artifact(str(dest), expect_type="pe")

    assert result["ok"], result["failures"]
    assert result["provenance_path"].endswith("traced.exe.provenance.json")
    assert artifact_fetch.read_provenance(dest)["url"] == url


def test_paths_outside_allowed_roots_are_refused(tmp_path):
    outside = Path(tmp_path).anchor + "sonder-artifact-fetch-should-not-exist.exe"

    with pytest.raises(PermissionError):
        artifact_fetch.verify_artifact(outside)


def test_missing_inputs_are_refused(tmp_path):
    with pytest.raises(artifact_fetch.ArtifactFetchError):
        artifact_fetch.fetch_artifact("", str(tmp_path / "x.exe"))
    with pytest.raises(artifact_fetch.ArtifactFetchError):
        artifact_fetch.fetch_artifact("http://example.invalid/x", "")
    with pytest.raises(artifact_fetch.ArtifactFetchError):
        artifact_fetch.verify_artifact("")


def test_disabled_web_tools_refuses_to_fetch(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_WEB_TOOLS", "0")

    with pytest.raises(artifact_fetch.ArtifactFetchError):
        artifact_fetch.fetch_artifact(
            "http://example.invalid/x.exe", str(tmp_path / "x.exe"),
        )


# --- block-page detector, in isolation ------------------------------------


@pytest.mark.parametrize(
    "body, expected_marker",
    [
        (AKAMAI_BLOCK, "errors.edgesuite.net"),
        (CLOUDFLARE_BLOCK, "cf-browser-verification"),
        (
            b"<html><head><title>Just a moment...</title></head><body></body></html>",
            "just a moment",
        ),
        (
            b"<html><body><div class=\"g-recaptcha\" data-sitekey=\"x\"></div></body></html>",
            "g-recaptcha",
        ),
        (
            b"<html><head><title>403 Forbidden</title></head><body>nginx</body></html>",
            "403 forbidden",
        ),
        (
            b"<html><body>Request blocked. We are sorry.</body></html>",
            "request blocked",
        ),
    ],
)
def test_detect_block_page_names_known_denials(body, expected_marker):
    block = artifact_fetch.detect_block_page(body, content_type="text/html")

    assert block is not None, body[:80]
    assert block["marker"] == expected_marker
    assert block["reason"]


def test_detect_block_page_passes_real_content():
    page = (
        "<html><head><title>Driver downloads</title></head><body>"
        + "<p>Release notes for the chipset package.</p>" * 400
        + "</body></html>"
    )

    assert artifact_fetch.detect_block_page(page, content_type="text/html") is None


def test_detect_block_page_ignores_prose_that_merely_mentions_captchas():
    """A long article about captchas is content, not a challenge page."""
    page = (
        "<html><head><title>How captcha systems work</title></head><body>"
        + "<p>A captcha asks the visitor to prove they are human.</p>" * 400
        + "</body></html>"
    )

    block = artifact_fetch.detect_block_page(page, content_type="text/html")

    # The title marker is the deliberate exception: a page whose own identity
    # is "captcha" is treated as a challenge even when it is long.
    assert block is None or block["confidence"] == "strong"


def test_detect_block_page_skips_binary_payloads():
    assert artifact_fetch.detect_block_page(
        PE_BODY + b"access denied", content_type="application/octet-stream",
    ) is None


def test_detect_block_page_flags_denial_status_codes():
    block = artifact_fetch.detect_block_page(
        b"<html><body>no</body></html>" + b"x" * 20000,
        content_type="text/html",
        status=403,
    )

    assert block is not None
    assert block["marker"] == "http_403"


def test_format_block_notice_refuses_to_read_as_content():
    block = artifact_fetch.detect_block_page(
        AKAMAI_BLOCK, content_type="text/html", url="https://mirror/x.exe",
    )

    notice = artifact_fetch.format_block_notice("https://mirror/x.exe", block)

    assert notice.startswith("BLOCKED:")
    assert "https://mirror/x.exe" in notice
    assert "refusal, not as data" in notice


# --- magic-byte table -----------------------------------------------------


def test_iso_is_detected_from_its_sector_16_stamp():
    image = bytearray(b"\x00" * 0x8006)
    image[0x8001:0x8006] = b"CD001"

    assert artifact_fetch.detect_type(bytes(image)) == "iso"


@pytest.mark.parametrize(
    "head, expected",
    [
        (b"MZ\x90\x00", "pe"),
        (b"PK\x03\x04", "zip"),
        (b"\x7fELF\x02", "elf"),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "msi"),
        (b"7z\xbc\xaf\x27\x1c", "7z"),
        (b"MSCF\x00", "cab"),
        (b"%PDF-1.7", "pdf"),
        (b"<!DOCTYPE html>", "html"),
        (b"\x99\x98\x97\x96random", "unknown"),
    ],
)
def test_detect_type_reads_magic_bytes(head, expected):
    assert artifact_fetch.detect_type(head) == expected


def test_expected_types_falls_back_to_the_extension():
    assert artifact_fetch.expected_types("", "driver.msi") == (("msi",), "extension .msi")
    assert artifact_fetch.expected_types("exe", "driver.bin")[0] == ("pe",)
    assert artifact_fetch.expected_types("", "notes.unknownext")[1] == "unconstrained"


# --- server wiring --------------------------------------------------------


def test_server_registers_both_tools_and_gates_them():
    import server

    names = {tool.name for tool in server.mcp._tool_manager.list_tools()}
    assert {"fetch_artifact", "verify_artifact"} <= names

    assert "fetch_artifact" in server._PROJECT_SCOPED_PATH_TOOLS
    assert "verify_artifact" in server._PROJECT_SCOPED_PATH_TOOLS
    assert "fetch_artifact" in server._WORK_MUTATION_TOOLS
    assert "verify_artifact" in server._WORK_INSPECTION_TOOLS
    assert "verify_artifact" in server.REPOSITORY_READ_ONLY_TOOLS
    assert "fetch_artifact" not in server.REPOSITORY_READ_ONLY_TOOLS


def test_server_verify_artifact_reports_a_rejection(tmp_path):
    import server

    target = tmp_path / "server-side.exe"
    target.write_bytes(AKAMAI_BLOCK)

    output = server.verify_artifact(str(target), expect_type="pe")

    assert "REJECTED" in output
    assert "block_page" in output
    assert not output.startswith("ERROR:")


def test_server_web_fetch_names_a_block_page_instead_of_returning_it(
    monkeypatch,
):
    """Failure #3 at the web_fetch layer: a denial must not read as the page."""
    import server
    import web_tools

    denial = AKAMAI_BLOCK.decode("utf-8")
    monkeypatch.setattr(web_tools, "web_fetch", lambda url, max_chars=8000: denial)

    output = server.web_fetch("https://mirror.example.com/installer.exe")

    assert output.startswith("BLOCKED:")
    assert "https://mirror.example.com/installer.exe" in output
    assert "refusal, not as data" in output


def test_server_web_fetch_still_returns_real_pages(monkeypatch):
    import server
    import web_tools

    page = "Release notes for the chipset package. " * 200
    monkeypatch.setattr(web_tools, "web_fetch", lambda url, max_chars=8000: page)

    output = server.web_fetch("https://example.com/notes")

    assert output == page
