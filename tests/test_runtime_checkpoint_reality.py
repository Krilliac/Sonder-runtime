"""Sealed observations are retained, but stale verifier inputs cannot be restored."""
import subprocess

from sonder_runtime.adapters.persistence.sqlite.runtime_checkpoints import SQLiteRuntimeCheckpointRepository
from sonder_runtime.application.ports.runtime_checkpoints import RuntimeCheckpoint, RestoreStatus
from sonder_runtime.adapters.workspace_reality import GitWorkspaceReality


class Reality:
    def capture(self, root, **kwargs):
        return {"version": 1, "head": "a", "root": str(root), "complete": True}

    def revalidate(self, root, snapshot, owned_paths=(), **kwargs):
        assert snapshot["head"] == "a"
        return {"status": "changed", "requires_reinspection": True,
                "requires_replan": True, "files": [{"path": "src/a.py"}]}


def test_checkpoint_snapshot_is_sealed_and_restore_invalidates_verification(tmp_path):
    store = SQLiteRuntimeCheckpointRepository(
        tmp_path / "cp.db", seal_key=b"x" * 32,
        workspace_reality=Reality(), workspace_roots=(tmp_path,),
    )
    saved = store.save(RuntimeCheckpoint("run", 0, {}, verification={"passed": True}),
                       expected_generation=-1)
    assert saved.repository_state["workspace_snapshots"][str(tmp_path)]["head"] == "a"
    restored = store.restore("run")
    assert restored.status is RestoreStatus.RESTORED
    assert restored.resume_delta[0]["requires_replan"]
    assert not restored.checkpoint.verification
    # Archive/seal validation still restores the original facts without a workspace binding.
    original = SQLiteRuntimeCheckpointRepository(tmp_path / "cp.db", seal_key=b"x" * 32).restore("run")
    assert original.checkpoint.verification["passed"] is True


def test_unbound_observer_keeps_legacy_shape(tmp_path):
    store = SQLiteRuntimeCheckpointRepository(tmp_path / "cp.db", seal_key=b"x" * 32)
    cp = RuntimeCheckpoint("run", 0, {}, verification={"passed": True})
    assert store.save(cp, expected_generation=-1) == cp
    restored = store.restore("run")
    assert restored.checkpoint == cp
    assert restored.resume_delta == ()


def test_real_git_checkpoint_identical_then_changed_and_legacy(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    (root / "source.py").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "-m", "base"], check=True, capture_output=True)
    plain = SQLiteRuntimeCheckpointRepository(tmp_path / "cp.db", seal_key=b"x" * 32)
    store = SQLiteRuntimeCheckpointRepository(tmp_path / "cp.db", seal_key=b"x" * 32,
                                             workspace_reality=GitWorkspaceReality(), workspace_roots=(root,))
    store.save(RuntimeCheckpoint("new", 0, {}, verification={"passed": True}), expected_generation=-1)
    assert store.restore("new").resume_delta == ()
    (root / "source.py").write_text("changed\n", encoding="utf-8")
    result = store.restore("new")
    assert result.resume_delta[0]["status"] == "changed"
    assert not result.checkpoint.verification
    plain.save(RuntimeCheckpoint("old", 0, {}, verification={"passed": True}), expected_generation=-1)
    legacy = store.restore("old")
    assert legacy.status is RestoreStatus.RESTORED
    assert legacy.resume_delta[0]["requires_reinspection"]
    assert not legacy.checkpoint.verification


def test_old_non_git_checkpoint_has_no_barrier(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    subprocess.run(["git", "init", "--bare", str(root)], check=True, capture_output=True)
    store = SQLiteRuntimeCheckpointRepository(tmp_path / "cp.db", seal_key=b"x" * 32,
                                             workspace_reality=GitWorkspaceReality(), workspace_roots=(root,))
    cp = RuntimeCheckpoint("plain", 0, {}, verification={"passed": True})
    assert store.save(cp, expected_generation=-1) == cp
    assert store.restore("plain").resume_delta == ()
