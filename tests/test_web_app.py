"""Pure behaviour of the /app/ static bundle resolver."""

from __future__ import annotations

import json
import os
import sys

import pytest

from sonder_runtime.interfaces.http import web_app
from tests.web_app_fixtures import DEFAULT_FILES, manifest_for, point_to, write_release


@pytest.fixture()
def root(tmp_path):
    web_app.clear_cache()
    write_release(tmp_path)
    yield tmp_path
    web_app.clear_cache()


def _resolve(root, target, inm=None):
    return web_app.resolve_request(target, web_app.load_bundle(root), inm)


def test_index_and_assets_resolve(root):
    for target in ("/app/", "/app/index.html", "/app/main.dart.js", "/app/canvaskit/canvaskit.wasm"):
        result = _resolve(root, target)
        assert result.kind == "file", target
        assert result.path.is_file()


def test_bare_app_redirects_keeping_query(root):
    result = _resolve(root, "/app?x=1")
    assert (result.kind, result.status, result.location) == ("redirect", 308, "/app/?x=1")
    assert _resolve(root, "/app").location == "/app/"


@pytest.mark.parametrize("target", [
    "/app/../x", "/app/%2e%2e/x", "/app/%2e%2e%2fx", "/app/%252e%252e", "/app/..%5cx",
    "/app/a\\..\\x", "/app/C:/Windows/win.ini", "/app//etc/passwd", "/app/%00",
    "/app/main.dart.js::$DATA", "/app/.hidden", "/app/%2E%2E%2Fx", "/app/%5C", "/app/a%00b",
    "/app/%ff", "/app/main.dart.js.", "/app/secret.txt", "/app/assets/../main.dart.js",
])
def test_traversal_and_unlisted_are_refused(root, target):
    (root / "releases" / "r1" / "secret.txt").write_text("nope")
    (root / "outside.txt").write_text("nope")
    result = _resolve(root, target)
    assert result.kind == "not_found", target


def test_unlisted_existing_file_is_not_served_even_in_subdir(root):
    (root / "releases" / "r1" / "assets" / "extra.json").write_text("{}")
    assert _resolve(root, "/app/assets/extra.json").kind == "not_found"


def test_spa_fallback_and_missing_asset(root):
    assert _resolve(root, "/app/settings/profile").path.name == "index.html"
    assert _resolve(root, "/app/settings/").path.name == "index.html"
    assert _resolve(root, "/app/missing.js").kind == "not_found"


@pytest.mark.parametrize("name,expected", [
    ("a.wasm", "application/wasm"), ("a.js", "text/javascript; charset=utf-8"),
    ("a.mjs", "text/javascript; charset=utf-8"), ("a.json", "application/json"),
    ("a.ttf", "font/ttf"), ("a.woff2", "font/woff2"), ("assets/NOTICES", "text/plain; charset=utf-8"),
    ("a.bin", "application/octet-stream"), ("a.html", "text/html; charset=utf-8"),
    ("a.weird", "application/octet-stream"),
])
def test_mime_table(name, expected):
    assert web_app.content_type_for(name) == expected


def test_etag_and_not_modified(root):
    first = _resolve(root, "/app/main.dart.js")
    assert first.etag.startswith('"') and len(first.etag) == 66
    again = _resolve(root, "/app/main.dart.js", first.etag)
    assert (again.kind, again.status) == ("not_modified", 304)
    assert _resolve(root, "/app/main.dart.js", "W/" + first.etag).kind == "not_modified"
    assert _resolve(root, "/app/main.dart.js", '"other"').kind == "file"


def test_missing_current_is_not_built(tmp_path):
    result = _resolve(tmp_path, "/app/")
    assert result.kind == "not_built"
    assert result.code == "WEB_APP_NOT_BUILT" and "build_web_app.py" in result.remedy


@pytest.mark.parametrize("release", ["../outside", "..", ".hidden", "a/b", "a\\b", "", "x" * 129])
def test_bad_release_names_are_not_built(tmp_path, release):
    write_release(tmp_path)
    (tmp_path / "current.json").write_text(json.dumps({"release": release}))
    assert web_app.load_bundle(tmp_path).ok is False


def test_size_and_hash_mismatch_are_refused(tmp_path):
    release = write_release(tmp_path)
    (release / "main.dart.js").write_bytes(b"// app!")  # size changed
    assert web_app.load_bundle(tmp_path).ok is False
    (release / "main.dart.js").write_bytes(b"// bpp")  # same size, other hash
    assert web_app.load_bundle(tmp_path).ok is False
    (release / "main.dart.js").write_bytes(b"// app")
    assert web_app.load_bundle(tmp_path).ok is True


