"""The ``batch_job_*`` tools: owner scoping, strict types, typed errors, config wiring."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

import server
from sonder_runtime.bootstrap import batch_job_tools
from sonder_runtime.platform.batch_jobs_config import BatchJobsConfig
from sonder_runtime.platform.config import SonderConfig
from tests.support.batch_job_harness import build, two_stages

ACCOUNTS = {
    "tok-a": ("fo-a", {"role": "developer"}),
    "tok-b": ("fo-b", {"role": "developer"}),
    "tok-admin": ("fo-admin", {"role": "admin"}),
}


@pytest.fixture
def harness(monkeypatch, tmp_path):
    return build(monkeypatch, tmp_path)


@pytest.fixture
def shared(harness, monkeypatch):
    monkeypatch.setattr(server, "_deployment_authenticates_callers", lambda: True)
    monkeypatch.setattr(server, "_direct_fanout_identity", lambda token: ACCOUNTS.get(token, ("", None)))
    return harness


def _error(payload):
    assert payload["ok"] is False
    assert set(payload) == {"ok", "error"} and set(payload["error"]) == {"code", "message"}
    return payload["error"]


def test_another_developer_gets_a_not_found_shaped_error_and_an_admin_can_reach_the_job(shared):
    job_id = shared.create(two_stages(), items=1, start=False, token="tok-a")["job"]["job_id"]
    missing = _error(shared.tool("batch_job_status", job_id="job-doesnotexist", token="tok-b"))
    for tool in ("batch_job_status", "batch_job_results", "batch_job_cancel"):
        denied = _error(shared.tool(tool, job_id=job_id, token="tok-b"))
        assert denied == missing and denied["code"] == "NOT_FOUND"
    for tool in ("batch_job_start", "batch_job_resume", "batch_job_add_items"):
        extra = {"items": []} if tool == "batch_job_add_items" else {}
        assert _error(shared.tool(tool, job_id=job_id, token="tok-b", **extra)) == missing
    assert shared.tool("batch_job_status", job_id=job_id, token="tok-a")["ok"] is True
    assert shared.tool("batch_job_status", job_id=job_id, token="tok-admin")["ok"] is True
    assert shared.tool("batch_job_results", job_id=job_id, token="tok-admin")["ok"] is True


def test_recent_is_owner_scoped_for_developers_and_global_for_an_admin(shared):
    mine = shared.create(two_stages(), items=1, token="tok-a")["job"]["job_id"]
    theirs = shared.create(two_stages(), items=1, token="tok-b")["job"]["job_id"]
    assert [job["job_id"] for job in shared.tool("batch_job_recent", token="tok-a")["jobs"]] == [mine]
    assert {job["job_id"] for job in shared.tool("batch_job_recent", token="tok-admin")["jobs"]} == {mine, theirs}


def test_the_developer_gate_refusal_is_a_typed_forbidden_error(harness, monkeypatch):
    monkeypatch.setattr(server, "_developer_gate", lambda *_a, **_k: "refused: developer authority is required.")
    assert _error(harness.tool("batch_job_recent"))["code"] == "FORBIDDEN"


@pytest.mark.parametrize("tool,kwargs", [
    ("batch_job_recent", {"limit": True}), ("batch_job_recent", {"limit": 0}),
    ("batch_job_recent", {"limit": 101}), ("batch_job_recent", {"include_finished": "yes"}),
    ("batch_job_results", {"job_id": "job-x", "offset": -1}), ("batch_job_results", {"job_id": "job-x", "limit": 201}),
    ("batch_job_results", {"job_id": "job-x", "limit": False}),
    ("batch_job_results", {"job_id": "job-x", "include_answers": 1}),
    ("batch_job_resume", {"job_id": "job-x", "include_failed": "false"}),
])
def test_strict_types_and_ranges_are_rejected_with_invalid_input(harness, tool, kwargs):
    assert _error(harness.tool(tool, **kwargs))["code"] == "INVALID_INPUT"


def test_job_id_must_be_a_string(harness):
    assert _error(harness.tool("batch_job_status", job_id=""))["code"] == "INVALID_INPUT"
    assert _error(harness.tool("batch_job_status", job_id=7))["code"] == "INVALID_INPUT"


def test_a_cloud_model_in_any_stage_is_refused_at_create_time(harness):
    stages = two_stages("m:1", "kimi-k3:cloud")
    error = _error(harness.create(stages, items=1, start=True))
    assert error["code"] == "INVALID_INPUT" and "cloud" in error["message"]
    assert harness.tool("batch_job_recent")["jobs"] == []
    assert harness.calls == []


def test_unknown_and_non_chat_models_are_refused(harness, monkeypatch):
    assert _error(harness.create(two_stages("not-installed:1"), items=1))["code"] == "INVALID_INPUT"
    monkeypatch.setattr(server, "_fanout_nonchat_reason", lambda record: "embedding model")
    assert _error(harness.create(two_stages(), items=1))["code"] == "INVALID_INPUT"


def test_a_catalog_outage_is_a_typed_dependency_error(harness, monkeypatch):
    def down(_path):
        raise OSError("connection refused")

    monkeypatch.setattr(server, "_get", down)
    assert _error(harness.create(two_stages(), items=1))["code"] == "DEPENDENCY_UNAVAILABLE"


def test_template_and_item_errors_surface_as_invalid_input_at_create_time(harness):
    bad = two_stages()
    bad[1]["prompt"] = "{{stage.r3}}"
    assert _error(harness.create(bad, items=1))["code"] == "INVALID_INPUT"
    forward = two_stages()
    forward[0]["prompt"] = "{{stage.r2}}"
    assert _error(harness.create(forward, items=1))["code"] == "INVALID_INPUT"
    unknown_field = two_stages()
    unknown_field[0]["prompt"] = "{{nope}}"
    assert _error(harness.create(unknown_field, items=1))["code"] == "INVALID_INPUT"
    duplicate = harness.tool(
        "batch_job_create", name="j", stages=two_stages(), fields=["text"], start=False,
        items=[{"key": "a", "fields": {"text": "x"}}, {"key": "a", "fields": {"text": "y"}}])
    assert _error(duplicate)["code"] == "INVALID_INPUT"


def test_items_added_in_pages_reject_duplicates_across_pages(harness):
    job_id = harness.create(two_stages(), items=1)["job"]["job_id"]
    added = harness.tool("batch_job_add_items", job_id=job_id, items=[{"key": "more", "fields": {"text": "x"}}])
    assert added["ok"] is True and added["items"] == 2
    again = harness.tool("batch_job_add_items", job_id=job_id, items=[{"key": "more", "fields": {"text": "x"}}])
    assert _error(again)["code"] == "INVALID_INPUT"


def test_defaults_apply_when_the_configuration_is_unavailable(harness):
    # build() patched load_config_or_none to return None.
    assert batch_job_tools._config() == BatchJobsConfig()
    assert harness.create(two_stages(), items=1)["ok"] is True


def test_configured_limits_apply_and_a_disabled_feature_is_forbidden(monkeypatch, tmp_path):
    config = replace(SonderConfig(), batch_jobs=replace(BatchJobsConfig(), max_stages=1, max_active_jobs=1))
    limited = build(monkeypatch, tmp_path, config=config)
    assert _error(limited.create(two_stages(), items=1))["code"] == "INVALID_INPUT"
    one = [two_stages()[0]]
    assert limited.create(one, items=1, start=False)["ok"] is True
    first = limited.create(one, items=1, start=False)["job"]["job_id"]
    # A queued job occupies the single active slot (a draft does not); keep it queued.
    limited.service.worker.submit = lambda _job_id: None
    assert limited.tool("batch_job_start", job_id=first)["ok"] is True
    second = limited.create(one, items=1, start=False)["job"]["job_id"]
    assert _error(limited.tool("batch_job_start", job_id=second))["code"] == "CAPACITY_EXCEEDED"

    off = replace(SonderConfig(), batch_jobs=replace(BatchJobsConfig(), enabled=False))
    (tmp_path / "off").mkdir()
    disabled = build(monkeypatch, tmp_path / "off", config=off)
    assert _error(disabled.create(one, items=1))["code"] == "FORBIDDEN"


def test_an_unexpected_failure_is_reported_without_leaking_its_text(harness, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr("sonder_runtime.adapters.persistence.batch_store.recent_jobs", boom)
    error = _error(harness.tool("batch_job_recent"))
    assert error["code"] == "INTERNAL_FAILURE" and "secret" not in json.dumps(error)


def test_unreadable_sealed_answers_give_a_typed_error_not_a_crash(harness):
    job_id = harness.create(two_stages(), items=1, start=True)["job"]["job_id"]
    harness.wait()
    key = harness.database.parent / "fanout.key"
    key.write_bytes(b"not-a-valid-fernet-key")
    server.fanout_prompt_vault.reset_cache_for_tests()

    error = _error(harness.tool("batch_job_results", job_id=job_id))
    assert error["code"] == "INTEGRITY_FAILURE"
    listing = harness.tool("batch_job_results", job_id=job_id, include_answers=False)
    assert listing["ok"] is True and "answer" not in listing["items"][0]["steps"][0]


def test_read_tools_are_graded_read_only_and_lifecycle_tools_keep_the_fail_closed_default():
    from sonder_runtime.adapters import command_catalog, mcp_tool_manifest

    assert {"batch_job_status", "batch_job_results", "batch_job_recent"} <= command_catalog._READ_ONLY
    for mutating in ("batch_job_create", "batch_job_add_items", "batch_job_start",
                     "batch_job_cancel", "batch_job_resume"):
        assert mutating not in command_catalog._READ_ONLY
    advertised = next(key for key in mcp_tool_manifest.MCP_TOOL_MANIFEST if key.startswith("batch_job_create/"))
    assert set(advertised.split("/")) == {
        "batch_job_create", "batch_job_add_items", "batch_job_start", "batch_job_status",
        "batch_job_results", "batch_job_recent", "batch_job_cancel", "batch_job_resume",
    }
