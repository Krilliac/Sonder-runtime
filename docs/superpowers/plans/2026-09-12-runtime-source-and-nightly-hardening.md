
# Runtime Source and Nightly Hardening Implementation Plan

> For agentic workers: use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Make source-checkout profile paths and nightly self-improvement paths deterministic, remove the merged-branch CI recovery job, and validate the result against the repository CI contract.

**Architecture:** Use origin/main as the source of truth in the isolated worktree. The packaged profile module will resolve the repository checkout explicitly. The nightly driver will normalize its two workspace-owned mutable-file overrides before importing runtime modules. The obsolete recovery workflow and helper will be removed without touching the live deployment copy or existing worktrees.

**Tech Stack:** Python 3.12, pytest, Git worktrees, GitHub Actions YAML, PowerShell deployment environment.

**Spec:** docs/superpowers/specs/2026-09-12-runtime-source-and-nightly-hardening-design.md

## Global Constraints

- Base the branch on origin/main at cd40b944bb029e20ae0c480a5a4ed9c9d9c6a190.
- Preserve D:/sonder-wt/tier-provider-dispatch, every existing worktree, the dirty D:/sonder-sol-wt/web-app-surface tree, and all state databases and locks.
- Do not enable cloud or remote inference and do not cut over the live MCP deployment.
- Write and run a failing real pytest test before each production behavior change.
- Stage targeted paths only and use DCO-signed commits.

## File Map

- Modify sonder_runtime/platform/system_profile.py for checkout-root ownership.
- Modify scripts/nightly_self_improve.py for workspace-local environment binding.
- Modify conftest.py to clear inherited SONDER_* and OLLAMA_* deployment settings before applying test defaults.
- Modify tests/test_system_profile_ownership.py and create tests/test_nightly_self_improve.py.
- Create tests/test_ci_retired_workflow.py.
- Create tests/test_test_environment.py.
- Delete .github/workflows/restore-reloadable-mcp.yml and scripts/apply_loop_docstring_sync.py only.

### Task 1: Correct packaged system-profile ownership

**Files:**
- Modify: sonder_runtime/platform/system_profile.py
- Modify: tests/test_system_profile_ownership.py
- Modify: conftest.py

**Interfaces:**
- workspace_root() returns the absolute checkout directory containing server.py and system_profile.md.
- Existing default_path(), _resolve_path(), read_profile(), and root-module identity behavior remain available.

- [ ] Step 1: Add the failing tests.

Append to tests/test_system_profile_ownership.py:

~~~python
from pathlib import Path


def test_profile_workspace_root_is_the_repository_checkout():
    profile = importlib.import_module("sonder_runtime.platform.system_profile")
    expected = Path(__file__).resolve().parents[1]

    assert Path(profile.workspace_root()) == expected
    assert (expected / "server.py").is_file()
    assert (expected / "system_profile.md").is_file()


def test_profile_accepts_an_explicit_checkout_level_override(monkeypatch):
    profile = importlib.import_module("sonder_runtime.platform.system_profile")
    expected = Path(__file__).resolve().parents[1] / "system_profile.md"
    monkeypatch.setenv("SONDER_SYSTEM_PROFILE", str(expected))

    assert Path(profile._resolve_path()) == expected
~~~

The first test fails because the current function returns the package directory. The second fails because the checkout-level override is rejected as outside that directory.

- [ ] Step 2: Run the red test.

~~~powershell
D:/sonder-runtime/venv/Scripts/python.exe -m pytest -q tests/test_system_profile_ownership.py
~~~

Expected: the new assertions fail with the current package-directory root or containment behavior, and collection completes normally.

- [ ] Step 3: Make the test harness independent of deployment environment.

Before the existing os.environ.update call in conftest.py, remove every inherited variable whose name starts with SONDER_ or OLLAMA_. This prevents deployment model selection, remote-worker consent, and absolute workspace paths from changing CI-equivalent tests. The existing os.environ.update call then restores only its documented safe defaults.

- [ ] Step 4: Implement the minimal root fix.

Import Path in sonder_runtime/platform/system_profile.py and use:

~~~python
def workspace_root():
    return str(Path(__file__).resolve().parents[2])
~~~

- [ ] Step 5: Run the focused profile suite.

~~~powershell
D:/sonder-runtime/venv/Scripts/python.exe -m pytest -q tests/test_system_profile.py tests/test_system_profile_ownership.py
~~~

Expected: every selected test passes.

