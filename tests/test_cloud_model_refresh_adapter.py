"""Adapters of the cloud model refresh against a loopback fake Ollama and a tmp home.

Never the real state home, never the network: the catalog transport is a spy,
Ollama is ``tests/support/cloud_refresh_harness.FakeOllama`` on 127.0.0.1:0.
"""

from __future__ import annotations

import json
import os
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

import sonder_runtime.adapters.cloud_model_refresh as refresh
import sonder_runtime.adapters.ollama_cloud_catalog as catalog
from sonder_runtime.adapters import model_lists
from sonder_runtime.adapters.filesystem import atomic_json
from sonder_runtime.adapters.inference.ollama_cloud_metadata import (
    MAX_BODY,
    OllamaCloudMetadata,
    OllamaPullFailed,
    OllamaTransportError,
)
from sonder_runtime.domain import cloud_model_refresh as crm
from sonder_runtime.domain.cloud_model_refresh import ProbeStatus
from tests.support.cloud_refresh_harness import (
    MEASURED_RETIREMENT,
    World,
    make_world,
    retired_message,
    snapshot,
    standard_shows,
)

SHIPPED = model_lists.load_shipped_document()
DEFAULT_CODE = SHIPPED["default_cloud_models"]["code"]
DEFAULT_GENERAL = SHIPPED["default_cloud_models"]["general"]
NOW = 1_790_000_000.0
LADDER = {"code": ["kimi-k3-code:cloud", "kimi-k3:cloud"], "version": 1}


@pytest.fixture
def world(tmp_path, monkeypatch):
    created: list[World] = []

    def build(**kwargs):
        kwargs.setdefault("tags", ("kimi-k3-code:cloud", "kimi-k3:cloud"))
        kwargs.setdefault("shows", standard_shows())
        built = make_world(tmp_path, monkeypatch, **kwargs)
        created.append(built)
        return built

    build.created = created  # type: ignore[attr-defined]
    yield build
    for built in created:
        built.fake.close()
    model_lists.clear_cache()


@pytest.fixture
def std(world):
    built = world()
    built.write_preferences(LADDER)
    return built


def _log_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _actions(path: Path) -> list[str]:
    return [line["action"] for line in _log_lines(path)]


# --- 10. OllamaCloudMetadata ---------------------------------------------------


def test_show_live_returns_metadata(world):
    w = world()
    probe = OllamaCloudMetadata(w.fake.url).show("kimi-k3:cloud")
    assert probe.status is ProbeStatus.LIVE
    assert (probe.parameters, probe.context_length, probe.family) == ("1.2T", 262144, "fakefam")
    assert probe.capabilities == ("completion", "tools", "thinking")
    body = [b for v, p, b in w.fake.requests if p == "/api/show"][0]
    assert body == {"model": "kimi-k3:cloud", "name": "kimi-k3:cloud"}


@pytest.mark.parametrize("status", [400, 404, 410, 500, 200])
def test_show_retirement_error_is_retired_at_any_http_status(world, status):
    w = world(shows={"x:cloud": (status, {"error": MEASURED_RETIREMENT})})
    probe = OllamaCloudMetadata(w.fake.url).show("x:cloud")
    assert probe.status is ProbeStatus.RETIRED
    assert probe.retirement.provider_name == "deepseek-v4-flash:0731"
    assert probe.retirement.suggestion == "glm-5.3-flash:cloud"


def test_show_not_found_and_generic_errors_draw_no_retirement_conclusion(world):
    w = world(shows={
        "missing:cloud": (404, {"error": "model 'missing:cloud' not found"}),
        "boom:cloud": (500, {"error": "upstream exploded"}),
        "weird:cloud": (200, "not-a-dict"),
    })
    client = OllamaCloudMetadata(w.fake.url)
    assert client.show("missing:cloud").status is ProbeStatus.NOT_FOUND
    boom = client.show("boom:cloud")
    assert boom.status is ProbeStatus.ERROR and boom.retirement is None
    assert client.show("weird:cloud").status is ProbeStatus.ERROR


def test_closed_port_raises_transport_error(world):
    w = world()
    w.fake.close()
    client = OllamaCloudMetadata(w.fake.url, show_timeout=2, tags_timeout=2)
    with pytest.raises(OllamaTransportError):
        client.show("a:cloud")
    with pytest.raises(OllamaTransportError):
        client.tags()


def test_pull_stub_refuses_non_stub_names_before_any_io(world):
    w = world()
    client = OllamaCloudMetadata(w.fake.url)
    for name in ("qwen2.5:7b", "foo-cloudy:7b", "cloud", ":cloud", "x y:cloud", "", None):
        with pytest.raises(ValueError):
            client.pull_stub(name)  # type: ignore[arg-type]
    assert w.fake.requests == []


def test_pull_stub_posts_non_streaming_pull_and_reports_failures(world):
    w = world(pull_status=500, pull_payload={"error": "nope"})
    client = OllamaCloudMetadata(w.fake.url)
    with pytest.raises(OllamaPullFailed):
        client.pull_stub("kimi-k3:cloud")
    w.fake.pull_status, w.fake.pull_payload = 200, {"status": "success"}
    client.pull_stub("kimi-k3:cloud")
    w.fake.pull_payload = {"error": "disk full"}
    with pytest.raises(OllamaPullFailed):
        client.pull_stub("kimi-k3:1017-cloud")
    pulls = [b for _v, p, b in w.fake.requests if p == "/api/pull"]
    assert pulls[1] == {"model": "kimi-k3:cloud", "stream": False}


