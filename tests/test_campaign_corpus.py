"""Characterize campaign datasets and their package extraction boundary."""

import hashlib
import json

import pytest


@pytest.mark.parametrize(
    ("name", "count", "width", "fingerprint"),
    [
        (
            "_CAMPAIGN_TASKS",
            12,
            2,
            "f3bd97ce261bc41009e51d8919b83e60c9d6cadb76c8b48e54a27a704ca62b9b",
        ),
        (
            "_REPO_REPAIR_TASKS",
            10,
            3,
            "d6e209f00d96d0173534858dc7effd214105c4d193607475320b3de3c7cd0c7f",
        ),
    ],
)
def test_server_corpus_matches_pre_extraction_values(name, count, width, fingerprint):
    import server

    rows = getattr(server, name)
    assert type(rows) is list
    assert len(rows) == count
    assert all(type(row) is tuple and len(row) == width for row in rows)
    assert all(type(value) is str and value for row in rows for value in row)
    encoded = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert hashlib.sha256(encoded).hexdigest() == fingerprint


@pytest.mark.parametrize(
    ("server_name", "export_name"),
    [
        ("_CAMPAIGN_TASKS", "CAMPAIGN_TASKS"),
        ("_REPO_REPAIR_TASKS", "REPO_REPAIR_TASKS"),
    ],
)
def test_package_corpus_exports_are_direct_server_aliases(server_name, export_name):
    from sonder_runtime.domain import campaign_corpus
    import server

    assert server._campaign_corpus is campaign_corpus
    assert getattr(server, server_name) is getattr(campaign_corpus, export_name)


@pytest.mark.parametrize(
    ("name", "temporary_row"),
    [
        ("_CAMPAIGN_TASKS", ("temporary task", "temporary prompt")),
        ("_REPO_REPAIR_TASKS", ("temporary task", "temporary source", "temporary fix")),
    ],
)
def test_server_corpus_compatibility_globals_remain_patchable(monkeypatch, name, temporary_row):
    import server

    original = getattr(server, name)
    original_values = original.copy()
    temporary = []
    with monkeypatch.context() as patch:
        patch.setattr(server, name, temporary)
        assert getattr(server, name) is temporary
        temporary.append(temporary_row)
        assert getattr(server, name) == [temporary_row]
        temporary.clear()
        assert getattr(server, name) == []
    assert getattr(server, name) is original
    assert original == original_values


def test_server_watches_package_campaign_corpus():
    import server

    assert "sonder_runtime.domain.campaign_corpus" in server.LIVE_RELOAD_MODULES


@pytest.mark.parametrize("empty", [False, True], ids=["nonempty", "empty"])
def test_live_reload_rebinds_corpus_module_and_both_lists(monkeypatch, empty):
    from types import ModuleType

    import server

    original_module = server._campaign_corpus
    original_campaign = server._CAMPAIGN_TASKS
    original_repair = server._REPO_REPAIR_TASKS
    replacement = ModuleType("sonder_runtime.domain.campaign_corpus")
    replacement.CAMPAIGN_TASKS = [] if empty else [("temporary task", "temporary prompt")]
    replacement.REPO_REPAIR_TASKS = [] if empty else [
        ("temporary task", "temporary source", "temporary fix")
    ]
    # Reload writes globals directly; register their originals for teardown.
    monkeypatch.setattr(server, "_campaign_corpus", original_module)
    monkeypatch.setattr(server, "_CAMPAIGN_TASKS", original_campaign)
    monkeypatch.setattr(server, "_REPO_REPAIR_TASKS", original_repair)
    watched = []
    policy_creation = []

    def reload_changed(names):
        watched.append(names)
        return {"sonder_runtime.domain.campaign_corpus": replacement}

    monkeypatch.setattr(server.live_reload, "reload_changed_modules", reload_changed)
    monkeypatch.setattr(
        server,
        "_refresh_runtime_policy",
        lambda **kwargs: policy_creation.append(kwargs["create"]),
    )

    server._maybe_live_reload(create_policy=False)

    assert watched == [server.LIVE_RELOAD_MODULES]
    assert policy_creation == [False]
    assert server._campaign_corpus is replacement
    assert server._campaign_corpus is not original_module
    assert server._CAMPAIGN_TASKS is replacement.CAMPAIGN_TASKS
    assert server._REPO_REPAIR_TASKS is replacement.REPO_REPAIR_TASKS
    assert server._CAMPAIGN_TASKS is not original_campaign
    assert server._REPO_REPAIR_TASKS is not original_repair
