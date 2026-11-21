"""``python -m sonder_runtime models cloud-refresh`` end to end, with a fake Ollama and a canned catalog."""

import json
from types import SimpleNamespace

import pytest

import sonder_runtime.adapters.ollama_cloud_catalog as catalog
from sonder_runtime import __main__ as cli
from sonder_runtime.adapters import model_lists
from sonder_runtime.bootstrap import cloud_models_cli
from tests.support.cloud_refresh_harness import make_world, snapshot, standard_shows

SHIPPED = model_lists.load_shipped_document()
DEFAULT_CODE = SHIPPED["default_cloud_models"]["code"]
DEFAULT_GENERAL = SHIPPED["default_cloud_models"]["general"]

PAGES = {
    catalog.SEARCH_URL: '<a href="/library/kimi-k3">k</a> <a href="/library/glm-5.3">g</a>',
    "https://ollama.com/library/kimi-k3/tags": "kimi-k3:cloud kimi-k3:1017-cloud kimi-k3:latest",
    "https://ollama.com/library/glm-5.3/tags": "glm-5.3:cloud",
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    world = make_world(tmp_path, monkeypatch, tags=("kimi-k3-code:cloud", "kimi-k3:cloud"), shows=standard_shows())
    world.write_preferences({"code": ["kimi-k2.7-code:cloud", "kimi-k3-code:cloud", "kimi-k3:cloud"]})
    monkeypatch.setattr(cli, "_load_config", lambda _args: SimpleNamespace(
        ollama=SimpleNamespace(url=world.fake.url), state=SimpleNamespace(home=str(world.home))))
    monkeypatch.setenv("SONDER_WEB_TOOLS", "1")
    asked = []

    def canned(url, timeout):
        asked.append(url)
        return PAGES[url].encode()

    monkeypatch.setattr(catalog, "_request_bytes", canned)
    world.asked = asked  # type: ignore[attr-defined]
    yield world
    world.fake.close()
    model_lists.clear_cache()


def _main(capsys, *argv):
    code = cli.main(["models", "cloud-refresh", *argv])
    return code, capsys.readouterr().out


def test_dry_run_json_plans_everything_and_writes_nothing(env, capsys):
    before = snapshot(env.home)
    code, out = _main(capsys, "--json")
    report = json.loads(out)
    assert code == 0 and report["outcome"] == "ok" and report["mode"] == "dry_run"
    assert snapshot(env.home) == before and not env.overlay.exists()
    assert {r["model"] for r in report["retirements"]} == {DEFAULT_CODE, DEFAULT_GENERAL}
    code_tier, general = report["decisions"]
    assert (code_tier["action"], code_tier["new_model"]) == ("rebind", "kimi-k3-code:cloud")
    assert [e["state"] for e in code_tier["ladder"]] == ["retired", "selected", "live"]
    assert general["action"] == "needs_operator_choice" and general["suggestion"] == "glm-5.3:cloud"
    assert report["catalog"]["status"] == "ok"
    assert {"kimi-k3:1017-cloud", "glm-5.3:cloud"} <= set(report["catalog"]["new_tags"])
    assert report["trigger"] == "cli"


def test_dry_run_text_output_names_the_plan(env, capsys):
    code, out = _main(capsys)
    assert code == 0
    assert "dry run: nothing written; re-run with --apply" in out
    assert "needs operator choice: tier general" in out and "provider suggests glm-5.3:cloud" in out
    assert "default_cloud_models.code: %s -> kimi-k3-code:cloud (ladder #2)" % DEFAULT_CODE in out
    assert "catalog: ok" in out and "(inert, nothing bound)" in out


def test_apply_then_second_apply_is_a_noop(env, capsys):
    code, out = _main(capsys, "--apply", "--json")
    report = json.loads(out)
    assert code == 0 and report["outcome"] == "ok" and report["mode"] == "apply"
    overlay = json.loads(env.overlay.read_text(encoding="utf-8"))
    assert overlay["default_cloud_models"] == {"code": "kimi-k3-code:cloud"}
    assert {DEFAULT_CODE, DEFAULT_GENERAL} <= set(overlay["retired_cloud_models"])
    assert set(SHIPPED["retired_cloud_models"]) <= set(overlay["retired_cloud_models"])
    config = model_lists.load()
    assert not config.issues and config.values["default_cloud_models"]["code"] == "kimi-k3-code:cloud"
    state = json.loads(env.state.read_text(encoding="utf-8"))
    assert state["catalog"]["families"]["kimi-k3"]["tags"] == ["kimi-k3:cloud", "kimi-k3:1017-cloud"]
    assert [p["tier"] for p in state["pending_choices"]] == ["general"]
    assert {row["file"] for row in report["files_written"]} == {
        str(env.overlay), str(env.state), str(env.change_log)}

    overlay_bytes, state_log = env.overlay.read_bytes(), len(env.change_log.read_text().splitlines())
    code, out = _main(capsys, "--apply", "--json")
    again = json.loads(out)
    assert code == 0 and again["overlay_changes"] == [] and env.overlay.read_bytes() == overlay_bytes
    new = [json.loads(line)["action"] for line in env.change_log.read_text().splitlines()[state_log:]]
    assert set(new) <= {"run"}


def test_requests_are_only_metadata_and_only_the_two_catalog_url_shapes(env, capsys):
    _main(capsys, "--apply", "--json")
    assert set(env.fake.paths()) <= {"/api/tags", "/api/show", "/api/pull"}
    assert "/api/pull" not in env.fake.paths()  # auto-pull defaults to off
    assert set(env.asked) <= set(PAGES)
    assert env.asked[0] == catalog.SEARCH_URL


def test_no_catalog_flag_skips_the_public_catalog(env, capsys):
    code, out = _main(capsys, "--json", "--no-catalog")
    assert code == 0 and json.loads(out)["catalog"]["status"] == "disabled" and env.asked == []


def test_unreachable_ollama_exits_one_and_writes_nothing(env, capsys):
    env.fake.close()
    before = snapshot(env.home)
    code, out = _main(capsys, "--apply", "--json")
    assert code == 1 and json.loads(out)["reason"] == "ollama_unreachable"
    assert snapshot(env.home) == before


def test_bad_overlay_exits_one_and_text_output_says_refused(env, capsys):
    env.overlay.write_text("{", encoding="utf-8")
    code, out = _main(capsys, "--apply")
    assert code == 1 and "refused (overlay_invalid)" in out and env.overlay.read_text() == "{"


def test_missing_subcommand_is_a_usage_error(env, capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(["models"])
    assert caught.value.code == 2
    capsys.readouterr()
    assert cloud_models_cli.run_cloud_refresh(
        SimpleNamespace(models_command=None), origin=env.fake.url, home=env.home) == 2
    assert env.fake.requests == []


def test_format_report_handles_an_aborted_report():
    text = cloud_models_cli.format_report({"mode": "apply", "outcome": "aborted", "reason": "ollama_unreachable"})
    assert text.splitlines()[0] == "cloud model refresh (apply): aborted (ollama_unreachable)"
    assert "nothing written" in text