- [ ] Step 6: Commit.

~~~powershell
git add -- conftest.py sonder_runtime/platform/system_profile.py tests/test_system_profile_ownership.py
git commit -s -m "fix: bind system profile paths to checkout root"
~~~

### Task 2: Isolate nightly workspace configuration

**Files:**
- Modify: scripts/nightly_self_improve.py
- Create: tests/test_nightly_self_improve.py

**Interfaces:**
- _bind_workspace_config_paths(root: Path | None = None) returns a tuple of variable names that were rehomed.
- The helper sets SONDER_EMOTION_VECTORS and SONDER_SYSTEM_PROFILE to verified paths inside the selected checkout.

- [ ] Step 1: Create failing behavior tests.

Create tests/test_nightly_self_improve.py:

~~~python
from scripts import nightly_self_improve


def test_nightly_rehomes_absolute_paths_from_another_checkout(tmp_path, monkeypatch):
    root = (tmp_path / "runtime").resolve()
    root.mkdir()
    other = (tmp_path / "other").resolve()
    other.mkdir()
    monkeypatch.setenv("SONDER_EMOTION_VECTORS", str(other / "emotion_vectors.json"))
    monkeypatch.setenv("SONDER_SYSTEM_PROFILE", str(other / "system_profile.md"))

    rebound = nightly_self_improve._bind_workspace_config_paths(root)

    assert rebound == ("SONDER_EMOTION_VECTORS", "SONDER_SYSTEM_PROFILE")
    assert nightly_self_improve.os.environ["SONDER_EMOTION_VECTORS"] == str(root / "emotion_vectors.json")
    assert nightly_self_improve.os.environ["SONDER_SYSTEM_PROFILE"] == str(root / "system_profile.md")


def test_nightly_preserves_an_in_checkout_override(tmp_path, monkeypatch):
    root = (tmp_path / "runtime").resolve()
    root.mkdir()
    custom = root / "custom-profile.md"
    monkeypatch.setenv("SONDER_EMOTION_VECTORS", "emotion_vectors.json")
    monkeypatch.setenv("SONDER_SYSTEM_PROFILE", str(custom))

    rebound = nightly_self_improve._bind_workspace_config_paths(root)

    assert rebound == ()
    assert nightly_self_improve.os.environ["SONDER_EMOTION_VECTORS"] == str(root / "emotion_vectors.json")
    assert nightly_self_improve.os.environ["SONDER_SYSTEM_PROFILE"] == str(custom)
~~~

The first test fails because the helper does not exist; the second defines preservation independently of the implementation.

- [ ] Step 2: Run the red test.

~~~powershell
D:/sonder-runtime/venv/Scripts/python.exe -m pytest -q tests/test_nightly_self_improve.py
~~~

Expected: collection fails only because the helper is undefined.

- [ ] Step 3: Implement the helper.

Add a two-entry workspace configuration table and a helper near _REPO_ROOT. Resolve relative values below root, resolve path components before containment checking, catch OSError, RuntimeError, and ValueError as escapes, fall back to root/default_name, set both variables, and return only variables that used the fallback.

- [ ] Step 4: Call it before runtime imports.

Immediately after the nightly-start log line, call _bind_workspace_config_paths(_REPO_ROOT). If the returned tuple is non-empty, log only the variable names. Keep the call before importing sonder_paths, server, lesson_pruner, or memory_store.

- [ ] Step 5: Run the nightly and profile regressions.

~~~powershell
D:/sonder-runtime/venv/Scripts/python.exe -m pytest -q tests/test_nightly_self_improve.py tests/test_nightly_selfmod_model_selection.py tests/test_system_profile.py tests/test_system_profile_ownership.py tests/test_logging_platform_seam.py
~~~

Expected: all selected tests pass without model or network access.

- [ ] Step 6: Commit.

~~~powershell
git add -- scripts/nightly_self_improve.py tests/test_nightly_self_improve.py
git commit -s -m "fix: isolate nightly workspace configuration"
~~~

### Task 3: Retire the deleted-branch CI recovery job

**Files:**
- Create: tests/test_ci_retired_workflow.py
- Delete: .github/workflows/restore-reloadable-mcp.yml
- Delete: scripts/apply_loop_docstring_sync.py

**Interfaces:**
- Keep .github/workflows/ci.yml, .github/workflows/build-apps.yml, reloadable_mcp.py, and every Spanda module and test unchanged.

