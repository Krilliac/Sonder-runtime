"""Sensitive text printed by the standalone curriculum and selfmod drivers."""

from __future__ import annotations

import sys

import curriculum_run
from scripts import nightly_selfmod, selfmod_forever


def test_curriculum_outcome_redacts_secret_without_changing_lesson_count(monkeypatch, capsys):
    monkeypatch.setattr(curriculum_run.server, "sonder", lambda _prompt: "answer")
    monkeypatch.setattr(curriculum_run.server, "parse_interaction_id", lambda _resp: "iid")
    monkeypatch.setattr(curriculum_run.grounding, "extract_code_block", lambda _resp: None)
    monkeypatch.setattr(
        curriculum_run.server, "record_self_graded_outcome",
        lambda _iid, _signal: "Recorded. Distilled lesson 7. password=hunter42x",
    )

    assert curriculum_run.practice_on([{"name": "sample", "prompt": "question"}]) == (0, 1)
    output = capsys.readouterr().out
    assert "FAIL" in output and "Distilled lesson 7." in output
    assert "hunter42x" not in output
    assert "password=[REDACTED]" in output


def test_nightly_entry_redacts_settings_and_run_log(monkeypatch, capsys):
    monkeypatch.setattr(nightly_selfmod.selfmod, "settings", lambda: {"mode": "propose"})

    def fake_run(_server, log):
        log("candidate password=hunter42x")
        return "review required"

    monkeypatch.setattr(nightly_selfmod, "run", fake_run)

    assert nightly_selfmod.main() == 0
    output = capsys.readouterr().out
    assert '{"mode": "propose"}' in output and "review required" in output
    assert "hunter42x" not in output
    assert "password=[REDACTED]" in output


def test_forever_entry_redacts_callback_without_changing_stop(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["selfmod_forever.py", "--hours", "0"])
    monkeypatch.setattr(selfmod_forever.selfmod, "settings", lambda: {"mode": "propose"})
    monkeypatch.setattr(
        selfmod_forever.nightly_selfmod,
        "reclaim_orphans",
        lambda log: log("orphan password=hunter42x"),
    )

    assert selfmod_forever.main() == 0
    output = capsys.readouterr().out
    assert "mode=propose" in output and "=== done: 0 pass(es), 0 commit(s) ===" in output
    assert "hunter42x" not in output
    assert "password=[REDACTED]" in output
