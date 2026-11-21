"""cloud_train.sh must never publish the private-data adapter to a public HF repo.

The push step is a Python heredoc inside the shell script. These tests pull that
heredoc out and run it against a fake ``huggingface_hub`` so the real visibility
logic is exercised without network access or credentials.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_FAKE_HUB = textwrap.dedent(
    """
    import json
    import os
    from types import SimpleNamespace


    def _load():
        with open(os.environ["FAKE_HF_STATE"], encoding="utf-8") as fh:
            return json.load(fh)


    def _save(state):
        with open(os.environ["FAKE_HF_STATE"], "w", encoding="utf-8") as fh:
            json.dump(state, fh)


    class HfApi:
        def __init__(self, token=None):
            self.token = token

        def create_repo(self, repo_id, *, private=None, exist_ok=False, **kwargs):
            state = _load()
            state["calls"].append(
                {"op": "create_repo", "repo_id": repo_id, "private": private,
                 "exist_ok": exist_ok}
            )
            if state["repo"] is None:
                # The Hub's default visibility for a new repo is public.
                state["repo"] = {"private": bool(private)}
            _save(state)

        def repo_info(self, repo_id, **kwargs):
            state = _load()
            state["calls"].append({"op": "repo_info", "repo_id": repo_id})
            _save(state)
            return SimpleNamespace(private=state["repo"]["private"])

        def upload_folder(self, *, folder_path, repo_id, **kwargs):
            state = _load()
            state["calls"].append(
                {"op": "upload_folder", "repo_id": repo_id, "folder_path": folder_path}
            )
            _save(state)
    """
)


def _push_snippet() -> str:
    script = (ROOT / "cloud_train.sh").read_text(encoding="utf-8")
    tail = script[script.index("pushing adapter to HF Hub") :]
    match = re.search(r"python - <<'?PY'?\n(.*?)\nPY\n", tail, re.DOTALL)
    assert match, "HF push heredoc not found in cloud_train.sh"
    return match.group(1) + "\n"


def _run_push(tmp_path: Path, existing, allow_public: str | None = None):
    hub_dir = tmp_path / "fakehub"
    (hub_dir / "huggingface_hub").mkdir(parents=True)
    (hub_dir / "huggingface_hub" / "__init__.py").write_text(_FAKE_HUB, encoding="utf-8")
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"repo": existing, "calls": []}), encoding="utf-8")
    snippet = tmp_path / "push.py"
    snippet.write_text(_push_snippet(), encoding="utf-8")
    adapter_dir = tmp_path / "adapter"
    adapter_dir.mkdir()

    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"SONDER_HF_ALLOW_PUBLIC", "PYTHONPATH"}
    }
    env.update(
        {
            "PYTHONPATH": str(hub_dir),
            "FAKE_HF_STATE": str(state_path),
            "HF_TOKEN": "test-token",
            "HF_REPO": "example-user/example-adapter",
            "SONDER_CLOUD_ADAPTER_DIR": str(adapter_dir),
        }
    )
    if allow_public is not None:
        env["SONDER_HF_ALLOW_PUBLIC"] = allow_public
    proc = subprocess.run(
        [sys.executable, str(snippet)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    calls = json.loads(state_path.read_text(encoding="utf-8"))["calls"]
    return proc, calls


def _ops(calls):
    return [call["op"] for call in calls]


def test_push_heredoc_is_not_shell_expanded():
    script = (ROOT / "cloud_train.sh").read_text(encoding="utf-8")
    tail = script[script.index("pushing adapter to HF Hub") :]
    assert "python - <<'PY'\n" in tail


def test_new_repo_is_created_private_before_upload(tmp_path):
    proc, calls = _run_push(tmp_path, existing=None)
    assert proc.returncode == 0, proc.stderr
    create = [call for call in calls if call["op"] == "create_repo"]
    assert len(create) == 1
    assert create[0]["private"] is True
    assert _ops(calls)[-1] == "upload_folder"


def test_existing_public_repo_is_refused_without_opt_in(tmp_path):
    proc, calls = _run_push(tmp_path, existing={"private": False})
    assert proc.returncode != 0
    assert "upload_folder" not in _ops(calls)
    assert "SONDER_HF_ALLOW_PUBLIC" in proc.stderr


@pytest.mark.parametrize("value", ["", "0", "true", "yes"])
def test_only_exact_opt_in_value_allows_public(tmp_path, value):
    proc, calls = _run_push(tmp_path, existing={"private": False}, allow_public=value)
    assert proc.returncode != 0
    assert "upload_folder" not in _ops(calls)


def test_existing_public_repo_uploads_with_explicit_opt_in(tmp_path):
    proc, calls = _run_push(tmp_path, existing={"private": False}, allow_public="1")
    assert proc.returncode == 0, proc.stderr
    assert _ops(calls)[-1] == "upload_folder"


def test_existing_private_repo_uploads(tmp_path):
    proc, calls = _run_push(tmp_path, existing={"private": True})
    assert proc.returncode == 0, proc.stderr
    assert _ops(calls)[-1] == "upload_folder"