def test_tags_returns_only_cloud_rows_by_name_or_remote_host():
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, _n=-1):
            rows = [{"name": "a:cloud"}, {"name": "b-cloud"}, {"name": "c:7b", "remote_host": "https://ollama.com:443"},
                    {"name": "local:7b"}, "junk", {"name": None}]
            return json.dumps({"models": rows}).encode()

    client = OllamaCloudMetadata("http://127.0.0.1:1", opener=lambda *a, **k: Response())
    assert [r["name"] for r in client.tags()] == ["a:cloud", "b-cloud", "c:7b"]


def test_metadata_client_uses_open_url_remote_policy_and_bounds_bodies():
    calls = []

    def opener(request, timeout, allow_remote):
        calls.append((request.full_url, request.get_method(), timeout, allow_remote))
        raise ValueError("remote Ollama is not allowed")

    client = OllamaCloudMetadata("http://ollama.example:11434", opener=opener)
    with pytest.raises(OllamaTransportError) as caught:
        client.show("a:cloud")
    assert caught.value.reason == "ollama_policy_refused"
    assert calls == [("http://ollama.example:11434/api/show", "POST", 10.0, None)]

    class Huge:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n=-1):
            return b" " * (MAX_BODY + 1)

    with pytest.raises(OllamaTransportError) as huge:
        OllamaCloudMetadata("http://127.0.0.1:1", opener=lambda *a, **k: Huge()).show("a:cloud")
    assert huge.value.reason == "ollama_body_too_large"


def test_no_chat_generate_or_embed_path_anywhere_in_the_refresh_code():
    import sonder_runtime.adapters.inference.ollama_cloud_metadata as metadata_module
    import sonder_runtime.bootstrap.cloud_models_cli as cli_module

    root = Path(refresh.__file__).parent
    sources = [Path(m.__file__) for m in (metadata_module, refresh, catalog, cli_module)]
    sources += [root.parent / "domain" / "cloud_model_refresh.py", root / "cloud_model_refresh_schedule.py"]
    for source in sources:
        text = source.read_text(encoding="utf-8")
        for forbidden in ("/api/chat", "/api/generate", "/api/embed", "SONDER_ALLOW_CLOUD", "runtime_policy.json", "policy_store",
                          "permission_modes"):
            assert forbidden not in text, (source.name, forbidden)


# --- write_json_validated / validate_overlay_file -----------------------------------


def test_write_json_validated_validates_before_replace_and_backs_up(tmp_path):
    target, backup = tmp_path / "t.json", tmp_path / "t.json.bak"
    seen = []

    def check(candidate):
        seen.append(json.loads(candidate.read_text()))

    atomic_json.write_json_validated(target, {"v": 1}, validate=check, backup=backup)
    assert seen == [{"v": 1}] and not backup.exists()  # nothing to back up on the first write
    atomic_json.write_json_validated(target, {"v": 2}, validate=check, backup=backup)
    assert json.loads(backup.read_text()) == {"v": 1} and json.loads(target.read_text()) == {"v": 2}


def test_write_json_validated_failure_leaves_everything_untouched(tmp_path):
    target, backup = tmp_path / "t.json", tmp_path / "t.json.bak"
    target.write_text('{"v": "orig"}', encoding="utf-8")
    backup.write_text('{"v": "oldbak"}', encoding="utf-8")
    before = snapshot(tmp_path)

    def reject(_candidate):
        raise ValueError("no")

    with pytest.raises(ValueError):
        atomic_json.write_json_validated(target, {"v": 2}, validate=reject, backup=backup)
    assert snapshot(tmp_path) == before


def test_validate_overlay_file_uses_the_real_loader_rules(tmp_path):
    good, typo, notjson = tmp_path / "g.json", tmp_path / "t.json", tmp_path / "n.json"
    good.write_text(json.dumps({"version": 1, "retired_cloud_models": ["a:cloud"]}), encoding="utf-8")
    typo.write_text(json.dumps({"version": 1, "retired_cloud_model": []}), encoding="utf-8")
    notjson.write_text("{", encoding="utf-8")
    model_lists.validate_overlay_file(good)
    for bad in (typo, notjson):
        with pytest.raises((ValueError, OSError)):
            model_lists.validate_overlay_file(bad)


# --- 11. dry run ---------------------------------------------------------------


