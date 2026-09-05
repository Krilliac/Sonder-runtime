# Reproducible Sonder baseline — 2026-09-05

This is a read-only qualification record for the current upstream `main` revision.
It does not modify an installed checkout, runtime database, model cache, or service.

## Source and environment

| Field | Value |
|---|---|
| Repository | `Krilliac/Sonder-runtime` |
| Source revision | `70df5e2103a41b49fa528bc69038bb66ab76378b` |
| Worktree | Fresh worktree `codex/p0-baseline-evidence` from `origin/main` |
| Platform | `Windows-11-10.0.26200-SP0` |
| Interpreter | `D:\\sonder-runtime\\venv\\Scripts\\python.exe` |
| Python | `3.12.10` |
| MCP | `2.0.0` |
| Pytest | `9.1.1` |
| Worktree status | clean after checkout; this evidence file is the only change |

Tracked Python inventory at this revision is 2,130 files: 1,077 production files,
1,053 test files, 129 root modules, and 876 modules under `sonder_runtime/`. These
counts are from `git ls-files '*.py'` and are not a claim about requirement completion.

## Checks run

The following commands were run with the interpreter above:

```text
python -c "import sys, platform, sonder_runtime; ..."
python scripts/check_architecture.py
python scripts/generate_documentation_catalogs.py --check
```

The import smoke, architecture ratchet, and generated-documentation freshness checks
passed. The import smoke resolved `sonder_runtime` from this worktree. The architecture
ratchet remains a migration check and does not prove the master specification is
complete.

The complete pytest suite was not run in this baseline capture. Current CI runs are the
authoritative full-suite evidence for each reviewed branch; focused suites are recorded
in their individual PRs and evidence documents. No model-serving, multi-node, failover,
or installed-runtime behavior is implied by these checks.

## Dependency snapshot

The qualified environment reported these relevant pinned packages: `mcp==2.0.0`,
`mcp-types==2.0.0`, `pytest==9.1.1`, `pydantic==2.13.4`, `cryptography==50.0.0`,
`httpx2==2.12.0`, `starlette==1.6.0`, and `uvicorn==0.52.4`. The complete `pip freeze`
output was captured during the run and can be reproduced with:

```text
D:\\sonder-runtime\\venv\\Scripts\\python.exe -m pip freeze
```

This record is a reproducibility gate for subsequent slices. It does not authorize
production deployment or expand any permission, network, or model-provider boundary.
