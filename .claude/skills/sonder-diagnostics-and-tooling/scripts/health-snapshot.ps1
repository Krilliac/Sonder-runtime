#Requires -Version 5.1
<#
.SYNOPSIS
Sonder Runtime health snapshot: preflight + doctor + status.

.DESCRIPTION
Runs the three CLI health surfaces via an explicit Python interpreter and
saves each JSON report (stdout) to a timestamped file under the
caller-supplied output directory, with stderr captured separately to a
matching .err.txt file so diagnostics noise can never corrupt the JSON.

Mutation contract: doctor and status are read-only. preflight is NOT fully
read-only — it creates the state home if missing and performs a write probe
in it. No migrate/repair is ever invoked.

Exit code is 0 only when all three subcommands exited 0. A doctor "warn"
still exits 0 by design; only "fail" makes doctor exit 1.

.EXAMPLE
powershell -File health-snapshot.ps1 -OutputDir C:\temp\sonder-health

.EXAMPLE
powershell -File health-snapshot.ps1 -OutputDir C:\temp\sonder-health `
  -Python D:\repo\venv\Scripts\python.exe
#>
param(
    [Parameter(Mandatory = $true)][string]$OutputDir,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"

# scripts/ -> skill folder -> skills -> .claude -> repo root
$repoRoot = (Get-Item $PSScriptRoot).Parent.Parent.Parent.Parent.FullName
if (-not (Test-Path (Join-Path $repoRoot "sonder_doctor.py"))) {
    Write-Error "Repo root not found at $repoRoot (sonder_doctor.py missing)."
}

if (-not $Python) {
    $candidate = Join-Path $repoRoot "venv\Scripts\python.exe"
    if (Test-Path $candidate) { $Python = $candidate } else { $Python = "python" }
}

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"

$overall = 0
$commands = @(
    @{ Name = "preflight"; CmdArgs = @("-m", "sonder_runtime", "preflight", "--json") },
    @{ Name = "doctor";    CmdArgs = @("-m", "sonder_runtime", "doctor", "--json") },
    @{ Name = "status";    CmdArgs = @("-m", "sonder_runtime", "status", "--json") }
)

foreach ($entry in $commands) {
    $outFile = Join-Path $OutputDir ("{0}-{1}.json" -f $stamp, $entry.Name)
    $errFile = Join-Path $OutputDir ("{0}-{1}.err.txt" -f $stamp, $entry.Name)
    # Start-Process keeps stdout (JSON) and stderr (diagnostics) in separate
    # files and avoids PowerShell 5.1 wrapping native stderr as error records.
    $proc = Start-Process -FilePath $Python -ArgumentList $entry.CmdArgs `
        -WorkingDirectory $repoRoot -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $outFile -RedirectStandardError $errFile
    $code = $proc.ExitCode
    if ($null -eq $code) { $code = 1 }
    Write-Host ("{0}: exit {1} -> {2}" -f $entry.Name, $code, $outFile)
    if ($code -ne 0) { $overall = 1 }
}

exit $overall