def test_dry_run_writes_nothing_and_plans_everything(std):
    std.write_overlay({"version": 1, "retired_cloud_models": ["mine:cloud"]})
    before = std.snapshot()
    report = std.run(apply=False, clock=lambda: NOW)
    assert report["outcome"] == "ok" and report["mode"] == "dry_run"
    assert std.snapshot() == before
    assert not list(std.home.glob("*.bak")) and not list(std.home.glob("*.jsonl"))
    assert not std.state.exists()
    assert {r["model"] for r in report["retirements"]} == {DEFAULT_CODE, DEFAULT_GENERAL}
    code, general = report["decisions"]
    assert (code["action"], code["new_model"]) == ("rebind", "kimi-k3-code:cloud")
    assert general["action"] == "needs_operator_choice" and general["suggestion"] == "glm-5.3:cloud"
    assert [c["action"] for c in report["overlay_changes"]] == ["heal_retired_union", "retire", "retire", "rebind"]
    assert [p["tier"] for p in report["pending_choices"]] == ["general"]
    assert any("dry run" in n for n in report["notes"])


def test_dry_run_without_any_overlay_creates_no_overlay(std):
    before = std.snapshot()
    std.run(apply=False)
    assert not std.overlay.exists() and std.snapshot() == before


# --- 12. apply -----------------------------------------------------------------


def test_apply_writes_overlay_backup_log_and_state(std):
    previous = json.dumps({"version": 1, "retired_cloud_models": ["mine:cloud"],
                           "default_local_models": {"code": "custom:7b"}}).encode()
    std.overlay.write_bytes(previous)
    model_lists.clear_cache()
    report = std.run(apply=True, clock=lambda: NOW)
    assert report["outcome"] == "ok", report
    overlay = json.loads(std.overlay.read_text(encoding="utf-8"))
    retired_cf = {n.casefold() for n in overlay["retired_cloud_models"]}
    assert {n.casefold() for n in SHIPPED["retired_cloud_models"]} <= retired_cf
    assert {"mine:cloud", DEFAULT_CODE.casefold(), DEFAULT_GENERAL.casefold()} <= retired_cf
    assert overlay["default_cloud_models"] == {"code": "kimi-k3-code:cloud"}
    assert overlay["default_local_models"] == {"code": "custom:7b"}
    config = model_lists.load()
    assert not config.issues
    assert config.values["default_cloud_models"]["code"] == "kimi-k3-code:cloud"
    assert (std.home / "model_lists.json.bak").read_bytes() == previous

    lines = _log_lines(std.change_log)
    assert [l["action"] for l in lines] == ["heal_retired_union", "retire", "retire", "rebind", "run"]
    assert {l["run_id"] for l in lines} == {report["run_id"]}
    retire = [l for l in lines if l["action"] == "retire"]
    assert {l["model"] for l in retire} == {DEFAULT_CODE, DEFAULT_GENERAL}
    assert {l["evidence"]["provider_name"] for l in retire} == {"kimi-k2.7-code:0801", "glm-5.2:0801"}
    rebind = next(l for l in lines if l["action"] == "rebind")
    assert (rebind["before"], rebind["after"], rebind["key"]) == (DEFAULT_CODE, "kimi-k3-code:cloud",
                                                                   "default_cloud_models.code")
    assert rebind["file"] == str(std.overlay) and rebind["backup"] == str(std.overlay) + ".bak"

    state = json.loads(std.state.read_text(encoding="utf-8"))
    crm.validate_state(state)
    entry = state["models"]["kimi-k3-code:cloud"]
    assert entry["status"] == "live" and entry["parameters"] == "1.2T" and entry["context_length"] == 262144
    assert entry["first_seen"] == entry["last_seen"] == crm.iso_timestamp(NOW)
    assert state["models"][DEFAULT_GENERAL]["retired"]["suggestion"] == "glm-5.3:cloud"
    assert [p["tier"] for p in state["pending_choices"]] == ["general"]
    assert state["pending_choices"][0]["suggestion"] == "glm-5.3:cloud"
    assert state["overlay_pins"]["code"]["model"] == "kimi-k3-code:cloud"
    assert state["overlay_pins"]["code"]["replaced"] == DEFAULT_CODE
    assert state["last_success_at"] == crm.iso_timestamp(NOW)
    assert state["last_refresh"]["outcome"] == "ok" and state["last_refresh"]["trigger"] == "cli"


def test_apply_creates_overlay_from_scratch_without_backup(std):
    report = std.run(apply=True)
    assert report["outcome"] == "ok"
    assert std.overlay.exists() and not (std.home / "model_lists.json.bak").exists()
    assert _actions(std.change_log)[0] == "create_overlay"


def test_second_apply_is_a_noop_for_the_overlay(std):
    std.run(apply=True)
    overlay_bytes, backup_bytes = std.overlay.read_bytes(), (std.home / "model_lists.json.bak").read_bytes() \
        if (std.home / "model_lists.json.bak").exists() else None
    before = len(_log_lines(std.change_log))
    report = std.run(apply=True)
    assert report["outcome"] == "ok" and report["overlay_changes"] == []
    assert std.overlay.read_bytes() == overlay_bytes
    bak = std.home / "model_lists.json.bak"
    assert (bak.read_bytes() if bak.exists() else None) == backup_bytes
    new = _log_lines(std.change_log)[before:]
    assert not {l["action"] for l in new} & {"retire", "rebind", "heal_retired_union", "create_overlay"}


def test_apply_reports_never_include_response_bodies(std):
    report = json.dumps(std.run(apply=True))
    assert "was retired on" not in report  # only parsed names and reason codes


# --- 13/14. refusals --------------------------------------------------------------


