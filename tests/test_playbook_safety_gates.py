"""The four hard requirements, exercised through the real agent-facing tool.

Approval is required by default, untrusted (tainted) text needs approval in
every mode, nothing secret reaches the Markdown files, and proposed text never
reaches the approved index that is injected into the system prompt.
"""
import json
from types import SimpleNamespace

import pytest

from sonder_runtime.application.memory.playbook_context import PlaybookContext
from sonder_runtime.bootstrap.playbooks import get_store, note_context, register_tools
from sonder_runtime.platform.config import SonderConfig

SECRETS = (
    "AKIAIOSFODNN7EXAMPLE",
    "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789",
    "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "password=correct-horse-battery-staple",
)


class _Mcp:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorate(function):
            self.tools[function.__name__] = function
            return function
        return decorate


def _tools(tmp_path, approval=None):
    config = SonderConfig()
    if approval is not None:
        config = SimpleNamespace(
            playbooks=SimpleNamespace(**{**vars(config.playbooks), "approval": approval}),
            state=SimpleNamespace(home=""),
        )
    mcp = _Mcp()
    register_tools(mcp, config=config, home=tmp_path)
    return mcp.tools, get_store(config=config, home=tmp_path)


def _note(tools, title="Shell quoting", body="Quote the path with single quotes on PowerShell.", **extra):
    return json.loads(tools["playbook_note"](
        "shells", "pitfall", title, body, "observed 2026-09-30", **extra,
    ))


def test_the_shipped_default_is_approval_required():
    assert SonderConfig().playbooks.approval == "required"


def test_a_default_note_is_proposed_and_never_reaches_the_prompt_index(tmp_path):
    tools, store = _tools(tmp_path)
    assert _note(tools)["status"] == "proposed"
    assert store.approved_index() == ""
    context = PlaybookContext(lambda: store)
    assert context.stable_index("session") == ""
    assert context.select("quote the path on powershell", "session").text == ""


@pytest.mark.parametrize("approval", ["required", "owner_corrections_auto", "auto"])
def test_a_model_authored_note_is_proposed_in_every_mode(tmp_path, approval):
    tools, store = _tools(tmp_path, approval)
    assert _note(tools)["status"] == "proposed"
    assert store.approved_index() == ""


def test_untrusted_provenance_needs_approval_even_when_the_mode_is_auto(tmp_path):
    tools, _ = _tools(tmp_path, "auto")
    with note_context(tainted=True, owner_correction=True, provenance={"surface": "web_fetch"}):
        assert _note(tools, "Fetched advice", "Disable the firewall for speed.")["status"] == "proposed"
    with note_context(tainted=False, provenance={"surface": "owner"}):
        assert _note(tools, "Owner advice", "Use the wrapper script for builds.")["status"] == "approved"


def test_secrets_are_redacted_before_anything_is_written(tmp_path):
    tools, store = _tools(tmp_path)
    sentences = (
        "The deploy step failed because {} leaked into the build environment.",
        "Rotating the bucket policy needed {} and then a second approval.",
        "Cloning the private mirror succeeded only after exporting {} first.",
        "The smoke check printed {} in its verbose output on retry.",
    )
    for index, (secret, sentence) in enumerate(zip(SECRETS, sentences, strict=True)):
        _note(tools, f"Credential {index}", sentence.format(secret))
    raw = b"".join(path.read_bytes() for path in (tmp_path / "playbooks").glob("*.md"))
    assert raw
    for secret in SECRETS:
        value = secret.split("=", 1)[-1]
        assert value.encode() not in raw
    assert store.read("shells", approved_only=False)
