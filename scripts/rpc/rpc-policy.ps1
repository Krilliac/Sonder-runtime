#Requires -Version 5.1
<#
.SYNOPSIS
  Shared fail-closed address and port policy for the llama.cpp RPC sharding scripts.
.DESCRIPTION
  Dot-sourced by start-rpc-worker.ps1, ensure-rpc-worker-firewall.ps1 and
  start-rpc-sharded.ps1. Mirrors sonder_runtime/platform/rpc_sharding_config.py:
  worker addresses must be literal IPv4 addresses inside 10.0.0.0/8,
  172.16.0.0/12, 192.168.0.0/16 or 169.254.0.0/16; hostnames, IPv6, loopback,
  0.0.0.0 and public addresses are refused; every peer uses the one configured
  RPC port. Keep the two implementations in step.
#>

$script:RpcDefaultPort = 50052
$script:RpcMaxPeers = 16

function ConvertTo-RpcPort([string]$Value, [string]$Label) {
    $text = ([string]$Value).Trim()
    if ($text -notmatch '^[0-9]{1,5}$') {
        throw "$Label must be a decimal port number, got '$Value'."
    }
    $port = [int]$text
    if ($port -lt 1 -or $port -gt 65535) {
        throw "$Label must be between 1 and 65535, got $port."
    }
    return $port
}

function ConvertTo-RpcPrivateIPv4([string]$Address, [string]$Label) {
    $trimmed = ([string]$Address).Trim()
    if (-not $trimmed) { throw "$Label must not be empty." }
    # Dotted quad without leading zeros: [IPAddress]::TryParse alone accepts
    # shorthand such as '10.1' and may read a leading zero as octal.
    if ($trimmed -notmatch '^(0|[1-9][0-9]{0,2})(\.(0|[1-9][0-9]{0,2})){3}$') {
        throw "$Label must be a literal IPv4 address (hostnames are refused), got '$Address'."
    }
    $ip = $null
    if (-not [System.Net.IPAddress]::TryParse($trimmed, [ref]$ip) -or
        $ip.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork -or
        $ip.IPAddressToString -ne $trimmed) {
        throw "$Label must be a literal IPv4 address, got '$Address'."
    }
    $b = $ip.GetAddressBytes()
    $allowed = ($b[0] -eq 10) -or
        ($b[0] -eq 172 -and $b[1] -ge 16 -and $b[1] -le 31) -or
        ($b[0] -eq 192 -and $b[1] -eq 168) -or
        ($b[0] -eq 169 -and $b[1] -eq 254)
    if (-not $allowed) {
        throw "$Label $trimmed is not a private (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16) or link-local (169.254.0.0/16) IPv4 address."
    }
    return $ip.IPAddressToString
}

function ConvertTo-RpcPeer([string]$Entry, [int]$Port) {
    $text = ([string]$Entry).Trim()
    if (-not $text) { throw 'RPC peer entry must not be empty.' }
    $parts = $text.Split(':')
    if ($parts.Count -gt 2) {
        throw "RPC peer '$Entry' must be an IPv4 literal with an optional :port."
    }
    $hostAddress = ConvertTo-RpcPrivateIPv4 -Address $parts[0] -Label 'RPC peer host'
    if ($parts.Count -eq 2) {
        $entryPort = ConvertTo-RpcPort -Value $parts[1] -Label "RPC peer port in '$Entry'"
        if ($entryPort -ne $Port) {
            throw "RPC peer '$Entry' uses port $entryPort, but the configured RPC port is $Port (SONDER_RPC_PORT / -RpcPort)."
        }
    }
    return [pscustomobject]@{ Host = $hostAddress; Port = $Port; Address = "${hostAddress}:$Port" }
}