@pytest.mark.parametrize("raw", [b"{", b'{"version": 1, "typo": []}', b"[]", b'{"version": 2}'])
def test_invalid_overlay_is_refused_and_bytes_preserved(std, raw):
    std.overlay.write_bytes(raw)
    model_lists.clear_cache()
    before = std.snapshot()
    for apply in (False, True):
        report = std.run(apply=apply)
        assert (report["outcome"], report["reason"]) == ("refused", "overlay_invalid")
        assert std.snapshot() == before and std.overlay.read_bytes() == raw


def test_loader_validation_failure_leaves_overlay_unchanged_and_clean(std, monkeypatch):
    std.write_overlay({"version": 1, "retired_cloud_models": list(SHIPPED["retired_cloud_models"])})
    before = std.snapshot()

    def reject(_path):
        raise ValueError("loader would ignore this overlay")

    monkeypatch.setattr(model_lists, "validate_overlay_file", reject)
    report = std.run(apply=True)
    assert (report["outcome"], report["reason"]) == ("refused", "overlay_validation_failed")
    assert std.snapshot() == before  # no overlay change, no .bak, no state, no log, no temp files
    assert not std.state.exists() and not std.change_log.exists()
    assert not [p for p in std.home.iterdir() if ".tmp-" in p.name]


@pytest.mark.parametrize("had_overlay", [True, False])
def test_post_write_loader_issues_roll_the_overlay_back(std, monkeypatch, had_overlay):
    original = None
    if had_overlay:
        std.write_overlay({"version": 1, "retired_cloud_models": list(SHIPPED["retired_cloud_models"])})
        original = std.overlay.read_bytes()
    monkeypatch.setattr(model_lists, "load", lambda: SimpleNamespace(issues=[object()]))
    report = std.run(apply=True)
    assert (report["outcome"], report["reason"]) == ("refused", "post_check_failed")
    assert (std.overlay.read_bytes() if std.overlay.exists() else None) == original
    assert not std.state.exists()


def test_apply_takes_the_editor_overlay_lock(std, monkeypatch):
    monkeypatch.setattr(refresh, "OVERLAY_LOCK_TIMEOUT", 0.3)
    before = std.snapshot()
    with atomic_json.file_lock(std.overlay, timeout=2):
        report = std.run(apply=True)
    assert report["outcome"] == "aborted"
    assert std.snapshot() == before and not std.overlay.exists()


# --- 15. network failures ------------------------------------------------------------


def _seed_existing_files(w):
    w.write_overlay({"version": 1, "retired_cloud_models": list(SHIPPED["retired_cloud_models"]) + ["mine:cloud"]})
    assert w.run(apply=True)["outcome"] == "ok"


def test_ollama_dropping_mid_run_aborts_with_every_file_byte_identical(world):
    w = world(tags=("kimi-k3-code:cloud",), shows=standard_shows())
    w.write_preferences(LADDER)
    _seed_existing_files(w)
    w.fake.drop_show_after = w.fake._shows_seen + 2
    before = w.snapshot()
    report = w.run(apply=True)
    assert (report["outcome"], report["reason"]) == ("aborted", "ollama_unreachable")
    assert w.snapshot() == before


def test_ollama_unreachable_at_tags_aborts_with_zero_writes(std):
    std.fake.close()
    before = std.snapshot()
    for apply in (False, True):
        report = std.run(apply=apply)
        assert (report["outcome"], report["reason"]) == ("aborted", "ollama_unreachable")
        assert std.snapshot() == before


def test_tags_failure_alone_aborts_before_any_probe_or_write(world):
    w = world(tags_status=500)
    w.write_preferences(LADDER)
    before = w.snapshot()
    for apply in (False, True):
        report = w.run(apply=apply)
        assert (report["outcome"], report["reason"]) == ("aborted", "ollama_tags_http_500")
    assert w.fake.shown() == [] and w.snapshot() == before


def _enable_web(monkeypatch, w):
    monkeypatch.setenv("SONDER_WEB_TOOLS", "1")
    w.env["SONDER_WEB_TOOLS"] = "1"


def _snapshot_fetcher(families, status="ok"):
    def fetch(*, known_families=None, now=None, **_):
        return catalog.CatalogSnapshot(now or NOW, dict(families), status, frozenset(families))
    return fetch


def test_catalog_transport_failure_discards_only_the_catalog_part(std, monkeypatch):
    _enable_web(monkeypatch, std)
    good = _snapshot_fetcher({"kimi-k3": ("kimi-k3:cloud", "kimi-k3:1017-cloud")})
    assert std.run(apply=True, use_catalog=True, catalog_fetcher=good)["catalog"]["status"] == "ok"
    first_catalog = json.loads(std.state.read_text())["catalog"]
    assert first_catalog["families"]["kimi-k3"]["tags"] == ["kimi-k3:cloud", "kimi-k3:1017-cloud"]

    def broken(**_):
        raise catalog.CatalogUnavailable("transport")

    report = std.run(apply=True, use_catalog=True, catalog_fetcher=broken)
    assert report["outcome"] == "ok" and report["catalog"]["status"] == "unavailable"
    assert json.loads(std.state.read_text())["catalog"] == first_catalog


