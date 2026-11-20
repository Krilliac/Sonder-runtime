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

# --- Firewall lockdown check (fails closed) -------------------------------
# rpc-server is unauthenticated, so Start refuses to launch unless Windows
# Firewall admits TCP $Port only from Main (10.77.0.1). Rules are read from the
# ActiveStore (effective policy, including GPO rules).

$script:MainPeerAddress = '10.77.0.1'

function ConvertTo-RpcFwTokens($Value) {
    return @(@($Value) | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ -ne '' })
}

function Test-RpcFwSingleHost($Spec, [string]$Address) {
    # True only when the address condition is exactly one host: $Address.
    $tokens = @(ConvertTo-RpcFwTokens $Spec)
    if ($tokens.Count -ne 1) { return $false }
    return ($tokens[0] -in @($Address, "$Address/32", "$Address/255.255.255.255"))
}

function Test-RpcFwProtocolCoversTcp($Protocol) {
    $tokens = @(ConvertTo-RpcFwTokens $Protocol)
    if ($tokens.Count -eq 0) { return $true }  # unreadable: fail closed
    foreach ($t in $tokens) {
        if ($t -in @('TCP', '6', 'Any')) { return $true }
    }
    return $false
}

function Test-RpcFwPortCovers($LocalPort, [int]$TargetPort) {
    $tokens = @(ConvertTo-RpcFwTokens $LocalPort)
    if ($tokens.Count -eq 0) { return $true }  # unreadable: fail closed
    foreach ($t in $tokens) {
        if ($t -eq 'Any') { return $true }
        if ($t -match '^\d+$') {
            if ([int]$t -eq $TargetPort) { return $true }
            continue
        }
        if ($t -match '^(\d+)\s*-\s*(\d+)$') {
            if ($TargetPort -ge [int]$Matches[1] -and $TargetPort -le [int]$Matches[2]) { return $true }
            continue
        }
        # Keyword ports (RPC, RPCEPMap, IPHTTPSIn, ...) cannot be evaluated here: fail closed.
        return $true
    }
    return $false
}

function ConvertTo-RpcFwPath([string]$Path) {
    $expanded = [Environment]::ExpandEnvironmentVariables($Path.Trim())
    try {
        return [System.IO.Path]::GetFullPath($expanded)
    }
    catch {
        return $expanded
    }
}

function Get-RpcFwFilterMap([string]$Kind) {
    # Bulk read; non-admin shells may get a partial list (some entries are access
    # denied), so callers fall back to a per-rule read for anything missing.
    $items = switch ($Kind) {
        'Port' { @(Get-NetFirewallPortFilter -All -PolicyStore ActiveStore -ErrorAction SilentlyContinue) }
        'Address' { @(Get-NetFirewallAddressFilter -All -PolicyStore ActiveStore -ErrorAction SilentlyContinue) }
        'Application' { @(Get-NetFirewallApplicationFilter -All -PolicyStore ActiveStore -ErrorAction SilentlyContinue) }
    }
    $map = @{}
    foreach ($item in @($items)) {
        if ($null -ne $item -and $item.InstanceID) { $map[[string]$item.InstanceID] = $item }
    }
    return $map
}

function Get-RpcFwRuleFilter($Rule, [hashtable]$Map, [string]$Kind) {
    $id = [string]$Rule.InstanceID
    if ($Map.ContainsKey($id)) { return $Map[$id] }
    try {
        $filter = switch ($Kind) {
            'Port' { $Rule | Get-NetFirewallPortFilter -ErrorAction Stop }
            'Address' { $Rule | Get-NetFirewallAddressFilter -ErrorAction Stop }
            'Application' { $Rule | Get-NetFirewallApplicationFilter -ErrorAction Stop }
        }
    }
    catch {
        throw "Refusing to start rpc-server: cannot read the $Kind filter of firewall rule '$($Rule.DisplayName)' [$id]: $($_.Exception.Message). Run Start from an elevated shell."
    }
    $filter = @($filter) | Select-Object -First 1
    if ($null -eq $filter) {
        throw "Refusing to start rpc-server: firewall rule '$($Rule.DisplayName)' [$id] has no readable $Kind filter."
    }
    return $filter
}