function ConvertTo-RpcPeerList([string[]]$Entries, [int]$Port) {
    $tokens = @($Entries | ForEach-Object { ([string]$_).Replace(';', ',').Split(',') } |
        ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if ($tokens.Count -gt $script:RpcMaxPeers) {
        throw "At most $($script:RpcMaxPeers) RPC peers are allowed, got $($tokens.Count)."
    }
    $peers = @()
    foreach ($token in $tokens) {
        $peer = ConvertTo-RpcPeer -Entry $token -Port $Port
        if (-not ($peers | Where-Object { $_.Address -eq $peer.Address })) {
            $peers += $peer
        }
    }
    return $peers
}

function Get-RpcDefaultInstallDir {
    if (([string]$env:SONDER_RPC_INSTALL_DIR).Trim()) { return $env:SONDER_RPC_INSTALL_DIR.Trim() }
    if (-not $env:LOCALAPPDATA) { throw 'LOCALAPPDATA is not set; pass -InstallDir explicitly.' }
    return (Join-Path $env:LOCALAPPDATA 'sonder\llama-rpc')
}

function Test-RpcTcpReachable([string]$TargetHost, [int]$TargetPort, [int]$TimeoutMs = 1500) {
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

function ConvertTo-RpcArgumentString([string[]]$Arguments) {
    # Quote per the CommandLineToArgvW rules so a Win32_Process command line
    # reaches the child as exactly these argv entries on Windows PowerShell 5.1
    # and PowerShell 7 (5.1 joins an array with spaces and does not quote).
    $parts = foreach ($arg in $Arguments) {
        $text = [string]$arg
        if ($text.Length -gt 0 -and $text -notmatch '[\s"]') {
            $text
        }
        else {
            $escaped = [regex]::Replace($text, '(\\*)"', '$1$1\"')
            $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
            '"' + $escaped + '"'
        }
    }
    return ($parts -join ' ')
}

function Start-RpcDetached([string]$ExePath, [string]$ArgumentString, [string]$WorkingDirectory) {
    # Launch detached through Win32_Process.Create. Unlike Start-Process with
    # redirected output, the child inherits none of the caller's pipe handles and
    # is not in the caller's job object, so a one-shot `ssh host powershell -File`
    # or a piped CI/agent wrapper returns at once and the server outlives the
    # session. Returns the new process id. No PowerShell source string is built.
    $commandLine = '"' + $ExePath + '"'
    if ($ArgumentString) { $commandLine += ' ' + $ArgumentString }
    $created = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine      = $commandLine
        CurrentDirectory = $WorkingDirectory
    }
    if ($created.ReturnValue -ne 0) {
        throw "Win32_Process Create failed with return code $($created.ReturnValue)."
    }
    return [int]$created.ProcessId
}

function ConvertTo-RpcBaseUrl([string]$HostAddress, [int]$Port) {
    $hostText = ([string]$HostAddress).Trim()
    if ($hostText.Contains(':') -and -not $hostText.StartsWith('[')) { $hostText = "[$hostText]" }
    return "http://${hostText}:$Port"
}

function Assert-RpcBaseUrl([string]$BaseUrl) {
    $text = ([string]$BaseUrl).Trim()
    $uri = $null
    if (-not [System.Uri]::TryCreate($text, [System.UriKind]::Absolute, [ref]$uri) -or
        ($uri.Scheme -ne 'http' -and $uri.Scheme -ne 'https')) {
        throw "Invalid base URL '$BaseUrl'."
    }
    if ($uri.UserInfo -or $text.Contains('@')) { throw 'Base URL must not contain credentials.' }
    if ($uri.Query -or $uri.Fragment -or $text.Contains('?') -or $text.Contains('#')) {
        throw 'Base URL must not contain a query or fragment.'
    }
    return $text.TrimEnd('/')
}

function Assert-RpcFirewallDisplayName([string]$DisplayName) {
    $prefix = 'Sonder llama-rpc '
    if ($DisplayName -match '[*?\[\]]') {
        throw "Firewall rule display name must not contain wildcard characters, got '$DisplayName'."
    }
    if (-not $DisplayName.StartsWith($prefix, [System.StringComparison]::Ordinal) -or $DisplayName.Length -le $prefix.Length) {
        throw "Firewall rule display name must start with '$prefix', got '$DisplayName'."
    }
    return $DisplayName
}

function Resolve-OllamaBlob([string]$Tag, [string]$ModelsRoot) {
    if (-not $ModelsRoot) {
        $ModelsRoot = if ($env:OLLAMA_MODELS) { $env:OLLAMA_MODELS } else { Join-Path $env:USERPROFILE '.ollama\models' }
    }
    $segmentPattern = '^[A-Za-z0-9][A-Za-z0-9._-]*$'
    if ($Tag -match ':') {
        $idx = $Tag.LastIndexOf(':')
        $repo = $Tag.Substring(0, $idx)
        $ver = $Tag.Substring($idx + 1)
    }
    else {
        $repo = $Tag
        $ver = 'latest'
    }
    $segs = @($repo.Split('/') | Where-Object { $_ })
    if ($segs.Count -eq 0 -or -not $ver) { throw "Invalid Ollama model name '$Tag'." }
    foreach ($seg in @($segs) + @($ver)) {
        if ($seg -notmatch $segmentPattern) { throw "Invalid Ollama model name '$Tag'." }
    }
    if ($segs.Count -eq 1) {
        $relative = "registry.ollama.ai\library\$($segs[0])\$ver"
    }
    elseif ($segs.Count -eq 2) {
        $relative = "registry.ollama.ai\$($segs[0])\$($segs[1])\$ver"
    }
    else {
        $relative = "$($segs[0])\$(($segs[1..($segs.Count - 1)]) -join '\')\$ver"
    }
    $manifestsRoot = [System.IO.Path]::GetFullPath((Join-Path $ModelsRoot 'manifests')).TrimEnd('\') + '\'
    $manifest = [System.IO.Path]::GetFullPath((Join-Path $manifestsRoot $relative))
    if (-not $manifest.StartsWith($manifestsRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Invalid Ollama model name '$Tag': manifest path escapes the manifests directory."
    }
    if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) {
        throw "Ollama manifest not found for '$Tag' at '$manifest'."
    }
    $doc = Get-Content -LiteralPath $manifest -Raw -Encoding UTF8 | ConvertFrom-Json
    $modelLayer = @($doc.layers | Where-Object { $_.mediaType -eq 'application/vnd.ollama.image.model' }) | Select-Object -First 1
    if (-not $modelLayer -or -not $modelLayer.digest) {
        throw "No model layer digest found in Ollama manifest '$manifest'."
    }
    $digest = [string]$modelLayer.digest
    if ($digest -notmatch '^sha256:[0-9a-f]{64}$') {
        throw "Ollama manifest '$manifest' has an invalid model layer digest."
    }
    $blobPath = Join-Path $ModelsRoot ('blobs\' + $digest.Replace(':', '-'))
    if (-not (Test-Path -LiteralPath $blobPath)) {
        throw "Resolved Ollama blob missing at '$blobPath'."
    }
    return $blobPath
}

function ConvertTo-RpcFullPath([string]$Path) {
    # Canonical absolute form (relative to the PowerShell location, not the
    # process working directory), with forward slashes and dot segments folded.
    $unresolved = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
    return [System.IO.Path]::GetFullPath($unresolved)
}

function Get-RpcProcessPath([int]$ProcessId) {
    # Canonical executable path of a live process, as the OS reports it.
    $proc = Get-Process -Id $ProcessId -ErrorAction Stop
    $path = [string]$proc.Path
    if (-not $path) { throw "Cannot read the executable path of process $ProcessId." }
    return $path
}

function Test-RpcOwnedProcess([int]$ProcessId, [string]$ExePath, [string]$StartedUtc) {
    # True only when the live process still is the one we launched: same
    # executable and same start time (guards against PID reuse).
    if ($ProcessId -le 0 -or -not $ExePath -or -not $StartedUtc) { return $false }
    $proc = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $proc) { return $false }
    try {
        $actualPath = [string]$proc.Path
        $actualStart = $proc.StartTime.ToUniversalTime()
    }
    catch { return $false }
    if (-not $actualPath) { return $false }
    try {
        $expectedFull = ConvertTo-RpcFullPath $ExePath
        $actualFull = ConvertTo-RpcFullPath $actualPath
    }
    catch { return $false }
    if (-not [string]::Equals($actualFull, $expectedFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $false
    }
    $recorded = [datetime]::MinValue
    if (-not [datetime]::TryParse($StartedUtc, [System.Globalization.CultureInfo]::InvariantCulture,
            [System.Globalization.DateTimeStyles]::AdjustToUniversal -bor [System.Globalization.DateTimeStyles]::AssumeUniversal,
            [ref]$recorded)) {
        return $false
    }
    return ([math]::Abs(($actualStart - $recorded).TotalSeconds) -lt 2)
}

function Get-RpcProcessStartUtc([int]$ProcessId) {
    $proc = Get-Process -Id $ProcessId -ErrorAction Stop
    return $proc.StartTime.ToUniversalTime().ToString('o')
}

$script:RpcAllowedSplitSpecTypes = @(
    'ngram-mod', 'ngram-simple', 'ngram-map-k', 'ngram-map-k4v', 'ngram-cache',
    'draft-simple', 'draft-eagle3', 'draft-mtp', 'draft-dflash', 'draft-dspark'
)

function ConvertTo-RpcIPv4UInt32([string]$Address) {
    $ip = [System.Net.IPAddress]::Parse($Address)
    $b = $ip.GetAddressBytes()
    return ([uint32]$b[0] -shl 24) -bor ([uint32]$b[1] -shl 16) -bor ([uint32]$b[2] -shl 8) -bor [uint32]$b[3]
}

function Assert-RpcDedicatedLink([string]$LinkCidr, [string[]]$PeerHosts = @(), [int]$MinPrefixLength = 24) {
    $text = ([string]$LinkCidr).Trim()
    if (-not $text) {
        throw 'SONDER_RPC_LINK_CIDR / -RpcLinkCidr must name a dedicated private IPv4 subnet (e.g. 10.77.0.0/30).'
    }
    if ($text -notmatch '^([^/]+)/([0-9]{1,2})$') {
        throw "Invalid dedicated RPC link CIDR '$LinkCidr': expected IPv4/prefix (e.g. 10.77.0.0/30)."
    }
    $netText = ConvertTo-RpcPrivateIPv4 -Address $Matches[1] -Label 'RPC link network'
    $prefix = [int]$Matches[2]
    if ($prefix -lt $MinPrefixLength -or $prefix -gt 32) {
        throw "Dedicated RPC link CIDR '$text' (/$prefix) must have prefix length in $MinPrefixLength..32 (e.g. /30)."
    }
    $mask = if ($prefix -eq 0) {
        [uint32]0
    }
    else {
        [uint32](([uint64]4294967295 -shl (32 - $prefix)) -band [uint64]4294967295)
    }
    $netInt = ConvertTo-RpcIPv4UInt32 $netText
    if (($netInt -band $mask) -ne $netInt) {
        throw "Dedicated RPC link CIDR '$text' has host bits set."
    }
    foreach ($peerHost in $PeerHosts) {
        $cleanPeer = ConvertTo-RpcPrivateIPv4 -Address $peerHost -Label 'RPC peer host'
        $peerInt = ConvertTo-RpcIPv4UInt32 $cleanPeer
        if (($peerInt -band $mask) -ne $netInt) {
            throw "RPC peer host $cleanPeer is not on the configured dedicated private link $netText/$prefix."
        }
    }
    return [pscustomobject]@{
        Cidr         = "$netText/$prefix"
        Network      = $netText
        PrefixLength = $prefix
    }
}

function Assert-RpcSplitPolicy(
    [string]$SplitMode = 'layer',
    [string]$SpecType = 'ngram-mod',
    [string]$TensorSplit = '',
    [int]$PeerCount = 1,
    [string[]]$Devices = @()
) {
    $mode = ([string]$SplitMode).Trim().ToLowerInvariant()
    if ($mode -eq 'tensor') {
        throw "Cross-host RPC split_mode='tensor' is refused: tensor parallelism over Ethernet is latency-floored; use 'layer' only."
    }
    if ($mode -eq 'row') {
        throw "Cross-host RPC split_mode='row' is refused: row split was removed from CUDA (#24216); use 'layer' only."
    }
    if ($mode -ne 'layer') {
        throw "Cross-host RPC split_mode must be 'layer' only, got '$SplitMode'."
    }

    $spec = ([string]$SpecType).Trim().ToLowerInvariant()
    if (-not $spec -or $spec -eq 'none' -or $spec -eq 'off') {
        throw "Cross-host RPC split requires speculative decoding enabled (e.g. 'ngram-mod'); got '$SpecType'."
    }
    if ($script:RpcAllowedSplitSpecTypes -notcontains $spec) {
        throw "Unsupported cross-host RPC spec_type '$SpecType'; allowed: $($script:RpcAllowedSplitSpecTypes -join ', ')."
    }

    $cleanDevices = @($Devices | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
    if ($cleanDevices.Count -gt 0) {
        $lastDev = $cleanDevices[$cleanDevices.Count - 1]
        if ($lastDev.ToUpperInvariant().StartsWith('RPC')) {
            throw "Cross-host RPC device order places remote device '$lastDev' last, which pushes the output head/logits over Ethernet."
        }
        $seenLocal = $false
        foreach ($dev in $cleanDevices) {
            if ($dev.ToUpperInvariant().StartsWith('RPC')) {
                if ($seenLocal) {
                    throw "Cross-host RPC device order places local device before RPC device '$dev'; RPC devices must precede local GPUs."
                }
            }
            else {
                $seenLocal = $true
            }
        }
        if (-not $seenLocal) {
            throw 'Cross-host RPC device list must include at least one local GPU.'
        }
    }

    $cleanSplit = ([string]$TensorSplit).Trim()
    if ($cleanSplit) {
        $parts = @($cleanSplit.Split(',') | ForEach-Object { $_.Trim() })
        if ($parts.Count -lt ($PeerCount + 1)) {
            throw "TensorSplit '$TensorSplit' requires at least $($PeerCount + 1) weights (RPC peers + local GPU)."
        }
        $weights = @()
        $sum = 0.0
        foreach ($p in $parts) {
            $val = 0.0
            if (-not [double]::TryParse($p, [System.Globalization.NumberStyles]::Float, [System.Globalization.CultureInfo]::InvariantCulture, [ref]$val) -or
                [double]::IsNaN($val) -or [double]::IsInfinity($val) -or $val -lt 0.0) {
                throw "Invalid TensorSplit weight '$p' in '$TensorSplit'."
            }
            $weights += $val
            $sum += $val
        }
        if ($sum -le 0.0) {
            throw "TensorSplit '$TensorSplit' must have a positive sum."
        }
        if ($weights[$weights.Count - 1] -le 0.0) {
            throw "TensorSplit '$TensorSplit' gives the trailing local device weight 0; local weight must be > 0 so output head/logits stay local."
        }
    }

    return [pscustomobject]@{
        splitMode   = $mode
        specType    = $spec
        tensorSplit = $cleanSplit
        devices     = $cleanDevices
    }
}

function Measure-RpcPeerRtt(
    [string]$TargetHost,
    [int]$TargetPort,
    [int]$Samples = 5,
    [double]$MaxRttMs = 2.0,
    [int]$TimeoutMs = 1500
) {
    if ($Samples -lt 1 -or $Samples -gt 20) {
        throw "RttSamples must be in 1..20, got $Samples."
    }
    if ($MaxRttMs -le 0.0 -or [double]::IsNaN($MaxRttMs) -or [double]::IsInfinity($MaxRttMs)) {
        throw "MaxRttMs must be a positive finite number, got $MaxRttMs."
    }
    $measured = @()
    for ($i = 0; $i -lt $Samples; $i++) {
        $client = New-Object System.Net.Sockets.TcpClient
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        try {
            $async = $client.BeginConnect($TargetHost, $TargetPort, $null, $null)
            if (-not $async.AsyncWaitHandle.WaitOne($TimeoutMs)) {
                throw "RPC peer ${TargetHost}:$TargetPort timed out during RTT probe."
            }
            $client.EndConnect($async)
            $sw.Stop()
            $measured += [math]::Round($sw.Elapsed.TotalMilliseconds, 4)
        }
        finally {
            $client.Close()
        }
    }
    $sorted = @($measured | Sort-Object)
    $mid = [int][math]::Floor($sorted.Count / 2)
    $medianMs = if (($sorted.Count % 2) -eq 1) {
        [double]$sorted[$mid]
    }
    else {
        [math]::Round(([double]$sorted[$mid - 1] + [double]$sorted[$mid]) / 2.0, 4)
    }
    if ($medianMs -gt $MaxRttMs) {
        throw "RPC peer ${TargetHost}:$TargetPort median RTT ${medianMs}ms exceeds MaxRttMs=${MaxRttMs}ms."
    }
    return [pscustomobject]@{
        endpoint  = "${TargetHost}:$TargetPort"
        samplesMs = $measured
        minMs     = $sorted[0]
        medianMs  = $medianMs
        maxMs     = $sorted[$sorted.Count - 1]
    }
}