def test_catalog_failure_can_be_configured_to_abort_with_zero_writes(std, monkeypatch):
    _enable_web(monkeypatch, std)
    monkeypatch.setattr(refresh, "CATALOG_FAILURE_ABORTS_RUN", True)

    def broken(**_):
        raise catalog.CatalogUnavailable("http")

    before = std.snapshot()
    report = std.run(apply=True, use_catalog=True, catalog_fetcher=broken)
    assert report["outcome"] == "aborted" and report["reason"] == "catalog_unavailable_http"
    assert std.snapshot() == before


def test_unparsed_catalog_leaves_the_catalog_section_untouched(std, monkeypatch):
    _enable_web(monkeypatch, std)
    std.run(apply=True, use_catalog=True, catalog_fetcher=_snapshot_fetcher({"kimi-k3": ("kimi-k3:cloud",)}))
    previous = json.loads(std.state.read_text())["catalog"]
    report = std.run(apply=True, use_catalog=True, catalog_fetcher=_snapshot_fetcher({}, "unparsed"))
    assert report["catalog"]["status"] == "unparsed"
    assert json.loads(std.state.read_text())["catalog"] == previous


def test_catalog_models_are_probed_but_never_bound(std, monkeypatch):
    _enable_web(monkeypatch, std)
    fetch = _snapshot_fetcher({"fresh": ("fresh:cloud",)})
    report = std.run(apply=True, use_catalog=True, catalog_fetcher=fetch)
    assert "fresh:cloud" in std.fake.shown()
    assert "fresh:cloud" in report["new_models"]
    overlay = json.loads(std.overlay.read_text())
    assert "fresh:cloud" not in json.dumps(overlay["default_cloud_models"])


# --- 16. catalog gating -----------------------------------------------------------------


class _Spy:
    def __init__(self):
        self.calls = 0

    def __call__(self, **_kw):
        self.calls += 1
        raise AssertionError("catalog must not be fetched")


def test_catalog_disabled_by_web_tools_off(std):
    spy = _Spy()
    assert os.environ["SONDER_WEB_TOOLS"] == "0"  # the repo's hermetic default
    report = std.run(apply=False, use_catalog=None, catalog_fetcher=spy)
    assert spy.calls == 0 and report["catalog"]["status"] == "disabled"


def test_catalog_disabled_by_preferences_flag(std, monkeypatch):
    _enable_web(monkeypatch, std)
    std.write_preferences({**LADDER, "catalog": False})
    spy = _Spy()
    report = std.run(apply=False, use_catalog=None, catalog_fetcher=spy)
    assert spy.calls == 0 and report["catalog"]["status"] == "disabled"


def test_catalog_disabled_by_no_catalog_flag_and_env_switch(std, monkeypatch):
    _enable_web(monkeypatch, std)
    spy = _Spy()
    assert std.run(apply=False, use_catalog=False, catalog_fetcher=spy)["catalog"]["status"] == "disabled"
    std.env["SONDER_CLOUD_CATALOG"] = "0"
    assert std.run(apply=False, use_catalog=None, catalog_fetcher=spy)["catalog"]["status"] == "disabled"
    assert spy.calls == 0


# --- 17. catalog hygiene -----------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://ollama.com/search?c=cloud",
        "https://evil.example/search?c=cloud",
        "https://ollama.com.evil.example/search?c=cloud",
        "https://user@ollama.com/search?c=cloud",
        "https://ollama.com:8443/search?c=cloud",
        "https://ollama.com/search?c=cloud&q=secret",
        "https://ollama.com/search",
        "https://ollama.com/search?c=cloud#frag",
        "https://ollama.com/library/kimi-k3",
        "https://ollama.com/library/kimi-k3/tags?x=1",
        "https://ollama.com/library/../admin/tags",
        "https://ollama.com/library/UPPER/tags",
        "https://ollama.com/library/a/b/tags",
        "file:///etc/passwd",
        "",
    ],
)
def test_request_bytes_refuses_any_url_outside_the_two_fixed_shapes(monkeypatch, url):
    import web_tools

    monkeypatch.setattr(web_tools, "_request", lambda *a, **k: pytest.fail("must not reach the network"))
    with pytest.raises(ValueError):
        catalog._request_bytes(url, 5.0)


def test_request_bytes_sends_only_the_fixed_url_through_web_tools(monkeypatch):
    import web_tools

    seen = []
    monkeypatch.setattr(web_tools, "_request", lambda *a, **k: seen.append((a, k)) or (b"<html/>", "text/html"))
    assert catalog._request_bytes(catalog.SEARCH_URL, 7.0) == b"<html/>"
    assert catalog._request_bytes(catalog.TAGS_URL.format(family="kimi-k3"), 7.0) == b"<html/>"
    assert seen == [((catalog.SEARCH_URL,), {"timeout": 7.0}),
                    (("https://ollama.com/library/kimi-k3/tags",), {"timeout": 7.0})]
    assert web_tools.USER_AGENT == "sonder-local-agent/1.0"


