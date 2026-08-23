---
name: sonder-build-and-env
description: >-
  Recreate the Sonder Runtime working environment from a bare checkout and run its
  verification gates. TRIGGER when the user says "set up the environment",
  "create the venv", "install requirements", "run the tests", "compile check",
  "pytest fails to collect", "fresh worktree setup", or "which requirements file".
  DO NOT TRIGGER for starting, serving, or operating the runtime itself
  (serve/doctor/preflight as operations) — that is sonder-run-and-operate; gate
  policy and merge rules live in sonder-change-control.
---

# Sonder Runtime: environment setup and verification gates

Sonder Runtime is pure Python 3.12. There is deliberately **no `pyproject.toml`,
no `setup.cfg`, and no installed package** — the repo runs from source via
`sys.path`, and tests depend on being launched from the repo root. Everything
below was verified against the real files at commit `99162cf9`.

**When NOT to use this skill.** Starting, serving, draining, or diagnosing
the runtime as an *operation* (serve/doctor/preflight/backups) is
`sonder-run-and-operate`. Which gates may block a merge, commit conventions,
and ratchet policy are `sonder-change-control`. This skill only gets you from
bare checkout to green local gates.

## Fast path: zero to green tests

Fresh worktrees ship **no venv** — `venv/` is gitignored. Create one first.

Windows (PowerShell or cmd), from the repo root:

```powershell
python -m venv venv
.\venv\Scripts\pip.exe install -r requirements-dev.txt
.\venv\Scripts\python.exe -m pytest -q
```

POSIX (Linux/macOS/WSL):

```bash
python3 -m venv venv
venv/bin/pip install -r requirements-dev.txt
venv/bin/python -m pytest -q
```

That is the whole environment for development and testing. The dependency
surface is intentionally tiny: `requirements-dev.txt` is
`requirements-runtime.txt` plus `pytest` and `pytest-xdist` (three lines,
read it if in doubt).

Sanity checks after install (both are cheap and read-only):

```powershell
.\venv\Scripts\python.exe -c "from mcp.server.mcpserver import MCPServer; from mcp.server.mcpserver.tools import ToolManager"
.\venv\Scripts\python.exe -m compileall -q sonder_runtime tests
```

The first is the exact MCP compatibility probe CI runs; the second is the
repo-wide required compile gate (cited across the evidence docs in
`docs/architecture/evidence/`).

## The four requirements files

| File | Contents | When to install |
|---|---|---|
| `requirements-runtime.txt` | `mcp==2.0.0`, `cryptography==50.0.0` — exact pins | Pulled in by `-dev`; alone only for a runtime-only env |
| `requirements-dev.txt` | `-r requirements-runtime.txt` + `pytest` + `pytest-xdist` | **Default.** Any dev or test work |
| `requirements-train.txt` | torch/transformers/accelerate/peft/datasets/bitsandbytes with security-floor ranges | **Never by default.** Only before actually running `qlora_train.py` (see `TRAINING.md`) |
| `requirements-update.txt` | `tuf==7.0.0`, `securesystemslib==1.4.0`, `cryptography==50.0.0` | Optional. Only when a bundle carries TUF metadata (signed-update path) |

Rules the files themselves state — do not fight them:

- **`mcp==2.0.0` is a hard pin, not a floor.** Bootstrap is unattended, so
  runtime deps must be the exact releases CI exercised. MCP 2.x renamed
  `mcp.server.fastmcp` to `mcp.server.mcpserver` and `FastMCP` to `MCPServer`;
  Sonder imports the 2.x names, so **MCP 1.x will not satisfy this repo**. An
  unpinned `pip install mcp` that resolves to another major version breaks
  every server import.
- **Do not re-add `pydantic-settings`** as a runtime dependency without code
  that imports it — the header of `requirements-runtime.txt` forbids it
  explicitly (MCP 2.x's server Settings is a plain BaseModel; nothing in
  Sonder imports `pydantic_settings`).