def test_unlisted_extra_index_html_is_refused(tmp_path):
    release = write_release(tmp_path)
    (release / "assets" / "index.html").write_text("<script>evil()</script>")
    load = web_app.load_bundle(tmp_path)
    assert not load.ok and "index.html" in load.message


def test_manifest_with_bad_relpath_is_refused(tmp_path):
    files = dict(DEFAULT_FILES)
    release = write_release(tmp_path, files=files)
    manifest = manifest_for(files)
    manifest["files"]["../evil.js"] = manifest["files"]["main.dart.js"]
    (release / "sonder-web-manifest.json").write_text(json.dumps(manifest))
    assert not web_app.load_bundle(tmp_path).ok


def test_manifest_without_index_is_refused(tmp_path):
    files = {k: v for k, v in DEFAULT_FILES.items() if k != "index.html"}
    write_release(tmp_path, files=files)
    assert not web_app.load_bundle(tmp_path).ok


def test_symlink_escape_is_refused(tmp_path):
    release = write_release(tmp_path)
    outside = tmp_path / "outside.js"
    outside.write_bytes(b"// app")
    target = release / "main.dart.js"
    target.unlink()
    try:
        os.symlink(outside, target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not permitted in this environment")
    assert not web_app.load_bundle(tmp_path).ok


def test_release_symlink_pointing_outside_root_is_refused(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    real = tmp_path / "elsewhere"
    write_release(real, "r1", point=False)
    (root / "releases").mkdir()
    try:
        os.symlink(real / "releases" / "r1", root / "releases" / "r1", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not permitted in this environment")
    point_to(root, "r1")
    assert not web_app.load_bundle(root).ok


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows only")
def test_junction_release_is_refused(tmp_path):
    import subprocess
    root = tmp_path / "root"
    (root / "releases").mkdir(parents=True)
    real = tmp_path / "elsewhere"
    write_release(real, "r1", point=False)
    junction = root / "releases" / "r1"
    done = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(real / "releases" / "r1")],
        capture_output=True)
    if done.returncode != 0:
        pytest.skip("junction creation failed: %r" % done.stdout)
    point_to(root, "r1")
    assert not web_app.load_bundle(root).ok


def test_cached_bundle_picks_up_a_swap(tmp_path):
    web_app.clear_cache()
    write_release(tmp_path, "r1")
    first = web_app.cached_bundle(tmp_path)
    assert first.ok and web_app.cached_bundle(tmp_path) is first
    files = dict(DEFAULT_FILES)
    files["main.dart.js"] = b"// second build"
    write_release(tmp_path, "r2", files=files)
    stamp = (tmp_path / "current.json").stat().st_mtime_ns + 5_000_000
    os.utime(tmp_path / "current.json", ns=(stamp, stamp))
    second = web_app.cached_bundle(tmp_path)
    assert second.ok and second.bundle.release_dir.name == "r2"
    web_app.clear_cache()


@pytest.mark.parametrize("origin,host,https,expected", [
    ("http://127.0.0.1:11435", "127.0.0.1:11435", False, True),
    ("http://LOCALHOST:11435", "localhost:11435", False, True),
    ("http://localhost", "localhost:80", False, True),
    ("http://localhost:80", "localhost", False, True),
    ("http://localhost:11435", "localhost:11436", False, False),
    ("http://localhost:11435", "other:11435", False, False),
    ("https://sonder.example", "sonder.example", True, True),
    ("https://sonder.example:443", "sonder.example", True, True),
    ("https://sonder.example", "sonder.example", False, False),
    ("http://sonder.example", "sonder.example:443", True, False),
    ("null", "localhost", False, False),
    (None, "localhost", False, False),
    ("http://localhost/path", "localhost", False, False),
    ("http://[::1]:5", "[::1]:5", False, True),
    ("http://user@localhost", "localhost", False, False),
    ("ftp://localhost", "localhost", False, False),
    ("http://localhost", None, False, False),
])
def test_same_origin_matrix(origin, host, https, expected):
    assert web_app.same_origin(origin, host, https_declared=https) is expected


def test_same_origin_request_refuses_repeated_origin():
    from email.message import Message
    headers = Message()
    headers["Host"] = "localhost:1"
    headers["Origin"] = "http://localhost:1"
    assert web_app.same_origin_request(headers, https_declared=False) is True
    headers["Origin"] = "http://localhost:1"
    assert web_app.same_origin_request(headers, https_declared=False) is False
