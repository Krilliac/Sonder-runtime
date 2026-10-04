"""Workspace observations attached to the existing Autopilot durability store."""
from pathlib import Path

from .persistence import autopilot_store
from .workspace_reality import GitWorkspaceReality
from ..application.execution.resume_reality import ResumeBarrier


class AutopilotWorkspaceReality:
    """No Git work for unscoped runs; observations never authorize task replay."""

    def __init__(self, run, owner_id):
        self.run_id, self.owner_id = run["id"], owner_id
        self.port = GitWorkspaceReality()
        self.state = autopilot_store.load_workspace_reality(self.run_id, owner_id) or {}
        self.root = self.state.get("root")
        if not self.root and run.get("project"):
            path = Path(run["project"]).expanduser().resolve()
            if path.is_dir():
                self.root = str(path)

    def _save(self):
        if not autopilot_store.save_workspace_reality(self.run_id, self.owner_id, self.state):
            raise PermissionError("Autopilot ownership lost during workspace revalidation")

    def resume(self, run):
        if not self.root:
            return None
        if not run.get("plan"):
            self.settled()
            return None
        delta = self.port.revalidate(self.root, self.state.get("snapshot"))
        pending = self.state.get("pending")
        if pending and delta is None:
            delta = pending
        if delta is not None:
            # Persist only the bounded notice. The previous observation already
            # lives in snapshot; duplicating both snapshots can overflow a row.
            self.state.update(root=self.root, pending=ResumeBarrier(delta).delta)
            self._save()
        return delta

    def settled(self):
        if not self.root or self.state.get("pending"):
            return
        snapshot = self.port.capture(self.root)
        if snapshot is not None:
            self.state = {"root": self.root, "snapshot": snapshot}
            self._save()
        elif not self.state:
            # An established non-Git project needs no further checkpoint probes
            # during this invocation. A later invocation detects it afresh.
            self.root = None
        else:
            # Preserve the former Git identity if a repository disappears, but
            # durably discharge a completed inspection/replan acknowledgement.
            self._save()

    def acknowledge(self):
        self.state.pop("pending", None)
        self.settled()
