"""Behavior tests for the llama.cpp RPC sharding PowerShell helpers (rpc-policy.ps1)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "rpc"
POLICY = SCRIPTS / "rpc-policy.ps1"
_SHELLS = [name for name in ("powershell", "pwsh") if shutil.which(name)]

pytestmark = pytest.mark.skipif(not _SHELLS, reason="PowerShell is not available")


def _run(shell: str, tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    script = tmp_path / "probe.ps1"
    script.write_text(f". '{POLICY}'\n$ErrorActionPreference = 'Stop'\n{body}\n", encoding="utf-8-sig")
    return subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.fixture(params=_SHELLS)
def shell(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def test_argument_string_quotes_per_argv_rules(shell: str, tmp_path: Path) -> None:
    body = r"""
$args1 = @('--model', 'C:\My $models\x "y".gguf', '--n', "a'b", 'plain', 'trail\')
ConvertTo-RpcArgumentString $args1
"""
    result = _run(shell, tmp_path, body)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        r'--model "C:\My $models\x \"y\".gguf" --n a'
        + "'"
        + "b plain trail" + chr(92)
    )


def test_base_url_brackets_ipv6(shell: str, tmp_path: Path) -> None:
    result = _run(shell, tmp_path, "ConvertTo-RpcBaseUrl '::1' 11438; ConvertTo-RpcBaseUrl '127.0.0.1' 11438")
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["http://[::1]:11438", "http://127.0.0.1:11438"]


@pytest.mark.parametrize(
    "url", ["http://u:p@127.0.0.1:1", "http://127.0.0.1:1/?a=b", "http://127.0.0.1:1/#f"]
)
def test_base_url_check_rejects_userinfo_and_query(shell: str, tmp_path: Path, url: str) -> None:
    result = _run(shell, tmp_path, f"Assert-RpcBaseUrl '{url}'")
    assert result.returncode != 0


@pytest.mark.parametrize(
    "name",
    ["*", "Sonder llama-rpc *", "Sonder llama-rpc [ab]", "Sonder llama-rpc ?", "Other rule", "sonder llama-rpc x"],
)
def test_firewall_display_name_policy_refuses(shell: str, tmp_path: Path, name: str) -> None:
    result = _run(shell, tmp_path, f"Assert-RpcFirewallDisplayName '{name}'")
    assert result.returncode != 0


def test_firewall_display_name_policy_accepts_default(shell: str, tmp_path: Path) -> None:
    result = _run(
        shell, tmp_path, "Assert-RpcFirewallDisplayName 'Sonder llama-rpc worker (main peer only)'"
    )
    assert result.returncode == 0, result.stderr


def test_firewall_script_matches_rules_exactly() -> None:
    text = (SCRIPTS / "ensure-rpc-worker-firewall.ps1").read_text(encoding="utf-8")
    assert "Assert-RpcFirewallDisplayName" in text
    assert "Where-Object { $_.DisplayName -ceq $DisplayName }" in text


def _make_ollama(tmp_path: Path, digest: str) -> Path:
    root = tmp_path / "om"
    manifests = root / "manifests" / "registry.ollama.ai" / "library" / "demo"
    manifests.mkdir(parents=True)
    (root / "blobs").mkdir()
    (manifests / "latest").write_text(
        json.dumps(
            {"layers": [{"mediaType": "application/vnd.ollama.image.model", "digest": digest}]}
        ),
        encoding="utf-8",
    )
    return root


def test_resolve_ollama_blob_happy_path(shell: str, tmp_path: Path) -> None:
    digest = "sha256:" + "ab" * 32
    root = _make_ollama(tmp_path, digest)
    blob = root / "blobs" / ("sha256-" + "ab" * 32)
    blob.write_bytes(b"GGUF")
    result = _run(shell, tmp_path, f"Resolve-OllamaBlob 'demo:latest' '{root}'")
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == blob


@pytest.mark.parametrize("tag", ["demo:a/../../x", "..:latest", "demo:..", "a/b/..:latest", "demo:-x", "C:" + chr(92) + "x:latest"])
def test_resolve_ollama_blob_rejects_bad_segments(shell: str, tmp_path: Path, tag: str) -> None:
    root = _make_ollama(tmp_path, "sha256:" + "ab" * 32)
    result = _run(shell, tmp_path, f"Resolve-OllamaBlob '{tag}' '{root}'")
    assert result.returncode != 0
    assert "invalid" in (result.stderr + result.stdout).lower()


def test_resolve_ollama_blob_rejects_bad_digest(shell: str, tmp_path: Path) -> None:
    root = _make_ollama(tmp_path, "sha256:../../escape")
    result = _run(shell, tmp_path, f"Resolve-OllamaBlob 'demo:latest' '{root}'")
    assert result.returncode != 0
    assert "digest" in (result.stderr + result.stdout).lower()


def test_owned_process_check_requires_matching_exe_and_start_time(shell: str, tmp_path: Path) -> None:
    body = r"""
$me = Get-Process -Id $PID
$exe = $me.Path
$started = $me.StartTime.ToUniversalTime().ToString('o')
$a = Test-RpcOwnedProcess -ProcessId $PID -ExePath $exe -StartedUtc $started
$b = Test-RpcOwnedProcess -ProcessId $PID -ExePath 'C:\nope\llama-server.exe' -StartedUtc $started
$c = Test-RpcOwnedProcess -ProcessId $PID -ExePath $exe -StartedUtc '2001-01-01T00:00:00.0000000Z'
$d = Test-RpcOwnedProcess -ProcessId $PID -ExePath $exe -StartedUtc ''
"$a $b $c $d"
"""
    result = _run(shell, tmp_path, body)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True False False False"


def test_owned_process_check_accepts_relative_and_noncanonical_exe(shell: str, tmp_path: Path) -> None:
    body = r"""
$me = Get-Process -Id $PID
$started = $me.StartTime.ToUniversalTime().ToString('o')
$dir = Split-Path -Parent $me.Path
$leaf = Split-Path -Leaf $me.Path
Set-Location -LiteralPath $dir
$rel = Test-RpcOwnedProcess -ProcessId $PID -ExePath (Join-Path '.' $leaf) -StartedUtc $started
$fwd = Test-RpcOwnedProcess -ProcessId $PID -ExePath ($me.Path.Replace([char]92, [char]47)) -StartedUtc $started
$dots = Test-RpcOwnedProcess -ProcessId $PID -ExePath (Join-Path $dir (Join-Path '..' (Join-Path (Split-Path -Leaf $dir) $leaf))) -StartedUtc $started
$rec = Get-RpcProcessPath -ProcessId $PID
$canon = [string]::Equals($rec, $me.Path, [System.StringComparison]::OrdinalIgnoreCase)
"$rel $fwd $dots $canon"
"""
    result = _run(shell, tmp_path, body)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True True True True"


@pytest.mark.parametrize("name", ["start-rpc-worker.ps1", "start-rpc-sharded.ps1"])
def test_launchers_record_canonical_exe_path(name: str) -> None:
    text = (SCRIPTS / name).read_text(encoding="utf-8")
    assert "Get-RpcProcessPath" in text


@pytest.mark.parametrize("name", ["start-rpc-worker.ps1", "start-rpc-sharded.ps1"])
def test_launchers_use_detached_cim_launch_not_encoded_wrapper(name: str) -> None:
    text = (SCRIPTS / name).read_text(encoding="utf-8")
    assert "EncodedCommand" not in text
    assert "Start-Process" not in text
    assert "Start-RpcDetached" in text
    assert "Test-RpcOwnedProcess" in text


def test_sharded_launcher_hardening_markers() -> None:
    text = (SCRIPTS / "start-rpc-sharded.ps1").read_text(encoding="utf-8")
    assert "[ValidateRange(1, 65535)]" in text
    assert "-MaximumRedirection 0" in text or "MaximumRedirection = 0" in text
    assert "NoProxy" in text
    assert "ConvertTo-RpcBaseUrl" in text
    assert os.path.basename(str(SCRIPTS)) == "rpc"

@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Start-RpcDetached uses Windows Win32_Process CIM and taskkill",
)
def test_detached_launch_does_not_inherit_caller_pipes(shell: str, tmp_path: Path) -> None:
    # A caller that pipes output (one-shot ssh, CI, agent wrappers) must get
    # control back immediately, not when the launched server exits.
    import time

    body = f"""
$exe = (Get-Process -Id $PID).Path
$argv = ConvertTo-RpcArgumentString @('-NoProfile', '-Command', 'Start-Sleep -Seconds 25')
$childPid = Start-RpcDetached -ExePath $exe -ArgumentString $argv -WorkingDirectory '{tmp_path}'
Write-Output "PID=$childPid"
"""
    start = time.monotonic()
    result = _run(shell, tmp_path, body)
    elapsed = time.monotonic() - start
    child = None
    for line in result.stdout.splitlines():
        if line.startswith("PID="):
            child = int(line[4:])
    try:
        assert result.returncode == 0, result.stderr
        assert child is not None
        assert elapsed < 15, f"launcher blocked for {elapsed:.1f}s on inherited pipes"
    finally:
        if child:
            subprocess.run(["taskkill", "/F", "/PID", str(child)], capture_output=True, check=False)


# ---------------------------------------------------------------------------
# Fail-closed firewall audit in start-rpc-worker.ps1 / ensure-rpc-worker-firewall.ps1
# ---------------------------------------------------------------------------

WORKER_SCRIPT = SCRIPTS / "start-rpc-worker.ps1"
ENSURE_FW_SCRIPT = SCRIPTS / "ensure-rpc-worker-firewall.ps1"

_FW_WRAPPER = r"""
param([Parameter(Mandatory = $true)][string]$ScenarioPath)
$ErrorActionPreference = 'Stop'
$scenario = Get-Content -LiteralPath $ScenarioPath -Raw | ConvertFrom-Json
$global:MockOut = [string]$scenario.outDir
$global:MockRules = New-Object System.Collections.ArrayList
foreach ($r in @($scenario.rules)) { if ($null -ne $r) { [void]$global:MockRules.Add($r) } }
$global:MockProfiles = @($scenario.profiles)
$global:MockPersistentProfiles = @($scenario.persistentProfiles)
$global:MockPersistentFails = [bool]$scenario.persistentFails
$global:MockLaunchPid = [int]$scenario.launchPid
$global:MockEnsureFails = [bool]$scenario.ensureFails
$global:MockRuleReadFails = [bool]$scenario.ruleReadFails
$global:MockIpFails = [bool]$scenario.ipFails
$global:MockIpAliases = @($scenario.ipAliases)
$global:MockTcpOpen = [bool]$scenario.tcpOpen
$global:MockStopFails = [bool]$scenario.stopFails
$global:MockLivePids = New-Object System.Collections.ArrayList
foreach ($p in @($scenario.livePids)) { if ($null -ne $p) { [void]$global:MockLivePids.Add([int]$p) } }
$global:MockProcessMeta = @{}
foreach ($pm in @($scenario.processMeta)) {
    if ($null -ne $pm) { $global:MockProcessMeta[[string]$pm.pid] = $pm }
}
$global:MockListeners = New-Object System.Collections.ArrayList
foreach ($l in @($scenario.listeners)) { if ($null -ne $l) { [void]$global:MockListeners.Add($l) } }
$global:MockPortproxyLines = if ($null -ne $scenario.portproxyLines) { @($scenario.portproxyLines) } else { @('') }
$global:MockPortproxyExit = [int]$scenario.portproxyExit
$global:MockPortproxyThrows = [bool]$scenario.portproxyThrows
$global:MockServiceStatus = if ($null -ne $scenario.serviceStatus) { [string]$scenario.serviceStatus } else { 'Running' }
$global:MockServiceFails = [bool]$scenario.serviceFails
$global:MockInterfaceFilterFails = [bool]$scenario.interfaceFilterFails

function Write-MockLog([string]$Name, $Value) {
    Add-Content -LiteralPath (Join-Path $global:MockOut $Name) -Value ($Value | ConvertTo-Json -Compress -Depth 6)
}

function ConvertTo-MockRule($r) {
    $rule = [pscustomobject]@{
        Name = [string]$r.name; InstanceID = [string]$r.name; DisplayName = [string]$r.displayName
        Enabled = [string]$r.enabled; Direction = [string]$r.direction; Action = [string]$r.action
        PackageFamilyName = [string]$r.packageFamilyName
        PolicyStoreSource = [string]$r.policyStoreSource; PolicyStoreSourceType = [string]$r.policyStoreSourceType
    }
    if (-not $r.omitKeywordProperty) {
        $kw = if ($null -ne $r.remoteDynamicKeywordAddresses) { [string[]]@($r.remoteDynamicKeywordAddresses) } else { [string[]]@() }
        $rule | Add-Member -MemberType NoteProperty -Name RemoteDynamicKeywordAddresses -Value $kw
    }
    $rule
}

function New-MockFilter($r, [string]$Kind) {
    switch ($Kind) {
        'Port' { [pscustomobject]@{ InstanceID = [string]$r.name; Protocol = $r.protocol; LocalPort = @($r.localPort) } }
        'Address' { [pscustomobject]@{ InstanceID = [string]$r.name; LocalAddress = @($r.localAddress); RemoteAddress = @($r.remoteAddress) } }
        'Application' { [pscustomobject]@{ InstanceID = [string]$r.name; Program = [string]$r.program; Package = [string]$r.package } }
        'Interface' {
            if ($r.omitInterface) { return }
            [pscustomobject]@{ InstanceID = [string]$r.name; InterfaceAlias = @($r.interfaceAlias) }
        }
        'InterfaceType' {
            if ($r.omitInterface) { return }
            [pscustomobject]@{ InstanceID = [string]$r.name; InterfaceType = @($r.interfaceType) }
        }
    }
}

function Get-MockFilter($InputRule, [bool]$All, [string]$Kind) {
    if ($All) {
        if (($Kind -eq 'Interface' -or $Kind -eq 'InterfaceType') -and $global:MockInterfaceFilterFails) {
            Write-Error -Message 'mock: Access is denied.' -Category PermissionDenied; return
        }
        foreach ($r in $global:MockRules) { if (-not $r.hiddenFromAll) { New-MockFilter $r $Kind } }
        return
    }
    $r = $global:MockRules | Where-Object { $_.name -eq $InputRule.InstanceID } | Select-Object -First 1
    if ($null -eq $r) { Write-Error -Message "mock: no rule $($InputRule.InstanceID)" -Category ObjectNotFound; return }
    if ($r.failPerRule) { Write-Error -Message 'mock: Access is denied.' -Category PermissionDenied; return }
    if (($Kind -eq 'Interface' -or $Kind -eq 'InterfaceType') -and $r.failInterface) {
        Write-Error -Message 'mock: Access is denied.' -Category PermissionDenied; return
    }
    New-MockFilter $r $Kind
}

function Get-NetFirewallRule {
    [CmdletBinding()]
    param([string]$DisplayName, [string]$Name, [string]$PolicyStore, [string]$Direction, [string]$Enabled, [string]$Action, [switch]$All)
    Write-MockLog 'get-rule.jsonl' $PSBoundParameters
    if ($global:MockRuleReadFails) { Write-Error -Message 'mock: Access is denied.' -Category PermissionDenied; return }
    $out = @($global:MockRules | Where-Object {
        (-not $DisplayName -or $_.displayName -eq $DisplayName) -and
        (-not $Direction -or $_.direction -eq $Direction) -and
        (-not $Enabled -or $_.enabled -eq $Enabled) -and
        (-not $Action -or $_.action -eq $Action)
    } | ForEach-Object { ConvertTo-MockRule $_ })
    if ($out.Count -eq 0) {
        if ($PSBoundParameters.ContainsKey('ErrorAction') -and $PSBoundParameters['ErrorAction'] -eq 'SilentlyContinue') { return }
        Write-Error -Message 'No MSFT_NetFirewallRule objects found' -Category ObjectNotFound; return
    }
    $out
}
function Get-NetFirewallPortFilter {
    [CmdletBinding()]
    param([Parameter(ValueFromPipeline = $true)]$AssociatedNetFirewallRule, [switch]$All, [string]$PolicyStore)
    process { Get-MockFilter $AssociatedNetFirewallRule ([bool]$All) 'Port' }
}
function Get-NetFirewallAddressFilter {
    [CmdletBinding()]
    param([Parameter(ValueFromPipeline = $true)]$AssociatedNetFirewallRule, [switch]$All, [string]$PolicyStore)
    process { Get-MockFilter $AssociatedNetFirewallRule ([bool]$All) 'Address' }
}
function Get-NetFirewallApplicationFilter {
    [CmdletBinding()]
    param([Parameter(ValueFromPipeline = $true)]$AssociatedNetFirewallRule, [switch]$All, [string]$PolicyStore)
    process { Get-MockFilter $AssociatedNetFirewallRule ([bool]$All) 'Application' }
}
function Get-NetFirewallInterfaceFilter {
    [CmdletBinding()]
    param([Parameter(ValueFromPipeline = $true)]$AssociatedNetFirewallRule, [switch]$All, [string]$PolicyStore)
    process { Get-MockFilter $AssociatedNetFirewallRule ([bool]$All) 'Interface' }
}
function Get-NetFirewallInterfaceTypeFilter {
    [CmdletBinding()]
    param([Parameter(ValueFromPipeline = $true)]$AssociatedNetFirewallRule, [switch]$All, [string]$PolicyStore)
    process { Get-MockFilter $AssociatedNetFirewallRule ([bool]$All) 'InterfaceType' }
}
function Get-NetFirewallProfile {
    [CmdletBinding()]
    param([string]$PolicyStore, [switch]$All)
    Write-MockLog 'get-profile.jsonl' @{ PolicyStore = $PolicyStore }
    if ($PolicyStore -eq 'PersistentStore') {
        if ($global:MockPersistentFails) { Write-Error -Message 'mock: Access is denied.' -Category PermissionDenied; return }
        return $global:MockPersistentProfiles
    }
    $global:MockProfiles
}
function Remove-NetFirewallRule {
    [CmdletBinding()]
    param([Parameter(ValueFromPipeline = $true)]$InputObject)
    process {
        Write-MockLog 'remove-rule.jsonl' @{ Name = $InputObject.Name }
        $hit = @($global:MockRules | Where-Object { $_.name -eq $InputObject.Name })
        foreach ($h in $hit) { $global:MockRules.Remove($h) }
    }
}
function New-NetFirewallRule {
    [CmdletBinding()]
    param([string]$DisplayName, [string]$Direction, [string]$Action, [string]$Protocol, [string[]]$LocalPort,
          [string[]]$LocalAddress, [string[]]$RemoteAddress, [string]$Profile, [string]$Description, [string]$Program,
          [string[]]$InterfaceAlias)
    Write-MockLog 'new-rule.jsonl' $PSBoundParameters
    if ($global:MockEnsureFails) { throw 'mock New-NetFirewallRule: Access is denied.' }
    [void]$global:MockRules.Add([pscustomobject]@{
        name = "mock-$DisplayName"; displayName = $DisplayName; enabled = 'True'; direction = $Direction; action = $Action
        protocol = $Protocol; localPort = $LocalPort; localAddress = $LocalAddress; remoteAddress = $RemoteAddress
        program = $(if ($Program) { $Program } else { 'Any' }); package = ''; packageFamilyName = ''
        interfaceAlias = $(if ($InterfaceAlias) { $InterfaceAlias } else { @('Any') }); interfaceType = 'Any'
        policyStoreSource = 'PersistentStore'; policyStoreSourceType = 'Local'
        remoteDynamicKeywordAddresses = @()
    })
}
function Get-NetTCPConnection {
    [CmdletBinding()]
    param([string]$State, [int]$LocalPort)
    foreach ($l in @($global:MockListeners)) {
        if (-not $LocalPort -or [int]$l.localPort -eq $LocalPort) {
            [pscustomobject]@{ LocalAddress = [string]$l.localAddress; LocalPort = [int]$l.localPort; OwningProcess = [int]$l.owningProcess }
        }
    }
}
function Get-NetIPAddress {
    [CmdletBinding()]
    param([string]$IPAddress)
    Write-MockLog 'get-ip.jsonl' @{ IPAddress = $IPAddress }
    if ($global:MockIpFails) { Write-Error -Message "mock: No MSFT_NetIPAddress objects found with property 'IPAddress' equal to '$IPAddress'" -Category ObjectNotFound; return }
    foreach ($a in @($global:MockIpAliases)) { [pscustomobject]@{ IPAddress = $IPAddress; InterfaceAlias = [string]$a } }
}
function Get-Process {
    [CmdletBinding()]
    param([int[]]$Id)
    foreach ($i in @($Id)) {
        if ($global:MockLivePids -contains $i) {
            $meta = $global:MockProcessMeta[[string]$i]
            if ($null -ne $meta) {
                [pscustomobject]@{ Id = $i; Path = [string]$meta.path; StartTime = [datetime]$meta.startTime }
            }
            else {
                [pscustomobject]@{ Id = $i }
            }
        }
        else { Write-Error -Message "mock: no process $i" -Category ObjectNotFound }
    }
}
function Stop-Process {
    [CmdletBinding()]
    param([int[]]$Id, [switch]$Force)
    foreach ($i in @($Id)) {
        Write-MockLog 'stop.jsonl' @{ Id = $i }
        if ($global:MockStopFails) { continue }
        while ($global:MockLivePids -contains $i) { $global:MockLivePids.Remove($i) }
        foreach ($l in @($global:MockListeners | Where-Object { [int]$_.owningProcess -eq $i })) { $global:MockListeners.Remove($l) }
    }
}
function Wait-Process {
    [CmdletBinding()]
    param([int[]]$Id, [int]$Timeout)
}
function Invoke-CimMethod {
    [CmdletBinding()]
    param([string]$ClassName, [string]$MethodName, [hashtable]$Arguments)
    Write-MockLog 'launch.json' @{ ClassName = $ClassName; MethodName = $MethodName }
    if ($global:MockLaunchPid -gt 0) { return [pscustomobject]@{ ReturnValue = 0; ProcessId = $global:MockLaunchPid } }
    [pscustomobject]@{ ReturnValue = 9; ProcessId = 0 }
}
function netsh {
    Write-MockLog 'netsh.jsonl' @{ Args = @($args) }
    if ($global:MockPortproxyThrows) { throw 'mock: netsh is not recognized' }
    $global:LASTEXITCODE = $global:MockPortproxyExit
    foreach ($line in $global:MockPortproxyLines) { [string]$line }
}
function Get-Service {
    [CmdletBinding()]
    param([string]$Name)
    Write-MockLog 'get-service.jsonl' @{ Name = $Name }
    if ($global:MockServiceFails) { Write-Error -Message "mock: Cannot find any service with service name '$Name'." -Category ObjectNotFound; return }
    [pscustomobject]@{ Name = $Name; Status = $global:MockServiceStatus }
}
function New-Object {
    param([Parameter(Position = 0)][string]$TypeName)
    if ($TypeName -ne 'System.Net.Sockets.TcpClient') { throw "mock New-Object: unexpected type $TypeName" }
    $client = [pscustomobject]@{}
    $client | Add-Member -MemberType ScriptMethod -Name BeginConnect -Value {
        Write-MockLog 'tcp.jsonl' @{ Host = $args[0]; Port = $args[1] }
        if (-not $global:MockTcpOpen) { throw 'mock: no network' }
        $handle = [pscustomobject]@{}
        $handle | Add-Member -MemberType ScriptMethod -Name WaitOne -Value { $true }
        [pscustomobject]@{ AsyncWaitHandle = $handle }
    }
    $client | Add-Member -MemberType ScriptMethod -Name EndConnect -Value { }
    $client | Add-Member -MemberType ScriptMethod -Name Close -Value { }
    $client
}

foreach ($cmd in 'Get-NetFirewallRule', 'Get-NetFirewallPortFilter', 'Get-NetFirewallAddressFilter',
                 'Get-NetFirewallApplicationFilter', 'Get-NetFirewallInterfaceFilter',
                 'Get-NetFirewallInterfaceTypeFilter', 'Get-NetFirewallProfile', 'Remove-NetFirewallRule',
                 'New-NetFirewallRule', 'Get-NetTCPConnection', 'Invoke-CimMethod', 'New-Object',
                 'Get-NetIPAddress', 'Get-Process', 'Stop-Process', 'Wait-Process',
                 'netsh', 'Get-Service') {
    if ((Get-Command $cmd).CommandType -ne 'Function') { throw "mock for $cmd is not active" }
}

$splat = @{}
foreach ($p in $scenario.args.PSObject.Properties) { $splat[$p.Name] = $p.Value }
try {
    & ([string]$scenario.script) @splat
}
catch {
    Set-Content -LiteralPath (Join-Path $global:MockOut 'error.txt') -Value $_.Exception.Message -Encoding UTF8
    exit 1
}
exit 0
"""

_FW_PEER = {
    "name": "peer",
    "displayName": "Sonder llama-rpc worker (main peer only)",
    "enabled": "True",
    "direction": "Inbound",
    "action": "Allow",
    "protocol": "TCP",
    "localPort": ["50052"],
    "localAddress": ["10.77.0.2"],
    "remoteAddress": ["10.77.0.1"],
    "program": "Any",
    "package": "",
    "packageFamilyName": "",
    "interfaceAlias": ["Private Link"],
    "interfaceType": "Any",
    "policyStoreSource": "PersistentStore",
    "policyStoreSourceType": "Local",
}

_FW_PROFILES_ON = [
    {"Name": n, "Enabled": "True", "DefaultInboundAction": "Block", "DisabledInterfaceAliases": None}
    for n in ("Domain", "Private", "Public")
]
_FW_PERSISTENT_ON = [
    {"Name": n, "Enabled": "True", "DefaultInboundAction": "NotConfigured", "DisabledInterfaceAliases": ["NotConfigured"]}
    for n in ("Domain", "Private", "Public")
]
_FW_BIND_ALIAS = "Private Link"


def _fw_rule(**overrides: object) -> dict:
    rule = dict(_FW_PEER)
    rule.update(overrides)
    return rule


def _fw_profiles(base: list[dict] | None = None, **aliases: object) -> list[dict]:
    profiles = [dict(p) for p in (_FW_PROFILES_ON if base is None else base)]
    for p in profiles:
        if p["Name"] in aliases:
            p["DisabledInterfaceAliases"] = aliases[p["Name"]]
    return profiles


class _FwOutcome:
    def __init__(self, proc: subprocess.CompletedProcess[str], out_dir: Path) -> None:
        self.returncode = proc.returncode
        self.text = proc.stdout + proc.stderr
        self.out_dir = out_dir

    @property
    def launched(self) -> bool:
        return (self.out_dir / "launch.json").exists()

    def log(self, name: str) -> list[dict]:
        path = self.out_dir / name
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]

    @property
    def error(self) -> str:
        path = self.out_dir / "error.txt"
        return path.read_text(encoding="utf-8-sig").strip() if path.exists() else ""


def _run_fw(
    tmp_path: Path,
    rules: list[dict],
    *,
    script: Path = WORKER_SCRIPT,
    args: dict | None = None,
    profiles: list[dict] | None = None,
    persistent: list[dict] | None = None,
    ensure_fails: bool = False,
    extra: dict | None = None,
    shell_exe: str | None = None,
) -> _FwOutcome:
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    install = tmp_path / "install"
    install.mkdir(parents=True, exist_ok=True)
    (install / "rpc-server.exe").write_bytes(b"")
    script_args = {
        "Action": "Start",
        "BindAddress": "10.77.0.2",
        "AllowedPeer": "10.77.0.1",
        "InstallDir": str(install),
        "StateRoot": str(tmp_path / "state_root"),
    }
    if args:
        for key, value in args.items():
            if value is None:
                script_args.pop(key, None)
            else:
                script_args[key] = value
    scenario = {
        "outDir": str(out_dir),
        "script": str(script),
        "rules": rules,
        "profiles": _FW_PROFILES_ON if profiles is None else profiles,
        "persistentProfiles": _FW_PERSISTENT_ON if persistent is None else persistent,
        "ensureFails": ensure_fails,
        "ipAliases": [_FW_BIND_ALIAS],
        "args": script_args,
    }
    scenario.update(extra or {})
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
    wrapper = tmp_path / "wrapper.ps1"
    wrapper.write_text(_FW_WRAPPER, encoding="utf-8")
    chosen_shell = shell_exe or ("powershell" if "powershell" in _SHELLS else _SHELLS[0])
    proc = subprocess.run(
        [chosen_shell, "-NoProfile", "-NonInteractive", "-File", str(wrapper), "-ScenarioPath", str(scenario_path)],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    outcome = _FwOutcome(proc, out_dir)
    assert "is not active" not in outcome.text and "mock New-Object: unexpected" not in outcome.text, outcome.text
    return outcome


def _assert_fw_refused(outcome: _FwOutcome, *needles: str) -> None:
    assert outcome.returncode != 0, outcome.text
    assert not outcome.launched, outcome.error or outcome.text
    assert outcome.error.startswith("Refusing to start rpc-server"), outcome.error or outcome.text
    for needle in needles:
        assert needle in outcome.error, outcome.error


def test_fw_peer_rule_only_allows_start(shell: str, tmp_path: Path) -> None:
    outcome = _run_fw(tmp_path, [_FW_PEER], shell_exe=shell)
    assert outcome.launched, outcome.error or outcome.text
    assert "Win32_Process Create failed" in outcome.error, outcome.error
    assert outcome.log("get-service.jsonl") == [{"Name": "mpssvc"}]
    assert outcome.log("netsh.jsonl") == [{"Args": ["interface", "portproxy", "show", "all"]}]


def test_fw_non_default_private_subnet_peer_allows_start(tmp_path: Path) -> None:
    peer_rule = _fw_rule(localAddress=["192.168.50.2"], remoteAddress=["192.168.50.1"])
    outcome = _run_fw(
        tmp_path, [peer_rule],
        args={"BindAddress": "192.168.50.2", "AllowedPeer": "192.168.50.1"},
    )
    assert outcome.launched, outcome.error or outcome.text


def test_fw_without_allowed_peer_accepts_single_private_peer_and_refuses_multiple(tmp_path: Path) -> None:
    ok_outcome = _run_fw(tmp_path / "single", [_FW_PEER], args={"AllowedPeer": None})
    assert ok_outcome.launched, ok_outcome.error or ok_outcome.text

    second = _fw_rule(name="peer2", displayName="Second peer", remoteAddress=["10.77.0.9"])
    bad_outcome = _run_fw(tmp_path / "multi", [_FW_PEER, second], args={"AllowedPeer": None})
    _assert_fw_refused(bad_outcome, "multiple peer addresses", "10.77.0.1", "10.77.0.9")


def test_fw_no_rule_refuses_and_names_the_fix(tmp_path: Path) -> None:
    _assert_fw_refused(
        _run_fw(tmp_path, []),
        "no enabled inbound Allow rule", "ensure-rpc-worker-firewall.ps1", "-Port 50052",
    )


def test_fw_wide_and_program_and_keyword_port_rules_refuse(tmp_path: Path) -> None:
    wide = _fw_rule(name="wide", displayName="Open 50052", remoteAddress=["Any"])
    _assert_fw_refused(_run_fw(tmp_path / "wide", [_FW_PEER, wide]), "'Open 50052'", "Disable-NetFirewallRule -Name 'wide'")

    lan = _fw_rule(name="lan", displayName="LAN range", localPort=["50000-50100"], remoteAddress=["LocalSubnet"])
    _assert_fw_refused(_run_fw(tmp_path / "lan", [_FW_PEER, lan]), "'LAN range'")

    kw_port = _fw_rule(name="kw", displayName="RPC dynamic", localPort=["RPC"], remoteAddress=["Any"])
    _assert_fw_refused(_run_fw(tmp_path / "kwp", [_FW_PEER, kw_port]), "'RPC dynamic'", "LocalPort=RPC")

    multi = _fw_rule(name="multi", displayName="Web plus rpc", localPort=["80", "50052"], remoteAddress=["Any"])
    _assert_fw_refused(_run_fw(tmp_path / "multi", [_FW_PEER, multi]), "'Web plus rpc'", "LocalPort=80,50052")

    exe = str(tmp_path / "prog" / "install" / "RPC-SERVER.EXE")
    prog = _fw_rule(name="prog", displayName="rpc-server.exe", protocol="Any", localPort=["Any"],
                    localAddress=["Any"], remoteAddress=["Any"], program=exe)
    _assert_fw_refused(_run_fw(tmp_path / "prog", [_FW_PEER, prog]), "'rpc-server.exe'")

    hidden = _fw_rule(name="hidden", displayName="Hidden wide", remoteAddress=["Any"], hiddenFromAll=True)
    _assert_fw_refused(_run_fw(tmp_path / "hidden", [_FW_PEER, hidden]), "'Hidden wide'")


@pytest.mark.parametrize("program", [
    r"\Device\HarddiskVolume3\llama-rpc\rpc-server.exe",
    r"C:\LLAMA-~1\GGML-RPC-SERVER.EXE",
    r"D:\links\llama\rpc-server.exe",
    r"C:\llama-rpc\RPC-SE~1.EXE",
])
def test_fw_rpc_server_program_rule_variants_refuse(tmp_path: Path, program: str) -> None:
    prog = _fw_rule(name="prog", displayName="rpc program rule", protocol="Any", localPort=["Any"],
                    localAddress=["Any"], remoteAddress=["Any"], program=program)
    _assert_fw_refused(_run_fw(tmp_path, [_FW_PEER, prog]), "'rpc program rule'", "Disable-NetFirewallRule -Name 'prog'")


@pytest.mark.parametrize("name, overrides", [
    ("alias-any", {"interfaceAlias": ["Any"]}),
    ("alias-missing", {"interfaceAlias": []}),
    ("alias-wildcard", {"interfaceAlias": ["Private*"]}),
    ("alias-wrong-adapter", {"interfaceAlias": ["Wi-Fi"]}),
    ("alias-several", {"interfaceAlias": ["Private Link", "Wi-Fi"]}),
    ("alias-several-wrong", {"interfaceAlias": ["Ethernet", "Wi-Fi"]}),
    ("alias-case-differs", {"interfaceAlias": ["private link"]}),
    ("filter-omitted", {"omitInterface": True}),
    ("per-rule-unreadable", {"failInterface": True, "hiddenFromAll": True}),
    ("type-empty", {"interfaceType": ""}),
])
def test_fw_peer_rule_without_a_pinned_interface_is_wide(tmp_path: Path, name: str, overrides: dict) -> None:
    unpinned = _fw_rule(**overrides)
    outcome = _run_fw(tmp_path / name, [unpinned])
    _assert_fw_refused(outcome, "'Sonder llama-rpc worker (main peer only)'", "Disable-NetFirewallRule -Name 'peer'",
                       "a peer rule must be pinned to the single interface alias that owns")


@pytest.mark.parametrize("name, aliases", [
    ("unresolved", []),
    ("two-owners", ["Private Link", "Wi-Fi"]),
])
def test_fw_peer_rule_is_wide_when_the_bind_adapter_is_not_exactly_one(tmp_path: Path, name: str, aliases: list) -> None:
    outcome = _run_fw(tmp_path / name, [_FW_PEER], extra={"ipAliases": aliases})
    _assert_fw_refused(outcome, "'Sonder llama-rpc worker (main peer only)'",
                       "the adapter owning the bind address could not be resolved")


def test_fw_failing_bulk_interface_listing_falls_back_to_the_per_rule_read(tmp_path: Path) -> None:
    outcome = _run_fw(tmp_path, [_FW_PEER], extra={"interfaceFilterFails": True})
    # The bulk listing failing falls back to a per-rule read, which succeeds, so the pinned rule still passes.
    assert outcome.launched, outcome.error or outcome.text


def test_fw_pinned_peer_rule_with_any_interface_type_is_peer_only(tmp_path: Path) -> None:
    assert _run_fw(tmp_path, [_fw_rule(interfaceType="Any")]).launched
    assert _run_fw(tmp_path / "wired", [_fw_rule(interfaceType="Wired")]).launched


def test_fw_unrelated_rules_allow_start(tmp_path: Path) -> None:
    rules = [
        _FW_PEER,
        _fw_rule(name="other-prog", displayName="Other app", protocol="Any", localPort=["Any"],
                 remoteAddress=["Any"], program=r"C:\Other\app.exe"),
        _fw_rule(name="store", displayName="Store app", protocol="Any", localPort=["Any"],
                 remoteAddress=["Any"], packageFamilyName="Microsoft.WindowsStore_8wekyb3d8bbwe"),
        _fw_rule(name="udp", displayName="UDP any", protocol="UDP", localPort=["Any"], remoteAddress=["Any"]),
        _fw_rule(name="disabled", displayName="Disabled wide", enabled="False", remoteAddress=["Any"]),
        _fw_rule(name="block", displayName="Block wide", action="Block", remoteAddress=["Any"]),
    ]
    assert _run_fw(tmp_path, rules).launched


def test_fw_service_and_profiles_and_disabled_aliases(tmp_path: Path) -> None:
    _assert_fw_refused(_run_fw(tmp_path / "svc-stop", [_FW_PEER], extra={"serviceStatus": "Stopped"}),
                       "(mpssvc) is not running", "Status=Stopped", "Start-Service mpssvc")
    _assert_fw_refused(_run_fw(tmp_path / "svc-fail", [_FW_PEER], extra={"serviceFails": True}),
                       "cannot read the state of the Windows Defender Firewall service (mpssvc)")

    p_off = [dict(p) for p in _FW_PROFILES_ON]
    p_off[2]["Enabled"] = "False"
    _assert_fw_refused(_run_fw(tmp_path / "prof-off", [_FW_PEER], profiles=p_off), "Public", "Set-NetFirewallProfile")

    p_allow = [dict(p) for p in _FW_PROFILES_ON]
    p_allow[0]["DefaultInboundAction"] = "Allow"
    _assert_fw_refused(_run_fw(tmp_path / "prof-allow", [_FW_PEER], profiles=p_allow),
                       "Domain", "DefaultInboundAction=Allow")

    _assert_fw_refused(
        _run_fw(tmp_path / "excl-active", [_FW_PEER], profiles=_fw_profiles(Private=["Wi-Fi"])),
        "ActiveStore Private: 'Wi-Fi'", "0.0.0.0",
        "Set-NetFirewallProfile -Name Private -DisabledInterfaceAliases NotConfigured",
    )
    _assert_fw_refused(
        _run_fw(tmp_path / "excl-persist", [_FW_PEER], persistent=_fw_profiles(_FW_PERSISTENT_ON, Public=["Ethernet 3"])),
        "PersistentStore Public: 'Ethernet 3'", "Set-NetFirewallProfile -Name Public",
    )
    _assert_fw_refused(
        _run_fw(tmp_path / "persist-fail", [_FW_PEER], extra={"persistentFails": True}),
        "cannot read Windows Firewall profiles from the PersistentStore",
    )


def test_fw_comm_port_and_portproxy_and_dynamic_keywords(tmp_path: Path) -> None:
    comm_wide = _fw_rule(name="comm-wide", displayName="Comm open", localPort=["51052"],
                         localAddress=["Any"], remoteAddress=["LocalSubnet"])
    _assert_fw_refused(_run_fw(tmp_path / "comm-wide", [_FW_PEER, comm_wide]), "comm port TCP 51052")

    comm_peer = _fw_rule(name="comm-peer", displayName="Comm from Main", localPort=["51052"],
                         localAddress=["Any"], remoteAddress=["10.77.0.1"])
    assert _run_fw(tmp_path / "comm-peer", [_FW_PEER, comm_peer]).launched

    high_peer = _fw_rule(localPort=["64600"])
    _assert_fw_refused(_run_fw(tmp_path / "high-port", [high_peer], args={"Port": "64600"}), "comm port", "above 65535")

    kw_id = "{3c4f2a11-7e0b-4d2a-9a51-0b7f6c1e2d90}"
    kw_peer = _fw_rule(name="peer-kw", displayName="Peer plus keyword", remoteDynamicKeywordAddresses=[kw_id])
    _assert_fw_refused(_run_fw(tmp_path / "dyn-kw", [_FW_PEER, kw_peer]),
                       "'Peer plus keyword'", f"RemoteDynamicKeywordAddresses={kw_id}")
    _assert_fw_refused(_run_fw(tmp_path / "omit-kw", [_fw_rule(omitKeywordProperty=True)]),
                       "cannot read RemoteDynamicKeywordAddresses")

    header = ["", "Listen on ipv4:             Connect to ipv4:", "",
              "Address         Port        Address         Port",
              "--------------- ----------  --------------- ----------"]
    _assert_fw_refused(
        _run_fw(tmp_path / "pp-main", [_FW_PEER], extra={"portproxyLines": [*header, "0.0.0.0 50052 10.77.0.2 50052", ""]}),
        "netsh interface portproxy", "listen 0.0.0.0:50052", "[v4tov4]",
    )
    _assert_fw_refused(
        _run_fw(tmp_path / "pp-comm", [_FW_PEER], extra={"portproxyLines": [*header, "192.168.1.2 8080 10.77.0.2 51052", ""]}),
        "netsh interface portproxy", "connect 10.77.0.2:51052",
    )
    _assert_fw_refused(
        _run_fw(tmp_path / "pp-bad", [_FW_PEER], extra={"portproxyExit": 1, "portproxyLines": ["error"]}),
        "cannot verify that no port proxy forwards to rpc-server", "exited with code 1",
    )


def test_fw_rule_source_fixes_and_single_quote_escaping(tmp_path: Path) -> None:
    quoted = _fw_rule(name="Bob's open rule", displayName="Quote rule", remoteAddress=["Any"])
    _assert_fw_refused(_run_fw(tmp_path / "quote", [_FW_PEER, quoted]), "Disable-NetFirewallRule -Name 'Bob''s open rule'")

    gpo = _fw_rule(name="gpo-wide", displayName="GPO open", remoteAddress=["Any"],
                   policyStoreSource=r"contoso.com\Worker FW", policyStoreSourceType="GroupPolicy")
    gpo_out = _run_fw(tmp_path / "gpo", [_FW_PEER, gpo])
    _assert_fw_refused(gpo_out, "PolicyStoreSourceType=GroupPolicy", "gpupdate /force")
    assert "Disable-NetFirewallRule -Name 'gpo-wide'" not in gpo_out.error

    mdm = _fw_rule(name="mdm-wide", displayName="MDM open", remoteAddress=["Any"],
                   policyStoreSource="MDM", policyStoreSourceType="MDM")
    mdm_out = _run_fw(tmp_path / "mdm", [_FW_PEER, mdm])
    _assert_fw_refused(mdm_out, "PolicyStoreSourceType=MDM", "MDM firewall policy")
    assert "Disable-NetFirewallRule -Name 'mdm-wide'" not in mdm_out.error


def test_fw_ensure_pins_interface_and_rejects_unresolved_without_removing_rule(tmp_path: Path) -> None:
    ok_out = _run_fw(tmp_path / "ok", [], args={"EnsureFirewall": True, "Port": "50999"})
    created = ok_out.log("new-rule.jsonl")
    assert len(created) == 1
    assert created[0]["LocalPort"] == ["50999"]
    assert created[0]["RemoteAddress"] == ["10.77.0.1"]
    assert created[0]["InterfaceAlias"] == [_FW_BIND_ALIAS]
    assert ok_out.launched, ok_out.error or ok_out.text

    bad_out = _run_fw(
        tmp_path / "two-nics", [_FW_PEER],
        args={"EnsureFirewall": True},
        extra={"ipAliases": ["Ethernet 2", "Wi-Fi"]},
    )
    _assert_fw_refused(bad_out, "found: 'Ethernet 2', 'Wi-Fi'", "no firewall rule was created")
    assert bad_out.log("new-rule.jsonl") == []
    assert bad_out.log("remove-rule.jsonl") == []


def test_fw_refusal_stops_owned_running_worker_and_preserves_unowned_pid(tmp_path: Path) -> None:
    state_dir = tmp_path / "owned" / "state_root" / "run"
    state_dir.mkdir(parents=True)
    exe_path = str(tmp_path / "owned" / "install" / "rpc-server.exe")
    started = "2026-11-25T10:00:00.0000000Z"
    (state_dir / "llama-rpc-worker.json").write_text(
        json.dumps({"pid": 4242, "exe": exe_path, "startedUtc": started}),
        encoding="utf-8",
    )
    owned_extra = {
        "livePids": [4242],
        "processMeta": [{"pid": 4242, "path": exe_path, "startTime": started}],
        "listeners": [{"localAddress": "10.77.0.2", "localPort": 50052, "owningProcess": 4242}],
        "tcpOpen": True,
    }
    refused = _run_fw(tmp_path / "owned", [], extra=owned_extra)
    _assert_fw_refused(refused, "already running (PID 4242)", "exposed by this firewall state, so it has been stopped")
    assert {"Id": 4242} in refused.log("stop.jsonl")
    assert not (state_dir / "llama-rpc-worker.json").exists()

    # Healthy firewall + running worker: check runs first, then exits cleanly with already running.
    state_dir_ok = tmp_path / "healthy" / "state_root" / "run"
    state_dir_ok.mkdir(parents=True)
    (state_dir_ok / "llama-rpc-worker.json").write_text(
        json.dumps({"pid": 4242, "exe": str(tmp_path / "healthy" / "install" / "rpc-server.exe"), "startedUtc": started}),
        encoding="utf-8",
    )
    healthy = _run_fw(tmp_path / "healthy", [_FW_PEER], extra=owned_extra)
    assert healthy.returncode == 0, healthy.error or healthy.text
    assert "already running (PID 4242)" in healthy.text
    assert healthy.log("get-rule.jsonl")
    assert healthy.log("stop.jsonl") == []
    assert not healthy.launched

    # Recycled/unrelated PID holding state PID is NOT stopped.
    state_dir_for = tmp_path / "foreign" / "state_root" / "run"
    state_dir_for.mkdir(parents=True)
    (state_dir_for / "llama-rpc-worker.json").write_text(
        json.dumps({"pid": 4242, "exe": str(tmp_path / "foreign" / "install" / "rpc-server.exe"), "startedUtc": started}),
        encoding="utf-8",
    )
    foreign_extra = {
        "livePids": [4242],
        "processMeta": [{"pid": 4242, "path": r"C:\Windows\System32\svchost.exe", "startTime": started}],
        "listeners": [{"localAddress": "10.77.0.2", "localPort": 50052, "owningProcess": 4242}],
    }
    foreign_out = _run_fw(tmp_path / "foreign", [], extra=foreign_extra)
    _assert_fw_refused(foreign_out, "stopping it FAILED")
    assert foreign_out.log("stop.jsonl") == []


