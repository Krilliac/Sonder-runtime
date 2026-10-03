"""Privacy and failure-retention contracts for the delegated-stall evidence."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
import textwrap
import threading
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

from tests import fleet_stall_diagnostics as diagnostics


TARGET_NODE = (
    "tests/test_adaptive_concurrency.py::"
    "test_run_delegated_stall_is_uncertain_and_retains_child_capacity"
)


def _read_artifact(artifact):
    assert isinstance(artifact, Path)
    return json.loads(artifact.read_text(encoding="utf-8"))


def _assert_private_metadata(payload, sentinel, private_path):
    serialized = json.dumps(payload, sort_keys=True)
    assert sentinel not in serialized
    assert str(private_path).replace("\\", "/") not in serialized.replace("\\\\", "/")
    for thread in payload["threads"]:
        for frame in thread["frames"]:
            # An allowlist prevents a future convenience field exposing locals,
            # arguments, source lines, or exception contents.
            assert set(frame) == {"file", "function", "line"}
            assert frame["file"].startswith(("repo/", "external/"))
            assert type(frame["line"]) is int


def test_blocked_coordinator_stack_is_useful_without_private_values(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    sentinel = "FLEET_STALL_LOCAL_SECRET_4a5fbf"
    private_directory = tmp_path / sentinel
    external_file = private_directory / "external_coordinator.py"
    namespace = {
        "entered": entered, "release": release,
        "private_directory": str(private_directory), "sentinel": sentinel,
    }
    # Compile a real blocked function with an external absolute source path.
    # Both sensitive values really are in its frame; neither belongs in JSON.
    exec(compile(
        "def held_coordinator():\n"
        "    secret_local = sentinel\n"
        "    private_local = private_directory\n"
        "    entered.set()\n"
        "    release.wait(5)\n",
        str(external_file), "exec",
    ), namespace)
    thread = threading.Thread(target=namespace["held_coordinator"])
    thread.start()
    try:
        assert entered.wait(5)
        payload = _read_artifact(diagnostics.write_stall_diagnostic(
            tmp_path / "evidence", phase="coordinator-join",
            coordinator_id=thread.ident,
            checkpoints={"worker_entered_ns": 1, "unapproved_value": sentinel},
        ))
        assert payload["phase"] == "coordinator-join"
        assert payload["coordinator_id"] == thread.ident
        assert payload["coordinator_present"] is True
        coordinator = next(
            item for item in payload["threads"] if item["thread_id"] == thread.ident
        )
        assert coordinator["is_coordinator"] is True
        assert any(
            frame["function"] == "held_coordinator"
            and frame["file"] == "external/external_coordinator.py"
            for frame in coordinator["frames"]
        )
        assert payload["checkpoints"]["worker_entered_ns"] == 1
        assert "unapproved_value" not in payload["checkpoints"]
        _assert_private_metadata(payload, sentinel, private_directory)
    finally:
        release.set()
        thread.join(5)
        assert not thread.is_alive()


def test_large_thread_snapshot_keeps_coordinator_with_bounded_frames(monkeypatch, tmp_path):
    sentinel = "FLEET_STALL_FAKE_LOCAL_SECRET_b58ced"
    private_directory = tmp_path / sentinel
    head = None
    for number in reversed(range(80)):
        head = SimpleNamespace(
            f_code=SimpleNamespace(
                co_filename=str(private_directory / "external_frames.py"),
                co_name=f"fake_frame_{number}",
            ),
            f_lineno=number + 1, f_back=head,
            f_locals={"must_never_be_recorded": sentinel},
        )
    frames = {identifier: head for identifier in range(1, 50)}
    coordinator_id = 10000  # Would fall outside an ordinary first-32 slice.
    frames[coordinator_id] = head
    monkeypatch.setattr(sys, "_current_frames", lambda: frames)

    payload = _read_artifact(diagnostics.write_stall_diagnostic(
        tmp_path / "evidence", phase="coordinator-join",
        coordinator_id=coordinator_id, checkpoints={},
    ))

    assert payload["thread_count"] == 50
    assert payload["threads_dropped"] == 18
    assert len(payload["threads"]) == 32
    assert payload["limits"] == {"threads": 32, "frames_per_thread": 64}
    coordinator = next(
        item for item in payload["threads"] if item["thread_id"] == coordinator_id
    )
    assert coordinator["is_coordinator"] is True
    assert len(coordinator["frames"]) == 64
    assert coordinator["frames_truncated"] is True
    assert coordinator["frames"][0]["function"] == "fake_frame_0"
    assert coordinator["frames"][-1]["function"] == "fake_frame_63"
    assert all(len(item["frames"]) <= 64 for item in payload["threads"])
    _assert_private_metadata(payload, sentinel, private_directory)


def test_artifact_write_failure_preserves_original_assertion_identity(tmp_path):
    blocked_directory = tmp_path / "a-file-is-not-an-artifact-directory"
    blocked_directory.write_text("occupied", encoding="utf-8")
    original = AssertionError("original stall assertion")

    with pytest.raises(AssertionError) as preserved:
        try:
            raise original
        except AssertionError:
            assert diagnostics.write_stall_diagnostic(
                blocked_directory, phase="coordinator-join",
                coordinator_id=threading.get_ident(), checkpoints={},
            ) is None
            raise

    assert preserved.value is original
    assert blocked_directory.read_text(encoding="utf-8") == "occupied"


_GATED_CHILD = textwrap.dedent(r'''
    import json
    from pathlib import Path
    import sys
    import threading

    import pytest

    coordinator_receipt = Path(sys.argv[1])
    evidence_directory = sys.argv[2]
    junit_path = sys.argv[3]
    target_node = sys.argv[4]

    class MasterFinishGate:
        @pytest.fixture(autouse=True)
        def hold_master_finish(self, monkeypatch):
            # Import after the repository conftest pins its disposable ledger.
            import master_orchestrator

            original_finish = master_orchestrator._finish
            gate = threading.Event()
            held = {}

            def gated_finish(agent_id, *args, **kwargs):
                if (master_orchestrator._AGENTS.get(agent_id) or {}).get("role") == "master":
                    private_local = "FLEET_STALL_GATED_SECRET_1451cb"
                    held["coordinator"] = threading.current_thread()
                    coordinator_receipt.write_text(
                        json.dumps({"coordinator_id": threading.get_ident()}),
                        encoding="utf-8",
                    )
                    # Real dispatch reaches its one-second stall first. Hold
                    # finishing until pytest tears down the original test;
                    # the process timeout contains lost teardown, not a timed
                    # release racing the assertion whose capture we verify.
                    gate.wait()
                return original_finish(agent_id, *args, **kwargs)

            monkeypatch.setattr(master_orchestrator, "_finish", gated_finish)
            try:
                yield
            finally:
                gate.set()
                coordinator = held.get("coordinator")
                if coordinator is not None:
                    coordinator.join(5)
                    assert not coordinator.is_alive(), "gated coordinator cleanup did not finish"

    raise SystemExit(pytest.main([
        "-q", "--color=no", "-p", "no:cacheprovider", "-p", "no:xdist",
        "-o", "addopts=", "-p", "tests.fleet_stall_diagnostics",
        "--fleet-stall-evidence-dir=" + evidence_directory,
        "--junitxml=" + junit_path, target_node,
    ], plugins=[MasterFinishGate()]))
''')


def test_original_stall_failure_retains_coordinator_stack_before_cleanup(tmp_path):
    """An injected blocker proves retention, not the historical flake's cause."""
    repo_root = Path(__file__).resolve().parent.parent
    evidence_directory = tmp_path / "child-evidence"
    coordinator_receipt = tmp_path / "coordinator-receipt.json"
    junit_path = tmp_path / "child-result.xml"
    completed = subprocess.run(
        [sys.executable, "-c", _GATED_CHILD, str(coordinator_receipt),
         str(evidence_directory), str(junit_path), TARGET_NODE],
        cwd=repo_root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", timeout=30, check=False,
    )
    (tmp_path / "child-pytest.log").write_text(completed.stdout, encoding="utf-8")

    assert completed.returncode == 1, completed.stdout
    cases = list(ET.parse(junit_path).getroot().iter("testcase"))
    assert len(cases) == 1, completed.stdout
    case = cases[0]
    assert case.get("name") == TARGET_NODE.rsplit("::", 1)[1]
    assert case.findall("error") == []
    assert case.findall("skipped") == []
    failures = case.findall("failure")
    assert len(failures) == 1
    failure_text = failures[0].text or ""
    assert "assert not thread.is_alive()" in failure_text, completed.stdout
    assert "test_adaptive_concurrency.py:" in failure_text, completed.stdout
    assert "AssertionError" in failure_text, completed.stdout
    assert re.search(
        r"(?m)^1 failed(?:, \d+ warnings?)? in \d+(?:\.\d+)?s(?: \([^\n]+\))?$",
        completed.stdout,
    ), completed.stdout

    # This is intentionally the RED point against the original, unmodified
    # target: its assertion still fails, but it has no failure-retention call.
    artifacts = list(evidence_directory.glob("*.json"))
    assert len(artifacts) == 1, "MISSING_OR_DUPLICATE_STALL_ARTIFACT"
    payload = _read_artifact(artifacts[0])
    expected_coordinator = json.loads(
        coordinator_receipt.read_text(encoding="utf-8")
    )["coordinator_id"]
    assert payload["phase"] == "coordinator-join"
    assert payload["target_node"] == TARGET_NODE
    assert payload["coordinator_id"] == expected_coordinator
    assert payload["coordinator_present"] is True
    coordinator = next(
        item for item in payload["threads"]
        if item["thread_id"] == expected_coordinator
    )
    assert coordinator["is_coordinator"] is True
    assert any(frame["function"] == "gated_finish" for frame in coordinator["frames"])
    _assert_private_metadata(
        payload, "FLEET_STALL_GATED_SECRET_1451cb", tmp_path,
    )
