<#
.SYNOPSIS
  Start, inspect, or stop the detached continuous selfmod loop.

.DESCRIPTION
  The loop is identified by an atomic state record containing the PID,
  process creation time, trusted interpreter, repository, launcher script, and
  script digest. PID and command-line substring matches are never sufficient
  to stop a process: every action revalidates the complete identity.
#>
[CmdletBinding()]
param(
  [double] $Hours = 4.0,
  [string] $Model = 'qwen2.5-coder:14b',
  [int] $MaxBarren = 6,
  [int] $NumCtx = 16384,
  [string] $Python = '',
  [switch] $Stop,
  [switch] $Status
)

$ErrorActionPreference = 'Stop'
$repo = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$scriptPath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'selfmod_forever.py'))
$stateRoot = $env:SONDER_SELFMOD_STATE_ROOT
if ([string]::IsNullOrWhiteSpace($stateRoot)) { $stateRoot = $env:LOCALAPPDATA }
if ([string]::IsNullOrWhiteSpace($stateRoot)) { $stateRoot = [IO.Path]::GetTempPath() }
$stateDir = Join-Path $stateRoot 'sonder'
$statePath = Join-Path $stateDir 'selfmod-continuous.json'
$log = Join-Path $stateDir 'selfmod-continuous.log'
$mutexName = 'Local\Sonder-Selfmod-Launcher'

function Normalize-Path([string] $value) {
  return ([IO.Path]::GetFullPath($value)).TrimEnd('\','/').ToLowerInvariant()
}

function Get-FileIdentity([string] $path) {
  $sha = [Security.Cryptography.SHA256]::Create()
  try {
    return ([BitConverter]::ToString($sha.ComputeHash([IO.File]::ReadAllBytes($path))) -replace '-', '').ToLowerInvariant()
  } finally { $sha.Dispose() }
}

function Enter-StateLock {
  $mutex = New-Object Threading.Mutex($false, $mutexName)
  $acquired = $false
  try {
    try { $acquired = $mutex.WaitOne(10000) }
    catch [Threading.AbandonedMutexException] { $acquired = $true }
    if (-not $acquired) { throw 'selfmod launcher state lock is busy' }
  } catch {
    $mutex.Dispose()
    throw
  }
  return $mutex
}

function Exit-StateLock($mutex) {
  if ($null -ne $mutex) {
    try { $mutex.ReleaseMutex() } catch [Threading.AbandonedMutexException] { }
    $mutex.Dispose()
  }
}

function Read-State {
  if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) { return $null }
  try { return (Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json) }
  catch { throw "selfmod state file is invalid: $statePath" }
}

function Write-State($state) {
  New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
  $tmp = "$statePath.$PID.$([guid]::NewGuid().ToString('N')).tmp"
  try {
    [IO.File]::WriteAllText($tmp, ($state | ConvertTo-Json -Depth 4), (New-Object Text.UTF8Encoding($false)))
    if (Test-Path -LiteralPath $statePath) { [IO.File]::Replace($tmp, $statePath, $null) }
    else { Move-Item -LiteralPath $tmp -Destination $statePath }
  } finally {
    if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force }
  }
}

function Remove-State {
  if (Test-Path -LiteralPath $statePath) { Remove-Item -LiteralPath $statePath -Force }
}

function Get-VerifiedIdentity($state) {
  if ($null -eq $state) { return [pscustomobject]@{ Valid = $false; SafeToClear = $true; Reason = 'no state record'; Process = $null } }
  try { $process = Get-Process -Id ([int]$state.pid) -ErrorAction Stop } catch {
    $gone = $_.Exception.Message -match 'Cannot find a process|not found|does not exist'
    return [pscustomobject]@{ Valid = $false; SafeToClear = $gone; Reason = $(if ($gone) { 'recorded process is not running' } else { 'process identity cannot be inspected' }); Process = $null }
  }
  try {
    $handle = $process.Handle
    if ($process.StartTime.Ticks -ne [int64]$state.creation_ticks) { throw 'creation time mismatch' }
    if ((Normalize-Path $process.Path) -ne (Normalize-Path $state.executable_path)) { throw 'executable identity mismatch' }
    if ((Normalize-Path $state.script_path) -ne (Normalize-Path $scriptPath)) { throw 'script path identity mismatch' }
    if ((Get-FileIdentity $scriptPath) -ne $state.script_sha256) { throw 'script identity changed' }
    if ((Normalize-Path $state.repo_path) -ne (Normalize-Path $repo)) { throw 'repository identity mismatch' }
    return [pscustomobject]@{ Valid = $true; SafeToClear = $false; Reason = 'verified'; Process = $process; Handle = $handle }
  } catch {
    $safe = $_.Exception.Message -match 'creation time mismatch|executable identity mismatch|recorded process is not running'
    return [pscustomobject]@{ Valid = $false; SafeToClear = $safe; Reason = $_.Exception.Message; Process = $process }
  }
}