def test_fetch_catalog_requests_only_valid_family_tags_pages():
    pages = {
        catalog.SEARCH_URL: '<a href="/library/kimi-k3">k</a><a href="/library/../etc">x</a><a href="/library/Evil">e</a>',
        "https://ollama.com/library/kimi-k3/tags": "kimi-k3:cloud kimi-k3:7b",
    }
    asked = []

    def request(url, timeout):
        asked.append(url)
        return pages[url].encode()

    snap = catalog.fetch_catalog(request=request, now=NOW)
    assert asked == [catalog.SEARCH_URL, "https://ollama.com/library/kimi-k3/tags"]
    assert snap.status == "ok" and dict(snap.families) == {"kimi-k3": ("kimi-k3:cloud",)}


def test_fetch_catalog_refetch_window_budget_and_failures():
    pages = {
        catalog.SEARCH_URL: '<a href="/library/aa">1</a><a href="/library/bb">2</a>',
        "https://ollama.com/library/aa/tags": "aa:cloud",
        "https://ollama.com/library/bb/tags": "bb:cloud",
    }
    asked = []

    def request(url, timeout):
        asked.append(url)
        return pages[url].encode()

    recent = catalog.fetch_catalog(request=request, now=NOW, known_families={"aa": NOW - 3600})
    assert "https://ollama.com/library/aa/tags" not in asked and recent.refreshed == frozenset({"bb"})
    asked.clear()
    stale = catalog.fetch_catalog(request=request, now=NOW, known_families={"aa": NOW - 8 * 86400})
    assert stale.refreshed == frozenset({"aa", "bb"})

    ticks = iter(range(0, 10_000, 100))
    partial = catalog.fetch_catalog(request=request, now=NOW, budget_seconds=90, monotonic=lambda: next(ticks))
    assert partial.status == "partial" and partial.refreshed == frozenset()

    pages[catalog.SEARCH_URL] = "<html>layout changed</html>"
    assert catalog.fetch_catalog(request=request, now=NOW).status == "unparsed"

    def http_error(url, timeout):
        raise urllib.error.HTTPError(url, 503, "x", {}, None)  # type: ignore[arg-type]

    with pytest.raises(catalog.CatalogUnavailable) as caught:
        catalog.fetch_catalog(request=http_error, now=NOW)
    assert caught.value.reason == "http"

    def refused(url, timeout):
        raise ConnectionRefusedError()

    with pytest.raises(catalog.CatalogUnavailable) as caught:
        catalog.fetch_catalog(request=refused, now=NOW)
    assert caught.value.reason == "transport"


# --- 18. auto-pull -----------------------------------------------------------------------


def test_auto_pull_is_off_by_default(world):
    w = world(tags=(), shows=standard_shows())
    w.write_preferences(LADDER)
    report = w.run(apply=True)
    assert "/api/pull" not in w.fake.paths()
    assert report["pulls"] == []
    code = report["decisions"][0]
    assert code["action"] == "needs_operator_choice"
    assert [e["state"] for e in code["ladder"]] == ["not_pulled", "not_pulled"]


def test_auto_pull_on_pulls_missing_live_ladder_stub_and_rebinds_in_the_same_run(world):
    w = world(tags=(), shows=standard_shows())
    w.write_preferences({**LADDER, "auto_pull_stubs": True})
    report = w.run(apply=True)
    pulls = [b for _v, p, b in w.fake.requests if p == "/api/pull"]
    assert pulls == [{"model": "kimi-k3-code:cloud", "stream": False}, {"model": "kimi-k3:cloud", "stream": False}]
    assert [(p["model"], p["outcome"]) for p in report["pulls"]] == [
        ("kimi-k3-code:cloud", "ok"), ("kimi-k3:cloud", "ok")]
    assert json.loads(w.overlay.read_text())["default_cloud_models"]["code"] == "kimi-k3-code:cloud"
    assert {"pull"} <= set(_actions(w.change_log))


def test_auto_pull_dry_run_only_plans(world):
    w = world(tags=(), shows=standard_shows())
    w.write_preferences({**LADDER, "auto_pull_stubs": True})
    before = w.snapshot()
    report = w.run(apply=False)
    assert "/api/pull" not in w.fake.paths()
    assert {p["outcome"] for p in report["pulls"]} == {"planned"}
    assert w.snapshot() == before


def test_auto_pull_skips_retired_unpulled_and_failed_pulls_do_not_rebind(world):
    shows = standard_shows()
    shows["dead:cloud"] = (200, {"error": retired_message("dead:0101", None)})
    w = world(tags=(), shows=shows, pull_status=500, pull_payload={"error": "no"})
    w.write_preferences({"code": ["dead:cloud", "kimi-k3:cloud"], "auto_pull_stubs": True})
    report = w.run(apply=True)
    pulled = [b["model"] for _v, p, b in w.fake.requests if p == "/api/pull"]
    assert pulled == ["kimi-k3:cloud"]  # the retired stub is never pulled
    assert report["pulls"][0]["outcome"] == "failed"
    assert report["decisions"][0]["action"] == "needs_operator_choice"
    assert "default_cloud_models" not in json.loads(w.overlay.read_text())


