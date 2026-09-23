<#
.SYNOPSIS
  Run the nightly Sonder maintenance synchronously with a dated log.

.DESCRIPTION
  This is the Task Scheduler entrypoint for a hidden nightly run. It waits for
  Python, writes stdout and stderr to separate dated logs, and propagates the
  Python exit code.

.PARAMETER Python
  Python executable. Defaults to <checkout>\venv\Scripts\python.exe, then PATH.

.PARAMETER LogDirectory
  Directory for dated logs. Defaults to %LOCALAPPDATA%\sonder\nightly-logs.

.PARAMETER Preflight
  Run nightly_self_improve.py --preflight only.
#>
[CmdletBinding()]
param(
  [string] $Python = '',
  [string] $LogDirectory = '',
  [switch] $Preflight,
  [int] $CampaignTotal = 24,
  [int] $RepairTotal = 10,
  [int] $Rounds = 1,
  [switch] $SkipCampaign
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$nightly = Join-Path $repo 'scripts\nightly_self_improve.py'
if (-not (Test-Path -LiteralPath $nightly -PathType Leaf)) {
  throw "nightly script not found: $nightly"
}

$py = $Python
if ([string]::IsNullOrWhiteSpace($py)) {
  $candidate = Join-Path $repo 'venv\Scripts\python.exe'
  if (Test-Path -LiteralPath $candidate -PathType Leaf) { $py = $candidate }
}
if ([string]::IsNullOrWhiteSpace($py)) {
  $command = Get-Command python -ErrorAction SilentlyContinue
  if ($null -ne $command) { $py = $command.Source }
}
if ([string]::IsNullOrWhiteSpace($py) -or -not (Test-Path -LiteralPath $py -PathType Leaf)) {
  throw 'no Python executable found; pass -Python <path>'
}

if ([string]::IsNullOrWhiteSpace($LogDirectory)) {
  $stateRoot = $env:LOCALAPPDATA
  if ([string]::IsNullOrWhiteSpace($stateRoot)) { $stateRoot = [IO.Path]::GetTempPath() }
  $LogDirectory = Join-Path $stateRoot 'sonder\nightly-logs'
}
New-Item -ItemType Directory -Force -Path $LogDirectory | Out-Null
$stamp = Get-Date -Format 'yyyy-MM-dd_HH-mm-ss'
$log = Join-Path $LogDirectory ("wrapper-$stamp.log")
$stderr = "$log.stderr"

$arguments = @('-u', $nightly)
if ($Preflight) {
  $arguments += '--preflight'
} else {
  $arguments += @('--campaign-total', "$CampaignTotal", '--repair-total', "$RepairTotal", '--rounds', "$Rounds")
  if ($SkipCampaign) { $arguments += '--skip-campaign' }
}

# Start-Process is explicitly waited on. Redirected output survives an
# interrupted shell or a Task Scheduler history inspection.
$process = Start-Process -FilePath $py -ArgumentList $arguments -WorkingDirectory $repo -RedirectStandardOutput $log -RedirectStandardError $stderr -WindowStyle Hidden -Wait -PassThru
Add-Content -LiteralPath $log -Value ([Environment]::NewLine + ("finished {0} exit={1}" -f (Get-Date -Format o), $process.ExitCode)) -Encoding UTF8
Write-Output "nightly exit=$($process.ExitCode) log=$log stderr=$stderr"
exit $process.ExitCode
