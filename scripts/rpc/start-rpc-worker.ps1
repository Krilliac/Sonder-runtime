#Requires -Version 5.1
<#
.SYNOPSIS
  Manage a llama.cpp rpc-server worker bound to its private-link address.
.DESCRIPTION
  Runs rpc-server from the dedicated RPC folder (default
  %LOCALAPPDATA%\sonder\llama-rpc, or SONDER_RPC_INSTALL_DIR) so the main
  machine can shard GGUF layers across both hosts with `llama-server --rpc`.
  The bind address comes from -BindAddress or SONDER_RPC_WORKER_ADDRESS and must
  be a literal private (RFC 1918) or link-local IPv4 address; 0.0.0.0, loopback,
  public addresses and hostnames are refused. The port comes from -Port or
  SONDER_RPC_PORT (default 50052).

  rpc-server has no authentication: only expose it on a private link and lock
  it to the main machine with ensure-rpc-worker-firewall.ps1 (-EnsureFirewall).
.EXAMPLE
  powershell -NoProfile -File .\start-rpc-worker.ps1 -Action Start -BindAddress <worker-ip> -EnsureFirewall -AllowedPeer <main-ip>
#>
[CmdletBinding()]
param(
    [ValidateSet('Start', 'Stop', 'Status', 'Health', 'Devices')]
    [string]$Action = 'Status',
    [string]$BindAddress = $env:SONDER_RPC_WORKER_ADDRESS,
    [string]$Port = $env:SONDER_RPC_PORT,
    [ValidatePattern('^[A-Za-z0-9,_-]*$')]
    [string]$Device = '',
    [int]$Threads = 0,
    [switch]$UseCache,
    [string]$InstallDir = '',
    [string]$StateRoot = '',
    [switch]$EnsureFirewall,
    [string]$AllowedPeer = ''
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'rpc-policy.ps1')

if (-not $InstallDir.Trim()) { $InstallDir = Get-RpcDefaultInstallDir }
if (-not $StateRoot.Trim()) {
    if (-not $env:LOCALAPPDATA) { throw 'LOCALAPPDATA is not set; pass -StateRoot explicitly.' }
    $StateRoot = Join-Path $env:LOCALAPPDATA 'sonder'
}

function Resolve-RpcServerExe([string]$Dir) {
    foreach ($candidate in @('rpc-server.exe', 'ggml-rpc-server.exe')) {
        $path = Join-Path $Dir $candidate
        if (Test-Path -LiteralPath $path) {
            return $path
        }
    }
    throw "No rpc-server binary found under '$Dir'. Install the llama.cpp release there first."
}

if ($Action -eq 'Devices') {
    $llamaCli = Join-Path $InstallDir 'llama-cli.exe'
    if (-not (Test-Path -LiteralPath $llamaCli)) {
        $llamaCli = Join-Path $InstallDir 'llama-server.exe'
    }
    if (-not (Test-Path -LiteralPath $llamaCli)) {
        throw "No llama-cli.exe or llama-server.exe found under '$InstallDir'."
    }
    & $llamaCli --list-devices
    exit $LASTEXITCODE
}

if (-not ([string]$BindAddress).Trim()) {
    throw 'No worker bind address configured: pass -BindAddress <worker-ip> or set SONDER_RPC_WORKER_ADDRESS.'
}
$bindHost = ConvertTo-RpcPrivateIPv4 -Address $BindAddress -Label 'rpc-server bind address'
$rpcPort = if (([string]$Port).Trim()) { ConvertTo-RpcPort -Value $Port -Label 'Port' } else { $script:RpcDefaultPort }
if ($Threads -lt 0) { throw "Threads must be >= 0, got $Threads." }

$stateDir = Join-Path $StateRoot 'run'
$logDir = Join-Path $StateRoot 'logs'
$statePath = Join-Path $stateDir 'llama-rpc-worker.json'
New-Item -ItemType Directory -Force -Path $stateDir, $logDir | Out-Null

function Get-CurrentState {
    if (-not (Test-Path -LiteralPath $statePath)) { return $null }
    try {
        $state = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue
        return $null
    }
    if ($null -ne $state -and $null -ne $state.pid) {
        $proc = Get-Process -Id ([int]$state.pid) -ErrorAction SilentlyContinue
        if ($null -ne $proc) { return $state }
    }
    Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue
    return $null
}

