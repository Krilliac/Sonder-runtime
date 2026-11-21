#Requires -Version 5.1
<#
.SYNOPSIS
  Launch and manage the main machine's sharded llama-server (llama.cpp --rpc) for Sonder.
.DESCRIPTION
  Runs llama-server.exe from the dedicated RPC folder (default
  %LOCALAPPDATA%\sonder\llama-rpc, or SONDER_RPC_INSTALL_DIR) bound strictly to
  loopback (127.0.0.1:11438 by default), sharding layers across the local GPU
  and the allowlisted rpc-server workers. Wires Sonder via the existing
  `openai-compatible` provider binding (SONDER_OPENAI_BASE_URL / SONDER_OPENAI_MODEL).

  Workers come only from -RpcPeers or SONDER_RPC_PEERS (no default): each must
  be a literal private (RFC 1918) or link-local IPv4 address on the one
  configured RPC port (-RpcPort or SONDER_RPC_PORT, default 50052).
.EXAMPLE
  powershell -NoProfile -File .\start-rpc-sharded.ps1 -Action Start -RpcPeers <worker-ip> -ModelPath <path-to-model.gguf>
#>
[CmdletBinding()]
param(
    [ValidateSet('Start', 'Stop', 'Status', 'Health', 'Env')]
    [string]$Action = 'Start',
    [string]$ModelPath = '',
    [string]$OllamaModel = '',
    [ValidatePattern('^[A-Za-z0-9._:-]+$')]
    [string]$Alias = 'sonder-rpc',
    [string]$BindHost = '127.0.0.1',
    [ValidateRange(1, 65535)]
    [int]$Port = 11438,
    [string[]]$RpcPeers = @(),
    [string]$RpcPort = $env:SONDER_RPC_PORT,
    [int]$CtxSize = 8192,
    [int]$GpuLayers = 999,
    [ValidatePattern('^([0-9.]+(,[0-9.]+)*)?$')]
    [string]$TensorSplit = '',
    [ValidateSet('on', 'off', 'auto')]
    [string]$FlashAttn = 'on',
    [ValidateSet('f32', 'f16', 'bf16', 'q8_0', 'q4_0', 'q4_1', 'iq4_nl', 'q5_0', 'q5_1')]
    [string]$CacheTypeK = 'q8_0',
    [ValidateSet('f32', 'f16', 'bf16', 'q8_0', 'q4_0', 'q4_1', 'iq4_nl', 'q5_0', 'q5_1')]
    [string]$CacheTypeV = 'q8_0',
    [int]$Parallel = 1,
    [int]$LogVerbosity = 4,
    [int]$ReadyWaitSeconds = 180,
    [string]$InstallDir = '',
    [string]$SonderRuntimeRoot = '',
    [string]$PythonExe = '',
    [ValidateSet('none', 'serve', 'mcp')]
    [string]$LaunchSonder = 'none',
    [string[]]$SonderTiers = @('general', 'reasoning')
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'rpc-policy.ps1')

if (-not $InstallDir.Trim()) { $InstallDir = Get-RpcDefaultInstallDir }
if (-not $SonderRuntimeRoot.Trim()) {
    $SonderRuntimeRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
}
if (-not $PythonExe.Trim()) {
    $PythonExe = if (([string]$env:SONDER_PYTHON).Trim()) { $env:SONDER_PYTHON.Trim() } else { 'python' }
}

function Assert-LoopbackHost([string]$Address) {
    $trimmed = ([string]$Address).Trim()
    if ($trimmed.ToLowerInvariant() -eq 'localhost') { return '127.0.0.1' }
    $parsed = $null
    if (-not [System.Net.IPAddress]::TryParse($trimmed, [ref]$parsed)) {
        throw "llama-server bind host must be loopback-only (127.0.0.1 or ::1), got '$Address'."
    }
    if (-not [System.Net.IPAddress]::IsLoopback($parsed)) {
        throw "Refusing non-loopback llama-server bind host '$Address': Sonder requires loopback for local plaintext OpenAI-compatible endpoints."
    }
    return $parsed.IPAddressToString
}

function Get-ServerHealth([string]$BaseUrl) {
    # No redirects and no proxy: the probe must only talk to the loopback listener
    # it was pointed at. Windows PowerShell 5.1 has no -NoProxy parameter.
    $request = @{ TimeoutSec = 3; MaximumRedirection = 0 }
    if ((Get-Command Invoke-RestMethod).Parameters.ContainsKey('NoProxy')) { $request['NoProxy'] = $true }
    try {
        $health = Invoke-RestMethod -Uri "$BaseUrl/health" @request
        $models = Invoke-RestMethod -Uri "$BaseUrl/v1/models" @request
        $ids = @($models.data | ForEach-Object { [string]$_.id })
        return [pscustomobject]@{
            healthy = ($health.status -eq 'ok')
            status  = [string]$health.status
            models  = $ids
        }
    }
    catch {
        return $null
    }
}

