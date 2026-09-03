"""Fanout receipt safety lives in the domain; root names stay aliases or delegates."""
import json

import server
from sonder_runtime.domain import fanout_receipts as receipts


def _cloud(name):
    return name.endswith(":cloud")


def test_root_safe_answer_is_an_identity_preserving_alias():
    assert server._fanout_safe_answer is receipts.safe_answer


def test_safe_answer_scrubs_credentials_after_prompt_echo_removal():
    prompt = "please summarize the quarterly ledger for the finance team"
    text = (
        "Sure: " + prompt + ". Use Authorization: Bearer abcdefghijklmnop and "
        "password=hunter2 then api_key: 'sk-123456'."
    )
    out = receipts.safe_answer(text, prompt)
    assert "<redacted prompt>" in out
    for secret in ("abcdefghijklmnop", "hunter2", "sk-123456"):
        assert secret not in out
    assert "Authorization: <redacted>" in out
    assert "password=<redacted>" in out
    assert "api_key=<redacted>" in out
    assert receipts.safe_answer("plain prose", "unrelated prompt") == "plain prose"


def test_snapshot_allows_only_immutable_targets_within_scope():
    run = {"models_json": json.dumps(["Local:7b", "far:cloud"]), "scope": "all"}

    def allows(current, model):
        return receipts.snapshot_allows(current, model, is_cloud_model_name=_cloud)

    assert allows(run, "local:7b") and allows(run, "far:cloud")
    assert not allows(run, "other:7b")
    assert allows(dict(run, scope="local"), "local:7b")
    assert not allows(dict(run, scope="local"), "far:cloud")
    assert allows(dict(run, scope="cloud"), "far:cloud")
    assert not allows(dict(run, scope="cloud"), "local:7b")
    assert allows(dict(run, scope="available"), "local:7b")
    assert not allows({"models_json": "broken"}, "local:7b")
    assert not allows({}, "local:7b")


def test_root_delegate_uses_the_server_cloud_classifier(monkeypatch):
    run = {"models_json": json.dumps(["x:y"]), "scope": "cloud"}
    monkeypatch.setattr(server, "_is_cloud_model_name", lambda name: name == "x:y")
    assert server._fanout_snapshot_allows(run, "x:y") is True
    monkeypatch.setattr(server, "_is_cloud_model_name", lambda name: False)
    assert server._fanout_snapshot_allows(run, "x:y") is False
