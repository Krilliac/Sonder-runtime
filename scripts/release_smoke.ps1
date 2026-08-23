<#
.SYNOPSIS
  Pre-tag release smoke check: version-policy gate plus a real end-to-end
  smoke run, chained into one pass/fail result (docs/runbooks/release-smoke-check.md).

.DESCRIPTION
  A release manager currently has to remember to run two independent, unrelated
  commands before tagging: scripts/check_release_version.py (does the runtime,
  Flutter, and tag version triad agree?) and `python -m sonder_runtime smoke`
  (does config load, do migrations apply, does a store round-trip?). Neither
  alone answers "is this checkout safe to tag" -- this script runs both and
  fails if either does, so there is one command and one answer.

  Read-only and offline: never contacts Ollama or a package registry, never
  writes outside the configured SONDER_HOME, and this script itself commits,
  tags, or pushes nothing.

.PARAMETER Python
  Interpreter to use. Defaults to $env:SONDER_PYTHON, then <repo>\venv, then
  `python` on PATH -- the same order scripts\run-tests.cmd uses.

.PARAMETER Tag
  Proposed release tag (e.g. app-v1.2.3), forwarded to check_release_version.py.
  Omit for a development-checkout check.

.PARAMETER Revision
  Full 40-character commit SHA, forwarded to check_release_version.py.
  Defaults to the checked-out HEAD when omitted.

.PARAMETER RequireRelease
  Forwarded as --require-release: fail unless -Tag is also set and the full
  triad is release-ready. Use this in the actual pre-tag gate, not day to day.

.EXAMPLE
  powershell -NoProfile -File scripts\release_smoke.ps1
  powershell -NoProfile -File scripts\release_smoke.ps1 -Tag app-v1.2.3 -Revision 0123456789abcdef0123456789abcdef01234567 -RequireRelease
#>
[CmdletBinding()]
param(
  [string] $Python = '',
  [string] $Tag = '',
  [string] $Revision = '',
  [switch] $RequireRelease
)

$repo = Split-Path -Parent $PSScriptRoot

$py = $Python
if ([string]::IsNullOrWhiteSpace($py)) { $py = $env:SONDER_PYTHON }
if ([string]::IsNullOrWhiteSpace($py)) {
  $venvPython = Join-Path $repo 'venv\Scripts\python.exe'
  if (Test-Path -LiteralPath $venvPython -PathType Leaf) { $py = $venvPython }
}
if ([string]::IsNullOrWhiteSpace($py)) {
  $found = Get-Command python -ErrorAction SilentlyContinue
  if ($found) { $py = $found.Source }
}
if ([string]::IsNullOrWhiteSpace($py)) {
  Write-Error 'no Python interpreter found; pass -Python or set SONDER_PYTHON'
  exit 3
}

$failed = @()

Write-Host '[release-smoke] checking runtime/app/tag version policy...'
$versionArgs = @((Join-Path $repo 'scripts\check_release_version.py'), '--json')
if ($Tag) { $versionArgs += @('--tag', $Tag) }
if ($Revision) { $versionArgs += @('--revision', $Revision) }
if ($RequireRelease) { $versionArgs += '--require-release' }
& $py @versionArgs
if ($LASTEXITCODE -ne 0) { $failed += 'version policy' }

Write-Host ''
Write-Host '[release-smoke] running end-to-end smoke (config, migrations, store round-trip)...'
& $py -m sonder_runtime smoke --skip-ollama
if ($LASTEXITCODE -ne 0) { $failed += 'runtime smoke' }

Write-Host ''
if ($failed) {
  Write-Host "[release-smoke] FAIL: $($failed -join ', ')"
  exit 1
}
Write-Host '[release-smoke] PASS: version policy and runtime smoke both succeeded'
exit 0