function Show-Log {
  if (Test-Path -LiteralPath $log) {
    '--- last 12 log lines ---'
    Get-Content -LiteralPath $log -Tail 12
  }
}

$mutex = Enter-StateLock
try {
  $state = Read-State
  $identity = Get-VerifiedIdentity $state

  if ($Stop) {
    if (-not $identity.Valid) {
      if (-not $identity.SafeToClear) { throw "refusing to stop unverified selfmod owner: $($identity.Reason)" }
      Remove-State
      "no verified selfmod loop is running ($($identity.Reason))"
      exit 0
    }
    # Re-read and revalidate immediately before using the process handle.
    $identity = Get-VerifiedIdentity (Read-State)
    if (-not $identity.Valid) { throw "refusing to stop: $($identity.Reason)" }
    $identity.Process.Kill()
    Remove-State
    "stopped pid=$($identity.Process.Id)"
    exit 0
  }

  if ($Status) {
    if ($identity.Valid) { "running: pid=$($state.pid)" }
    elseif ($identity.SafeToClear) { "not running ($($identity.Reason))" }
    else { "unknown/refused ($($identity.Reason))" }
    Show-Log
    exit 0
  }

  if ($identity.Valid) { throw 'a verified selfmod loop is already running; use -Stop first or -Status to inspect it' }
  if ($null -ne $state) {
    if (-not $identity.SafeToClear) { throw "refusing to replace unverified selfmod owner: $($identity.Reason)" }
    Remove-State
  }

  $py = $Python
  if ([string]::IsNullOrWhiteSpace($py)) { $py = $env:SONDER_PYTHON }
  if ([string]::IsNullOrWhiteSpace($py)) {
    $localPython = Join-Path $repo 'venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $localPython -PathType Leaf) { $py = $localPython }
  }
  if ([string]::IsNullOrWhiteSpace($py)) { $py = (Get-Command python -ErrorAction SilentlyContinue).Source }
  if ([string]::IsNullOrWhiteSpace($py) -or -not (Test-Path -LiteralPath $py -PathType Leaf)) {
    throw 'no Python interpreter found; pass -Python or set SONDER_PYTHON'
  }
  $py = [IO.Path]::GetFullPath($py)

  Push-Location $repo
  try { $dirty = & git status --porcelain=v1 --untracked-files=all } finally { Pop-Location }
  if ($dirty) { throw "working tree is dirty ($(($dirty | Measure-Object).Count) path(s)); commit or stash first" }

  New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
  $proc = Start-Process -FilePath $py -ArgumentList @(
      '-u', "`"$scriptPath`"", '--hours', "$Hours", '--model', "`"$Model`"",
      '--max-barren', "$MaxBarren", '--num-ctx', "$NumCtx"
    ) -WorkingDirectory $repo -RedirectStandardOutput $log `
      -RedirectStandardError "$log.err" -WindowStyle Hidden -PassThru
  $creation = $null
  for ($i = 0; $i -lt 20 -and $null -eq $creation; $i++) {
    try { $creation = $proc.StartTime.ToUniversalTime().ToString('o') } catch { Start-Sleep -Milliseconds 50 }
  }
  if ($null -eq $creation) { $proc.Kill(); throw 'unable to record child process creation time' }
  try {
    Write-State ([pscustomobject]@{
      pid = $proc.Id; creation_time = $creation; creation_ticks = $proc.StartTime.Ticks; executable_path = $py
      repo_path = $repo; script_path = $scriptPath; script_sha256 = Get-FileIdentity $scriptPath
      started_at = [DateTime]::UtcNow.ToString('o')
    })
  } catch {
    try { $proc.Kill() } catch { }
    throw
  }
  "started pid=$($proc.Id) for $Hours h on $Model (ctx $NumCtx; max-barren $MaxBarren; 0=unlimited)"
  "log: $log"
  "stop with: powershell -NoProfile -File scripts\start-selfmod.ps1 -Stop"
} finally {
  Exit-StateLock $mutex
}
