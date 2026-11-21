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
    # Quote per the CommandLineToArgvW rules so Start-Process -ArgumentList
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
    if (-not $actualPath -or -not [string]::Equals($actualPath, $ExePath, [System.StringComparison]::OrdinalIgnoreCase)) {
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
