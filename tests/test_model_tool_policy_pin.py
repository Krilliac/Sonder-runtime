"""The shipped model tool policy is pinned when it is first read, for the life of the process.

A model that gets a write into the shipped file by some route nobody guarded
(a git tool, a formatter, a name the file tools do not recognise) must not
change what this process enforces.  The first valid read of the shipped file
pins its sha256; a later read that finds other bytes makes the floor
unavailable, which refuses every tool lane (the same rule as a missing file),
until the console operator restarts Sonder or runs ``/runtime tools reload``,
which pins what is on disk and prints both hashes.  A rewrite with identical
bytes is not a change.
"""
from __future__ import annotations

import hashlib
import json
import shutil

import pytest

from sonder_runtime.adapters import model_tool_policy as mtp
from sonder_runtime.adapters import model_tool_policy_editor as editor
from sonder_runtime.adapters import runtime_policy
from sonder_runtime.adapters import tool_lane_guard as guard
from sonder_runtime.domain import tool_lane_models as rule
from sonder_runtime.domain.tool_lane_models import ModelLaneStatus
from tests.support.tool_lane import ALLOWED, DENIED, clear_environment


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    clear_environment(monkeypatch)
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime_policy.json"))
    monkeypatch.setenv(mtp.OVERLAY_ENV, str(tmp_path / "overlay" / "model_tool_policy.json"))
    (tmp_path / "overlay").mkdir()
    monkeypatch.setattr(guard, "_GENERATION_CACHE", None, raising=False)
    mtp.clear_cache()
    yield
    mtp.clear_cache()


@pytest.fixture
def shipped(monkeypatch, tmp_path):
    """A private copy of the shipped file; ``SHIPPED_PATH`` points at it."""
    path = tmp_path / "pkg" / "model_tool_policy.json"
    path.parent.mkdir()
    shutil.copyfile(mtp.SHIPPED_PATH, path)
    monkeypatch.setattr(mtp, "SHIPPED_PATH", path)
    mtp.clear_cache()
    return path


def _decide(name):
    return rule.decide(name, runtime_policy.tool_lane_deny_list(runtime_policy.load(create=False)), free_form=False)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _with_an_extra_floor_prefix(path):
    document = json.loads(path.read_text(encoding="utf-8"))
    document["model_profiles"][0]["match"]["prefixes"].append("extra-prefix-")
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def test_a_shipped_change_after_the_pin_refuses_every_lane_until_reload(shipped):
    original = shipped.read_bytes()
    assert mtp.load().readable_shipped  # the first valid read pins it
    assert _decide(DENIED).status is ModelLaneStatus.DENIED
    _with_an_extra_floor_prefix(shipped)  # valid JSON, still a valid floor: only the bytes differ
    changed = shipped.read_bytes()
    assert changed != original

    config = mtp.load()  # no clear_cache: a write that races the process is what is being modelled
    assert config.readable_shipped is False
    assert config.shipped_pin.name == "CHANGED"
    assert any("restart" in issue.message or "reload" in issue.message for issue in config.issues), config.issues
    for name in (ALLOWED, DENIED):
        assert _decide(name).status is ModelLaneStatus.CONFIG_UNAVAILABLE, name
    assert "shipped model tool policy is unavailable" in guard.refusal(ALLOWED)

    # Not the unattended callers, and not an agent: the pin is not theirs to move.
    refused = editor.command("reload", operator_attended=False)
    assert refused.startswith("refused"), refused
    assert mtp.load().readable_shipped is False

    # The operator at the console re-pins, and sees both hashes.
    report = editor.command("reload", operator_attended=True)
    assert _sha(original) in report and _sha(changed) in report, report
    config = mtp.load()
    assert config.readable_shipped is True
    assert "extra-prefix-" in config.floor.prefixes
    assert _decide(DENIED).status is ModelLaneStatus.DENIED


def test_an_identical_rewrite_keeps_the_floor(shipped):
    assert mtp.load().readable_shipped
    shipped.write_bytes(shipped.read_bytes())  # new mtime, same bytes
    config = mtp.load()
    assert config.readable_shipped is True and not config.issues
    assert _decide(DENIED).status is ModelLaneStatus.DENIED


def test_a_change_that_is_undone_restores_the_floor_without_a_reload(shipped):
    """The pin is a hash, not a latch: the pinned bytes back on disk are the pinned floor again."""
    original = shipped.read_bytes()
    assert mtp.load().readable_shipped
    _with_an_extra_floor_prefix(shipped)
    assert mtp.load().readable_shipped is False
    shipped.write_bytes(original)
    assert mtp.load().readable_shipped is True
    assert _decide(DENIED).status is ModelLaneStatus.DENIED


def test_each_shipped_path_is_pinned_on_its_own(shipped, tmp_path, monkeypatch):
    """A second file (another SHIPPED_PATH, as the tests and a relocated install use) pins at its own first read."""
    assert mtp.load().readable_shipped
    other = tmp_path / "other" / "model_tool_policy.json"
    other.parent.mkdir()
    shutil.copyfile(shipped, other)
    _with_an_extra_floor_prefix(other)  # different bytes from the first file, never read before
    monkeypatch.setattr(mtp, "SHIPPED_PATH", other)
    mtp.clear_cache()
    config = mtp.load()
    assert config.readable_shipped is True and "extra-prefix-" in config.floor.prefixes
