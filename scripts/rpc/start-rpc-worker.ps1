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
            Stop-Process -Id ([int]$state.pid) -Force -ErrorAction SilentlyContinue
            Wait-Process -Id ([int]$state.pid) -Timeout 10 -ErrorAction SilentlyContinue
            if ($state.wrapperPid) {
                Stop-Process -Id ([int]$state.wrapperPid) -Force -ErrorAction SilentlyContinue
            }
        }
        Get-NetTCPConnection -State Listen -LocalPort $rpcPort -ErrorAction SilentlyContinue |
            Where-Object { $_.LocalAddress -eq $bindHost } |
            ForEach-Object {
                Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
            }
        Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue
        Write-Output "Stopped rpc-server on ${bindHost}:${rpcPort}"
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

        $argLine = ($rpcArgs | ForEach-Object {
            if ($_ -match '\s') { '"' + $_ + '"' } else { $_ }
        }) -join ' '
        $wrapperScript = "& `"$exe`" $argLine 1> `"$stdout`" 2> `"$stderr`""
        $encodedWrapper = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($wrapperScript))
        $cmdLine = "powershell.exe -NoProfile -WindowStyle Hidden -EncodedCommand $encodedWrapper"

        $cimResult = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
            CommandLine      = $cmdLine
            CurrentDirectory = $InstallDir
        }
        if ($cimResult.ReturnValue -ne 0) {
            throw "Win32_Process Create failed with return code $($cimResult.ReturnValue)."
        }
        $wrapperPid = [int]$cimResult.ProcessId

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
            $wrapperAlive = Get-Process -Id $wrapperPid -ErrorAction SilentlyContinue
            if ($null -eq $wrapperAlive) {
                $errTail = if (Test-Path -LiteralPath $stderr) { (Get-Content -LiteralPath $stderr -Tail 20) -join "`n" } else { '' }
                throw "rpc-server wrapper exited during startup: $errTail"
            }
            Start-Sleep -Milliseconds 250
        }
        if (-not $ready -or $null -eq $rpcPid) {
            Stop-Process -Id $wrapperPid -Force -ErrorAction SilentlyContinue
            throw "rpc-server did not start listening on ${bindHost}:${rpcPort} within 20s."
        }

        $stateObj = [ordered]@{
            pid        = $rpcPid
            wrapperPid = $wrapperPid
            exe        = $exe
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