def test_auto_pull_is_capped_at_max_pulls(world):
    names = ["m%d:cloud" % i for i in range(crm.MAX_PULLS + 3)]
    w = world(tags=(), shows=standard_shows())
    w.write_preferences({"code": names, "auto_pull_stubs": True})
    w.run(apply=True)
    assert len([1 for _v, p, _b in w.fake.requests if p == "/api/pull"]) == crm.MAX_PULLS


def test_plan_pulls_refuses_non_stub_names_even_if_a_hand_built_preference_has_one():
    prefs = crm.Preferences(code=("qwen2.5:7b", "ok:cloud"), auto_pull_stubs=True)
    probes = {n: crm.ProbeResult(n, ProbeStatus.LIVE) for n in prefs.code}
    pulls, refused = crm.plan_pulls(prefs, probes, frozenset(), frozenset())
    assert pulls == ("ok:cloud",) and refused == ("qwen2.5:7b",)


# --- 19. hard limits ----------------------------------------------------------------------


def test_refresh_never_touches_env_policy_or_permission_state_and_sends_no_prompt(std):
    state_home = Path(os.environ["SONDER_HOME"])
    env_before, home_before = dict(os.environ), snapshot(state_home)
    std.run(apply=True)
    assert dict(os.environ) == env_before and os.environ["SONDER_ALLOW_CLOUD"] == "0"
    assert snapshot(state_home) == home_before
    assert {p.name for p in std.home.iterdir() if p.suffix != ".lock"} == {
        "cloud_model_preferences.json", "model_lists.json", "cloud_model_catalog.json",
        "cloud_model_changes.jsonl"}
    assert set(std.fake.paths()) <= {"/api/tags", "/api/show", "/api/pull"}
    for _verb, _path, body in std.fake.requests:
        assert not (set(body or {}) & {"prompt", "messages", "input", "options"})


def test_env_bindings_are_never_written_or_moved(world):
    w = world()
    w.write_preferences(LADDER)
    w.env["SONDER_CLOUD_CODE"] = "mine:cloud"
    report = w.run(apply=True)
    code = report["decisions"][0]
    assert (code["action"], code["effective_source"], code["effective_model"]) == ("keep", "env", "mine:cloud")
    overlay = json.loads(w.overlay.read_text())
    assert "default_cloud_models" not in overlay  # code default left as shipped; only retirements written
    assert DEFAULT_CODE in overlay["retired_cloud_models"]
    assert w.env["SONDER_CLOUD_CODE"] == "mine:cloud" and "SONDER_CLOUD_CODE" not in os.environ


def test_retired_env_binding_is_reported_not_changed(world):
    w = world()
    w.write_preferences(LADDER)
    w.env["SONDER_CLOUD_GENERAL"] = DEFAULT_GENERAL  # retired by the fake
    report = w.run(apply=False)
    assert any("env SONDER_CLOUD_GENERAL names retired" in n for n in report["notes"])
    assert w.env["SONDER_CLOUD_GENERAL"] == DEFAULT_GENERAL


def test_ladder_never_selects_outside_itself_nor_a_retired_entry(world):
    shows = standard_shows()
    shows["dead:cloud"] = (500, {"error": retired_message("dead:0101", "glm-5.3:cloud")})
    w = world(tags=("dead:cloud", "glm-5.3:cloud", "ok:cloud"), shows=shows)
    w.write_preferences({"code": ["dead:cloud", "ok:cloud"], "general": ["dead:cloud"]})
    report = w.run(apply=True)
    code, general = report["decisions"]
    assert code["new_model"] == "ok:cloud"
    assert general["action"] == "needs_operator_choice" and general["new_model"] is None
    overlay = json.loads(w.overlay.read_text())
    assert overlay["default_cloud_models"] == {"code": "ok:cloud"}  # suggestion glm-5.3:cloud never bound
    assert "dead:cloud" in overlay["retired_cloud_models"]


def test_invalid_preferences_still_retire_but_never_rebind_or_pull(world):
    w = world(tags=())
    w.preferences.write_text(json.dumps({"code": ["a:cloud"], "auto_pull_stubs": True, "bogus": 1}))
    report = w.run(apply=True)
    assert report["preferences_error"].startswith("preferences_invalid")
    assert "/api/pull" not in w.fake.paths()
    assert {r["model"] for r in report["retirements"]} == {DEFAULT_CODE, DEFAULT_GENERAL}
    assert all(d["action"] != "rebind" for d in report["decisions"])
    assert "default_cloud_models" not in json.loads(w.overlay.read_text())


def test_shipped_retirements_survive_every_write(std):
    std.write_overlay({"version": 1, "retired_cloud_models": ["only-mine:cloud"]})
    std.run(apply=True)
    retired = {n.casefold() for n in json.loads(std.overlay.read_text())["retired_cloud_models"]}
    assert {n.casefold() for n in SHIPPED["retired_cloud_models"]} <= retired
    assert "only-mine:cloud" in retired
    assert set(model_lists.load().values["retired_cloud_models"]) >= set(SHIPPED["retired_cloud_models"])


def test_no_retirement_evidence_never_heals_a_deliberately_shorter_retired_list(world):
    w = world(shows={})  # every probe is live: no provider evidence this run
    w.write_preferences(LADDER)
    w.write_overlay({"version": 1, "retired_cloud_models": ["only-mine:cloud"]})
    before = w.overlay.read_bytes()
    for apply in (False, True):
        report = w.run(apply=apply)
        assert report["outcome"] == "ok" and report["write_retired"] is False
        assert report["retirements"] == [] and report["overlay_changes"] == []
    assert w.overlay.read_bytes() == before  # the operator's un-retire stands


