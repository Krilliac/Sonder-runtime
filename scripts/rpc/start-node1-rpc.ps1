#Requires -Version 5.1
<#
.SYNOPSIS
  Manage Node1's llama.cpp rpc-server worker strictly bound to 10.77.0.2:50052.
.DESCRIPTION
  Runs the Vulkan-enabled rpc-server from C:\NewBuild\llama-rpc\ so Main
  (10.77.0.1) can shard GGUF layers across Main + Node1 via `llama-server --rpc`.
  Refuses wildcard (0.0.0.0) or non-10.77.0.0/30 bind addresses.
#>
[CmdletBinding()]
param(
    [ValidateSet('Start', 'Stop', 'Status', 'Health', 'Devices')]
    [string]$Action = 'Status',
    [string]$HostAddress = '10.77.0.2',
    [int]$Port = 50052,
    [string]$Device = '',
    [int]$Threads = 0,
    [switch]$UseCache,
    [string]$InstallDir = 'C:\NewBuild\llama-rpc',
    [string]$RootDir = 'C:\NewBuild',
    [switch]$EnsureFirewall
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Assert-PrivateLinkHost([string]$Address) {
    $trimmed = ([string]$Address).Trim()
    $parsed = $null
    if (-not [System.Net.IPAddress]::TryParse($trimmed, [ref]$parsed)) {
        throw "Node1 rpc-server host must be an IPv4 address inside 10.77.0.0/30, got '$Address'."
    }
    if ($parsed.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) {
        throw "Node1 rpc-server host must be IPv4 inside 10.77.0.0/30, got '$Address'."
    }
    $bytes = $parsed.GetAddressBytes()
    # 10.77.0.0/30 allows only 10.77.0.1 and 10.77.0.2 (never 0.0.0.0 or public).
    if ($bytes[0] -ne 10 -or $bytes[1] -ne 77 -or $bytes[2] -ne 0 -or ($bytes[3] -ne 1 -and $bytes[3] -ne 2)) {
        throw "Refusing to bind rpc-server to '$Address': only 10.77.0.1 or 10.77.0.2 on the private link is permitted."
    }
    return $parsed.IPAddressToString
}

function Resolve-RpcServerExe([string]$Dir) {
    foreach ($candidate in @('rpc-server.exe', 'ggml-rpc-server.exe')) {
        $path = Join-Path $Dir $candidate
        if (Test-Path -LiteralPath $path) {
            return $path
        }
    }
    throw "No rpc-server binary found under '$Dir'. Install llama.cpp Vulkan binaries there first."
}

function Test-TcpPort([string]$TargetHost, [int]$TargetPort, [int]$TimeoutMs = 1500) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect($TargetHost, $TargetPort, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne($TimeoutMs)) { return $false }
        $client.EndConnect($async)
        return $true
    }
    catch {
        return $false
    }
    finally {
        $client.Close()
    }
}

$bindHost = Assert-PrivateLinkHost $HostAddress
if ($Port -lt 1 -or $Port -gt 65535) {
    throw "Port must be between 1 and 65535, got $Port."
}

$stateDir = Join-Path $RootDir 'state'
$logDir = Join-Path $RootDir 'logs'
$statePath = Join-Path $stateDir 'llama-rpc-server.json'
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
    'Devices' {
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

    'Status' {
        $state = Get-CurrentState
        $listener = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
            Where-Object { $_.LocalAddress -eq $bindHost } |
            Select-Object LocalAddress, LocalPort, OwningProcess)
        [pscustomobject]@{
            Running   = ($null -ne $state)
            PID       = if ($state) { $state.pid } else { $null }
            Endpoint  = "${bindHost}:${Port}"
            Reachable = ($listener.Count -gt 0)
            Listener  = $listener
            State     = $state
        } | ConvertTo-Json -Depth 4
    }

    'Health' {
        $ok = Test-TcpPort -TargetHost $bindHost -TargetPort $Port
        $payload = [pscustomobject]@{
            endpoint  = "${bindHost}:${Port}"
            reachable = $ok
        }
        $payload | ConvertTo-Json
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
        Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
            Where-Object { $_.LocalAddress -eq $bindHost } |
            ForEach-Object {
                Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
            }
        Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue
        Write-Output "Stopped Node1 rpc-server on ${bindHost}:${Port}"
    }

    'Start' {
        if ($EnsureFirewall) {
            $fwScript = Join-Path $PSScriptRoot 'ensure-node1-rpc-fw.ps1'
            if (Test-Path -LiteralPath $fwScript) {
                & $fwScript | Out-Null
            }
        }

        $existing = Get-CurrentState
        if ($existing -and (Test-TcpPort -TargetHost $bindHost -TargetPort $Port)) {
            Write-Output ("Node1 rpc-server already running (PID {0}) on {1}:{2}" -f $existing.pid, $bindHost, $Port)
            exit 0
        }
        if (Test-TcpPort -TargetHost $bindHost -TargetPort $Port) {
            throw "Port $Port on $bindHost is already occupied by another process."
        }

        $exe = Resolve-RpcServerExe $InstallDir
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $stdout = Join-Path $logDir "llama-rpc-server-$stamp.stdout.log"
        $stderr = Join-Path $logDir "llama-rpc-server-$stamp.stderr.log"

        $rpcArgs = @('--host', $bindHost, '--port', [string]$Port)
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
            $listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
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
            throw "Node1 rpc-server did not start listening on ${bindHost}:${Port} within 20s."
        }

        $stateObj = [ordered]@{
            pid        = $rpcPid
            wrapperPid = $wrapperPid
            exe        = $exe
            host       = $bindHost
            port       = $Port
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
