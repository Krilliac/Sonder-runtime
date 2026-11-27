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
  RPC port. When SONDER_RPC_LINK_CIDR names the private link, every address must
  also sit inside it. SONDER_RPC_PROTO_MAJOR pins the llama.cpp RPC protocol
  major every worker must report over HELLO (Assert-RpcProtocolLockstep, mirror
  of check_rpc_lockstep in adapters/inference/rpc_cluster.py). Keep the two
  implementations in step.
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

function ConvertTo-RpcIPv4Value([string]$Address) {
    $b = [System.Net.IPAddress]::Parse($Address).GetAddressBytes()
    return ([long]$b[0] * 16777216) + ([long]$b[1] * 65536) + ([long]$b[2] * 256) + [long]$b[3]
}

function ConvertTo-RpcLinkNetwork([string]$Cidr) {
    # Mirrors parse_link_network: blank means no link restriction; otherwise a
    # strict IPv4 network (no host bits) wholly inside one allowed private range.
    $text = ([string]$Cidr).Trim()
    if (-not $text) { return $null }
    if ($text -notmatch '^([0-9.]+)/([0-9]{1,2})$') {
        throw "SONDER_RPC_LINK_CIDR must be an IPv4 network such as 10.77.0.0/30 with no host bits set, got '$Cidr'."
    }
    $prefix = [int]$Matches[2]
    $baseText = $Matches[1]
    if ($prefix -gt 32) {
        throw "SONDER_RPC_LINK_CIDR prefix must be between 0 and 32, got '$Cidr'."
    }
    $base = ConvertTo-RpcPrivateIPv4 -Address $baseText -Label 'SONDER_RPC_LINK_CIDR network'
    $first = [int]([System.Net.IPAddress]::Parse($base).GetAddressBytes()[0])
    $minPrefix = if ($first -eq 10) { 8 } elseif ($first -eq 172) { 12 } else { 16 }
    if ($prefix -lt $minPrefix) {
        throw "SONDER_RPC_LINK_CIDR $text is not inside a private (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16) or link-local (169.254.0.0/16) range."
    }
    $size = [long]1 -shl (32 - $prefix)
    $value = ConvertTo-RpcIPv4Value $base
    if (($value % $size) -ne 0) {
        throw "SONDER_RPC_LINK_CIDR must be an IPv4 network such as 10.77.0.0/30 with no host bits set, got '$Cidr'."
    }
    return [pscustomobject]@{ Base = $value; Size = $size; Text = "$base/$prefix" }
}

function Test-RpcAddressOnLink([string]$Address, $Link) {
    if ($null -eq $Link) { return $true }
    $value = ConvertTo-RpcIPv4Value $Address
    return ($value -ge $Link.Base -and $value -lt ($Link.Base + $Link.Size))
}

function Assert-RpcAddressOnLink([string]$Address, $Link, [string]$Label) {
    if (-not (Test-RpcAddressOnLink -Address $Address -Link $Link)) {
        throw "$Label $Address is not on the configured private link $($Link.Text) (SONDER_RPC_LINK_CIDR)."
    }
    return $Address
}

function ConvertTo-RpcProtoMajor([string]$Value) {
    # Mirrors parse_rpc_proto_major: blank means no pinned major.
    $text = ([string]$Value).Trim()
    if (-not $text) { return $null }
    if ($text -notmatch '^[0-9]{1,3}$') {
        throw "SONDER_RPC_PROTO_MAJOR must be a decimal llama.cpp RPC protocol major version, got '$Value'."
    }
    $major = [int]$text
    if ($major -lt 1 -or $major -gt 255) {
        throw "SONDER_RPC_PROTO_MAJOR must be between 1 and 255, got $major."
    }
    return $major
}