$loopbackHost = Assert-LoopbackHost $BindHost
$rpcPortValue = if (([string]$RpcPort).Trim()) { ConvertTo-RpcPort -Value $RpcPort -Label 'RpcPort' } else { $script:RpcDefaultPort }
$peerEntries = if ($RpcPeers.Count -gt 0) { $RpcPeers } else { @([string]$env:SONDER_RPC_PEERS) }
$validatedPeers = @(ConvertTo-RpcPeerList -Entries $peerEntries -Port $rpcPortValue)
if ($validatedPeers.Count -eq 0) {
    throw 'No RPC peers configured: pass -RpcPeers <worker-ip> or set SONDER_RPC_PEERS. Sharding stays off until one is configured.'
}
$baseUrl = Assert-RpcBaseUrl (ConvertTo-RpcBaseUrl -HostAddress $loopbackHost -Port $Port)
$runDir = Join-Path $env:LOCALAPPDATA 'sonder\run'
$logDir = Join-Path $env:LOCALAPPDATA 'sonder\logs'
$statePath = Join-Path $runDir 'llama-rpc-server.json'
New-Item -ItemType Directory -Force -Path $runDir, $logDir | Out-Null

function Get-CurrentState {
    if (-not (Test-Path -LiteralPath $statePath)) { return $null }
    try {
        $s = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($s -and $s.pid -and (Get-Process -Id ([int]$s.pid) -ErrorAction SilentlyContinue)) { return $s }
    }
    catch { }
    Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue
    return $null
}

function Get-SonderRpcEnv {
    $tierVars = @{
        fast      = 'SONDER_FAST_PROVIDER'
        general   = 'SONDER_GENERAL_PROVIDER'
        code      = 'SONDER_CODE_PROVIDER'
        reasoning = 'SONDER_REASONING_PROVIDER'
        vision    = 'SONDER_VISION_PROVIDER'
    }
    $result = [ordered]@{
        SONDER_OPENAI_BASE_URL = $baseUrl
        SONDER_OPENAI_MODEL    = $Alias
        SONDER_RPC_PEERS       = (($validatedPeers | ForEach-Object { $_.Address }) -join ',')
        SONDER_RPC_PORT        = [string]$rpcPortValue
    }
    foreach ($t in $SonderTiers) {
        $k = $t.Trim().ToLowerInvariant()
        if (-not $tierVars.ContainsKey($k)) { throw "Unknown Sonder tier '$t'." }
        $result[$tierVars[$k]] = 'openai-compatible'
    }
    return $result
}

