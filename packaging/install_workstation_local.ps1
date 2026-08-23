<#
.SYNOPSIS
  Automate the workstation-local install from a Windows PowerShell source
  checkout (docs/runbooks/install-workstation-local.md).

.DESCRIPTION
  Scripts the steps documented in README.md / docs/wiki/02-getting-started.md
  for a from-source Windows install: create a venv, install pinned runtime
  dependencies, optionally create the sonder:latest Ollama alias, then run
  preflight and migrate so the checkout is ready for `serve`, `repl`, or `mcp`.

  Idempotent and non-destructive by default: an existing venv is reused, not
  overwritten. Pass -Force to recreate it. This only ever touches
  <repo>\venv and per-user Sonder state (SONDER_HOME); it never modifies
  system PATH, installs a service, or touches Git history.

  This is a convenience wrapper, not a new install path: every step it runs
  is a documented, independently supported command, so a step can always be
  re-run by hand if the script is unavailable.

.PARAMETER Python
  Path to a Python 3.11+ interpreter to build the venv with. Defaults to the
  `py -3` launcher, then `python` on PATH.

.PARAMETER VenvPath
  Where to create the virtual environment. Defaults to <repo>\venv, matching
  every launcher script's default lookup (sonder-runtime.cmd).

.PARAMETER Force
  Delete and recreate an existing venv instead of reusing it.

.PARAMETER SkipModelAlias
  Skip creating the sonder:latest Ollama alias. Use this when Ollama is not
  installed yet, or when the alias already exists; preflight/migrate do not
  require a model.

.PARAMETER BaseModel
  Base Ollama model to alias as sonder:latest (forwarded to setup_alias.py
  --model). Defaults to setup_alias.py's own live-RAM-based choice.

.EXAMPLE
  powershell -NoProfile -File packaging\install_workstation_local.ps1
  powershell -NoProfile -File packaging\install_workstation_local.ps1 -SkipModelAlias
  powershell -NoProfile -File packaging\install_workstation_local.ps1 -Force -BaseModel qwen2.5-coder:14b
#>
[CmdletBinding()]
param(
  [string] $Python = '',
  [string] $VenvPath = '',
  [switch] $Force,
  [switch] $SkipModelAlias,
  [string] $BaseModel = ''
)

$ErrorActionPreference = 'Stop'

function Invoke-Step {
  param([string] $Description, [string] $FilePath, [string[]] $Arguments)
  Write-Host "[sonder] $Description..."
  & $FilePath @Arguments
  if ($LASTEXITCODE -ne 0) {
    throw "$Description failed (exit $LASTEXITCODE): $FilePath $($Arguments -join ' ')"
  }
}

$repo = Split-Path -Parent $PSScriptRoot
$requirementsFile = Join-Path $repo 'requirements-runtime.txt'
if (-not (Test-Path -LiteralPath $requirementsFile -PathType Leaf) -or
    -not (Test-Path -LiteralPath (Join-Path $repo 'sonder_version.py') -PathType Leaf)) {
  throw "this script must run from packaging\ inside a Sonder Runtime checkout (expected $requirementsFile)"
}

if ([string]::IsNullOrWhiteSpace($VenvPath)) {
  $VenvPath = Join-Path $repo 'venv'
}

# Resolve an interpreter the same way the rest of the toolchain does: an
# explicit override first, then the Windows launcher, then plain `python`.
$py = $Python
if ([string]::IsNullOrWhiteSpace($py)) {
  $launcher = Get-Command py -ErrorAction SilentlyContinue
  if ($launcher) { $py = $launcher.Source }
}
if ([string]::IsNullOrWhiteSpace($py)) {
  $found = Get-Command python -ErrorAction SilentlyContinue
  if ($found) { $py = $found.Source }
}
if ([string]::IsNullOrWhiteSpace($py)) {
  throw 'no Python interpreter found; install Python 3.11+ or pass -Python <path>'
}

$pyArgs = @()
if ((Split-Path -Leaf $py) -eq 'py.exe') { $pyArgs = @('-3') }
# Get the human-readable version from --version (no embedded quotes to lose
# to the py.exe launcher's own command-line reparsing) and the pass/fail
# check from a quote-free -c snippet, rather than combining both in one
# quoted -c argument.
$versionText = (& $py @pyArgs --version 2>&1) -join ' '
& $py @pyArgs -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)'
if ($LASTEXITCODE -ne 0) {
  throw "Python 3.11+ is required; $py reports: $versionText"
}
Write-Host "[sonder] using $versionText ($py $($pyArgs -join ' '))"

if (Test-Path -LiteralPath $VenvPath) {
  if ($Force) {
    Write-Host "[sonder] removing existing venv at $VenvPath (-Force)..."
    Remove-Item -LiteralPath $VenvPath -Recurse -Force
  } else {
    Write-Host "[sonder] reusing existing venv at $VenvPath (pass -Force to recreate)"
  }
}
if (-not (Test-Path -LiteralPath $VenvPath)) {
  Invoke-Step -Description "creating venv at $VenvPath" -FilePath $py -Arguments (@($pyArgs) + @('-m', 'venv', $VenvPath))
}

$venvPython = Join-Path $VenvPath 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
  throw "venv creation did not produce $venvPython"
}

# `pip.exe install --upgrade pip` fails on Windows because the running exe
# cannot replace itself; `python -m pip` reinvokes pip as a module instead,
# which both Windows and POSIX accept.
Invoke-Step -Description 'upgrading pip' -FilePath $venvPython -Arguments @('-m', 'pip', 'install', '--quiet', '--upgrade', 'pip')
Invoke-Step -Description 'installing runtime dependencies' -FilePath $venvPython -Arguments @('-m', 'pip', 'install', '--quiet', '-r', $requirementsFile)

if (-not $SkipModelAlias) {
  Write-Host '[sonder] creating the sonder:latest Ollama alias (needs Ollama running)...'
  $aliasArgs = @((Join-Path $repo 'setup_alias.py'))
  if ($BaseModel) { $aliasArgs += @('--model', $BaseModel) }
  & $venvPython @aliasArgs
  if ($LASTEXITCODE -ne 0) {
    Write-Warning "sonder:latest alias setup did not complete (exit $LASTEXITCODE); start Ollama and re-run: $venvPython $($aliasArgs -join ' ')"
  }
} else {
  Write-Host '[sonder] skipped Ollama alias setup (-SkipModelAlias)'
}

Invoke-Step -Description 'running preflight' -FilePath $venvPython -Arguments @('-m', 'sonder_runtime', 'preflight', '--skip-ollama')
Invoke-Step -Description 'applying migrations' -FilePath $venvPython -Arguments @('-m', 'sonder_runtime', 'migrate')

Write-Host ''
Write-Host 'Install complete.'
Write-Host ''
Write-Host 'Next steps:'
Write-Host "  1. Start the API:  $venvPython -m sonder_runtime serve"
Write-Host "     or the REPL:    $venvPython -m sonder_runtime repl"
Write-Host '  2. State lives under %LOCALAPPDATA%\sonder (override with SONDER_HOME).'
if ($SkipModelAlias) {
  Write-Host "  3. Create a model alias when ready: $venvPython $(Join-Path $repo 'setup_alias.py')"
}
