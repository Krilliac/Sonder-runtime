"""The read-only ``cloud_models_status`` MCP tool and its registry entries."""

import json
import socket
from pathlib import Path

import pytest

import permission_modes
import sonder_runtime.adapters.cloud_model_refresh as refresh
import sonder_runtime.adapters.inference.ollama_endpoint as ollama_endpoint
from sonder_runtime.adapters import command_catalog, model_lists
from sonder_runtime.bootstrap import cloud_models_agent_tools as tool
from sonder_runtime.bootstrap.agent_help_tools import discover_agent_tool_registrars
from sonder_runtime.domain import cloud_model_refresh as crm
from sonder_runtime.domain.tools import builtin_traits
from sonder_runtime.platform import paths as runtime_paths
from tests.support.cloud_refresh_harness import make_world, snapshot, standard_shows

SHIPPED = model_lists.load_shipped_document()
DEFAULT_CODE = SHIPPED["default_cloud_models"]["code"]
FIXTURES = Path(__file__).parent / "fixtures"

REQUIRED_FIELDS = {
    "last_refresh", "last_success_at", "next_due_after", "retired", "new_models", "ladder",
    "pending_choices", "overlay_pins", "effective", "auto_pull_stubs", "auto_refresh",
    "preferences_error", "model_lists_issues", "notes",
}