switch ($Action) {
    'Env' {
        Get-SonderRpcEnv | ConvertTo-Json
        exit 0
    }

    'Status' {
        $state = Get-CurrentState
        $h = Get-ServerHealth $baseUrl
        $peerHealth = @($validatedPeers | ForEach-Object {
            [pscustomobject]@{
                endpoint  = $_.Address
                reachable = (Test-RpcTcpReachable -TargetHost $_.Host -TargetPort $_.Port)
            }
        })
        [pscustomobject]@{
            Running  = ($null -ne $state)
            PID      = if ($state) { $state.pid } else { $null }
            BaseUrl  = $baseUrl
            Health   = $h
            RpcPeers = $peerHealth
            State    = $state
        } | ConvertTo-Json -Depth 5
        exit 0
    }

    'Health' {
        $h = Get-ServerHealth $baseUrl
        if ($null -eq $h -or -not $h.healthy) {
            Write-Output (@{ base_url = $baseUrl; healthy = $false } | ConvertTo-Json)
            exit 1
        }
        $h | ConvertTo-Json -Depth 4
        exit 0
    }

    'Stop' {
        $state = Get-CurrentState
        if ($state) {
            # Only stop a process that is still the one we launched (same exe and
            # start time); a recycled PID or a foreign listener is left alone.
            if (Test-RpcOwnedProcess -ProcessId ([int]$state.pid) -ExePath ([string]$state.exe) -StartedUtc ([string]$state.startedUtc)) {
                Stop-Process -Id ([int]$state.pid) -Force -ErrorAction SilentlyContinue
                Wait-Process -Id ([int]$state.pid) -Timeout 10 -ErrorAction SilentlyContinue
                Write-Output "Stopped sharded llama-server ($baseUrl)"
            }
            else {
                Write-Warning "PID $($state.pid) is not the llama-server recorded in the state file; not stopping it."
            }
        }
        else {
            Write-Output "No tracked sharded llama-server to stop ($baseUrl)"
        }
        Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue
        exit 0
    }

    'Start' {
        if (-not $ModelPath.Trim() -and $OllamaModel.Trim()) {
            $ModelPath = Resolve-OllamaBlob $OllamaModel.Trim()
        }
        if (-not $ModelPath.Trim()) {
            throw 'Provide -ModelPath <path-to-model.gguf> or -OllamaModel <ollama-tag>.'
        }
        if (-not (Test-Path -LiteralPath $ModelPath)) {
            throw "Model GGUF not found: $ModelPath"
        }

        $llamaExe = Join-Path $InstallDir 'llama-server.exe'
        if (-not (Test-Path -LiteralPath $llamaExe)) {
            throw "llama-server.exe not found under '$InstallDir'."
        }

        foreach ($peer in $validatedPeers) {
            if (-not (Test-RpcTcpReachable -TargetHost $peer.Host -TargetPort $peer.Port)) {
                throw "RPC peer $($peer.Address) is unreachable. Start rpc-server on that worker first (start-rpc-worker.ps1 -Action Start)."
            }
        }

        $existing = Get-CurrentState
        $existingHealth = Get-ServerHealth $baseUrl
        if ($existing -and $existingHealth -and $existingHealth.healthy) {
            Write-Output ("Sharded llama-server already healthy at $baseUrl (PID $($existing.pid))")
        }
        else {
            if (Test-RpcTcpReachable -TargetHost $loopbackHost -TargetPort $Port -TimeoutMs 500) {
                throw "Port $Port on $loopbackHost is already held by another process."
            }

            $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
            $stdout = Join-Path $logDir "llama-rpc-$stamp.stdout.log"
            $stderr = Join-Path $logDir "llama-rpc-$stamp.stderr.log"
            $rpcArg = ($validatedPeers | ForEach-Object { $_.Address }) -join ','

            $serverArgs = @(
                '--model', $ModelPath,
                '--alias', $Alias,
                '--host', $loopbackHost,
                '--port', [string]$Port,
                '--rpc', $rpcArg,
                '--ctx-size', [string]$CtxSize,
                '--n-gpu-layers', [string]$GpuLayers,
                '--flash-attn', $FlashAttn,
                '--cache-type-k', $CacheTypeK,
                '--cache-type-v', $CacheTypeV,
                '--parallel', [string]$Parallel,
                '-lv', [string]$LogVerbosity,
                '--no-webui'
            )
            if ($TensorSplit.Trim()) {
                $serverArgs += @('--tensor-split', $TensorSplit.Trim())
            }

            $proc = Start-Process -FilePath $llamaExe `
                -ArgumentList (ConvertTo-RpcArgumentString $serverArgs) `
                -WorkingDirectory $InstallDir `
                -RedirectStandardOutput $stdout -RedirectStandardError $stderr `
                -WindowStyle Hidden -PassThru
            $launchedPid = [int]$proc.Id
            $startedUtc = Get-RpcProcessStartUtc -ProcessId $launchedPid

            $deadline = [DateTime]::UtcNow.AddSeconds($ReadyWaitSeconds)
            $ready = $null
            while ([DateTime]::UtcNow -lt $deadline) {
                $h = Get-ServerHealth $baseUrl
                if ($null -ne $h -and $h.healthy) {
                    $ready = $h
                    break
                }
                $alive = Get-Process -Id $launchedPid -ErrorAction SilentlyContinue
                if ($null -eq $alive) {
                    $tail = if (Test-Path -LiteralPath $stderr) { (Get-Content -LiteralPath $stderr -Tail 30) -join "`n" } else { '' }
                    throw "llama-server exited during startup:`n$tail"
                }
                Start-Sleep -Milliseconds 750
            }
            if ($null -eq $ready) {
                if (Test-RpcOwnedProcess -ProcessId $launchedPid -ExePath $llamaExe -StartedUtc $startedUtc) {
                    Stop-Process -Id $launchedPid -Force -ErrorAction SilentlyContinue
                }
                throw "Sharded llama-server did not become healthy within ${ReadyWaitSeconds}s. See $stderr"
            }

            $stateObj = [ordered]@{
                pid        = $launchedPid
                exe        = $llamaExe
                startedUtc = $startedUtc
                baseUrl    = $baseUrl
                modelPath  = $ModelPath
                alias      = $Alias
                rpcPeers   = @($validatedPeers | ForEach-Object { $_.Address })
                args       = $serverArgs
                stdout     = $stdout
                stderr     = $stderr
                startedAt  = (Get-Date).ToString('o')
            }
            $stateObj | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $statePath -Encoding UTF8
            $stateObj | ConvertTo-Json -Depth 4
        }

        if ($LaunchSonder -ne 'none') {
            $envMap = Get-SonderRpcEnv
            foreach ($entry in $envMap.GetEnumerator()) {
                [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, 'Process')
            }
            Set-Location -LiteralPath $SonderRuntimeRoot
            & $PythonExe -m sonder_runtime $LaunchSonder
            exit $LASTEXITCODE
        }
    }
}
