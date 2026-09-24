from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sonder_runtime.application.scenario_validation import (
    EvidenceClass,
    GitHubPublisher,
    ScenarioValidationRunner,
    Scenario,
    build_publish_plan,
)
from sonder_runtime.adapters.filesystem.durable_locks import exclusive_file_lock
from sonder_runtime.adapters.scenario_validation import ProcessAdapter


def test_runner_records_success_and_stable_marker(tmp_path):
    scenario = Scenario("smoke", "command works", (sys.executable, "-c", "print('ok')"))
    report = ScenarioValidationRunner(ProcessAdapter(), cwd=tmp_path).run([scenario], repository="Krilliac/Sonder-runtime", commit_sha="abc123")[0]
    assert report.passed is True
    assert report.result == "passed"
    assert report.stdout == "ok\n"
    assert report.marker.startswith("sonder-scenario-validation:")
    assert "<" not in report.marker


def test_runner_redacts_token_output_and_secret_argv(tmp_path):
    token = "ghp_" + "x" * 30
    scenario = Scenario("<script>", "claim", (sys.executable, "-c", f"print('{token}')", "--token", token))
    report = ScenarioValidationRunner(ProcessAdapter(), cwd=tmp_path).run([scenario], commit_sha="e" * 40)[0]
    assert token not in report.stdout
    assert token not in report.command
    assert report.command[-1] == "[REDACTED_ARG]"
    assert report.marker == "sonder-scenario-validation:" + report.marker.split(":", 1)[1]
    assert report.as_dict()["exit_code"] == 0


def test_runner_redacts_equals_form_headers_and_artifact_credentials(tmp_path):
    secret = "secret-value"
    scenario = Scenario(
        "redact",
        "claim",
        (sys.executable, "-c", "print('authorization: bearer secret-value')", "--token=" + secret, "--header", "Authorization: Basic " + secret, "--header", "Proxy-Authorization: Basic " + secret),
        artifact_refs=("https://example.test/report?token=" + secret, "safe-artifact.json"),
    )
    report = ScenarioValidationRunner(ProcessAdapter(), cwd=tmp_path).run([scenario])[0]
    assert secret not in report.command
    assert secret not in report.stdout
    assert report.artifact_refs == ("[REDACTED_ARTIFACT]", "safe-artifact.json")


def test_runner_stops_a_child_that_exceeds_output_limit(tmp_path):
    adapter = ProcessAdapter()
    adapter.max_output_bytes = 128
    scenario = Scenario("large", "bounded", (sys.executable, "-c", "print('x' * 10000)"))
    report = ScenarioValidationRunner(adapter, cwd=tmp_path).run([scenario])[0]
    assert report.result == "blocked"
    assert report.errors == ("adapter error: OutputLimitExceeded",)
    assert len(report.stdout.encode()) <= 128


def test_runner_terminates_an_infinite_noisy_child_at_output_limit(tmp_path):
    adapter = ProcessAdapter()
    adapter.max_output_bytes = 128
    scenario = Scenario("noisy", "bounded", (sys.executable, "-c", "import sys; [print('x' * 10000, flush=True) for _ in iter(int, 1)]"), timeout_seconds=30)
    started = time.monotonic()
    report = ScenarioValidationRunner(adapter, cwd=tmp_path).run([scenario])[0]
    elapsed = time.monotonic() - started
    assert report.result == "blocked"
    assert report.errors == ("adapter error: OutputLimitExceeded",)
    assert elapsed < 5