- **Training deps: CUDA torch comes from PyTorch's own index**, not PyPI's CPU
  wheel. On Windows for CUDA 12.1:
  `pip install torch --index-url https://download.pytorch.org/whl/cu121`
  (check `nvidia-smi` for the driver's supported CUDA version first). If a
  bitsandbytes wheel is unavailable for your Python/CUDA pair, the file's own
  guidance is WSL2, where the official Linux wheels apply.
- **A TUF pin bump requires re-running the adversarial acceptance suite**
  (`tests/production/test_tuf_publisher.py` plus the archive-safety / rollback
  / freeze tests) before shipping, per SPEC-4 §16.

## Venv conventions (three venvs, three purposes)

| Path | Purpose | Gitignored |
|---|---|---|
| `venv/` | Dev/test venv you create at repo root | yes |
| `.runtime/` | The installed runtime's own virtualenv (the runtime updater manages it) | yes — the `.gitignore` comment records that when it moved from `venv/` a single `git add -A` nearly committed 83 MB |
| `venv-train/` | Training venv for `requirements-train.txt` | yes |

**Always invoke the explicit interpreter path** (`venv\Scripts\python.exe` on
Windows, `venv/bin/python` on POSIX). Never rely on `activate` — activation
state does not survive shell boundaries, and agent shells reset between calls.
Scripts honor `SONDER_PYTHON` as an interpreter override (see
`scripts/run-tests.cmd`, `sonder.cmd`, `sonder-runtime.sh`, etc.).

## Running the tests

From the **repo root only**. The root `conftest.py` does
`sys.path.insert(0, repo_root)` at import; launched from anywhere else,
imports break and collection errors look like missing modules.

```powershell
# Full suite, the pre-PR gate (CONTRIBUTING.md):
.\venv\Scripts\python.exe -m pytest -q

# CI's exact form (parallel, with timing report):
.\venv\Scripts\python.exe -m pytest -q -n 2 --dist load --durations=20

# Windows wrapper — resolves the interpreter itself, takes plain pytest args:
scripts\run-tests.cmd
scripts\run-tests.cmd tests\test_foo.py
scripts\run-tests.cmd -q -k pattern
```

`scripts/run-tests.cmd` exit codes: **3** = no interpreter at
`venv\Scripts\python.exe` (venv missing — recreate it), **4** = the
interpreter exists but will not start (the base Python named in
`venv\pyvenv.cfg` is gone — recreate the venv). The script's header documents
why it exists: a quoting failure crossing bash → cmd → the venv redirector
produced `No Python at '"<path>'` (note the stray quote), was misdiagnosed
twice as a missing file and a sandbox denial, and cost an agent lane its
entire verification step — it shipped fixes whose tests never ran. If you see
a stray quote inside a "No Python at" path, it is a quoting bug at a shell
boundary, not a missing interpreter.

For evidence runs, repo convention is a fresh basetemp per run so leftover
state cannot leak between runs:

```powershell
.\venv\Scripts\python.exe -m pytest -q --basetemp .pytest-tmp-run1
```

(`.pytest-tmp*/` is gitignored.)

### What pytest.ini declares — and does not

- `testpaths = tests proposals`. **`proposals` is deliberate**: proposal
  modules ship in the desktop payload, and omitting the directory once made
  their production-facing compatibility tests silently disappear from bare CI
  runs. Do not "simplify" it away.
- Markers: `unit`, `integration`, `network`, `model`. There is **no `slow`
  marker** — do not filter on one.
- `network`- and `model`-marked tests are skipped unless you opt in with
  `--run-network` / `--run-model` (defined in the root `conftest.py`).

### Hermetic-by-default test environment

The root `conftest.py`, at import time (before collection):

- Builds a temp `SONDER_HOME` (`sonder-pytest-*` under the system temp dir)
  and points `SONDER_DB` / `SONDER_FLEET_DB` into it; cleans it up at exit.
- Sets offline sentinels so nothing reaches a real service (port-1 endpoints
  fail fast; cloud/web/embed-cache legs disabled). The full sentinel table is
  owned by `sonder-validation-and-qa` §2; tests that want live services must
  opt in.
- Snapshots and restores `SONDER_AUTH_MODE` / `SONDER_API_KEY` /
  `SONDER_REQUIRE_ACCOUNT` around every test, because production entrypoints
  export resolved config into `os.environ` as a deliberate side effect and
  the leak otherwise poisons the whole session.

Consequence: a plain `pytest -q` is safe on any machine with no Ollama, no
network, and no Sonder state — and it will not touch your real `SONDER_HOME`.

### Live/opt-in tests

The only live test is `tests/live/test_model_gateway_live_smoke.py`. To run it:

```powershell
$env:SONDER_LIVE_MODEL_GATEWAY = "ollama"   # or "openai"
.\venv\Scripts\python.exe -m pytest -q tests\live\test_model_gateway_live_smoke.py
```

A remote (non-local) endpoint additionally requires
`SONDER_LIVE_ALLOW_REMOTE=1`. Without the env var the test self-skips; bare CI
only ever collects a skip.

### Focused selection for a change

```powershell
.\venv\Scripts\python.exe scripts\select_regression_tests.py --since main --format args
```

Derives search terms from the diff itself and selects every test referencing a
changed identifier or module; also reports changed identifiers **no** test
mentions, which is the number that matters. Exit code **2 means the selection
went vacuous** (no identifiers extracted or no tests selected) — that is an
infrastructure failure and must never be read as "nothing to run". Exit 0 with
`--format args` prints paths you can paste straight into pytest.

## The gates that exist — and the ones that do not

| Gate | Command | Status |
|---|---|---|
| Compile | `python -m compileall -q sonder_runtime tests` | Required, repo-wide |
| MCP import probe | `python -c "from mcp.server.mcpserver import MCPServer; from mcp.server.mcpserver.tools import ToolManager"` | Required in CI |
| Test suite | `python -m pytest -q` (CI: `-n 2 --dist load --durations=20`) | Required |
| Architecture ratchets | `python scripts/check_architecture.py`, `check_requirement_evidence.py`, `check_error_signals.py`, `check_history_privacy.py --json` | Required in CI (policy: sonder-change-control) |
| Whitespace | `git diff --check` | Repo convention before committing |
| Runtime import sanity | `python -m sonder_runtime doctor` / `python -m sonder_runtime preflight` | Optional smoke; operating them is sonder-run-and-operate |
| Lint / typecheck | — | **Does not exist. Do not invent one.** |

**There is no ruff or mypy gate.** No ruff/mypy/pyproject/setup.cfg/tox.ini
config exists anywhere in the repo (verified by glob). You will find
`ruff_verifier.py` and `harness_tools.py` wrapping ruff/mypy — those are
**runtime verifiers/tools the product applies to model-generated artifacts and
user projects**, not project lint config. The nightly selfmod worker
(`scripts/nightly_selfmod.py`) explicitly treats ruff as optional because
requiring it made every candidate fail with `No module named ruff` from a bare
checkout; **`py_compile` is the required syntax gate**. Style is prose, not
tooling: match the file you are editing; comments explain *why*
(CONTRIBUTING.md).

## CI reference (`.github/workflows/`)

- `ci.yml`: ubuntu-latest, **Python 3.12 only** (single version), checkout
  with `fetch-depth: 0` because the history-privacy and ratchet checks need
  full history. The exact gate order (and what it means for merging) is owned
  by `sonder-change-control` §2; reproducing CI locally = the Fast path above
  plus those gates in that order.
- `build-apps.yml`: the Flutter app path (`app/`). `flutter create` generates
  native scaffolding on the fly (only Dart sources are in git), then
  `flutter analyze` + `flutter test`, an Android/Linux/Windows/macOS build
  matrix, SHA-pinned actions, and an `integrity` job producing a CycloneDX
  SBOM and in-toto provenance. Setting up Flutter locally is out of scope
  here; which of these are required merge checks is sonder-change-control's
  territory.
- `aggregate-lessons.yml`: unrelated to environment setup.

## Known traps

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: mcp.server.mcpserver` | MCP 1.x installed (unpinned install, stale venv) | `pip install -r requirements-dev.txt` into a fresh venv; the `==2.0.0` pin is load-bearing |
| `No module named pytest` | Installed `requirements-runtime.txt` instead of `-dev` | Runtime file has no test deps; install `requirements-dev.txt` |
| Collection errors / import failures on a machine where the code is fine | pytest launched outside the repo root | Run from repo root; root `conftest.py` inserts it into `sys.path` |
| `No Python at '"C:\...'` (stray quote in path) | Quoting failure across bash → cmd → venv redirector | Use `scripts\run-tests.cmd` or one explicit quoted interpreter path; not a missing file, not a sandbox denial |
| `run-tests.cmd` exits 3 or 4 | No venv (3) or broken venv redirector (4) | Recreate: `python -m venv venv` then install `-dev` reqs |
| Proposal compatibility tests never run | `testpaths` trimmed to `tests` only | Keep `tests proposals` in pytest.ini |
| A `network`/`model` test "passes" instantly | It self-skipped without the opt-in flag | Re-run with `--run-network` / `--run-model`; a skip is not a pass |
| torch installed but no CUDA | PyPI CPU wheel | Reinstall from `https://download.pytorch.org/whl/cu121` (match your driver) |
| `git add -A` stages tens of MB of packages | Venv at a non-ignored path | Only `venv/`, `.runtime/`, `venv-train/` are ignored; keep venvs at those paths |
| New `.claude/skills/` files never commit | `.claude/` is gitignored (worktrees under it would otherwise self-report as offenders) | `git add -f .claude/skills/...` when a skill is meant to be tracked |
| Ratchet scan flags files inside a worktree/venv | Scan run against a copy outside the exclusion filter | By design, `tests/production/test_architecture.py` excludes dot-dirs (`.claude/`, `.runtime/`) and `venv/` from ratchet scans and guards the filter both ways; keep nested worktrees under dot-dirs |
| Selection script exits 2 and you conclude "nothing to test" | Vacuous selection | Exit 2 is an infrastructure failure; fix the invocation, never treat it as clean |

## Provenance and maintenance

Verified against commit 99162cf9 (2026-08-22), branch `claude/fable-skill-forge`,
local Python 3.12.10. Re-verify each claim with:

- Pins and file roles: `cat requirements-runtime.txt requirements-dev.txt requirements-train.txt requirements-update.txt`
- Test config and markers: `cat pytest.ini` (expect `testpaths = tests proposals`, four markers, no `slow`)
- Hermetic env, opt-in flags, posture restore: `cat conftest.py` (repo root)
- Wrapper exit codes 3/4 and the quoting postmortem: `cat scripts/run-tests.cmd`
- Pre-PR gate wording: `grep -n "pytest -q" CONTRIBUTING.md`
- CI steps and pytest form: `cat .github/workflows/ci.yml`
- App build + integrity job: `grep -n "integrity\|sbom\|intoto\|flutter" .github/workflows/build-apps.yml`
- No lint config: `ls pyproject.toml setup.cfg ruff.toml .ruff.toml mypy.ini tox.ini 2>/dev/null` (expect nothing)
- Ruff-is-optional in selfmod: `grep -n "_ruff_command" scripts/nightly_selfmod.py`
- Live test env vars: `grep -n "SONDER_LIVE" tests/live/test_model_gateway_live_smoke.py`
- Selection exit codes: `python scripts/select_regression_tests.py --help` or read its module docstring
- Venv ignore paths and `.claude/` ignore: `grep -n "venv\|.runtime\|.claude" .gitignore`
- Dot-dir ratchet exclusion guard: `grep -n "_is_repo_source" tests/production/test_architecture.py`