class FakeMcp:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorate(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorate


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A tmp refresh home; status reads it through the env overrides, as the tool does in production."""
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("SONDER_MODEL_LISTS", str(root / "model_lists.json"))
    monkeypatch.setenv(refresh.ENV_STATE, str(root / refresh.STATE_FILE))
    monkeypatch.setenv(refresh.ENV_PREFERENCES, str(root / refresh.PREFERENCES_FILE))
    monkeypatch.delenv("SONDER_CLOUD_CODE", raising=False)
    monkeypatch.delenv("SONDER_CLOUD_GENERAL", raising=False)
    runtime_paths.configure_home(root)  # change log and defaults land here too; restored by the autouse fixture
    model_lists.clear_cache()
    yield root
    model_lists.clear_cache()


@pytest.fixture
def no_io(monkeypatch):
    """Any network access fails the test."""
    import web_tools

    def boom(*_a, **_k):
        pytest.fail("cloud_models_status must not touch the network")

    monkeypatch.setattr(ollama_endpoint, "open_url", boom)
    monkeypatch.setattr(web_tools, "_request", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)


def _call():
    mcp, calls = FakeMcp(), []
    tool.register(mcp, lambda name, args, **kw: calls.append((name, args, kw["ok"])))
    return json.loads(mcp.tools["cloud_models_status"]()), calls


def test_registrar_is_auto_discovered_without_server_edits():
    assert "cloud_models_agent_tools" in dict(discover_agent_tool_registrars())


def test_registers_one_tool_and_records_the_call(home, no_io):
    mcp, calls = FakeMcp(), []
    tool.register(mcp, lambda name, args, **kw: calls.append((name, args, kw["ok"])))
    assert list(mcp.tools) == ["cloud_models_status"]
    payload = json.loads(mcp.tools["cloud_models_status"]())
    assert payload["ok"] is True and calls == [("cloud_models_status", {}, True)]
    tool.register(FakeMcp(), None)  # a missing recorder is fine


def test_grade_is_safe_and_it_is_catalogued_read_only():
    golden = json.loads((FIXTURES / "builtin_risk_golden.json").read_text(encoding="utf-8"))
    assert golden["cloud_models_status"] == "safe"
    assert permission_modes.risk_of("cloud_models_status") == "safe"
    assert "cloud_models_status" in command_catalog._READ_ONLY
    assert "cloud_models_status" in builtin_traits.READ_ONLY_TOOLS


def test_never_refreshed_payload_has_every_field_and_no_side_effects(home, no_io):
    before = snapshot(home)
    payload, _calls = _call()
    assert REQUIRED_FIELDS <= set(payload)
    assert "never refreshed" in payload["notes"]
    assert payload["last_success_at"] is None and payload["next_due_after"] is None
    assert payload["effective"]["code"] == {"model": DEFAULT_CODE, "source": "default"}
    assert {row["source"] for row in payload["retired"]} == {"shipped"}
    assert {row["model"] for row in payload["retired"]} == set(SHIPPED["retired_cloud_models"])
    assert snapshot(home) == before and list(home.iterdir()) == []


def test_status_after_a_real_refresh_reports_the_run_without_writing(tmp_path, monkeypatch):
    world = make_world(tmp_path, monkeypatch, tags=("kimi-k3-code:cloud",), shows=standard_shows())
    world.write_preferences({"code": ["kimi-k3-code:cloud", "unpulled:cloud"]})
    monkeypatch.setenv(refresh.ENV_STATE, str(world.state))
    monkeypatch.setenv(refresh.ENV_PREFERENCES, str(world.preferences))
    monkeypatch.delenv("SONDER_CLOUD_CODE", raising=False)
    runtime_paths.configure_home(world.home)
    clock = 1_790_000_000.0
    assert world.run(apply=True, clock=lambda: clock)["outcome"] == "ok"
    world.fake.close()
    model_lists.clear_cache()

    import web_tools

    def boom(*_a, **_k):
        pytest.fail("cloud_models_status must not touch the network")

    monkeypatch.setattr(ollama_endpoint, "open_url", boom)
    monkeypatch.setattr(web_tools, "_request", boom)
    before = snapshot(world.home)
    payload, _ = _call()
    assert snapshot(world.home) == before
    assert REQUIRED_FIELDS <= set(payload) and "never refreshed" not in payload["notes"]
    assert payload["last_success_at"] == crm.iso_timestamp(clock)
    assert payload["next_due_after"] == crm.iso_timestamp(clock + 24 * 3600)
    assert payload["last_refresh"]["outcome"] == "ok"
    sources = {row["model"]: row["source"] for row in payload["retired"]}
    assert sources[DEFAULT_CODE] == "refresh-evidence"
    assert all(sources[name] == "shipped" for name in SHIPPED["retired_cloud_models"])
    ladder = {row["model"]: row for row in payload["ladder"]["code"]}
    assert ladder["kimi-k3-code:cloud"]["eligible"] is True
    assert ladder["unpulled:cloud"]["eligible"] is False and ladder["unpulled:cloud"]["pulled"] is False
    assert payload["effective"]["code"] == {"model": "kimi-k3-code:cloud", "source": "default"}
    assert [p["tier"] for p in payload["pending_choices"]] == ["general"]
    assert payload["overlay_pins"]["code"]["active"] is True


def test_env_bound_tier_is_reported_as_env(home, monkeypatch, no_io):
    monkeypatch.setenv("SONDER_CLOUD_CODE", "mine:cloud")
    payload, _ = _call()
    assert payload["effective"]["code"] == {"model": "mine:cloud", "source": "env"}
    monkeypatch.setenv("SONDER_CLOUD_CODE", SHIPPED["retired_cloud_models"][0])  # a retired env value is not effective
    assert _call()[0]["effective"]["code"]["source"] == "default"


def test_pin_hiding_a_newer_shipped_default_is_flagged(home, no_io):
    (home / "model_lists.json").write_text(
        json.dumps({"version": 1, "default_cloud_models": {"code": "pinned:cloud"}}), encoding="utf-8")
    (home / refresh.STATE_FILE).write_text(json.dumps({
        "version": 1, "last_success_at": "2026-11-01T00:00:00Z",
        "overlay_pins": {"code": {"model": "pinned:cloud", "replaced": "older:cloud",
                                  "written_at": "2026-11-01T00:00:00Z", "run_id": "r"}}}), encoding="utf-8")
    model_lists.clear_cache()
    pin = _call()[0]["overlay_pins"]["code"]
    assert pin["active"] is True and pin["shipped_default"] == DEFAULT_CODE
    assert pin["hides_newer_shipped_default"] is True
    (home / "model_lists.json").write_text(
        json.dumps({"version": 1, "default_cloud_models": {"code": "operator-changed:cloud"}}), encoding="utf-8")
    model_lists.clear_cache()
    pin = _call()[0]["overlay_pins"]["code"]
    assert pin["active"] is False and pin["hides_newer_shipped_default"] is False


def test_bad_inputs_are_reported_not_raised(home, no_io):
    (home / "model_lists.json").write_text("{", encoding="utf-8")
    (home / refresh.PREFERENCES_FILE).write_text('{"nope": 1}', encoding="utf-8")
    (home / refresh.STATE_FILE).write_text("[]", encoding="utf-8")
    model_lists.clear_cache()
    payload, _ = _call()
    assert payload["ok"] is True
    assert payload["model_lists_issues"] and payload["preferences_error"].startswith("preferences_invalid")
    assert payload["state_error"] and "never refreshed" in payload["notes"]


def test_internal_failure_becomes_a_typed_error_not_an_exception(monkeypatch):
    def explode():
        raise RuntimeError("secret detail " * 30)

    monkeypatch.setattr(tool, "status_payload", explode)
    payload, calls = _call()
    assert payload["ok"] is False and payload["error"].startswith("RuntimeError") and len(payload["error"]) < 260
    assert calls == [("cloud_models_status", {}, False)]


def test_tool_source_has_no_write_or_network_calls():
    text = Path(tool.__file__).read_text(encoding="utf-8")
    for forbidden in ("run_refresh", "write_", "urlopen", "open_url", "subprocess", "requests"):
        assert forbidden not in text


def test_model_visible_paths_and_issues_are_redacted(home, no_io, monkeypatch):
    root = str(home)
    monkeypatch.setattr(tool, "default_path_redactor",
                        lambda: (lambda text: text.replace(root, "<HOME>")))
    (home / "model_lists.json").write_text('{"version": 1, "typo": 1}', encoding="utf-8")
    model_lists.clear_cache()
    payload, _calls = _call()
    assert payload["paths"] and all(v.startswith("<HOME>") for v in payload["paths"].values())
    assert payload["model_lists_issues"]  # the bad overlay is reported, by redacted path
    assert root not in json.dumps(payload)
    raw = refresh.status_payload()  # the operator-facing CLI payload keeps real paths
    assert raw["paths"]["state"].startswith(root)
