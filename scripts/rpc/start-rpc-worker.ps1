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
    [string]$AllowedPeer = '',
    [string]$LinkCidr = $env:SONDER_RPC_LINK_CIDR
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
$rpcLink = ConvertTo-RpcLinkNetwork $LinkCidr
$null = Assert-RpcAddressOnLink -Address $bindHost -Link $rpcLink -Label 'rpc-server bind address'
$rpcPort = if (([string]$Port).Trim()) { ConvertTo-RpcPort -Value $Port -Label 'Port' } else { $script:RpcDefaultPort }
if ($Threads -lt 0) { throw "Threads must be >= 0, got $Threads." }
$peerRaw = if (([string]$AllowedPeer).Trim()) { $AllowedPeer } else { [string]$env:SONDER_RPC_ALLOWED_PEER }
$peerHost = ''
if ($peerRaw.Trim()) {
    $peerHost = ConvertTo-RpcPrivateIPv4 -Address $peerRaw -Label 'AllowedPeer'
    $null = Assert-RpcAddressOnLink -Address $peerHost -Link $rpcLink -Label 'AllowedPeer'
    if ($peerHost -eq $bindHost) { throw 'BindAddress and AllowedPeer must differ.' }
}

# --- Firewall lockdown check (fails closed) -------------------------------
# rpc-server has no authentication, so Start refuses to launch unless Windows
# Firewall (mpssvc) is running, every profile filters every interface, no
# netsh portproxy forwards the RPC or comm port, and enabled inbound Allow rules
# admit TCP $rpcPort only from the single peer. Rules are read from ActiveStore
# (effective policy including GPO/MDM); profiles check both ActiveStore and
# PersistentStore for DisabledInterfaceAliases.
#
# In llama.cpp ggml-rpc, when Main's client has 2+ RPC endpoints and
# GGML_RPC_NO_COMM is unset, rank-0 opens an unauthenticated comm listener on
# 0.0.0.0:<Port + 1000>, so TCP <Port + 1000> is guarded identically.

$script:RpcCommPortOffset = 1000
$script:RpcServerExeNames = @('rpc-server.exe', 'ggml-rpc-server.exe')

function ConvertTo-RpcFwTokens($Value) {
    return @(@($Value) | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ -ne '' })
}

function Test-RpcFwSingleHost($Spec, [string]$Address) {
    $tokens = @(ConvertTo-RpcFwTokens $Spec)
    if ($tokens.Count -ne 1) { return $false }
    return ($tokens[0] -in @($Address, "$Address/32", "$Address/255.255.255.255"))
}

