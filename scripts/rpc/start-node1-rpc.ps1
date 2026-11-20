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
#
# The comm port is protected the same way. In the pinned llama.cpp b12494,
# when Main's client has 2+ distinct RPC endpoints (and GGML_RPC_NO_COMM is
# unset) it sends RPC_CMD_COMM_INIT with port = rank-0 port + 1000
# (ggml-rpc.cpp:3258), and the rank-0 rpc-server then opens a second,
# unauthenticated listener on 0.0.0.0:<that port> (ggml-rpc.cpp:2352). The main
# listener keeps --host.

$script:MainPeerAddress = '10.77.0.1'
$script:RpcCommPortOffset = 1000

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

$script:RpcServerExeNames = @('rpc-server.exe', 'ggml-rpc-server.exe')

function Test-RpcFwProgramIsRpc([string]$Program, [hashtable]$ExeSet) {
    # Matched by file name too, so a rule written through another directory form
    # (8.3 directory, junction, \Device\HarddiskVolumeN, another install dir)
    # still counts as an rpc-server rule.
    if ($ExeSet.ContainsKey((ConvertTo-RpcFwPath $Program).ToLowerInvariant())) { return $true }
    $leaf = ([string](@($Program.Trim().Trim('"') -split '[\\/]') | Select-Object -Last 1)).Trim()
    if ($leaf -in $script:RpcServerExeNames) { return $true }
    # An 8.3 short file name (e.g. RPC-SE~1.EXE) cannot be compared by name: fail closed.
    if ($leaf -match '~\d+(\.[^.\\/]*)?$') { return $true }
    return $false
}