function Assert-RpcFirewallLockdown {
    param(
        [Parameter(Mandatory = $true)][string]$BindHost,
        [Parameter(Mandatory = $true)][int]$TargetPort,
        [Parameter(Mandatory = $true)][string[]]$ProgramPaths,
        [Parameter(Mandatory = $true)][string]$FixCommand
    )
    $peer = $script:MainPeerAddress
    $prefix = "Refusing to start rpc-server on ${BindHost}:${TargetPort}"

    try {
        $profiles = @(Get-NetFirewallProfile -PolicyStore ActiveStore -ErrorAction Stop)
    }
    catch {
        throw "${prefix}: cannot read Windows Firewall profiles: $($_.Exception.Message)"
    }
    if ($profiles.Count -eq 0) {
        throw "${prefix}: Windows Firewall returned no profiles, so the peer-only rule cannot be verified."
    }
    $openProfiles = @($profiles | Where-Object {
        ([string]$_.Enabled) -ne 'True' -or ([string]$_.DefaultInboundAction) -eq 'Allow'
    })
    if ($openProfiles.Count -gt 0) {
        $names = ($openProfiles | ForEach-Object { "$($_.Name) (Enabled=$($_.Enabled), DefaultInboundAction=$($_.DefaultInboundAction))" }) -join ', '
        throw "${prefix}: Windows Firewall is off or allows inbound by default for: $names. Fix (elevated): Set-NetFirewallProfile -All -Enabled True -DefaultInboundAction Block"
    }

    $rules = @()
    try {
        $rules = @(Get-NetFirewallRule -PolicyStore ActiveStore -Direction Inbound -Enabled True -Action Allow -ErrorAction Stop)
    }
    catch {
        if ($_.CategoryInfo.Category -ne [System.Management.Automation.ErrorCategory]::ObjectNotFound) {
            throw "${prefix}: cannot read Windows Firewall rules: $($_.Exception.Message)"
        }
        $rules = @()
    }
    $rules = @($rules | Where-Object {
        $null -ne $_ -and ([string]$_.Enabled) -eq 'True' -and ([string]$_.Direction) -eq 'Inbound' -and ([string]$_.Action) -eq 'Allow'
    })

    $exeSet = @{}
    foreach ($p in $ProgramPaths) { $exeSet[(ConvertTo-RpcFwPath $p).ToLowerInvariant()] = $true }

    $appMap = Get-RpcFwFilterMap 'Application'
    $portMap = Get-RpcFwFilterMap 'Port'
    $addrMap = Get-RpcFwFilterMap 'Address'

    $peerRules = @()
    $wideRules = @()
    foreach ($rule in $rules) {
        $app = Get-RpcFwRuleFilter $rule $appMap 'Application'
        $program = ([string]$app.Program).Trim()
        $programAny = ($program -eq '' -or $program -eq 'Any')
        $programIsRpc = (-not $programAny) -and $exeSet.ContainsKey((ConvertTo-RpcFwPath $program).ToLowerInvariant())
        $package = ([string]$app.Package).Trim()
        $packaged = ($package -ne '' -and $package -ne 'Any') -or ([string]$rule.PackageFamilyName).Trim() -ne ''
        # Rules bound to another program, or to a packaged (AppContainer) app, cannot admit rpc-server traffic.
        if (-not $programIsRpc -and (-not $programAny -or $packaged)) { continue }

        $portFilter = Get-RpcFwRuleFilter $rule $portMap 'Port'
        $addrFilter = Get-RpcFwRuleFilter $rule $addrMap 'Address'
        $remotePeerOnly = Test-RpcFwSingleHost $addrFilter.RemoteAddress $peer
        $coversTcpPort = (Test-RpcFwProtocolCoversTcp $portFilter.Protocol) -and (Test-RpcFwPortCovers $portFilter.LocalPort $TargetPort)

        if (-not $remotePeerOnly -and ($programIsRpc -or $coversTcpPort)) {
            $wideRules += [pscustomobject]@{
                Name          = [string]$rule.Name
                DisplayName   = [string]$rule.DisplayName
                Protocol      = (ConvertTo-RpcFwTokens $portFilter.Protocol) -join ','
                LocalPort     = (ConvertTo-RpcFwTokens $portFilter.LocalPort) -join ','
                RemoteAddress = (ConvertTo-RpcFwTokens $addrFilter.RemoteAddress) -join ','
                Program       = $program
            }
            continue
        }

        $protoTokens = @(ConvertTo-RpcFwTokens $portFilter.Protocol)
        $portTokens = @(ConvertTo-RpcFwTokens $portFilter.LocalPort)
        $isTcpOnly = ($protoTokens.Count -eq 1 -and $protoTokens[0] -in @('TCP', '6'))
        $isPortOnly = ($portTokens.Count -eq 1 -and $portTokens[0] -eq [string]$TargetPort)
        if ($isTcpOnly -and $isPortOnly -and $remotePeerOnly -and (Test-RpcFwSingleHost $addrFilter.LocalAddress $BindHost)) {
            $peerRules += $rule
        }
    }

    $problems = @()
    if ($wideRules.Count -gt 0) {
        $listed = ($wideRules | ForEach-Object {
            "'$($_.DisplayName)' [Name=$($_.Name); Protocol=$($_.Protocol); LocalPort=$($_.LocalPort); RemoteAddress=$($_.RemoteAddress); Program=$($_.Program)]"
        }) -join '; '
        $disable = ($wideRules | ForEach-Object { "Disable-NetFirewallRule -Name '$($_.Name)'" }) -join '; '
        $problems += "enabled inbound Allow rule(s) admit TCP $TargetPort or the rpc-server program from beyond ${peer}: $listed. Fix (elevated): $disable"
    }
    if ($peerRules.Count -eq 0) {
        $problems += "no enabled inbound Allow rule admits exactly TCP $TargetPort from $peer to $BindHost. Fix (elevated): $FixCommand"
    }
    if ($problems.Count -gt 0) {
        throw "${prefix}: " + ($problems -join ' AND ')
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
        $fwScript = Join-Path $PSScriptRoot 'ensure-node1-rpc-fw.ps1'
        if ($EnsureFirewall) {
            if (-not (Test-Path -LiteralPath $fwScript -PathType Leaf)) {
                throw "Refusing to start rpc-server: -EnsureFirewall was passed but '$fwScript' is missing. Restore ensure-node1-rpc-fw.ps1 next to start-node1-rpc.ps1."
            }
            $global:LASTEXITCODE = 0
            try {
                & $fwScript -Port $Port | Out-Null
            }
            catch {
                throw "Refusing to start rpc-server: '$fwScript -Port $Port' failed: $($_.Exception.Message)"
            }
            if ($LASTEXITCODE -ne 0) {
                throw "Refusing to start rpc-server: '$fwScript -Port $Port' exited with code $LASTEXITCODE."
            }
        }

        # Always verified, with or without -EnsureFirewall.
        Assert-RpcFirewallLockdown -BindHost $bindHost -TargetPort $Port `
            -ProgramPaths @((Join-Path $InstallDir 'rpc-server.exe'), (Join-Path $InstallDir 'ggml-rpc-server.exe')) `
            -FixCommand ("powershell -NoProfile -File `"$fwScript`" -Port $Port (or re-run Start with -EnsureFirewall from an elevated shell)")

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
