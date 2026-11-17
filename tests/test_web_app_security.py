"""Security tests for the /app/ static bundle: path handling, request-time
containment, host policy, the origin exemption, API auth and response headers.

Complements test_web_app.py (pure resolver) and test_web_app_http.py (happy path).
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from sonder_runtime.interfaces.http import web_app
from tests.test_serve_host_policy import API_KEY, _server
from tests.test_web_app_http import _enable, _request
from tests.web_app_fixtures import DEFAULT_FILES, manifest_for, write_release

SECRET = b"TOP-SECRET-OUTSIDE-THE-BUNDLE"


@pytest.fixture()
def bundle(tmp_path):
    web_app.clear_cache()
    write_release(tmp_path)
    (tmp_path / "outside.txt").write_bytes(SECRET)
    (tmp_path / "releases" / "r1" / "unlisted.txt").write_bytes(SECRET)
    (tmp_path / "releases" / "r1" / "assets" / "unlisted.json").write_bytes(SECRET)
    yield tmp_path
    web_app.clear_cache()


def _csp(headers) -> dict[str, list[str]]:
    value = {k.lower(): v for k, v in headers.items()}["content-security-policy"]
    out: dict[str, list[str]] = {}
    for part in value.split(";"):
        tokens = part.split()
        if tokens:
            out[tokens[0]] = tokens[1:]
    return out


# -- traversal over a real socket -------------------------------------------

TRAVERSAL = [
    "/app/../outside.txt", "/app/../../outside.txt", "/app/%2e%2e/outside.txt",
    "/app/%2E%2E/outside.txt", "/app/%2e%2e%2foutside.txt", "/app/%2e%2e%2Foutside.txt",
    "/app/%252e%252e/outside.txt", "/app/%252e%252e%252foutside.txt",
    "/app/..%5coutside.txt", "/app/..%5Coutside.txt", "/app/..\\outside.txt",
    "/app/a\\..\\..\\outside.txt", "/app/%5c%5cserver%5cshare",
    "/app/C:/Windows/win.ini", "/app/C%3A/Windows/win.ini", "/app/C%3a%5cWindows%5cwin.ini",
    "/app//outside.txt", "/app///etc/passwd", "/app/%2f%2fetc/passwd",
    "/app/%00", "/app/index.html%00.png", "/app/main.dart.js::$DATA", "/app/main.dart.js%3A%3A%24DATA",
    "/app/.hidden", "/app/assets/.gitkeep", "/app/./main.dart.js", "/app/%2e/main.dart.js",
    "/app/%c0%ae%c0%ae/outside.txt", "/app/%ef%bc%8e%ef%bc%8e/outside.txt",
    "/app/unlisted.txt", "/app/assets/unlisted.json", "/app/sonder-web-manifest.json.bak",
    "/app/main.dart.js.", "/app/%ff",
]


@pytest.mark.parametrize("target", TRAVERSAL)
def test_http_traversal_never_leaks_files(monkeypatch, bundle, target):
    _enable(monkeypatch, bundle)
    with _server(monkeypatch) as port:
        try:
            status, _headers, body = _request(port, "GET", target)
        except http.client.InvalidURL:
            pytest.skip("client refuses to send %r" % target)
    assert SECRET not in body, target
    assert status in (404, 400), (target, status)


def test_traversal_matrix_actually_reaches_the_resolver(monkeypatch, bundle):
    """Guard against the matrix passing vacuously: a legitimate path is served."""
    _enable(monkeypatch, bundle)
    with _server(monkeypatch) as port:
        assert _request(port, "GET", "/app/main.dart.js")[0] == 200


def test_error_target_carries_security_headers_and_no_store(monkeypatch, bundle):
    _enable(monkeypatch, bundle)
    with _server(monkeypatch) as port:
        status, headers, _b = _request(port, "GET", "/app/%2e%2e/outside.txt")
    lowered = {k.lower(): v for k, v in headers.items()}
    assert status == 404
    assert lowered["cache-control"] == "no-store"
    for name, value in web_app.SECURITY_HEADERS:
        assert lowered[name.lower()] == value, name


def test_routing_prefix_is_exact(monkeypatch, bundle):
    _enable(monkeypatch, bundle)
    with _server(monkeypatch) as port:
        for target in ("/APP/", "/appx/", "/application/main.dart.js", "/%61pp/", "/app%2f"):
            status, _h, body = _request(port, "GET", target)
            assert status == 404 and b"<!doctype" not in body, target
            assert "WEB_APP" not in body.decode(), target


def test_redirect_cannot_inject_headers_or_leave_the_app(monkeypatch, bundle):
    _enable(monkeypatch, bundle)
    with _server(monkeypatch) as port:
        status, headers, _b = _request(port, "GET", "/app?x=%0d%0aSet-Cookie:a=b")
    assert status == 308
    assert "Set-Cookie" not in headers
    assert headers["Location"] == "/app/?x=%0d%0aSet-Cookie:a=b"
    assert headers["Location"].startswith("/app/")


def test_post_does_not_serve_the_shell(monkeypatch, bundle):
    _enable(monkeypatch, bundle)
    with _server(monkeypatch) as port:
        status, _h, body = _request(port, "POST", "/app/", body=b"{}")
    assert status != 200 and b"<!doctype" not in body


# -- request-time containment (bundle altered after the load-time check) -----

def _load(root):
    web_app.clear_cache()
    return web_app.load_bundle(root)


def test_file_swapped_for_symlink_after_load_is_refused(tmp_path):
    release = write_release(tmp_path)
    load = _load(tmp_path)
    assert load.ok
    outside = tmp_path / "outside.js"
    outside.write_bytes(b"// app")  # same size and hash: only the link check can stop it
    target = release / "main.dart.js"
    target.unlink()
    try:
        os.symlink(outside, target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not permitted in this environment")
    assert web_app.resolve_request("/app/main.dart.js", load).kind == "not_found"


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows only")
def test_directory_swapped_for_junction_after_load_is_refused(tmp_path):
    release = write_release(tmp_path)
    load = _load(tmp_path)
    assert load.ok
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "fonts").mkdir(parents=True)
    (elsewhere / "fonts" / "a.ttf").write_bytes(DEFAULT_FILES["assets/fonts/a.ttf"])
    (elsewhere / "font.woff2").write_bytes(DEFAULT_FILES["assets/font.woff2"])
    (elsewhere / "NOTICES").write_bytes(DEFAULT_FILES["assets/NOTICES"])
    (elsewhere / "AssetManifest.bin").write_bytes(DEFAULT_FILES["assets/AssetManifest.bin"])
    import shutil
    shutil.rmtree(release / "assets")
    done = subprocess.run(["cmd", "/c", "mklink", "/J", str(release / "assets"), str(elsewhere)],
                          capture_output=True)
    if done.returncode != 0:
        pytest.skip("junction creation failed: %r" % done.stdout)
    assert web_app.resolve_request("/app/assets/fonts/a.ttf", load).kind == "not_found"


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows only")
def test_junction_inside_release_is_refused_even_when_target_is_inside(tmp_path):
    """Only the explicit link check can stop this: containment alone would pass."""
    files = dict(DEFAULT_FILES)
    files["alias/a.ttf"] = files["assets/fonts/a.ttf"]
    release = write_release(tmp_path, files=files, point=False)
    import shutil
    shutil.rmtree(release / "alias")
    done = subprocess.run(["cmd", "/c", "mklink", "/J", str(release / "alias"),
                           str(release / "assets" / "fonts")], capture_output=True)
    if done.returncode != 0:
        pytest.skip("junction creation failed: %r" % done.stdout)
    from tests.web_app_fixtures import point_to
    point_to(tmp_path, "r1")
    load = _load(tmp_path)
    assert not load.ok and "alias/a.ttf" in load.message


def test_symlink_inside_release_is_refused_even_when_target_is_inside(tmp_path):
    files = dict(DEFAULT_FILES)
    files["linked.js"] = files["main.dart.js"]
    release = write_release(tmp_path, files=files, point=False)
    (release / "linked.js").unlink()
    try:
        os.symlink(release / "main.dart.js", release / "linked.js")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not permitted in this environment")
    from tests.web_app_fixtures import point_to
    point_to(tmp_path, "r1")
    load = _load(tmp_path)
    assert not load.ok and "linked.js" in load.message


def test_file_replaced_after_load_is_not_served(monkeypatch, bundle):
    """The size is re-checked at send time; tampered bytes never go out."""
    _enable(monkeypatch, bundle)
    with _server(monkeypatch) as port:
        assert _request(port, "GET", "/app/main.dart.js")[0] == 200
        (bundle / "releases" / "r1" / "main.dart.js").write_bytes(b"// evil, and longer")
        status, _h, body = _request(port, "GET", "/app/main.dart.js")
    assert status == 500 and b"evil" not in body
    assert json.loads(body)["error"]["code"] == "WEB_APP_BUNDLE_MODIFIED"


# -- manifest membership is the allowlist ------------------------------------

def test_manifest_listing_the_manifest_or_dotfile_is_refused(tmp_path):
    for rel in (".last_build_id", "../x", "a/../b", "C:/x", "a\\b", "x::$DATA"):
        files = dict(DEFAULT_FILES)
        release = write_release(tmp_path, files=files)
        manifest = manifest_for(files)
        manifest["files"][rel] = manifest["files"]["main.dart.js"]
        (release / "sonder-web-manifest.json").write_text(json.dumps(manifest))
        web_app.clear_cache()
        assert not web_app.load_bundle(tmp_path).ok, rel


def test_current_json_cannot_point_outside_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    write_release(tmp_path / "other", "r1", point=False)
    for name in ("../other/releases/r1", "..\\other", str(tmp_path / "other" / "releases" / "r1"),
                 "r1/../../other", "%2e%2e"):
        (root / "current.json").write_text(json.dumps({"release": name}))
        assert not web_app.load_bundle(root).ok, name


# -- host policy on the new routes -------------------------------------------

@pytest.mark.parametrize("target", ["/app", "/app/", "/app/main.dart.js", "/app/settings/x"])
def test_rebinding_host_is_421_on_every_app_route_and_leaks_nothing(monkeypatch, bundle, target):
    _enable(monkeypatch, bundle)
    with _server(monkeypatch) as port:
        status, headers, body = _request(port, "GET", target, host="evil.example:%d" % port)
    assert status == 421, target
    assert json.loads(body)["error"]["code"] == "HOST_NOT_ALLOWED"
    assert "Location" not in headers and b"<!doctype" not in body


def test_duplicate_host_header_is_421_on_app(monkeypatch, bundle):
    _enable(monkeypatch, bundle)
    with _server(monkeypatch) as port:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.putrequest("GET", "/app/", skip_host=True)
            conn.putheader("Host", "127.0.0.1:%d" % port)
            conn.putheader("Host", "evil.example")
            conn.endheaders()
            assert conn.getresponse().status == 421
        finally:
            conn.close()


def test_rebinding_host_gets_421_even_with_matching_origin_header(monkeypatch, bundle):
    _enable(monkeypatch, bundle)
    host = "evil.example:%d"
    with _server(monkeypatch) as port:
        status, _h, _b = _request(port, "GET", "/app/", host=host % port,
                                  headers={"Origin": "http://" + host % port})
    assert status == 421


# -- API auth and origin gate unchanged --------------------------------------

def test_api_auth_is_identical_with_the_web_app_enabled(monkeypatch, bundle):
    results = []
    for enabled in (False, True):
        _enable(monkeypatch, bundle, enabled=enabled)
        with _server(monkeypatch, api_key=API_KEY) as port:
            results.append((
                _request(port, "GET", "/v1/sonder/status")[0],
                _request(port, "GET", "/v1/sonder/status", headers={"Authorization": "Bearer wrong"})[0],
                _request(port, "GET", "/v1/sonder/status",
                         headers={"Origin": "http://127.0.0.1:%d" % port})[0],
            ))
    disabled, enabled = results
    assert disabled[:2] == enabled[:2] == (401, 401)
    # A same-origin Origin only clears the origin gate (403 -> 401), never auth.
    assert disabled[2] == 403 and enabled[2] == 401


def test_same_origin_post_with_wrong_key_is_401_not_403(monkeypatch, bundle):
    """The exemption opens only the origin gate, never authentication."""
    _enable(monkeypatch, bundle)
    with _server(monkeypatch, api_key=API_KEY) as port:
        origin = "http://127.0.0.1:%d" % port
        for headers in ({"Origin": origin},
                        {"Origin": origin, "Authorization": "Bearer wrong"}):
            status, _h, _b = _request(port, "POST", "/v1/chat/completions",
                                      headers=headers, body=b"{}")
            assert status == 401, headers


@pytest.mark.parametrize("origin", [
    "http://127.0.0.1:1", "http://127.0.0.2:%(port)d", "http://localhost:%(port)d",
    "http://127.0.0.1:%(port)d/", "http://127.0.0.1:%(port)d/x", "http://user@127.0.0.1:%(port)d",
    "https://127.0.0.1:%(port)d", "HTTP://EVIL.EXAMPLE", "null", "*",
    "http://127.0.0.1:%(port)d, http://evil.example",
])
def test_non_matching_origins_stay_forbidden(monkeypatch, bundle, origin):
    _enable(monkeypatch, bundle)
    with _server(monkeypatch, api_key=API_KEY) as port:
        status, _h, body = _request(port, "POST", "/v1/chat/completions",
                                    headers={"Origin": origin % {"port": port}}, body=b"{}")
    assert status == 403 and json.loads(body)["error"]["code"] == "forbidden_origin"


def test_repeated_origin_header_stays_forbidden(monkeypatch, bundle):
    _enable(monkeypatch, bundle)
    with _server(monkeypatch, api_key=API_KEY) as port:
        origin = "http://127.0.0.1:%d" % port
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.putrequest("POST", "/v1/chat/completions", skip_host=True)
            conn.putheader("Host", "127.0.0.1:%d" % port)
            conn.putheader("Origin", origin)
            conn.putheader("Origin", origin)
            conn.putheader("Content-Length", "2")
            conn.endheaders(b"{}")
            assert conn.getresponse().status == 403
        finally:
            conn.close()


def test_cross_origin_page_cannot_read_the_shell(monkeypatch, bundle):
    _enable(monkeypatch, bundle)
    with _server(monkeypatch) as port:
        for origin in ("http://evil.example", "null", "http://127.0.0.1:%d" % port):
            _s, headers, _b = _request(port, "GET", "/app/", headers={"Origin": origin})
            lowered = {k.lower() for k in headers}
            assert "access-control-allow-origin" not in lowered, origin
            assert "access-control-allow-credentials" not in lowered, origin


def test_options_preflight_gets_no_cors_grant_for_app(monkeypatch, bundle):
    _enable(monkeypatch, bundle)
    with _server(monkeypatch) as port:
        _s, headers, _b = _request(port, "OPTIONS", "/app/main.dart.js", headers={
            "Origin": "http://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in {k.lower() for k in headers}


# -- CSP and headers ----------------------------------------------------------

@pytest.mark.parametrize("target,expected", [
    ("/app/", 200), ("/app/main.dart.js", 200), ("/app/settings/x", 200),
    ("/app", 308), ("/app/nope.js", 404), ("/app/..%5cx", 404),
])
def test_csp_and_headers_on_every_app_response_kind(monkeypatch, bundle, target, expected):
    _enable(monkeypatch, bundle)
    with _server(monkeypatch) as port:
        status, headers, _b = _request(port, "GET", target)
        assert status == expected
        csp = _csp(headers)
        assert csp["default-src"] == ["'none'"]
        assert csp["script-src"] == ["'self'", "'wasm-unsafe-eval'"]
        assert csp["connect-src"] == ["'self'"]
        assert csp["frame-ancestors"] == ["'none'"]
        assert csp["object-src"] == ["'none'"] and csp["form-action"] == ["'none'"]
        assert csp["base-uri"] == ["'self'"]
        lowered = {k.lower(): v for k, v in headers.items()}
        assert lowered["x-content-type-options"] == "nosniff"
        assert lowered["x-frame-options"] == "DENY"
        assert lowered["cross-origin-resource-policy"] == "same-origin"


def test_csp_forbids_script_escape_hatches(monkeypatch, bundle):
    _enable(monkeypatch, bundle)
    with _server(monkeypatch) as port:
        _s, headers, _b = _request(port, "GET", "/app/")
    csp = _csp(headers)
    every = [token for tokens in csp.values() for token in tokens]
    assert "'unsafe-eval'" not in every
    assert "'unsafe-inline'" not in csp["script-src"]
    for directive in ("script-src", "connect-src", "default-src", "worker-src"):
        assert not {"*", "http:", "https:", "data:"} & set(csp[directive]), directive


def test_referrer_policy_keeps_same_origin_posts_working(monkeypatch, bundle):
    """no-referrer makes browsers send 'Origin: null' on POSTs, which the gate refuses."""
    _enable(monkeypatch, bundle)
    with _server(monkeypatch) as port:
        _s, headers, _b = _request(port, "GET", "/app/")
    value = {k.lower(): v for k, v in headers.items()}["referrer-policy"]
    assert value == "same-origin"


def test_shell_has_no_inline_script_for_the_csp(monkeypatch, tmp_path):
    """The committed web shell must load under script-src without unsafe-inline."""
    import re
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "app" / "web" / "index.html").read_text("utf-8")
    scripts = re.findall(r"<script\b([^>]*)>", html, re.IGNORECASE)
    assert scripts and all("src=" in attrs for attrs in scripts)
    assert not re.search(r"\son[a-z]+\s*=", html, re.IGNORECASE)


# -- concurrency ---------------------------------------------------------------

@pytest.mark.parametrize("schedule", ["free", "starved-first-reader", "swap-mid-load"])
def test_concurrent_requests_during_swap_never_see_a_half_loaded_bundle(
        tmp_path, monkeypatch, schedule):
    """Six request threads hammer the bundle cache while ``current.json`` flips between
    two releases 30 times.  Every response must come from one whole release (the file
    served matches the manifest of the bundle that answered), and the release must have
    been current at some point during that request.

    The writer waits for a request that began after each swap to finish before making
    the next swap, so every swap is observed whatever the thread scheduling.  Without
    that wait, a reader starved for the length of the writer's loop saw only the final
    pointer (r1) and the test flaked (CI run 43828341167).  Schedules forced on top:

    * ``starved-first-reader``: the first bundle load stalls before it reads anything.
    * ``swap-mid-load``: at three swaps a load reads the pointer, then holds until the
      next swap has landed, then finishes, so the pointer changes under a load in
      progress in both directions (r2 to r1 and r1 to r2).
    """
    from tests.web_app_fixtures import point_to
    web_app.clear_cache()
    write_release(tmp_path, "r1")
    files = dict(DEFAULT_FILES)
    files["main.dart.js"] = b"// second build"
    write_release(tmp_path, "r2", files=files, point=False)
    main_js = {"r1": DEFAULT_FILES["main.dart.js"], "r2": files["main.dart.js"]}

    swaps = threading.Condition()
    landed = [0]  # swaps fully in place: pointer replaced and its mtime stamped
    begun = [0]  # swaps that may have touched current.json (landed, or in flight)
    current = {0: "r1"}  # swap generation -> release current.json names
    # (swaps landed before the request began, swaps begun when it ended, release served)
    observed: list[tuple[int, int, str]] = []
    newest_began = [0]  # max "began" over observed, so the writer's wait stays O(1)
    errors: list[str] = []
    stop = threading.Event()

    hold_at = {3, 10, 21} if schedule == "swap-mid-load" else set()
    # Generation at which the next pointer read must hold its load. -1 = disarmed:
    # with 0, a reader that read the pointer before the first swap began held at
    # generation 0 too (seen on Linux CI, where readers start before the writer).
    armed = [-1]
    held = [0]  # generation whose pointer a held load has read
    held_done: set[int] = set()
    # (generation the pointer was read at, release it named, swaps landed when the load
    #  resumed, swaps landed when the request began, swaps begun when it ended, served)
    straddles: list[tuple[int, str, int, int, int, str]] = []
    local = threading.local()

    if schedule == "starved-first-reader":
        real_load = web_app.load_bundle
        loads = {"n": 0}

        def slow_first_load(root):
            loads["n"] += 1
            if loads["n"] == 1:
                time.sleep(0.25)
            return real_load(root)

        monkeypatch.setattr(web_app, "load_bundle", slow_first_load)
    elif schedule == "swap-mid-load":
        real_read_json = web_app._read_json

        def read_pointer_then_hold(path):
            value = real_read_json(path)
            if os.path.basename(path) != web_app.CURRENT_NAME:
                return value
            with swaps:
                generation = landed[0]
                if (generation != armed[0] or begun[0] != generation
                        or generation in held_done):
                    return value
                held_done.add(generation)
                held[0] = generation
                swaps.notify_all()
                if not swaps.wait_for(lambda: landed[0] > generation, timeout=10):
                    errors.append(f"swap {generation + 1} never landed under the held load")
                named = value.get("release") if isinstance(value, dict) else None
                local.held = (generation, str(named), landed[0])
            return value

        monkeypatch.setattr(web_app, "_read_json", read_pointer_then_hold)

    def request() -> None:
        local.held = None
        with swaps:
            began = landed[0]
        load = web_app.cached_bundle(tmp_path)
        if not load.ok:
            # Windows can refuse the read while os.replace is in flight; that
            # failure is transient and never cached (test below).
            if not load.transient:
                errors.append(load.message)
            return
        assert load.bundle is not None
        release = load.bundle.release_dir
        result = web_app.resolve_request("/app/main.dart.js", load)
        if result.kind != "file" or result.path is None:
            errors.append(result.kind)
        else:
            body = result.path.read_bytes()
            if (not result.path.is_relative_to(release)
                    or body != main_js.get(release.name)
                    or hashlib.sha256(body).hexdigest()
                    != load.bundle.files["main.dart.js"].sha256):
                errors.append(f"{release.name} served main.dart.js from {result.path} "
                              "that does not match the answering bundle's manifest")
        with swaps:
            observed.append((began, begun[0], release.name))
            if local.held is not None:
                straddles.append((*local.held, began, begun[0], release.name))
            newest_began[0] = max(newest_began[0], began)
            swaps.notify_all()

    def reader() -> None:
        while not stop.is_set():
            try:
                request()
            except Exception as exc:  # noqa: BLE001 - reported to the test; a dead reader reads as a hang
                errors.append(repr(exc))
                return

    threads = [threading.Thread(target=reader) for _ in range(6)]
    for thread in threads:
        thread.start()
    try:
        for index in range(30):
            generation, name = index + 1, ("r2" if index % 2 == 0 else "r1")
            with swaps:
                current[generation] = name
                begun[0] = generation
            deadline = time.monotonic() + 10
            while True:
                try:
                    point_to(tmp_path, name)
                    break
                except PermissionError:  # Windows refuses os.replace while a reader has the file open
                    if time.monotonic() > deadline:
                        raise AssertionError("could not swap current.json") from None
                    stop.wait(0.005)
            stamp = (tmp_path / "current.json").stat().st_mtime_ns + generation * 5_000_000
            os.utime(tmp_path / "current.json", ns=(stamp, stamp))
            with swaps:
                landed[0] = generation
                swaps.notify_all()  # wakes a load held on this swap
                if not swaps.wait_for(
                        lambda generation=generation: newest_began[0] >= generation, timeout=10):
                    raise AssertionError(
                        f"no request completed after swap {generation} to {name}: {errors[:3]}")
            if generation in hold_at:
                # Swap `generation` has been observed.  Now make the next load read this
                # pointer and hold, and make the next swap while it holds.  The cache is
                # cleared outside the condition because a held load owns the cache lock.
                with swaps:
                    armed[0] = generation
                web_app.clear_cache()
                with swaps:
                    if not swaps.wait_for(
                            lambda generation=generation: held[0] == generation, timeout=10):
                        raise AssertionError(
                            f"no load read the pointer after swap {generation}: {errors[:3]}")
    finally:
        stop.set()
        for thread in threads:
            thread.join(10)
    assert not any(thread.is_alive() for thread in threads)
    web_app.clear_cache()
    assert not errors, errors[:3]
    seen = [release for _, _, release in observed]
    assert set(seen) <= {"r1", "r2"} and "r2" in seen
    # Every swap was seen by a request that started after it landed ...
    assert {began for began, _, _ in observed} >= set(range(1, 31))
    # ... and no request was answered from a release that was not current during it.
    stale = [(began, ended, release) for began, ended, release in observed
             if release not in {current[g] for g in range(began, ended + 1)}]
    assert not stale, stale[:3]
    if schedule == "swap-mid-load":
        # Each held load really had the pointer change under it, and still answered
        # with the whole release that its own pointer read named.
        assert sorted(g for g, *_ in straddles) == sorted(hold_at), straddles
        for generation, named, resumed_at, began, ended, served in straddles:
            assert named == current[generation] != current[resumed_at], straddles
            assert resumed_at > generation and ended > began, straddles
            assert served == named, straddles
        assert any(ended > began for began, ended, _ in observed)


def test_transient_read_failure_is_not_cached(tmp_path, monkeypatch):
    web_app.clear_cache()
    write_release(tmp_path)
    real = web_app._read_json
    calls = {"n": 0}

    def flaky(path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("sharing violation during os.replace")
        return real(path)

    monkeypatch.setattr(web_app, "_read_json", flaky)
    assert not web_app.cached_bundle(tmp_path).ok  # the racing request
    assert web_app.cached_bundle(tmp_path).ok  # the next one must recover
    web_app.clear_cache()