function Get-RpcFwRuleFix($Rule) {
    # Where the rule comes from decides how it can be turned off.
    $name = [string]$Rule.Name
    $source = ([string]$Rule.PolicyStoreSource).Trim()
    $type = ([string]$Rule.PolicyStoreSourceType).Trim()
    if ($type -in @('', 'Local', 'HostFirewallLocal')) {
        # PowerShell single-quoted string: a ' inside the name is written as ''.
        return "Disable-NetFirewallRule -Name '$($name -replace "'", "''")'"
    }
    if ($type -in @('GroupPolicy', 'HostFirewallGroupPolicy')) {
        return "rule '$name' comes from Group Policy '$source', so Disable-NetFirewallRule on this machine will not stick: disable or narrow it in that GPO (GPMC / gpedit.msc), then gpupdate /force"
    }
    if ($type -in @('MDM', 'HostFirewallMDM')) {
        return "rule '$name' is pushed by MDM ('$source'): disable or narrow it in the MDM firewall policy, then sync the device"
    }
    return "rule '$name' is not a local rule (PolicyStoreSourceType=$type, PolicyStoreSource='$source'): disable or narrow it where it is defined"
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

function Get-RpcFwRuleKeywords($Rule) {
    # A rule's dynamic keyword addresses widen its remote address condition: the
    # rule also admits every address the keywords resolve to. They live on the
    # rule object (RemoteDynamicKeywordAddresses), not on its address filter.
    $id = [string]$Rule.InstanceID
    try {
        $prop = $Rule.PSObject.Properties['RemoteDynamicKeywordAddresses']
        if ($null -eq $prop) {
            throw 'the rule object has no RemoteDynamicKeywordAddresses property'
        }
        return @(ConvertTo-RpcFwTokens $prop.Value)
    }
    catch {
        throw "Refusing to start rpc-server: cannot read RemoteDynamicKeywordAddresses of firewall rule '$($Rule.DisplayName)' [$id]: $($_.Exception.Message)."
    }
}

function Get-RpcFwPortProxyEntries {
    # Parses `netsh interface portproxy show all`. Each section starts with a
    # header such as "Listen on ipv4:  Connect to ipv4:", then a column header
    # row, a dashed separator and rows of "<listen addr> <port> <connect addr> <port>".
    # Throws on a netsh failure or on any line it cannot classify.
    $global:LASTEXITCODE = 0
    try {
        $out = @(& netsh interface portproxy show all 2>&1)
    }
    catch {
        throw "cannot run 'netsh interface portproxy show all': $($_.Exception.Message)"
    }
    $code = $LASTEXITCODE
    $lines = @($out | ForEach-Object { ([string]$_).Trim() })
    if ($code -ne 0) {
        throw "'netsh interface portproxy show all' exited with code ${code}: $(($lines | Where-Object { $_ -ne '' }) -join ' ')"
    }
    $entries = @()
    $unparsed = @()
    $section = ''
    for ($i = 0; $i -lt $lines.Count; $i++) {
        $line = $lines[$i]
        if ($line -eq '') { continue }
        if ($line -match '^-[-\s]*$') { continue }
        $next = ''
        for ($j = $i + 1; $j -lt $lines.Count; $j++) {
            if ($lines[$j] -ne '') { $next = $lines[$j]; break }
        }
        if ($next -match '^-[-\s]*$') { continue }  # column header row
        if ($line -match '(?i)ipv([46])\s*:.*ipv([46])\s*:\s*$') {
            $section = "v$($Matches[1])tov$($Matches[2])"
            continue
        }
        $tokens = @($line -split '\s+')
        if ($tokens.Count -eq 4 -and $tokens[1] -match '^\d{1,5}$' -and $tokens[3] -match '^\d{1,5}$') {
            $entries += [pscustomobject]@{
                Section        = $section
                ListenAddress  = $tokens[0]
                ListenPort     = [int]$tokens[1]
                ConnectAddress = $tokens[2]
                ConnectPort    = [int]$tokens[3]
            }
            continue
        }
        $unparsed += $line
    }
    if ($unparsed.Count -gt 0) {
        throw "cannot parse the output of 'netsh interface portproxy show all' (line(s): $(($unparsed | ForEach-Object { "'$_'" }) -join ', '))"
    }
    return $entries
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
    $commPort = $TargetPort + $script:RpcCommPortOffset
    if ($commPort -gt 65535) {
        throw "${prefix}: the rpc-server comm port (${TargetPort} + $($script:RpcCommPortOffset) = $commPort) is above 65535, so the port the COMM_INIT listener would bind cannot be checked. Use a port of 64535 or lower."
    }

    # The rules below only filter while the Windows Defender Firewall service runs.
    try {
        $svc = Get-Service -Name mpssvc -ErrorAction Stop
    }
    catch {
        throw "${prefix}: cannot read the state of the Windows Defender Firewall service (mpssvc): $($_.Exception.Message)"
    }
    $svcStatus = [string](@($svc) | Select-Object -First 1).Status
    if ($svcStatus -ne 'Running') {
        throw "${prefix}: the Windows Defender Firewall service (mpssvc) is not running (Status=$svcStatus), so no firewall rule is enforced. Fix (elevated): Start-Service mpssvc"
    }

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

    # A profile's DisabledInterfaceAliases switches the firewall off for those
    # interfaces. rpc-server's comm listener binds 0.0.0.0 (all interfaces), so
    # any excluded interface, whatever it names, would leave it unfiltered: no
    # entry other than NotConfigured is allowed. Both stores are read and their
    # lists united: ActiveStore is the effective policy (GPO included) and
    # PersistentStore is the local setting that will apply after a refresh.
    $excluded = @()
    foreach ($store in @('ActiveStore', 'PersistentStore')) {
        if ($store -eq 'ActiveStore') {
            $storeProfiles = $profiles
        }
        else {
            try {
                $storeProfiles = @(Get-NetFirewallProfile -PolicyStore $store -ErrorAction Stop)
            }
            catch {
                throw "${prefix}: cannot read Windows Firewall profiles from the $store (needed for DisabledInterfaceAliases): $($_.Exception.Message)"
            }
            if ($storeProfiles.Count -eq 0) {
                throw "${prefix}: Windows Firewall returned no profiles from the $store, so DisabledInterfaceAliases cannot be verified."
            }
        }
        $noAliasList = @($storeProfiles | Where-Object { $null -eq $_.PSObject.Properties['DisabledInterfaceAliases'] })
        if ($noAliasList.Count -gt 0) {
            $names = ($noAliasList | ForEach-Object { [string]$_.Name }) -join ', '
            throw "${prefix}: cannot read DisabledInterfaceAliases of Windows Firewall profile(s) in the ${store}: $names, so it cannot be verified that the firewall filters every interface."
        }
        foreach ($fwProfile in $storeProfiles) {
            foreach ($alias in @(ConvertTo-RpcFwTokens $fwProfile.DisabledInterfaceAliases)) {
                if ($alias -eq 'NotConfigured') { continue }
                $excluded += [pscustomobject]@{ Store = $store; Profile = [string]$fwProfile.Name; Alias = $alias }
            }
        }
    }
    if ($excluded.Count -gt 0) {
        $listed = ($excluded | ForEach-Object { "$($_.Store) $($_.Profile): '$($_.Alias)'" }) -join ', '
        $profileNames = ($excluded | ForEach-Object { $_.Profile } | Select-Object -Unique) -join ','
        $fix = "Fix (elevated): Set-NetFirewallProfile -Name $profileNames -DisabledInterfaceAliases NotConfigured (clears each listed profile's whole exclusion list); an entry listed only for the ActiveStore comes from Group Policy or MDM, so remove it there (then gpupdate /force)"
        throw "${prefix}: Windows Firewall profile(s) exclude interfaces from filtering (DisabledInterfaceAliases): $listed. Traffic on an excluded interface is not filtered at all, and rpc-server's comm listener binds 0.0.0.0 (every interface), so no exclusion is allowed. $fix"
    }

    # A portproxy entry relays traffic it accepts to its connect address as a
    # new local connection, which the inbound rules below do not filter.
    try {
        $proxies = @(Get-RpcFwPortProxyEntries)
    }
    catch {
        throw "${prefix}: cannot verify that no port proxy forwards to rpc-server: $($_.Exception.Message)"
    }
    $proxyHits = @($proxies | Where-Object {
        $_.ListenPort -in @($TargetPort, $commPort) -or $_.ConnectPort -in @($TargetPort, $commPort)
    })
    if ($proxyHits.Count -gt 0) {
        $listed = ($proxyHits | ForEach-Object {
            $type = if ($_.Section) { $_.Section } else { '<v4tov4|v4tov6|v6tov4|v6tov6>' }
            "[$type] listen $($_.ListenAddress):$($_.ListenPort) -> connect $($_.ConnectAddress):$($_.ConnectPort) (remove, elevated: netsh interface portproxy delete $type listenport=$($_.ListenPort) listenaddress=$($_.ListenAddress))"
        }) -join '; '
        throw "${prefix}: netsh interface portproxy has entries that listen on or connect to TCP $TargetPort or the rpc-server comm port TCP ${commPort}: $listed. A port proxy relays connections as local traffic that the firewall rules do not filter, so none is allowed for these ports."
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
        $programIsRpc = (-not $programAny) -and (Test-RpcFwProgramIsRpc $program $exeSet)
        $package = ([string]$app.Package).Trim()
        $packaged = ($package -ne '' -and $package -ne 'Any') -or ([string]$rule.PackageFamilyName).Trim() -ne ''
        # Rules bound to another program, or to a packaged (AppContainer) app, cannot admit rpc-server traffic.
        if (-not $programIsRpc -and (-not $programAny -or $packaged)) { continue }

        $portFilter = Get-RpcFwRuleFilter $rule $portMap 'Port'
        $addrFilter = Get-RpcFwRuleFilter $rule $addrMap 'Address'
        $keywords = @(Get-RpcFwRuleKeywords $rule)
        # Any dynamic keyword also admits the addresses it resolves to: never peer-only.
        $remotePeerOnly = ($keywords.Count -eq 0) -and (Test-RpcFwSingleHost $addrFilter.RemoteAddress $peer)
        $coversTcpPort = (Test-RpcFwProtocolCoversTcp $portFilter.Protocol) -and
            ((Test-RpcFwPortCovers $portFilter.LocalPort $TargetPort) -or (Test-RpcFwPortCovers $portFilter.LocalPort $commPort))

        if (-not $remotePeerOnly -and ($programIsRpc -or $coversTcpPort)) {
            $wideRules += [pscustomobject]@{
                Name          = [string]$rule.Name
                DisplayName   = [string]$rule.DisplayName
                Protocol      = (ConvertTo-RpcFwTokens $portFilter.Protocol) -join ','
                LocalPort     = (ConvertTo-RpcFwTokens $portFilter.LocalPort) -join ','
                RemoteAddress = (ConvertTo-RpcFwTokens $addrFilter.RemoteAddress) -join ','
                Keywords      = $keywords -join ','
                Program       = $program
                Source        = ([string]$rule.PolicyStoreSource).Trim()
                SourceType    = ([string]$rule.PolicyStoreSourceType).Trim()
                Fix           = Get-RpcFwRuleFix $rule
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
            $kw = if ($_.Keywords) { "; RemoteDynamicKeywordAddresses=$($_.Keywords)" } else { '' }
            "'$($_.DisplayName)' [Name=$($_.Name); Protocol=$($_.Protocol); LocalPort=$($_.LocalPort); RemoteAddress=$($_.RemoteAddress)$kw; Program=$($_.Program); PolicyStoreSource=$($_.Source); PolicyStoreSourceType=$($_.SourceType)]"
        }) -join '; '
        $kwNote = ''
        if (@($wideRules | Where-Object { $_.Keywords }).Count -gt 0) {
            $kwNote = " A rule with RemoteDynamicKeywordAddresses also admits every address its dynamic keywords resolve to, so it is never peer-only, even when its RemoteAddress is exactly ${peer}."
        }
        $disable = ($wideRules | ForEach-Object { $_.Fix }) -join '; '
        $problems += "enabled inbound Allow rule(s) admit TCP $TargetPort, the rpc-server comm port TCP $commPort (opened on 0.0.0.0 on COMM_INIT) or the rpc-server program from beyond ${peer}: $listed.$kwNote Fix (elevated): $disable"
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

function Get-RpcServerListener {
    return @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalAddress -eq $bindHost })
}

function Stop-RpcServerInstance {
    # Used by -Action Stop and by a refused Start that finds an instance running.
    $state = Get-CurrentState
    if ($state) {
        Stop-Process -Id ([int]$state.pid) -Force -ErrorAction SilentlyContinue
        Wait-Process -Id ([int]$state.pid) -Timeout 10 -ErrorAction SilentlyContinue
        if ($state.wrapperPid) {
            Stop-Process -Id ([int]$state.wrapperPid) -Force -ErrorAction SilentlyContinue
        }
    }
    Get-RpcServerListener | ForEach-Object {
        Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue
}

function Stop-RpcProcessTree([int]$RootPid) {
    # Stops $RootPid and every process descended from it (the powershell
    # wrapper and the rpc-server it runs). A process counts as a child only if
    # it was created after its parent, so a recycled parent PID cannot pull in
    # an unrelated process. Returns '' or a note when the tree could not be read.
    $note = ''
    $tree = @($RootPid)
    try {
        $all = @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop)
        $byPid = @{}
        foreach ($proc in $all) { $byPid[[int]$proc.ProcessId] = $proc }
        $queue = [System.Collections.Queue]::new()
        $queue.Enqueue($RootPid)
        $seen = @{ $RootPid = $true }
        while ($queue.Count -gt 0) {
            $parentPid = [int]$queue.Dequeue()
            $parent = $byPid[$parentPid]
            foreach ($proc in $all) {
                $childPid = [int]$proc.ProcessId
                if ([int]$proc.ParentProcessId -ne $parentPid -or $seen.ContainsKey($childPid)) { continue }
                if ($null -ne $parent -and $null -ne $parent.CreationDate -and $null -ne $proc.CreationDate -and
                    ([datetime]$proc.CreationDate) -lt ([datetime]$parent.CreationDate)) { continue }
                $seen[$childPid] = $true
                $tree += $childPid
                $queue.Enqueue($childPid)
            }
        }
    }
    catch {
        $note = " Could not list its child processes ($($_.Exception.Message)), so only PID $RootPid was stopped: check for a stray rpc-server process."
    }
    # Deepest first, so a parent cannot start anything new after its child is gone.
    [array]::Reverse($tree)
    foreach ($id in $tree) {
        Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
    }
    return $note
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
        Stop-RpcServerInstance
        Write-Output "Stopped Node1 rpc-server on ${bindHost}:${Port}"
    }

    'Start' {
        $fwScript = Join-Path $PSScriptRoot 'ensure-node1-rpc-fw.ps1'
        try {
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

            # Always verified, with or without -EnsureFirewall, and before the
            # "already running" exit below.
            Assert-RpcFirewallLockdown -BindHost $bindHost -TargetPort $Port `
                -ProgramPaths @($script:RpcServerExeNames | ForEach-Object { Join-Path $InstallDir $_ }) `
                -FixCommand ("powershell -NoProfile -File `"$fwScript`" -Port $Port (or re-run Start with -EnsureFirewall from an elevated shell)")
        }
        catch {
            # An instance that is already up is exposed by the same firewall
            # state, so a refusal stops it as well (same logic as -Action Stop).
            $refusal = $_.Exception.Message
            $running = Get-CurrentState
            $listening = @(Get-RpcServerListener)
            if ($null -eq $running -and $listening.Count -eq 0) { throw }
            $pids = @(@($running | Where-Object { $null -ne $_ } | ForEach-Object { [string]$_.pid }) +
                @($listening | ForEach-Object { [string]$_.OwningProcess }) | Select-Object -Unique) -join ', '
            Stop-RpcServerInstance
            $left = @(Get-RpcServerListener)
            if ($left.Count -gt 0) {
                $leftPids = (@($left | ForEach-Object { [string]$_.OwningProcess }) | Select-Object -Unique) -join ', '
                throw "$refusal ALSO: an rpc-server instance was already running (PID $pids) on ${bindHost}:${Port} and is exposed by this firewall state; stopping it FAILED (still listening: PID $leftPids). Stop it now: Stop-Process -Id $leftPids -Force"
            }
            throw "$refusal ALSO: an rpc-server instance was already running (PID $pids) on ${bindHost}:${Port} and was exposed by this firewall state, so it has been stopped."
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
            # The wrapper and any rpc-server child it started (which may have bound elsewhere).
            $treeNote = Stop-RpcProcessTree $wrapperPid
            throw "Node1 rpc-server did not start listening on ${bindHost}:${Port} within 20s; stopped the wrapper (PID $wrapperPid) and its child processes.$treeNote"
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