function Try-GetRpcFwSinglePrivateHost($Spec) {
    $tokens = @(ConvertTo-RpcFwTokens $Spec)
    if ($tokens.Count -ne 1) { return $null }
    $raw = $tokens[0]
    foreach ($suffix in @('/32', '/255.255.255.255')) {
        if ($raw.EndsWith($suffix, [System.StringComparison]::Ordinal)) {
            $raw = $raw.Substring(0, $raw.Length - $suffix.Length)
            break
        }
    }
    try {
        return (ConvertTo-RpcPrivateIPv4 -Address $raw -Label 'RemoteAddress')
    }
    catch {
        return $null
    }
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

function Test-RpcFwProgramIsRpc([string]$Program, [hashtable]$ExeSet) {
    if ($ExeSet.ContainsKey((ConvertTo-RpcFwPath $Program).ToLowerInvariant())) { return $true }
    $leaf = ([string](@($Program.Trim().Trim('"') -split '[\\/]') | Select-Object -Last 1)).Trim()
    if ($leaf -in $script:RpcServerExeNames) { return $true }
    # An 8.3 short file name (e.g. RPC-SE~1.EXE) cannot be compared by name: fail closed.
    if ($leaf -match '~\d+(\.[^.\\/]*)?$') { return $true }
    return $false
}

function Get-RpcFwRuleFix($Rule) {
    $name = [string]$Rule.Name
    $source = ([string]$Rule.PolicyStoreSource).Trim()
    $type = ([string]$Rule.PolicyStoreSourceType).Trim()
    if ($type -in @('', 'Local', 'HostFirewallLocal')) {
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
    $items = switch ($Kind) {
        'Port' { @(Get-NetFirewallPortFilter -All -PolicyStore ActiveStore -ErrorAction SilentlyContinue) }
        'Address' { @(Get-NetFirewallAddressFilter -All -PolicyStore ActiveStore -ErrorAction SilentlyContinue) }
        'Application' { @(Get-NetFirewallApplicationFilter -All -PolicyStore ActiveStore -ErrorAction SilentlyContinue) }
        'Interface' { @(Get-NetFirewallInterfaceFilter -All -PolicyStore ActiveStore -ErrorAction SilentlyContinue) }
        'InterfaceType' { @(Get-NetFirewallInterfaceTypeFilter -All -PolicyStore ActiveStore -ErrorAction SilentlyContinue) }
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
            'Interface' { $Rule | Get-NetFirewallInterfaceFilter -ErrorAction Stop }
            'InterfaceType' { $Rule | Get-NetFirewallInterfaceTypeFilter -ErrorAction Stop }
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

# The comm listener binds 0.0.0.0, so a peer rule is only peer-only when it is
# pinned to the ONE adapter that owns $BindHost. An alias of Any, a wildcard, an
# empty or unreadable value, a list of several aliases, or a different adapter
# admits the peer address on other interfaces, where overlapping addressing or a
# spoofed or routed packet can match it. The match is case-sensitive, as the
# installer pins the exact name. The interface-type filter must be readable too
# (Any is fine once the alias is pinned). Anything missing or unreadable is wide.
function Get-RpcFwBindAdapterAlias([string]$BindHost) {
    try {
        $aliases = @(Get-NetIPAddress -IPAddress $BindHost -ErrorAction Stop |
            ForEach-Object { ([string]$_.InterfaceAlias).Trim() } |
            Where-Object { $_ -ne '' } |
            Select-Object -Unique)
    }
    catch { return $null }
    if ($aliases.Count -ne 1) { return $null }
    return [string]$aliases[0]
}

function Get-RpcFwInterfaceState($Rule, [hashtable]$InterfaceMap, [hashtable]$TypeMap, $OwnerAlias) {
    $state = [pscustomobject]@{ Pinned = $false; Alias = '<unreadable>'; Type = '<unreadable>' }
    try {
        $ifaceFilter = Get-RpcFwRuleFilter $Rule $InterfaceMap 'Interface'
        $typeFilter = Get-RpcFwRuleFilter $Rule $TypeMap 'InterfaceType'
        $aliasProp = $ifaceFilter.PSObject.Properties['InterfaceAlias']
        $typeProp = $typeFilter.PSObject.Properties['InterfaceType']
        if ($null -eq $aliasProp -or $null -eq $typeProp) { return $state }
        $aliases = @(ConvertTo-RpcFwTokens $aliasProp.Value)
        $types = @(ConvertTo-RpcFwTokens $typeProp.Value)
        $state.Alias = $aliases -join ','
        $state.Type = $types -join ','
        if ($aliases.Count -eq 0 -or $types.Count -eq 0) { return $state }
        if ($null -eq $OwnerAlias) {
            $state.Alias = "$($state.Alias) (the adapter owning the bind address could not be resolved to exactly one alias)"
            return $state
        }
        if ($aliases.Count -ne 1) { return $state }
        $alias = [string]$aliases[0]
        if ($alias -ieq 'Any' -or [System.Management.Automation.WildcardPattern]::ContainsWildcardCharacters($alias)) { return $state }
        if ($alias -cne [string]$OwnerAlias) { return $state }
        $state.Pinned = $true
    }
    catch {
        $state.Alias = '<unreadable>'
        $state.Pinned = $false
    }
    return $state
}

function Get-RpcFwRuleKeywords($Rule) {
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
        [string]$PeerAddress = '',
        [Parameter(Mandatory = $true)][string[]]$ProgramPaths,
        [Parameter(Mandatory = $true)][string]$FixCommand
    )
    $prefix = "Refusing to start rpc-server on ${BindHost}:${TargetPort}"
    $commPort = $TargetPort + $script:RpcCommPortOffset
    if ($commPort -gt 65535) {
        throw "${prefix}: the rpc-server comm port (${TargetPort} + $($script:RpcCommPortOffset) = $commPort) is above 65535, so the port the COMM_INIT listener would bind cannot be checked. Use a port of 64535 or lower."
    }

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
    $interfaceMap = Get-RpcFwFilterMap 'Interface'
    $ownerAlias = Get-RpcFwBindAdapterAlias $BindHost
    $typeMap = Get-RpcFwFilterMap 'InterfaceType'

    $peerRules = @()
    $seenRemotePeers = @()
    $wideRules = @()
    $hasExplicitPeer = (-not [string]::IsNullOrEmpty($PeerAddress))
    foreach ($rule in $rules) {
        $app = Get-RpcFwRuleFilter $rule $appMap 'Application'
        $program = ([string]$app.Program).Trim()
        $programAny = ($program -eq '' -or $program -eq 'Any')
        $programIsRpc = (-not $programAny) -and (Test-RpcFwProgramIsRpc $program $exeSet)
        $package = ([string]$app.Package).Trim()
        $packaged = ($package -ne '' -and $package -ne 'Any') -or ([string]$rule.PackageFamilyName).Trim() -ne ''
        if (-not $programIsRpc -and (-not $programAny -or $packaged)) { continue }

        $portFilter = Get-RpcFwRuleFilter $rule $portMap 'Port'
        $addrFilter = Get-RpcFwRuleFilter $rule $addrMap 'Address'
        $keywords = @(Get-RpcFwRuleKeywords $rule)
        $coversTcpPort = (Test-RpcFwProtocolCoversTcp $portFilter.Protocol) -and
            ((Test-RpcFwPortCovers $portFilter.LocalPort $TargetPort) -or (Test-RpcFwPortCovers $portFilter.LocalPort $commPort))

        $matchedSinglePeer = $null
        if ($keywords.Count -eq 0) {
            if ($hasExplicitPeer) {
                if (Test-RpcFwSingleHost $addrFilter.RemoteAddress $PeerAddress) {
                    $matchedSinglePeer = $PeerAddress
                }
            }
            else {
                $candidatePeer = Try-GetRpcFwSinglePrivateHost $addrFilter.RemoteAddress
                if ($null -ne $candidatePeer -and $candidatePeer -ne $BindHost) {
                    $matchedSinglePeer = $candidatePeer
                }
            }
        }
        $remotePeerOnly = ($null -ne $matchedSinglePeer)
        $ifaceState = $null
        if ($remotePeerOnly -and ($programIsRpc -or $coversTcpPort)) {
            $ifaceState = Get-RpcFwInterfaceState $rule $interfaceMap $typeMap $ownerAlias
            if (-not $ifaceState.Pinned) { $remotePeerOnly = $false }
        }

        if (-not $remotePeerOnly -and ($programIsRpc -or $coversTcpPort)) {
            $wideRules += [pscustomobject]@{
                Name          = [string]$rule.Name
                DisplayName   = [string]$rule.DisplayName
                Protocol      = (ConvertTo-RpcFwTokens $portFilter.Protocol) -join ','
                LocalPort     = (ConvertTo-RpcFwTokens $portFilter.LocalPort) -join ','
                RemoteAddress = (ConvertTo-RpcFwTokens $addrFilter.RemoteAddress) -join ','
                Keywords      = $keywords -join ','
                InterfaceNote = $(if ($null -ne $ifaceState) { "; InterfaceAlias=$($ifaceState.Alias); InterfaceType=$($ifaceState.Type) (a peer rule must be pinned to the single interface alias that owns $BindHost)" } else { '' })
                Program       = $program
                Source        = ([string]$rule.PolicyStoreSource).Trim()
                SourceType    = ([string]$rule.PolicyStoreSourceType).Trim()
                Fix           = Get-RpcFwRuleFix $rule
            }
            continue
        }

        if ($remotePeerOnly -and ($programIsRpc -or $coversTcpPort)) {
            $seenRemotePeers += $matchedSinglePeer
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
    $peerLabel = if ($hasExplicitPeer) { $PeerAddress } else { 'a single private peer' }
    if ($wideRules.Count -gt 0) {
        $listed = ($wideRules | ForEach-Object {
            $kw = if ($_.Keywords) { "; RemoteDynamicKeywordAddresses=$($_.Keywords)" } else { '' }
            "'$($_.DisplayName)' [Name=$($_.Name); Protocol=$($_.Protocol); LocalPort=$($_.LocalPort); RemoteAddress=$($_.RemoteAddress)$kw$($_.InterfaceNote); Program=$($_.Program); PolicyStoreSource=$($_.Source); PolicyStoreSourceType=$($_.SourceType)]"
        }) -join '; '
        $kwNote = ''
        if (@($wideRules | Where-Object { $_.Keywords }).Count -gt 0) {
            $kwNote = " A rule with RemoteDynamicKeywordAddresses also admits every address its dynamic keywords resolve to, so it is never peer-only, even when its RemoteAddress is exactly ${peerLabel}."
        }
        $disable = ($wideRules | ForEach-Object { $_.Fix }) -join '; '
        $problems += "enabled inbound Allow rule(s) admit TCP $TargetPort, the rpc-server comm port TCP $commPort (opened on 0.0.0.0 on COMM_INIT) or the rpc-server program from beyond ${peerLabel}: $listed.$kwNote Fix (elevated): $disable"
    }
    if (-not $hasExplicitPeer) {
        $distinctPeers = @($seenRemotePeers | Select-Object -Unique)
        if ($distinctPeers.Count -gt 1) {
            $problems += "enabled inbound Allow rule(s) admit multiple peer addresses ($($distinctPeers -join ', ')); pass -AllowedPeer <main-ip> so Start can verify which single peer is intended"
            $peerRules = @()
        }
    }
    if ($peerRules.Count -eq 0) {
        $fromClause = if ($hasExplicitPeer) { "from $PeerAddress " } else { 'from a single private peer IPv4 host ' }
        $problems += "no enabled inbound Allow rule admits exactly TCP $TargetPort ${fromClause}to $BindHost. Fix (elevated): $FixCommand"
    }
    if ($problems.Count -gt 0) {
        throw "${prefix}: " + ($problems -join ' AND ')
    }
}

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

function Get-RpcWorkerListener {
    return @(Get-NetTCPConnection -State Listen -LocalPort $rpcPort -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalAddress -eq $bindHost })
}

function Stop-RpcWorkerInstance([switch]$Quiet) {
    $state = Get-CurrentState
    if ($state) {
        # Only stop a process that is still the one we launched (same exe and
        # start time); a recycled PID or a foreign listener is left alone.
        if (Test-RpcOwnedProcess -ProcessId ([int]$state.pid) -ExePath ([string]$state.exe) -StartedUtc ([string]$state.startedUtc)) {
            Stop-Process -Id ([int]$state.pid) -Force -ErrorAction SilentlyContinue
            Wait-Process -Id ([int]$state.pid) -Timeout 10 -ErrorAction SilentlyContinue
            if (-not $Quiet) { Write-Output "Stopped rpc-server on ${bindHost}:${rpcPort}" }
        }
        else {
            Write-Warning "PID $($state.pid) is not the rpc-server recorded in the state file; not stopping it."
        }
    }
    else {
        if (-not $Quiet) { Write-Output "No tracked rpc-server to stop on ${bindHost}:${rpcPort}" }
    }
    Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue
}

switch ($Action) {
    'Status' {
        $state = Get-CurrentState
        $listener = @(Get-RpcWorkerListener | Select-Object LocalAddress, LocalPort, OwningProcess)
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
        Stop-RpcWorkerInstance
    }

    'Start' {
        $fwScript = Join-Path $PSScriptRoot 'ensure-rpc-worker-firewall.ps1'
        try {
            if ($EnsureFirewall) {
                if (-not $peerHost) {
                    throw 'Refusing to start rpc-server: -EnsureFirewall needs -AllowedPeer <main-ip> (the only address allowed to connect).'
                }
                if (-not (Test-Path -LiteralPath $fwScript -PathType Leaf)) {
                    throw "Refusing to start rpc-server: -EnsureFirewall was passed but '$fwScript' is missing. Restore ensure-rpc-worker-firewall.ps1 next to start-rpc-worker.ps1."
                }
                $global:LASTEXITCODE = 0
                try {
                    & $fwScript -LocalAddress $bindHost -RemoteAddress $peerHost -Port $rpcPort | Out-Null
                }
                catch {
                    throw "Refusing to start rpc-server: '$fwScript -LocalAddress $bindHost -RemoteAddress $peerHost -Port $rpcPort' failed: $($_.Exception.Message)"
                }
                if ($LASTEXITCODE -ne 0) {
                    throw "Refusing to start rpc-server: '$fwScript -LocalAddress $bindHost -RemoteAddress $peerHost -Port $rpcPort' exited with code $LASTEXITCODE."
                }
            }

            $peerHint = if ($peerHost) { $peerHost } else { '<main-ip>' }
            # Always verified (with or without -EnsureFirewall) before returning "already running" or launching.
            Assert-RpcFirewallLockdown -BindHost $bindHost -TargetPort $rpcPort -PeerAddress $peerHost `
                -ProgramPaths @($script:RpcServerExeNames | ForEach-Object { Join-Path $InstallDir $_ }) `
                -FixCommand ("powershell -NoProfile -File `"$fwScript`" -LocalAddress $bindHost -RemoteAddress $peerHint -Port $rpcPort (or re-run Start with -EnsureFirewall -AllowedPeer $peerHint from an elevated shell)")
        }
        catch {
            $refusal = $_.Exception.Message
            $running = Get-CurrentState
            $listening = @(Get-RpcWorkerListener)
            if ($null -eq $running -and $listening.Count -eq 0) { throw }
            $pids = @(@($running | Where-Object { $null -ne $_ } | ForEach-Object { [string]$_.pid }) +
                @($listening | ForEach-Object { [string]$_.OwningProcess }) | Select-Object -Unique) -join ', '
            Stop-RpcWorkerInstance -Quiet
            $left = @(Get-RpcWorkerListener)
            if ($left.Count -gt 0) {
                $leftPids = (@($left | ForEach-Object { [string]$_.OwningProcess }) | Select-Object -Unique) -join ', '
                throw "$refusal ALSO: an rpc-server instance was already running (PID $pids) on ${bindHost}:${rpcPort} and is exposed by this firewall state; stopping it FAILED (still listening: PID $leftPids). Stop it now: Stop-Process -Id $leftPids -Force"
            }
            throw "$refusal ALSO: an rpc-server instance was already running (PID $pids) on ${bindHost}:${rpcPort} and was exposed by this firewall state, so it has been stopped."
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
        # rpc-server has no --version and cannot report its build over RPC, so the
        # worker's llama.cpp build comes from a sibling binary of the same release.
        # Read before launch so it never runs alongside rpc-server's device setup.
        $llamaBuild = $null
        foreach ($sibling in @('llama-server.exe', 'llama-cli.exe')) {
            $llamaBuild = Get-RpcLlamaBuild -ExePath (Join-Path $InstallDir $sibling)
            if ($llamaBuild) { break }
        }

        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        # Detached launch has no stdout/stderr capture (rpc-server has no verified
        # log-file option), so no log paths are recorded for the worker.
        $stdout = $null
        $stderr = $null

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

        $launchedPid = Start-RpcDetached -ExePath $exe `
            -ArgumentString (ConvertTo-RpcArgumentString $rpcArgs) `
            -WorkingDirectory $InstallDir
        $startedUtc = Get-RpcProcessStartUtc -ProcessId $launchedPid
        $recordedExe = Get-RpcProcessPath -ProcessId $launchedPid

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
                throw "rpc-server exited during startup."
            }
            Start-Sleep -Milliseconds 250
        }
        if (-not $ready -or $null -eq $rpcPid) {
            if (Test-RpcOwnedProcess -ProcessId $launchedPid -ExePath $recordedExe -StartedUtc $startedUtc) {
                Stop-Process -Id $launchedPid -Force -ErrorAction SilentlyContinue
            }
            throw "rpc-server did not start listening on ${bindHost}:${rpcPort} within 20s."
        }

        $stateObj = [ordered]@{
            pid        = $launchedPid
            exe        = $recordedExe
            startedUtc = $startedUtc
            llamaBuild = $llamaBuild
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
