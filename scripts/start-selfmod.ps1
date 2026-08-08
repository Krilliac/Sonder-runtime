<#
.SYNOPSIS
  Start the continuous selfmod loop detached from the calling session.

.DESCRIPTION
  Every attempt to run selfmod_forever.py as a child of an agent session was
  reaped within a few minutes -- five launches, none surviving past its second
  pass, which is useless for a job whose whole point is running for hours.

  Start-Process with -WindowStyle Hidden gives the loop its own process rather
  than a child of the caller's shell, so it outlives the session that started
  it. Output goes to a file under the state home instead of a pipe, because a
  pipe dies with its reader.

  Refuses to start a second instance. Two loops ran concurrently once and
  raced on the same run state and deployment lock; the duplicate is the kind
  of mistake that is invisible until the artifacts collide.

.EXAMPLE
  powershell -NoProfile -File scripts\start-selfmod.ps1 -Hours 4
  powershell -NoProfile -File scripts\start-selfmod.ps1 -Stop
#>
[CmdletBinding()]
param(
  [double] $Hours = 4.0,
  [string] $Model = 'qwen2.5-coder:14b',
  [switch] $Stop,
  [switch] $Status
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$py   = Join-Path $repo 'venv\Scripts\python.exe'
$log  = Join-Path $env:LOCALAPPDATA 'sonder\selfmod-continuous.log'

function Get-LoopProcesses {
  Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'selfmod_forever' }
}

if ($Stop) {
  $found = Get-LoopProcesses
  if (-not $found) { 'no selfmod loop is running'; exit 0 }
  $found | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; "stopped $($_.ProcessId)" }
  exit 0
}

if ($Status) {
  $found = Get-LoopProcesses
  if ($found) { "running: $($found.ProcessId -join ', ')" } else { 'not running' }
  if (Test-Path $log) {
    '--- last 12 log lines ---'
    Get-Content $log -Tail 12
  }
  exit 0
}

if (Get-LoopProcesses) {
  Write-Error 'a selfmod loop is already running; use -Stop first or -Status to inspect it'
  exit 1
}

if (-not (Test-Path $py)) { Write-Error "no interpreter at $py"; exit 3 }

# A run that starts on a dirty tree cannot be committed, so the loop would
# refuse every pass anyway. Say so here rather than after a launch that does
# nothing.
Push-Location $repo
$dirty = & git status --porcelain=v1 --untracked-files=all
Pop-Location
if ($dirty) {
  Write-Error "working tree is dirty ($(($dirty | Measure-Object).Count) path(s)); commit or stash first"
  exit 2
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $log) | Out-Null
$proc = Start-Process -FilePath $py -ArgumentList @(
    '-u', 'scripts/selfmod_forever.py', '--hours', "$Hours", '--model', $Model
  ) -WorkingDirectory $repo -RedirectStandardOutput $log `
    -RedirectStandardError "$log.err" -WindowStyle Hidden -PassThru

"started pid $($proc.Id) for $Hours h on $Model"
"log: $log"
"stop with: powershell -NoProfile -File scripts\start-selfmod.ps1 -Stop"