switch ($Action) {
    'Status' {
        $state = Get-CurrentState
        $listener = @(Get-NetTCPConnection -State Listen -LocalPort $rpcPort -ErrorAction SilentlyContinue |
            Where-Object { $_.LocalAddress -eq $bindHost } |
            Select-Object LocalAddress, LocalPort, OwningProcess)
        [pscustomobject]@{
            Running   = ($null -ne $state)
            PID       = if ($state) { $state.pid } else { $null }
            Endpoint  = "${bindHost}:${rpcPort}"
            Listening = ($listener.Count -gt 0)
            Listener  = $listener
            State     = $state
        } | ConvertTo-Json -Depth 4
    }

    'Health' {
        $ok = Test-RpcTcpReachable -TargetHost $bindHost -TargetPort $rpcPort
        [pscustomobject]@{
            endpoint  = "${bindHost}:${rpcPort}"
            reachable = $ok
        } | ConvertTo-Json
        if (-not $ok) { exit 1 }
    }

    'Stop' {
        $state = Get-CurrentState
        if ($state) {
            # Only stop a process that is still the one we launched (same exe and
            # start time); a recycled PID or a foreign listener is left alone.
            if (Test-RpcOwnedProcess -ProcessId ([int]$state.pid) -ExePath ([string]$state.exe) -StartedUtc ([string]$state.startedUtc)) {
                Stop-Process -Id ([int]$state.pid) -Force -ErrorAction SilentlyContinue
                Wait-Process -Id ([int]$state.pid) -Timeout 10 -ErrorAction SilentlyContinue
                Write-Output "Stopped rpc-server on ${bindHost}:${rpcPort}"
            }
            else {
                Write-Warning "PID $($state.pid) is not the rpc-server recorded in the state file; not stopping it."
            }
        }
        else {
            Write-Output "No tracked rpc-server to stop on ${bindHost}:${rpcPort}"
        }
        Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue
    }

    'Start' {
        if ($EnsureFirewall) {
            if (-not $AllowedPeer.Trim()) {
                throw '-EnsureFirewall needs -AllowedPeer <main-ip> (the only address allowed to connect).'
            }
            $fwScript = Join-Path $PSScriptRoot 'ensure-rpc-worker-firewall.ps1'
            & $fwScript -LocalAddress $bindHost -RemoteAddress $AllowedPeer -Port $rpcPort | Out-Null
        }

        $existing = Get-CurrentState
        if ($existing -and (Test-RpcTcpReachable -TargetHost $bindHost -TargetPort $rpcPort)) {
            Write-Output ("rpc-server already running (PID {0}) on {1}:{2}" -f $existing.pid, $bindHost, $rpcPort)
            exit 0
        }
        if (Test-RpcTcpReachable -TargetHost $bindHost -TargetPort $rpcPort) {
            throw "Port $rpcPort on $bindHost is already occupied by another process."
        }

        $exe = Resolve-RpcServerExe $InstallDir
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $stdout = Join-Path $logDir "llama-rpc-worker-$stamp.stdout.log"
        $stderr = Join-Path $logDir "llama-rpc-worker-$stamp.stderr.log"

        $rpcArgs = @('--host', $bindHost, '--port', [string]$rpcPort)
        if ($Device.Trim()) {
            $rpcArgs += @('--device', $Device.Trim())
        }
        if ($Threads -gt 0) {
            $rpcArgs += @('--threads', [string]$Threads)
        }
        if ($UseCache) {
            $rpcArgs += '-c'
        }

        $proc = Start-Process -FilePath $exe `
            -ArgumentList (ConvertTo-RpcArgumentString $rpcArgs) `
            -WorkingDirectory $InstallDir `
            -RedirectStandardOutput $stdout -RedirectStandardError $stderr `
            -WindowStyle Hidden -PassThru
        $launchedPid = [int]$proc.Id
        $startedUtc = Get-RpcProcessStartUtc -ProcessId $launchedPid

        $deadline = [DateTime]::UtcNow.AddSeconds(20)
        $ready = $false
        $rpcPid = $null
        while ([DateTime]::UtcNow -lt $deadline) {
            $listener = Get-NetTCPConnection -State Listen -LocalPort $rpcPort -ErrorAction SilentlyContinue |
                Where-Object { $_.LocalAddress -eq $bindHost } |
                Select-Object -First 1
            if ($null -ne $listener) {
                $rpcPid = [int]$listener.OwningProcess
                $ready = $true
                break
            }
            $alive = Get-Process -Id $launchedPid -ErrorAction SilentlyContinue
            if ($null -eq $alive) {
                $errTail = if (Test-Path -LiteralPath $stderr) { (Get-Content -LiteralPath $stderr -Tail 20) -join "`n" } else { '' }
                throw "rpc-server exited during startup: $errTail"
            }
            Start-Sleep -Milliseconds 250
        }
        if (-not $ready -or $null -eq $rpcPid) {
            if (Test-RpcOwnedProcess -ProcessId $launchedPid -ExePath $exe -StartedUtc $startedUtc) {
                Stop-Process -Id $launchedPid -Force -ErrorAction SilentlyContinue
            }
            throw "rpc-server did not start listening on ${bindHost}:${rpcPort} within 20s."
        }

        $stateObj = [ordered]@{
            pid        = $launchedPid
            exe        = $exe
            startedUtc = $startedUtc
            host       = $bindHost
            port       = $rpcPort
            device     = $Device
            args       = $rpcArgs
            stdout     = $stdout
            stderr     = $stderr
            startedAt  = (Get-Date).ToString('o')
        }
        $stateObj | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $statePath -Encoding UTF8
        $stateObj | ConvertTo-Json -Depth 4
    }
}