def test_process_adapter_does_not_forward_github_token(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_" + "x" * 30)
    scenario = Scenario("env", "secret absent", (sys.executable, "-c", "import os; print(os.getenv('GH_TOKEN', 'absent'))"))
    report = ScenarioValidationRunner(ProcessAdapter(), cwd=tmp_path).run([scenario])[0]
    assert report.stdout.strip() == "absent"


def test_missing_executable_is_blocked(tmp_path):
    scenario = Scenario("missing", "does not start", ("definitely-not-an-executable",))
    report = ScenarioValidationRunner(ProcessAdapter(), cwd=tmp_path).run([scenario])[0]
    assert report.result == "blocked"
    assert report.passed is False


def test_runner_isolates_timeout_and_stops_after_failures(tmp_path):
    bad = Scenario("bad", "fails", (sys.executable, "-c", "import sys; sys.exit(3)"), timeout_seconds=1)
    reports = ScenarioValidationRunner(ProcessAdapter(), cwd=tmp_path, max_steps=4, max_failures=2).run([bad, bad, bad])
    assert len(reports) == 2
    assert all(report.result == "failed" for report in reports)
    assert all(report.exit_code == 3 for report in reports)


def test_scenario_rejects_shell_style_command():
    scenario = Scenario("safe", "safe", ("echo", "$(whoami)"))
    assert scenario.command[1].startswith("$(")


def test_catalog_defaults_to_structural_evidence():
    scenario = Scenario.from_mapping({"name": "catalog", "claim": "loads", "command": [sys.executable, "-c", "pass"]})
    assert scenario.evidence_class is EvidenceClass.STRUCTURAL_ONLY


def test_cli_requires_explicit_trust_for_catalog_commands(tmp_path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps([{"name": "smoke", "claim": "works", "command": [sys.executable, "-c", "pass"]}]), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(Path(__file__).parents[1] / "scripts" / "scenario_validation.py"), "--catalog", str(catalog)],
        cwd=Path(__file__).parents[1], capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2
    assert "--trusted-local" in result.stderr


def test_github_publisher_is_dry_run_and_does_not_call_gh():
    report = ScenarioValidationRunner(ProcessAdapter()).run([Scenario("smoke", "works", (sys.executable, "-c", "pass"))], commit_sha="a" * 40)[0]
    plan = build_publish_plan(report, repository="Krilliac/Sonder-runtime")
    called = []
    result = GitHubPublisher().publish(plan)
    assert result["dry_run"] is True
    assert "commands" not in result
    assert called == []
    assert "sonder-scenario-validation:" in plan.issue_body
    assert "stdout" not in plan.issue_body


def test_github_publisher_requires_lock_provider_for_live_publish():
    report = ScenarioValidationRunner(ProcessAdapter()).run(
        [Scenario("smoke", "works", (sys.executable, "-c", "pass"))],
        commit_sha="a" * 40,
    )[0]
    plan = build_publish_plan(report, repository="x/y")

    class Adapter:
        def run(self, command):
            raise AssertionError("lock failure must precede GitHub calls")

    with pytest.raises(RuntimeError, match="lock provider"):
        GitHubPublisher(dry_run=False, adapter=Adapter()).publish(plan)


def test_github_body_redacts_query_credentials():
    scenario = Scenario("body", "claim?token=ghp_" + "x" * 30, (sys.executable, "-c", "print('pass')"), artifact_refs=("https://example.test/a?token=secret", "safe-artifact.json"))
    report = ScenarioValidationRunner(ProcessAdapter()).run([scenario], commit_sha="a" * 40)[0]
    plan = build_publish_plan(report, repository="x/y")
    assert "ghp_" not in plan.issue_body
    assert "token=secret" not in plan.issue_body
    assert "safe-artifact.json" in plan.issue_body


def test_github_title_is_sanitized_and_bounded():
    token = "ghp_" + "x" * 30
    scenario = Scenario("bad\n" + token + "!", "claim", (sys.executable, "-c", "pass"))
    report = ScenarioValidationRunner(ProcessAdapter()).run([scenario], commit_sha="a" * 40)[0]
    plan = build_publish_plan(report, repository="x/y")
    assert "\n" not in plan.issue_title
    assert token not in plan.issue_title
    assert len(plan.issue_title) < 140


def test_github_publisher_deduplicates_by_marker_without_creating_issue():
    report = ScenarioValidationRunner(ProcessAdapter()).run([Scenario("smoke", "works", (sys.executable, "-c", "pass"))], commit_sha="d" * 40)[0]
    plan = build_publish_plan(report, repository="x/y")
    calls = []

    def fake_gh(command, **kwargs):
        calls.append(command)
        if command[2] == "list":
            return subprocess.CompletedProcess(command, 0, json.dumps([{
                "number": 9, "body": f"<!-- {plan.marker} -->",
            }]), "")
        raise AssertionError("duplicate issue must not be created")

    class Adapter:
        def run(self, command):
            return fake_gh(command)
    result = GitHubPublisher(dry_run=False, adapter=Adapter(), lock_factory=exclusive_file_lock).publish(plan)
    assert result["deduplicated_issue"] is True
    assert len(calls) == 1
    assert calls[0][calls[0].index("--state") + 1] == "all"


def test_github_publisher_requires_exact_marker_body_before_deduplication():
    report = ScenarioValidationRunner(ProcessAdapter()).run([
        Scenario("smoke", "works", (sys.executable, "-c", "pass"))
    ], commit_sha="d" * 40)[0]
    plan = build_publish_plan(report, repository="x/y")
    calls = []

    class Adapter:
        def run(self, command):
            calls.append(command)
            if command[2] == "list":
                return subprocess.CompletedProcess(command, 0, json.dumps([{
                    "number": 9, "body": "another marker in a closed issue",
                }]), "")
            return subprocess.CompletedProcess(command, 0, "https://example.test/issues/10", "")

    result = GitHubPublisher(dry_run=False, adapter=Adapter(), lock_factory=exclusive_file_lock).publish(plan)
    assert result["deduplicated_issue"] is False
    assert calls[-1][:3] == ("gh", "issue", "create")


def test_github_publisher_deduplicates_merged_pr_by_exact_marker():
    report = ScenarioValidationRunner(ProcessAdapter()).run([
        Scenario("smoke", "works", (sys.executable, "-c", "pass"))
    ], commit_sha="e" * 40)[0]
    plan = build_publish_plan(
        report, repository="x/y", branch="feature/scenario-validation", request_pr=True,
        clean=True, head_sha="e" * 40,
    )
    calls = []

    class Adapter:
        def run(self, command):
            calls.append(command)
            if command[:3] == ("gh", "pr", "list"):
                return subprocess.CompletedProcess(command, 0, json.dumps([{
                    "number": 9, "body": f"<!-- {plan.marker} -->",
                }]), "")
            raise AssertionError("merged PR must not be recreated")

    result = GitHubPublisher(dry_run=False, adapter=Adapter(), lock_factory=exclusive_file_lock).publish(
        plan, create_issue=False, create_pr=True,
    )
    assert result["deduplicated_pr"] is True
    assert calls[0][calls[0].index("--state") + 1] == "all"


def test_pr_requires_clean_non_base_branch_at_report_sha():
    report = ScenarioValidationRunner(ProcessAdapter()).run([Scenario("smoke", "works", (sys.executable, "-c", "pass"))], commit_sha="b" * 40)[0]
    with pytest.raises(ValueError, match="clean"):
        build_publish_plan(report, repository="x/y", branch="main", request_pr=True, clean=False, head_sha="b" * 40)
    plan = build_publish_plan(report, repository="x/y", branch="feature/scenario-validation", request_pr=True, clean=True, head_sha="b" * 40)
    assert plan.pr_command[:3] == ("gh", "pr", "create")


def test_pr_rejects_stale_sha_or_failed_report():
    report = ScenarioValidationRunner(ProcessAdapter()).run([Scenario("smoke", "works", (sys.executable, "-c", "pass"))], commit_sha="f" * 40)[0]
    with pytest.raises(ValueError):
        build_publish_plan(report, repository="x/y", branch="feature/scenario-validation", request_pr=True, clean=True, head_sha="0" * 40)
    failed = report.__class__(**{**report.__dict__, "passed": False, "result": "failed"})
    with pytest.raises(ValueError):
        build_publish_plan(failed, repository="x/y", branch="feature/scenario-validation", request_pr=True, clean=True, head_sha="f" * 40)


def test_timeout_is_blocked(tmp_path):
    scenario = Scenario("slow", "finishes", (sys.executable, "-c", "import time; time.sleep(1)"), timeout_seconds=0.05)
    report = ScenarioValidationRunner(ProcessAdapter(), cwd=tmp_path).run([scenario])[0]
    assert report.result == "blocked"
    assert report.passed is False


def test_evidence_class_is_serializable():
    scenario = Scenario("normal-flow", "application flow", (sys.executable, "-c", "pass"), EvidenceClass.NORMAL_FLOW)
    report = ScenarioValidationRunner(ProcessAdapter()).run([scenario], commit_sha="c" * 40)[0]
    assert json.dumps(report.as_dict(), sort_keys=True)