function ConvertTo-RpcPeerList([string[]]$Entries, [int]$Port, $Link = $null) {
    $tokens = @($Entries | ForEach-Object { ([string]$_).Replace(';', ',').Split(',') } |
        ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if ($tokens.Count -gt $script:RpcMaxPeers) {
        throw "At most $($script:RpcMaxPeers) RPC peers are allowed, got $($tokens.Count)."
    }
    $peers = @()
    foreach ($token in $tokens) {
        $peer = ConvertTo-RpcPeer -Entry $token -Port $Port
        $null = Assert-RpcAddressOnLink -Address $peer.Host -Link $Link -Label 'RPC peer host'
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

function Read-RpcExact($Stream, [int]$Count) {
    $buffer = New-Object byte[] $Count
    $offset = 0
    while ($offset -lt $Count) {
        $read = $Stream.Read($buffer, $offset, $Count - $offset)
        if ($read -le 0) { throw 'rpc-server closed the connection during HELLO.' }
        $offset += $read
    }
    return ,$buffer
}

function Get-RpcProtocolVersion([string]$TargetHost, [int]$TargetPort, [int]$TimeoutMs = 3000) {
    # One llama.cpp RPC HELLO (ggml-rpc.cpp, protocol 7.x at b11195 through 10.x at
    # v1.1.0): send cmd 14, uint64 LE size 24 and 24 zero conn_caps (plain TCP), read
    # uint64 LE size then major, minor, patch. rpc-server serves one client at a
    # time, so call this before llama-server connects, never against a live split.
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect($TargetHost, $TargetPort, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne($TimeoutMs)) {
            throw "no connection within $TimeoutMs ms"
        }
        $client.EndConnect($async)
        $client.ReceiveTimeout = $TimeoutMs
        $client.SendTimeout = $TimeoutMs
        $stream = $client.GetStream()
        $request = New-Object byte[] 33
        $request[0] = 14
        $request[1] = 24
        $stream.Write($request, 0, $request.Length)
        $stream.Flush()
        $header = Read-RpcExact $stream 8
        $size = [BitConverter]::ToUInt64($header, 0)
        if ($size -lt 3 -or $size -gt 4096) {
            throw "HELLO reply of $size bytes; it is not a compatible llama.cpp rpc-server"
        }
        $reply = Read-RpcExact $stream ([int]$size)
        return [pscustomobject]@{
            Major = [int]$reply[0]
            Minor = [int]$reply[1]
            Patch = [int]$reply[2]
            Text  = "$([int]$reply[0]).$([int]$reply[1]).$([int]$reply[2])"
        }
    }
    finally {
        $client.Close()
    }
}

function Assert-RpcProtocolLockstep($Peers, $ExpectedMajor = $null, [int]$TimeoutMs = 3000) {
    # Mirrors check_rpc_lockstep: every peer must answer HELLO, and every major must
    # equal $ExpectedMajor (when pinned) or each other. Returns one record per peer.
    $records = @()
    $errors = @()
    foreach ($peer in @($Peers)) {
        try {
            $version = Get-RpcProtocolVersion -TargetHost $peer.Host -TargetPort $peer.Port -TimeoutMs $TimeoutMs
            $records += [pscustomobject]@{ endpoint = $peer.Address; rpcProtocol = $version.Text; rpcProtoMajor = $version.Major }
        }
        catch {
            $detail = $_.Exception.Message
            $records += [pscustomobject]@{ endpoint = $peer.Address; rpcProtocol = $null; rpcProtoMajor = $null }
            $errors += "RPC peer $($peer.Address) did not answer the RPC HELLO ($detail); start rpc-server there, check the firewall, or stop the llama-server that already holds it."
        }
    }
    $answered = @($records | Where-Object { $null -ne $_.rpcProtoMajor })
    if ($null -ne $ExpectedMajor) {
        foreach ($r in $answered) {
            if ($r.rpcProtoMajor -ne [int]$ExpectedMajor) {
                $errors += "RPC protocol mismatch: $($r.endpoint) speaks llama.cpp RPC protocol $($r.rpcProtocol) (major $($r.rpcProtoMajor)) but SONDER_RPC_PROTO_MAJOR=$ExpectedMajor; run the same llama.cpp release on every node of the split."
            }
        }
    }
    elseif (@($answered | Select-Object -ExpandProperty rpcProtoMajor -Unique).Count -gt 1) {
        $listing = (@($answered | ForEach-Object { "$($_.endpoint)=$($_.rpcProtocol)" })) -join ', '
        $errors += "RPC protocol mismatch between workers ($listing); run the same llama.cpp release on every node of the split."
    }
    if ($errors.Count -gt 0) {
        throw ("Cross-host RPC split refused:`n" + ((@($errors | ForEach-Object { "- $_" })) -join "`n"))
    }
    return $records
}

function ConvertFrom-RpcLlamaVersionText([string]$Text) {
    # Mirrors parse_llama_build: 'version: <v> (build <n>, commit <hash>)', or the
    # older 'version: <n> (<hash>)'. Returns 'b<n>-<hash>' or $null.
    $raw = [string]$Text
    $m = [regex]::Match($raw, '\(build ([0-9]{1,9}), commit ([0-9A-Za-z]{1,64})\)')
    if (-not $m.Success) {
        $m = [regex]::Match($raw, '(?m)^version:\s*([0-9]{1,9})\s*\(([0-9A-Za-z]{1,64})\)')
    }
    if (-not $m.Success) { return $null }
    return "b$([long]$m.Groups[1].Value)-$($m.Groups[2].Value)"
}

function Get-RpcLlamaBuild([string]$ExePath, [int]$TimeoutMs = 15000) {
    # The llama.cpp build of a binary from `<exe> --version`; $null when the binary
    # is missing, hangs past $TimeoutMs or prints no build line.
    if (-not $ExePath -or -not (Test-Path -LiteralPath $ExePath -PathType Leaf)) { return $null }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $ExePath
    $psi.Arguments = '--version'
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.RedirectStandardInput = $true
    try {
        $proc = [System.Diagnostics.Process]::Start($psi)
    }
    catch {
        return $null
    }
    try {
        $proc.StandardInput.Close()
        $outTask = $proc.StandardOutput.ReadToEndAsync()
        $errTask = $proc.StandardError.ReadToEndAsync()
        if (-not $proc.WaitForExit($TimeoutMs)) {
            try { $proc.Kill() } catch { }
            return $null
        }
        return (ConvertFrom-RpcLlamaVersionText ($outTask.Result + "`n" + $errTask.Result))
    }
    finally {
        $proc.Dispose()
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