- [ ] Step 1: Add the failing retirement test.

Create tests/test_ci_retired_workflow.py:

~~~python
from pathlib import Path


def test_one_shot_reloadable_recovery_job_is_retired():
    root = Path(__file__).resolve().parents[1]

    assert not (root / ".github" / "workflows" / "restore-reloadable-mcp.yml").exists()
    assert not (root / "scripts" / "apply_loop_docstring_sync.py").exists()
    assert (root / "reloadable_mcp.py").is_file()
~~~

- [ ] Step 2: Run the red test.

~~~powershell
D:/sonder-runtime/venv/Scripts/python.exe -m pytest -q tests/test_ci_retired_workflow.py
~~~

Expected: an assertion failure names a still-present obsolete file.

- [ ] Step 3: Delete only the two obsolete files with apply_patch.

Do not delete reloadable_mcp.py, the Spanda files, or any main CI workflow.

- [ ] Step 4: Run the retirement test and diff checks.

~~~powershell
D:/sonder-runtime/venv/Scripts/python.exe -m pytest -q tests/test_ci_retired_workflow.py
git diff --check
git status --short
~~~

Expected: the test passes and only the planned test plus two deletions are dirty.

- [ ] Step 5: Commit.

~~~powershell
git add -- tests/test_ci_retired_workflow.py .github/workflows/restore-reloadable-mcp.yml scripts/apply_loop_docstring_sync.py
git commit -s -m "ci: retire merged-branch reload recovery job"
~~~

### Task 4: Verify, publish, and report

**Files:**
- Inspect all changed paths from Tasks 1–3.
- Inspect CONTRIBUTING.md and the current CI workflow files.

- [ ] Step 1: Run focused regressions.

~~~powershell
    D:/sonder-runtime/venv/Scripts/python.exe -m pytest -q tests/test_system_profile.py tests/test_system_profile_ownership.py tests/test_nightly_self_improve.py tests/test_nightly_selfmod_model_selection.py tests/test_ci_retired_workflow.py tests/test_test_environment.py tests/test_logging_platform_seam.py
~~~

- [ ] Step 2: Run every static CI gate.

~~~powershell
D:/sonder-runtime/venv/Scripts/python.exe scripts/check_architecture.py
D:/sonder-runtime/venv/Scripts/python.exe scripts/check_requirement_evidence.py
D:/sonder-runtime/venv/Scripts/python.exe scripts/check_error_signals.py
D:/sonder-runtime/venv/Scripts/python.exe scripts/check_history_privacy.py --json
~~~

Each command must reach exit code zero with complete output.

- [ ] Step 3: Run the full CI-equivalent pytest suite.

~~~powershell
D:/sonder-runtime/venv/Scripts/python.exe -m pytest -q -n auto --dist load --durations=25
~~~

Use Codex Process Jobs with --goal-mode if this finite suite exceeds the short-command budget. Inspect the terminal result; do not treat a timeout or empty output as success.

- [ ] Step 4: Audit ancestry and scope.

~~~powershell
git status --short --branch
git diff --check origin/main...HEAD
git diff --name-status origin/main...HEAD
git log --oneline --decorate origin/main..HEAD
~~~

Confirm that the active CI workflows and reloadable_mcp.py are unchanged and that no deployment copy or existing worktree appears in the diff.

- [ ] Step 5: Write the PR body to work/nightly-hardening-pr-body.md.

Use the exact observed outputs from Steps 1–3 and include these fixed facts: origin/main was cd40b944 at branch creation; the nightly failure was caused by absolute SONDER_EMOTION_VECTORS and SONDER_SYSTEM_PROFILE values from another checkout; the restore workflow checked out deleted feat/spanda-rsc on a main push; the live D:/sonder-wt/tier-provider-dispatch deployment was not changed; and the existing worktrees were preserved.

- [ ] Step 6: Push the branch and open a PR.

~~~powershell
git push -u origin codex/nightly-workspace-hardening-20260912
gh pr create --repo Krilliac/Sonder-runtime --base main --head codex/nightly-workspace-hardening-20260912 --title "fix: isolate nightly workspace paths and retire stale CI recovery" --body-file work/nightly-hardening-pr-body.md
~~~

The PR body must include the original nightly path failure, the deleted-branch CI failure, exact commands and results, and the explicit non-goal that D:/sonder-wt/tier-provider-dispatch was not cut over. Do not claim a remote merge until GitHub checks are terminal and successful.