# --- 20. concurrency -----------------------------------------------------------------------


def test_held_run_lock_reports_busy_with_zero_io(std):
    before = std.snapshot()
    with refresh._run_lock(std.home / refresh.RUN_LOCK):
        report = std.run(apply=True)
    assert (report["outcome"], report["reason"]) == ("busy", "run_lock_busy")
    assert std.fake.requests == [] and std.snapshot() == before


def test_editor_write_between_plan_and_write_is_skipped_as_changed_concurrently(std):
    class Racing(OllamaCloudMetadata):
        def show(self, model):
            result = super().show(model)
            if model == "kimi-k3:cloud":  # the last probe: the operator edits the overlay now
                std.write_overlay({"version": 1, "default_cloud_models": {"code": "operator-pick:cloud"}})
            return result

    report = std.run(apply=True, metadata=Racing(std.fake.url))
    assert report["outcome"] == "ok"
    assert report["changed_concurrently"] == ["default_cloud_models.code"]
    overlay = json.loads(std.overlay.read_text())
    assert overlay["default_cloud_models"] == {"code": "operator-pick:cloud"}
    assert DEFAULT_CODE in overlay["retired_cloud_models"]
    assert "rebind" not in _actions(std.change_log)


# --- 21. state bounds ------------------------------------------------------------------------


def test_state_is_bounded_and_prunes_the_oldest_unseen_names():
    stamp = lambda i: crm.iso_timestamp(1_700_000_000 + i)  # noqa: E731
    previous = {"version": 1, "models": {
        "old%d:cloud" % i: {"first_seen": stamp(0), "last_seen": stamp(i), "status": "live", "sources": ["catalog"]}
        for i in range(crm.MAX_STATE_MODELS)}}
    known = tuple(crm.KnownModel("new%d:cloud" % i, frozenset({"catalog"})) for i in range(10))
    plan = crm.RefreshPlan((), (), False, (), (), (), ())
    state = crm.merge_state(
        previous, now_iso=stamp(10_000), run_id="r1", known=known, probes={}, plan=plan, applied_rebinds=(),
        catalog=None, catalog_refreshed=(), catalog_source="s", last_refresh={}, succeeded=True)
    crm.validate_state(state)
    assert len(state["models"]) == crm.MAX_STATE_MODELS
    assert all("new%d:cloud" % i in state["models"] for i in range(10))
    assert "old0:cloud" not in state["models"] and "old9:cloud" not in state["models"]
    assert "old10:cloud" in state["models"]


def test_validate_state_rejects_oversize_and_malformed_documents():
    too_many = {"version": 1, "models": {"m%d:cloud" % i: {"status": "live"} for i in range(crm.MAX_STATE_MODELS + 1)}}
    for bad in (too_many, [], {"version": 2}, {"version": 1, "extra": 1},
                {"version": 1, "models": {"x:cloud": {"status": "bogus"}}},
                {"version": 1, "pending_choices": [{"tier": "nope"}]},
                {"version": 1, "last_success_at": "yesterday"}):
        with pytest.raises(ValueError):
            crm.validate_state(bad)


def test_invalid_state_is_treated_as_empty_and_its_bytes_kept_as_bak(std):
    garbage = b'{"version": 1, "models": {"x:cloud": {"status": "bogus"}}}'
    std.state.write_bytes(garbage)
    report = std.run(apply=True)
    assert report["state_error"].startswith("state_invalid")
    crm.validate_state(json.loads(std.state.read_text()))
    assert (std.home / "cloud_model_catalog.json.bak").read_bytes() == garbage


def test_preferences_and_state_loaders_report_reasons(tmp_path):
    assert refresh.load_preferences(tmp_path / "nope.json") == (None, None)
    assert refresh.load_state(tmp_path / "nope.json") == (None, None)
    broken = tmp_path / "b.json"
    broken.write_text("{", encoding="utf-8")
    assert refresh.load_preferences(broken)[1].startswith("preferences_unreadable")
    assert refresh.load_state(broken)[1].startswith("state_unreadable")


def test_state_and_preferences_paths_honour_env_overrides_and_home(tmp_path, monkeypatch):
    monkeypatch.setenv(refresh.ENV_STATE, str(tmp_path / "s.json"))
    monkeypatch.setenv(refresh.ENV_PREFERENCES, str(tmp_path / "p.json"))
    monkeypatch.setenv("SONDER_MODEL_LISTS", str(tmp_path / "ml.json"))
    paths = refresh.RefreshPaths.resolve()
    assert (paths.state, paths.preferences, paths.overlay) == (
        tmp_path / "s.json", tmp_path / "p.json", tmp_path / "ml.json")
    homed = refresh.RefreshPaths.resolve(tmp_path / "h")
    assert homed.state == tmp_path / "h" / "cloud_model_catalog.json"
    assert homed.change_log == tmp_path / "h" / "cloud_model_changes.jsonl"
