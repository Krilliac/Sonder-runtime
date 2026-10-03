"""Prove failure capture reaches the uploader after real pytest startup."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace as NS
import xml.etree.ElementTree as ET

import pytest


def _workflow_route(runner_temp):
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    exercise = workflow.split("      - name: Exercise managed owner, artifact verifier, and lifecycle regressions\n", 1)[1].split("      - name:", 1)[0]
    variable, directory = re.search(r"(?m)^          ([A-Z_]+): (.+)$", exercise).groups()
    upload = workflow.split("      - name: Retain managed owner failure diagnostics\n", 1)[1].split("      - name:", 1)[0]
    artifact = re.search(r"(?m)^          path: (.+)$", upload).group(1)
    prefix = "${{ runner.temp }}"
    assert directory.startswith(prefix + "/") and artifact.startswith(prefix + "/")
    directory = Path(directory.replace(prefix, str(runner_temp), 1))
    artifact = Path(artifact.replace(prefix, str(runner_temp), 1))
    assert artifact == directory / "owner-diagnostic.json"
    return variable, directory, artifact


def _exercise_failure_capture(failure_phase, tmp_path, monkeypatch, artifact):
    from tests import test_managed_runtime_owner as subject

    # This assertion observes the real repository-root conftest startup scrub.
    assert "SONDER_OWNER_DIAGNOSTIC_DIR" not in os.environ
    sensitive = "synthetic-diagnostic-sensitive-value"
    opaque = "a" * 64
    output = "cleanup detail\nAuthorization: Bearer " + sensitive + "\n" + opaque
    calls = []
    close_observations = []
    refusal = subject.OwnerRefused("synthetic first-launch readiness refusal")

    class Registry:
        def view(self, job):
            calls.append(("view", job))
            return NS(record=NS(status=NS(value="failed"), result={"exit_code": 7},
                                error=output, revision=4, created_at="t0", updated_at="t1"),
                      process_id=123, process_group_id=123)

        def stream(self, job, **bounds):
            calls.append(("stream", job, bounds))
            return NS(events=[NS(watermark=NS(sequence=1), stream=NS(value="stderr"), data=output)],
                      next_watermark=NS(sequence=1), has_more=False, truncated=False)

    class Owner:
        def __init__(self):
            self.path = tmp_path / "owner"
            self.path.mkdir()
            self._process = NS(registry=Registry())
            self._launch_id = None
            self.journal = NS(complete=lambda command, result, state: None)
            terminal = {"phase": "STARTING" if failure_phase == "launch" else "UNCLEAN",
                        "components": [{"component": "workers", "state": "UNRESOLVED", "evidence": output}]}
            (self.path / "runtime-launch0.json").write_text(json.dumps(terminal), encoding="utf-8")

        def register_configuration(self, **kwargs):
            return "configuration"

        def prepare(self, operation_id, action, payload):
            return NS(operation_id=operation_id, action=action)

        def execute(self, command):
            if command.action == "select":
                return {"state": "SELECTED"}
            if command.action == "launch":
                self._launch_id = command.operation_id
                if failure_phase == "launch":
                    raise refusal
                return {"state": "RUNNING"}
            assert command.action == "stop"
            receipt = {"state": "STOPPED_UNCLEAN"}
            self.journal.complete(command, receipt, receipt["state"])
            return receipt

        @property
        def selected_store(self):
            raise subject.OwnerRefused("running child owns the selected store")

        def _config(self, reference):
            return {"port": 8765}

        def close(self):
            # Observe publication before the original finally closes the owner.
            # Never replace the original failure if capture went to the wrong path.
            close_observations.append(artifact.is_file())

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    owner = Owner()
    monkeypatch.setattr(subject, "ManagedRuntimeOwner", lambda *args, **kwargs: owner)
    monkeypatch.setattr(subject, "require_bounded_real_runtime_closure", lambda: None)
    monkeypatch.setattr(subject, "port", lambda: 8765)
    monkeypatch.setattr(subject.urllib.request, "urlopen", lambda *args, **kwargs: Response())
    expected_error = subject.OwnerRefused if failure_phase == "launch" else AssertionError
    with pytest.raises(expected_error) as caught:
        subject.test_full_manifest_owned_http_and_relaunch(tmp_path, monkeypatch)
    if failure_phase == "launch":
        assert caught.value is refusal
    assert calls == [("view", "launch0"),
                     ("stream", "launch0", {"max_events": 256, "max_bytes": 65536})]
    assert close_observations == [True], "capture did not reach the uploader path before owner cleanup"
    assert not (tmp_path / "owner-diagnostic.json").exists()
    raw = artifact.read_text(encoding="utf-8")
    diagnostic = json.loads(raw)
    assert sensitive not in raw and opaque not in raw
    assert "cleanup detail" in raw
    assert diagnostic["iteration"] == 0 and diagnostic["launch_job_id"] == "launch0"
    assert diagnostic["phase"] == ("launch" if failure_phase == "launch" else "assert-clean-stop")
    assert diagnostic["stop_receipt"] == (None if failure_phase == "launch" else {"state": "STOPPED_UNCLEAN"})
    assert diagnostic["injected_completion_reached"] is (failure_phase == "stop")
    assert diagnostic["original_exception"]["type"] == expected_error.__name__
    assert diagnostic["original_exception"]["message"] == str(caught.value)
    assert diagnostic["job_record"]["result"] == {"exit_code": 7}
    assert diagnostic["terminal_evidence"]["components"][0]["state"] == "UNRESOLVED"
    assert diagnostic["retained_output"]["events"][0]["stream"] == "stderr"
    assert diagnostic["retained_output"]["truncated"] is False


@pytest.mark.parametrize("failure_phase", ["launch", "stop"])
def test_failure_capture_survives_pytest_initialization(failure_phase, tmp_path, monkeypatch):
    if os.environ.get("PYTEST_MANAGED_OWNER_CAPTURE_PROBE") == failure_phase:
        artifact = Path(os.environ["PYTEST_MANAGED_OWNER_PROBE_ARTIFACT"])
        assert not artifact.exists()
        _exercise_failure_capture(failure_phase, tmp_path, monkeypatch, artifact)
        return

    checkout = Path(__file__).resolve().parents[1]
    runner_temp = tmp_path / "runner-temp"
    variable, directory, artifact = _workflow_route(runner_temp)
    obsolete_directory = runner_temp / "obsolete-diagnostic-route"
    report = tmp_path / "child-pytest.xml"
    env = dict(os.environ)
    env.update({"SONDER_OWNER_DIAGNOSTIC_DIR": str(obsolete_directory),
                variable: str(directory),
                "PYTEST_MANAGED_OWNER_CAPTURE_PROBE": failure_phase,
                "PYTEST_MANAGED_OWNER_PROBE_ARTIFACT": str(artifact)})
    node = f"tests/test_managed_owner_diagnostic_capture.py::test_failure_capture_survives_pytest_initialization[{failure_phase}]"
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist",
               "--junitxml=" + str(report), "--basetemp=" + str(tmp_path / "child-pytest"), node]
    child = subprocess.run(command, cwd=checkout, env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=45)
    assert child.returncode == 0, child.stdout + child.stderr
    cases = list(ET.parse(report).getroot().iter("testcase"))
    assert len(cases) == 1 and not list(cases[0])
    assert artifact.is_file()
    assert not (obsolete_directory / "owner-diagnostic.json").exists()
