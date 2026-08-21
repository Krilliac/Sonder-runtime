# WP1 Two-Hundred-Eighty-Sixth Slice — native MCP artifact-risk parity

## Boundary

Ported `pdf_risk.py` and `artifact_risk.py` into packaged adapters, changing
the artifact inspector to use the packaged PDF implementation and existing
typed `artifact_risk_policy`. Native MCP now exposes `artifact_risk_inspect`
through `ToolExecutorAdapter` as a read-only, bounded static inspection; it
does not execute, render, download, or expose artifact contents.

## Evidence

- Native MCP, typed executor, artifact-risk, PDF-risk, policy, server
  compatibility, and stdio regressions pass: **77 passed**.
- A guarded artifact inspection was verified through the packaged adapter,
  while the existing PDF active-content and scan-bound tests remain green.
- The native catalog now reports **35** deterministic names against the legacy
  source audit's **204** registered MCP tools.
- `git diff --check` and the architecture gate pass.

## Limitation

Artifact acquisition (`fetch_artifact`/`verify_artifact`) and model-backed
`vision_analyze` remain separate policy/service migrations. Full MCP parity,
epoch-2 bridge retirement, and formal checklist acceptance remain incomplete.
