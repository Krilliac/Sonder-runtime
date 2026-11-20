"""Start in scripts/rpc/start-node1-rpc.ps1 must fail closed on the Node1 firewall.

rpc-server is unauthenticated, so Start may only launch it when Windows Firewall
admits TCP <port> solely from Main (10.77.0.1). Each test runs the real script
under powershell.exe through a wrapper that replaces every firewall, process
and socket cmdlet with an in-memory mock (PowerShell resolves functions before
cmdlets), so no firewall rule is read from or written to this machine, no
process is launched and no TCP connection is made.

Outcome markers:
- launch reached: the Invoke-CimMethod mock (the Win32_Process Create call
  that would start rpc-server) wrote ``launch.json``.
- refused: the script exited non-zero before that, with the refusal message.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
START_SCRIPT = REPO / "scripts" / "rpc" / "start-node1-rpc.ps1"
ENSURE_SCRIPT = REPO / "scripts" / "rpc" / "ensure-node1-rpc-fw.ps1"

pytestmark = pytest.mark.skipif(
    os.name != "nt" or shutil.which("powershell") is None,
    reason="Windows PowerShell script; needs powershell.exe",
)

WRAPPER = r"""
param([Parameter(Mandatory = $true)][string]$ScenarioPath)
$ErrorActionPreference = 'Stop'
$scenario = Get-Content -LiteralPath $ScenarioPath -Raw | ConvertFrom-Json
$global:MockOut = [string]$scenario.outDir
$global:MockRules = New-Object System.Collections.ArrayList
foreach ($r in @($scenario.rules)) { if ($null -ne $r) { [void]$global:MockRules.Add($r) } }
$global:MockProfiles = @($scenario.profiles)
$global:MockPersistentProfiles = @($scenario.persistentProfiles)
$global:MockPersistentFails = [bool]$scenario.persistentFails
$global:MockProcesses = @($scenario.processes)
$global:MockLaunchPid = [int]$scenario.launchPid
$global:MockCimFails = [bool]$scenario.cimFails
$global:MockEnsureFails = [bool]$scenario.ensureFails
$global:MockRuleReadFails = [bool]$scenario.ruleReadFails
$global:MockIpFails = [bool]$scenario.ipFails
$global:MockIpAliases = @($scenario.ipAliases)
$global:MockTcpOpen = [bool]$scenario.tcpOpen
$global:MockStopFails = [bool]$scenario.stopFails
$global:MockLivePids = New-Object System.Collections.ArrayList
foreach ($p in @($scenario.livePids)) { if ($null -ne $p) { [void]$global:MockLivePids.Add([int]$p) } }
$global:MockListeners = New-Object System.Collections.ArrayList
foreach ($l in @($scenario.listeners)) { if ($null -ne $l) { [void]$global:MockListeners.Add($l) } }
# netsh interface portproxy show all: no entries prints one blank line (seen on a real PC).
$global:MockPortproxyLines = if ($null -ne $scenario.portproxyLines) { @($scenario.portproxyLines) } else { @('') }
$global:MockPortproxyExit = [int]$scenario.portproxyExit
$global:MockPortproxyThrows = [bool]$scenario.portproxyThrows
$global:MockServiceStatus = if ($null -ne $scenario.serviceStatus) { [string]$scenario.serviceStatus } else { 'Running' }
$global:MockServiceFails = [bool]$scenario.serviceFails

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
    # The real rule object carries RemoteDynamicKeywordAddresses as string[] (empty when unset).
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
    }
}

function Get-MockFilter($InputRule, [bool]$All, [string]$Kind) {
    if ($All) {
        foreach ($r in $global:MockRules) { if (-not $r.hiddenFromAll) { New-MockFilter $r $Kind } }
        return
    }
    $r = $global:MockRules | Where-Object { $_.name -eq $InputRule.InstanceID } | Select-Object -First 1
    if ($null -eq $r) { Write-Error -Message "mock: no rule $($InputRule.InstanceID)" -Category ObjectNotFound; return }
    if ($r.failPerRule) { Write-Error -Message 'mock: Access is denied.' -Category PermissionDenied; return }
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
    if ($out.Count -eq 0) { Write-Error -Message 'No MSFT_NetFirewallRule objects found' -Category ObjectNotFound; return }
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
        if ($global:MockLivePids -contains $i) { [pscustomobject]@{ Id = $i } }
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
    # launchPid > 0: the launch "succeeds" with that wrapper PID (no process is started).
    if ($global:MockLaunchPid -gt 0) { return [pscustomobject]@{ ReturnValue = 0; ProcessId = $global:MockLaunchPid } }
    [pscustomobject]@{ ReturnValue = 9; ProcessId = 0 }
}
function Get-CimInstance {
    [CmdletBinding()]
    param([string]$ClassName, [string]$Filter)
    Write-MockLog 'get-cim.jsonl' @{ ClassName = $ClassName }
    if ($global:MockCimFails) { Write-Error -Message 'mock: Access is denied.' -Category PermissionDenied; return }
    foreach ($proc in @($global:MockProcesses)) {
        if ($null -ne $proc) {
            [pscustomobject]@{ ProcessId = [int]$proc.pid; ParentProcessId = [int]$proc.parent; CreationDate = [datetime]$proc.created }
        }
    }
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
    # Only the TcpClient used by Test-TcpPort is mocked: it never connects.
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

# Refuse to run the real script unless every side-effecting command is the mock.
foreach ($cmd in 'Get-NetFirewallRule', 'Get-NetFirewallPortFilter', 'Get-NetFirewallAddressFilter',
                 'Get-NetFirewallApplicationFilter', 'Get-NetFirewallProfile', 'Remove-NetFirewallRule',
                 'New-NetFirewallRule', 'Get-NetTCPConnection', 'Invoke-CimMethod', 'New-Object',
                 'Get-NetIPAddress', 'Get-Process', 'Stop-Process', 'Wait-Process', 'Get-CimInstance',
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

PEER = {
    "name": "peer",
    "displayName": "Node1 llama-rpc 50052 from Main peer",
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
    "policyStoreSource": "PersistentStore",
    "policyStoreSourceType": "Local",
}

# ActiveStore profiles report DisabledInterfaceAliases as null when nothing is excluded.
PROFILES_ON = [
    {"Name": n, "Enabled": "True", "DefaultInboundAction": "Block", "DisabledInterfaceAliases": None}
    for n in ("Domain", "Private", "Public")
]
# PersistentStore profiles report DisabledInterfaceAliases as {NotConfigured} (seen on a real PC).
PERSISTENT_ON = [
    {"Name": n, "Enabled": "True", "DefaultInboundAction": "NotConfigured", "DisabledInterfaceAliases": ["NotConfigured"]}
    for n in ("Domain", "Private", "Public")
]
BIND_ALIAS = "Private Link"


def _rule(**overrides: object) -> dict:
    rule = dict(PEER)
    rule.update(overrides)
    return rule


class Outcome:
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


def _run(
    tmp_path: Path,
    rules: list[dict],
    *,
    script: Path = START_SCRIPT,
    args: dict | None = None,
    profiles: list[dict] | None = None,
    persistent: list[dict] | None = None,
    ensure_fails: bool = False,
    extra: dict | None = None,
) -> Outcome:
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    install = tmp_path / "install"
    install.mkdir()
    (install / "rpc-server.exe").write_bytes(b"")
    script_args = {
        "Action": "Start",
        "HostAddress": "10.77.0.2",
        "InstallDir": str(install),
        "RootDir": str(tmp_path / "root"),
    }
    script_args.update(args or {})
    scenario = {
        "outDir": str(out_dir),
        "script": str(script),
        "rules": rules,
        "profiles": PROFILES_ON if profiles is None else profiles,
        "persistentProfiles": PERSISTENT_ON if persistent is None else persistent,
        "ensureFails": ensure_fails,
        "ipAliases": [BIND_ALIAS],
        "args": script_args,
    }
    scenario.update(extra or {})
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
    wrapper = tmp_path / "wrapper.ps1"
    wrapper.write_text(WRAPPER, encoding="utf-8")
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-File", str(wrapper), "-ScenarioPath", str(scenario_path)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    outcome = Outcome(proc, out_dir)
    # The wrapper aborts before running the script if any mock is not active.
    assert "is not active" not in outcome.text and "mock New-Object: unexpected" not in outcome.text, outcome.text
    return outcome


def _assert_refused(outcome: Outcome, *needles: str) -> None:
    assert outcome.returncode != 0, outcome.text
    assert not outcome.launched, outcome.error or outcome.text
    assert outcome.error.startswith("Refusing to start rpc-server"), outcome.error or outcome.text
    for needle in needles:
        assert needle in outcome.error, outcome.error


def test_peer_rule_only_allows_start(tmp_path: Path) -> None:
    outcome = _run(tmp_path, [PEER])
    assert outcome.launched, outcome.error or outcome.text
    # The launch mock returns a failure code, so the script stops right after it.
    assert "Win32_Process Create failed" in outcome.error, outcome.error


def test_no_rule_refuses_and_names_the_fix(tmp_path: Path) -> None:
    outcome = _run(tmp_path, [])
    _assert_refused(outcome, "no enabled inbound Allow rule", "ensure-node1-rpc-fw.ps1", "-Port 50052")


def test_peer_rule_plus_wide_rule_on_same_port_refuses(tmp_path: Path) -> None:
    wide = _rule(name="wide", displayName="Open 50052", remoteAddress=["Any"])
    outcome = _run(tmp_path, [PEER, wide])
    _assert_refused(outcome, "'Open 50052'", "Disable-NetFirewallRule -Name 'wide'")


def test_local_subnet_port_range_rule_refuses(tmp_path: Path) -> None:
    wide = _rule(name="lan", displayName="LAN range", localPort=["50000-50100"], remoteAddress=["LocalSubnet"])
    _assert_refused(_run(tmp_path, [PEER, wide]), "'LAN range'")


def test_program_rule_for_rpc_server_from_any_refuses(tmp_path: Path) -> None:
    # What the "allow this app" prompt creates: program-scoped, any port, any remote.
    exe = str(tmp_path / "install" / "RPC-SERVER.EXE")
    prog = _rule(name="prog", displayName="rpc-server.exe", protocol="Any", localPort=["Any"],
                 localAddress=["Any"], remoteAddress=["Any"], program=exe)
    _assert_refused(_run(tmp_path, [PEER, prog]), "'rpc-server.exe'")


def test_rule_for_a_different_port_refuses(tmp_path: Path) -> None:
    other = _rule(name="other", displayName="peer 51000", localPort=["51000"])
    _assert_refused(_run(tmp_path, [other]), "no enabled inbound Allow rule admits exactly TCP 50052")


def test_peer_rule_must_match_a_custom_port(tmp_path: Path) -> None:
    # Default-port rule present, Start on 50999: not covered.
    _assert_refused(_run(tmp_path, [PEER], args={"Port": 50999}), "TCP 50999", "-Port 50999")


def test_peer_rule_with_extra_remote_is_not_peer_only(tmp_path: Path) -> None:
    loose = _rule(name="loose", displayName="peer plus", remoteAddress=["10.77.0.1", "192.168.1.0/24"])
    _assert_refused(_run(tmp_path, [loose]), "'peer plus'", "no enabled inbound Allow rule")


def test_unrelated_rules_do_not_block_start(tmp_path: Path) -> None:
    rules = [
        PEER,
        _rule(name="other-prog", displayName="Other app", protocol="Any", localPort=["Any"],
              remoteAddress=["Any"], program=r"C:\Other\app.exe"),
        _rule(name="store", displayName="Store app", protocol="Any", localPort=["Any"], remoteAddress=["Any"],
              packageFamilyName="Microsoft.WindowsStore_8wekyb3d8bbwe"),
        _rule(name="udp", displayName="UDP any", protocol="UDP", localPort=["Any"], remoteAddress=["Any"]),
        _rule(name="disabled", displayName="Disabled wide", enabled="False", remoteAddress=["Any"]),
        _rule(name="block", displayName="Block wide", action="Block", remoteAddress=["Any"]),
    ]
    outcome = _run(tmp_path, rules)
    assert outcome.launched, outcome.error or outcome.text


def test_wide_rule_missing_from_bulk_read_is_still_found(tmp_path: Path) -> None:
    hidden = _rule(name="hidden", displayName="Hidden wide", remoteAddress=["Any"], hiddenFromAll=True)
    _assert_refused(_run(tmp_path, [PEER, hidden]), "'Hidden wide'")


def test_unreadable_rule_filter_refuses(tmp_path: Path) -> None:
    denied = _rule(name="denied", displayName="Denied", hiddenFromAll=True, failPerRule=True)
    _assert_refused(_run(tmp_path, [PEER, denied]), "cannot read the Application filter", "'Denied'")


def test_firewall_profile_off_refuses(tmp_path: Path) -> None:
    profiles = [dict(p) for p in PROFILES_ON]
    profiles[2]["Enabled"] = "False"
    _assert_refused(_run(tmp_path, [PEER], profiles=profiles), "Public", "Set-NetFirewallProfile")


def test_ensure_firewall_with_missing_helper_refuses(tmp_path: Path) -> None:
    lone = tmp_path / "lone"
    lone.mkdir()
    copy = lone / START_SCRIPT.name
    shutil.copyfile(START_SCRIPT, copy)
    outcome = _run(tmp_path, [PEER], script=copy, args={"EnsureFirewall": True})
    _assert_refused(outcome, "is missing", "ensure-node1-rpc-fw.ps1")


def test_ensure_firewall_passes_port_then_verifies(tmp_path: Path) -> None:
    outcome = _run(tmp_path, [], args={"EnsureFirewall": True, "Port": 50999})
    created = outcome.log("new-rule.jsonl")
    assert len(created) == 1, outcome.text
    assert created[0]["LocalPort"] == ["50999"]
    assert created[0]["RemoteAddress"] == ["10.77.0.1"]
    assert created[0]["DisplayName"] == "Node1 llama-rpc 50999 from Main peer"
    assert outcome.launched, outcome.error or outcome.text


def test_ensure_firewall_default_port_keeps_rule_name(tmp_path: Path) -> None:
    outcome = _run(tmp_path, [], args={"EnsureFirewall": True})
    created = outcome.log("new-rule.jsonl")
    assert [c["DisplayName"] for c in created] == ["Node1 llama-rpc 50052 from Main peer"]
    assert created[0]["LocalPort"] == ["50052"]
    assert outcome.launched, outcome.error or outcome.text


def test_ensure_firewall_failure_refuses(tmp_path: Path) -> None:
    outcome = _run(tmp_path, [], args={"EnsureFirewall": True}, ensure_fails=True)
    _assert_refused(outcome, "ensure-node1-rpc-fw.ps1 -Port 50052' failed", "Access is denied")


def test_ensure_script_is_peer_only_and_parameterised() -> None:
    text = ENSURE_SCRIPT.read_text(encoding="utf-8")
    assert "[int]$Port = 50052" in text
    assert "-LocalPort $Port" in text
    assert "-RemoteAddress 10.77.0.1" in text
    assert "-LocalAddress 10.77.0.2" in text


def _profiles(base: list[dict] | None = None, **aliases: object) -> list[dict]:
    """PROFILES_ON (or ``base``) with DisabledInterfaceAliases set per profile name."""
    profiles = [dict(p) for p in (PROFILES_ON if base is None else base)]
    for p in profiles:
        if p["Name"] in aliases:
            p["DisabledInterfaceAliases"] = aliases[p["Name"]]
    return profiles


# --- Any interface excluded from filtering (DisabledInterfaceAliases) ---
# rpc-server's COMM_INIT listener binds 0.0.0.0 (ggml-rpc.cpp b12494 :2352), so
# an exclusion of ANY interface leaves it unfiltered there: every entry other
# than NotConfigured refuses, in either store, whatever it names.


def test_bind_interface_excluded_on_one_profile_refuses(tmp_path: Path) -> None:
    profiles = _profiles(Domain=["NotConfigured"], Public=["Wi-Fi", "PRIVATE LINK"])
    outcome = _run(tmp_path, [PEER], profiles=profiles)
    _assert_refused(outcome, "exclude interfaces from filtering", "ActiveStore Public: 'PRIVATE LINK'",
                    "ActiveStore Public: 'Wi-Fi'", "0.0.0.0",
                    "Set-NetFirewallProfile -Name Public -DisabledInterfaceAliases NotConfigured")
    assert "Domain" not in outcome.error, outcome.error


def test_unrelated_interface_excluded_on_one_profile_refuses(tmp_path: Path) -> None:
    """Flipped from cycle 2 (which let this start): an excluded Wi-Fi is not the
    private link, but the firewall does not filter Wi-Fi at all, and the comm
    listener rpc-server opens on COMM_INIT binds 0.0.0.0, so it would be open to
    anything on Wi-Fi. Any exclusion now refuses, whatever interface it names."""
    profiles = _profiles(Domain=["NotConfigured"], Private=["Wi-Fi", "vEthernet (Default Switch)"])
    outcome = _run(tmp_path, [PEER], profiles=profiles)
    _assert_refused(outcome, "ActiveStore Private: 'Wi-Fi'", "ActiveStore Private: 'vEthernet (Default Switch)'",
                    "Set-NetFirewallProfile -Name Private -DisabledInterfaceAliases NotConfigured")


def test_exclusion_only_in_persistent_store_refuses(tmp_path: Path) -> None:
    persistent = _profiles(PERSISTENT_ON, Public=["Ethernet 3"])
    outcome = _run(tmp_path, [PEER], persistent=persistent)
    _assert_refused(outcome, "PersistentStore Public: 'Ethernet 3'", "Set-NetFirewallProfile -Name Public")
    assert "ActiveStore Public:" not in outcome.error, outcome.error
    assert {e["PolicyStore"] for e in outcome.log("get-profile.jsonl")} == {"ActiveStore", "PersistentStore"}


def test_exclusions_from_both_stores_are_united(tmp_path: Path) -> None:
    outcome = _run(tmp_path, [PEER], profiles=_profiles(Domain=["Wi-Fi"]),
                   persistent=_profiles(PERSISTENT_ON, Private=["Ethernet 3"]))
    _assert_refused(outcome, "ActiveStore Domain: 'Wi-Fi'", "PersistentStore Private: 'Ethernet 3'",
                    "Set-NetFirewallProfile -Name Domain,Private")


def test_persistent_store_unreadable_refuses(tmp_path: Path) -> None:
    outcome = _run(tmp_path, [PEER], extra={"persistentFails": True})
    _assert_refused(outcome, "cannot read Windows Firewall profiles from the PersistentStore", "Access is denied")


def test_persistent_store_without_profiles_refuses(tmp_path: Path) -> None:
    _assert_refused(_run(tmp_path, [PEER], persistent=[]), "no profiles from the PersistentStore")


def test_persistent_alias_list_unreadable_refuses(tmp_path: Path) -> None:
    persistent = _profiles(PERSISTENT_ON)
    del persistent[2]["DisabledInterfaceAliases"]
    _assert_refused(_run(tmp_path, [PEER], persistent=persistent),
                    "cannot read DisabledInterfaceAliases", "in the PersistentStore: Public")


def test_guid_form_exclusion_refuses(tmp_path: Path) -> None:
    guid = "{4d36e972-e325-11ce-bfc1-08002be10318}"
    outcome = _run(tmp_path, [PEER], profiles=_profiles(Public=[guid]))
    _assert_refused(outcome, f"ActiveStore Public: '{guid}'")


def test_not_configured_only_allows_start(tmp_path: Path) -> None:
    profiles = _profiles(Domain=["NotConfigured"], Private=["NotConfigured"], Public=["NotConfigured"])
    outcome = _run(tmp_path, [PEER], profiles=profiles, persistent=_profiles(PERSISTENT_ON))
    assert outcome.launched, outcome.error or outcome.text


def test_disabled_interface_aliases_unreadable_refuses(tmp_path: Path) -> None:
    profiles = _profiles()
    del profiles[1]["DisabledInterfaceAliases"]
    _assert_refused(_run(tmp_path, [PEER], profiles=profiles), "cannot read DisabledInterfaceAliases",
                    "in the ActiveStore: Private")


def test_start_no_longer_depends_on_resolving_the_bind_interface(tmp_path: Path) -> None:
    # The bind-alias match is gone: Start never asks Get-NetIPAddress.
    outcome = _run(tmp_path, [PEER], extra={"ipFails": True})
    assert outcome.launched, outcome.error or outcome.text
    assert outcome.log("get-ip.jsonl") == []


# --- A refused Start stops an instance that is already running ---


def _running_instance(tmp_path: Path, *, stop_fails: bool = False) -> dict:
    state_dir = tmp_path / "root" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "llama-rpc-server.json").write_text(json.dumps({"pid": 4242, "wrapperPid": 4241}), encoding="utf-8")
    return {
        "livePids": [4242, 4241],
        "listeners": [{"localAddress": "10.77.0.2", "localPort": 50052, "owningProcess": 4242}],
        "tcpOpen": True,
        "stopFails": stop_fails,
    }


def test_refusal_stops_an_already_running_instance(tmp_path: Path) -> None:
    extra = _running_instance(tmp_path)
    outcome = _run(tmp_path, [], extra=extra)
    _assert_refused(outcome, "no enabled inbound Allow rule", "already running (PID 4242)",
                    "exposed by this firewall state, so it has been stopped")
    stopped = {e["Id"] for e in outcome.log("stop.jsonl")}
    assert {4242, 4241} <= stopped, outcome.log("stop.jsonl")
    assert not (tmp_path / "root" / "state" / "llama-rpc-server.json").exists()


def test_refusal_reports_a_running_instance_it_could_not_stop(tmp_path: Path) -> None:
    extra = _running_instance(tmp_path, stop_fails=True)
    outcome = _run(tmp_path, [], extra=extra)
    _assert_refused(outcome, "already running (PID 4242)", "stopping it FAILED", "Stop-Process -Id 4242 -Force")


def test_firewall_check_runs_before_the_already_running_exit(tmp_path: Path) -> None:
    # Healthy firewall + running instance: the check still runs, then "already running".
    outcome = _run(tmp_path, [PEER], extra=_running_instance(tmp_path))
    assert outcome.returncode == 0, outcome.error or outcome.text
    assert "already running (PID 4242)" in outcome.text, outcome.text
    assert outcome.log("get-rule.jsonl"), "firewall rules were never read"
    assert outcome.log("stop.jsonl") == []
    assert not outcome.launched


# --- rpc-server program rules in any path form ---


@pytest.mark.parametrize(
    "program",
    [
        r"\Device\HarddiskVolume3\NewBuild\llama-rpc\rpc-server.exe",
        r"C:\NEWBUI~1\LLAMA-~1\GGML-RPC-SERVER.EXE",
        r"D:\links\llama\rpc-server.exe",
        r"%SystemDrive%\Other\Rpc-Server.exe",
        "C:/NewBuild/llama-rpc/rpc-server.exe",
        r"C:\NewBuild\llama-rpc\RPC-SE~1.EXE",
    ],
)
def test_rpc_server_program_rule_in_any_path_form_refuses(tmp_path: Path, program: str) -> None:
    prog = _rule(name="prog", displayName="rpc program rule", protocol="Any", localPort=["Any"],
                 localAddress=["Any"], remoteAddress=["Any"], program=program)
    _assert_refused(_run(tmp_path, [PEER, prog]), "'rpc program rule'", "Disable-NetFirewallRule -Name 'prog'")


# --- Rule source decides the suggested fix ---


def test_local_wide_rule_reports_its_source(tmp_path: Path) -> None:
    wide = _rule(name="wide", displayName="Open 50052", remoteAddress=["Any"])
    _assert_refused(_run(tmp_path, [PEER, wide]), "PolicyStoreSource=PersistentStore",
                    "PolicyStoreSourceType=Local", "Disable-NetFirewallRule -Name 'wide'")


def test_gpo_wide_rule_suggests_fixing_the_gpo(tmp_path: Path) -> None:
    gpo = _rule(name="gpo-wide", displayName="GPO open 50052", remoteAddress=["Any"],
                policyStoreSource=r"contoso.com\Node1 Firewall", policyStoreSourceType="GroupPolicy")
    outcome = _run(tmp_path, [PEER, gpo])
    _assert_refused(outcome, r"PolicyStoreSource=contoso.com\Node1 Firewall", "PolicyStoreSourceType=GroupPolicy",
                    r"Group Policy 'contoso.com\Node1 Firewall'", "gpupdate /force")
    assert "Disable-NetFirewallRule -Name 'gpo-wide'" not in outcome.error, outcome.error


def test_mdm_wide_rule_suggests_fixing_the_mdm_policy(tmp_path: Path) -> None:
    mdm = _rule(name="mdm-wide", displayName="MDM open", localPort=["Any"], remoteAddress=["Any"],
                policyStoreSource="MDM", policyStoreSourceType="MDM")
    outcome = _run(tmp_path, [PEER, mdm])
    _assert_refused(outcome, "PolicyStoreSourceType=MDM", "pushed by MDM", "MDM firewall policy")
    assert "Disable-NetFirewallRule -Name 'mdm-wide'" not in outcome.error, outcome.error


# --- Remaining coverage gaps from review ---


def test_default_inbound_allow_refuses(tmp_path: Path) -> None:
    profiles = [dict(p) for p in PROFILES_ON]
    profiles[0]["DefaultInboundAction"] = "Allow"
    _assert_refused(_run(tmp_path, [PEER], profiles=profiles), "Domain", "DefaultInboundAction=Allow",
                    "Set-NetFirewallProfile -All -Enabled True -DefaultInboundAction Block")


def test_empty_profile_list_refuses(tmp_path: Path) -> None:
    _assert_refused(_run(tmp_path, [PEER], profiles=[]), "returned no profiles")


def test_tcp_any_port_any_program_rule_refuses(tmp_path: Path) -> None:
    wide = _rule(name="tcp-any", displayName="TCP any port", localPort=["Any"], localAddress=["Any"],
                 remoteAddress=["Any"])
    _assert_refused(_run(tmp_path, [PEER, wide]), "'TCP any port'", "LocalPort=Any")


def test_keyword_port_rule_refuses(tmp_path: Path) -> None:
    wide = _rule(name="kw", displayName="RPC dynamic", localPort=["RPC"], remoteAddress=["Any"])
    _assert_refused(_run(tmp_path, [PEER, wide]), "'RPC dynamic'", "LocalPort=RPC")


def test_multi_port_list_rule_refuses(tmp_path: Path) -> None:
    wide = _rule(name="multi", displayName="Web plus rpc", localPort=["80", "50052"], remoteAddress=["Any"])
    _assert_refused(_run(tmp_path, [PEER, wide]), "'Web plus rpc'", "LocalPort=80,50052")


def test_rule_read_access_denied_refuses(tmp_path: Path) -> None:
    outcome = _run(tmp_path, [PEER], extra={"ruleReadFails": True})
    _assert_refused(outcome, "cannot read Windows Firewall rules", "Access is denied")


def test_ensure_helper_exiting_non_zero_refuses(tmp_path: Path) -> None:
    alt = tmp_path / "alt"
    alt.mkdir()
    copy = alt / START_SCRIPT.name
    shutil.copyfile(START_SCRIPT, copy)
    (alt / "ensure-node1-rpc-fw.ps1").write_text("param([int]$Port)\nexit 3\n", encoding="utf-8")
    outcome = _run(tmp_path, [PEER], script=copy, args={"EnsureFirewall": True})
    _assert_refused(outcome, "ensure-node1-rpc-fw.ps1 -Port 50052' exited with code 3")


# --- The comm port (<Port> + 1000) is protected like <Port> ---
# b12494: COMM_INIT makes the rank-0 rpc-server listen on 0.0.0.0:port0+1000
# (ggml-rpc.cpp :2352, :3258), so 51052 for the default port.

COMM_PORT_SHAPES = [
    pytest.param({"localPort": ["51052"]}, id="exact-port"),
    pytest.param({"localPort": ["51000-51100"]}, id="range"),
    pytest.param({"protocol": "Any", "localPort": ["51052"]}, id="protocol-any"),
    pytest.param({"localPort": ["80", "51052"]}, id="port-list"),
]


@pytest.mark.parametrize("remote", ["LocalSubnet", "Any"])
@pytest.mark.parametrize("shape", COMM_PORT_SHAPES)
def test_rule_covering_only_the_comm_port_from_beyond_peer_refuses(tmp_path: Path, shape: dict, remote: str) -> None:
    # program=Any (from PEER): the rule is not bound to another program.
    wide = _rule(name="comm-wide", displayName="Comm port open", remoteAddress=[remote], localAddress=["Any"], **shape)
    outcome = _run(tmp_path, [PEER, wide])
    _assert_refused(outcome, "'Comm port open'", "comm port TCP 51052", "Disable-NetFirewallRule -Name 'comm-wide'")


@pytest.mark.parametrize("shape", COMM_PORT_SHAPES)
def test_rule_covering_the_comm_port_from_exactly_peer_allows_start(tmp_path: Path, shape: dict) -> None:
    comm = _rule(name="comm-peer", displayName="Comm port from Main", remoteAddress=["10.77.0.1"],
                 localAddress=["Any"], **shape)
    outcome = _run(tmp_path, [PEER, comm])
    assert outcome.launched, outcome.error or outcome.text


def test_comm_port_follows_a_custom_port(tmp_path: Path) -> None:
    peer = _rule(localPort=["50999"])
    wide = _rule(name="comm-wide", displayName="Comm 51999 open", localPort=["51999"], remoteAddress=["Any"])
    _assert_refused(_run(tmp_path, [peer, wide], args={"Port": 50999}), "'Comm 51999 open'", "comm port TCP 51999")


def test_port_whose_comm_port_does_not_fit_refuses(tmp_path: Path) -> None:
    peer = _rule(localPort=["64600"])
    _assert_refused(_run(tmp_path, [peer], args={"Port": 64600}), "comm port", "above 65535")


# --- Fix commands quote rule names safely ---


def test_rule_name_with_single_quote_is_escaped_in_fix_command(tmp_path: Path) -> None:
    wide = _rule(name="Bob's open rule", displayName="Quote rule", remoteAddress=["Any"])
    outcome = _run(tmp_path, [PEER, wide])
    _assert_refused(outcome, "Disable-NetFirewallRule -Name 'Bob''s open rule'")
    assert "-Name 'Bob's" not in outcome.error, outcome.error


# --- No-listener timeout stops the whole process tree ---


def test_listen_timeout_stops_the_whole_process_tree(tmp_path: Path) -> None:
    # Wrapper 5000 -> rpc-server 5001 -> helper 5002; 6000 is unrelated; 7000 claims
    # parent 5000 but is older than it (a recycled PID), so it is not part of the tree.
    processes = [
        {"pid": 5000, "parent": 900, "created": "2026-11-19T10:00:00"},
        {"pid": 5001, "parent": 5000, "created": "2026-11-19T10:00:01"},
        {"pid": 5002, "parent": 5001, "created": "2026-11-19T10:00:02"},
        {"pid": 6000, "parent": 900, "created": "2026-11-19T10:00:03"},
        {"pid": 7000, "parent": 5000, "created": "2026-11-19T09:00:00"},
    ]
    extra = {"launchPid": 5000, "livePids": [5000, 5001, 5002, 6000, 7000], "processes": processes}
    outcome = _run(tmp_path, [PEER], extra=extra)
    assert outcome.returncode != 0, outcome.text
    assert outcome.launched
    assert "did not start listening on 10.77.0.2:50052 within 20s" in outcome.error, outcome.error
    assert "stopped the wrapper (PID 5000) and its child processes" in outcome.error, outcome.error
    stopped = [e["Id"] for e in outcome.log("stop.jsonl")]
    assert sorted(stopped) == [5000, 5001, 5002], stopped
    # Deepest first: the wrapper goes last.
    assert stopped[-1] == 5000, stopped
    assert outcome.log("get-cim.jsonl") == [{"ClassName": "Win32_Process"}]


def test_listen_timeout_reports_when_the_process_tree_cannot_be_read(tmp_path: Path) -> None:
    extra = {"launchPid": 5000, "livePids": [5000], "cimFails": True}
    outcome = _run(tmp_path, [PEER], extra=extra)
    assert "did not start listening" in outcome.error, outcome.error
    assert "Could not list its child processes" in outcome.error, outcome.error
    assert "check for a stray rpc-server process" in outcome.error, outcome.error
    assert [e["Id"] for e in outcome.log("stop.jsonl")] == [5000]


# --- ensure-node1-rpc-fw.ps1 pins the peer rule to the private-link interface ---


def test_ensure_pins_the_rule_to_the_interface_owning_the_bind_address(tmp_path: Path) -> None:
    outcome = _run(tmp_path, [], args={"EnsureFirewall": True})
    created = outcome.log("new-rule.jsonl")
    assert len(created) == 1, outcome.text
    assert created[0]["InterfaceAlias"] == [BIND_ALIAS]
    assert created[0]["LocalAddress"] == ["10.77.0.2"]
    assert {"IPAddress": "10.77.0.2"} in outcome.log("get-ip.jsonl")
    assert outcome.launched, outcome.error or outcome.text


@pytest.mark.parametrize(
    ("extra", "needle"),
    [
        pytest.param({"ipFails": True}, "Cannot resolve the interface that owns 10.77.0.2", id="lookup-error"),
        pytest.param({"ipAliases": []}, "(found: none)", id="no-interface"),
        pytest.param({"ipAliases": ["Ethernet 2", "Wi-Fi"]}, "(found: 'Ethernet 2', 'Wi-Fi')", id="two-interfaces"),
        pytest.param({"ipAliases": ["Link[1]"]}, "contains wildcard characters", id="wildcard-name"),
    ],
)
def test_ensure_fails_without_creating_a_rule_when_the_interface_is_unresolved(
    tmp_path: Path, extra: dict, needle: str
) -> None:
    outcome = _run(tmp_path, [PEER], args={"EnsureFirewall": True}, extra=extra)
    _assert_refused(outcome, "ensure-node1-rpc-fw.ps1 -Port 50052' failed", needle, "no firewall rule was created")
    assert outcome.log("new-rule.jsonl") == []
    # The existing rule is left alone (removal happens only after the interface is resolved).
    assert outcome.log("remove-rule.jsonl") == []


def test_existing_unpinned_peer_rule_still_allows_start(tmp_path: Path) -> None:
    # PEER carries no interface condition: Start does not add a refusal for that.
    outcome = _run(tmp_path, [PEER], extra={"ipFails": True})
    assert outcome.launched, outcome.error or outcome.text


# --- Dynamic keyword addresses widen a rule's remote address ---
# A rule admits its RemoteAddress plus every address its RemoteDynamicKeywordAddresses
# resolve to, so any keyword makes it not peer-only.

KEYWORD_ID = "{3c4f2a11-7e0b-4d2a-9a51-0b7f6c1e2d90}"


def test_peer_shaped_rule_with_a_dynamic_keyword_refuses(tmp_path: Path) -> None:
    kw_peer = _rule(name="peer-kw", displayName="Peer plus keyword", remoteDynamicKeywordAddresses=[KEYWORD_ID])
    outcome = _run(tmp_path, [kw_peer])
    _assert_refused(outcome, "'Peer plus keyword'", f"RemoteDynamicKeywordAddresses={KEYWORD_ID}",
                    "never peer-only", "Disable-NetFirewallRule -Name 'peer-kw'",
                    "no enabled inbound Allow rule admits exactly TCP 50052")


def test_peer_rule_plus_a_peer_shaped_keyword_rule_refuses(tmp_path: Path) -> None:
    kw_peer = _rule(name="peer-kw", displayName="Peer plus keyword", remoteDynamicKeywordAddresses=[KEYWORD_ID])
    outcome = _run(tmp_path, [PEER, kw_peer])
    _assert_refused(outcome, "'Peer plus keyword'", f"RemoteDynamicKeywordAddresses={KEYWORD_ID}")
    assert "no enabled inbound Allow rule admits exactly" not in outcome.error, outcome.error


def test_comm_port_rule_from_the_peer_with_a_dynamic_keyword_refuses(tmp_path: Path) -> None:
    comm = _rule(name="comm-kw", displayName="Comm from Main plus keyword", localPort=["51052"],
                 localAddress=["Any"], remoteAddress=["10.77.0.1"], remoteDynamicKeywordAddresses=[KEYWORD_ID])
    outcome = _run(tmp_path, [PEER, comm])
    _assert_refused(outcome, "'Comm from Main plus keyword'", "comm port TCP 51052",
                    f"RemoteDynamicKeywordAddresses={KEYWORD_ID}", "Disable-NetFirewallRule -Name 'comm-kw'")


def test_peer_rule_with_an_empty_keyword_list_allows_start(tmp_path: Path) -> None:
    outcome = _run(tmp_path, [_rule(remoteDynamicKeywordAddresses=[])])
    assert outcome.launched, outcome.error or outcome.text


def test_unreadable_keyword_property_refuses(tmp_path: Path) -> None:
    outcome = _run(tmp_path, [_rule(omitKeywordProperty=True)])
    _assert_refused(outcome, "cannot read RemoteDynamicKeywordAddresses", "'Node1 llama-rpc 50052 from Main peer'")


# --- netsh interface portproxy must not forward the rpc ports ---

PORTPROXY_HEADER = ["", "Listen on ipv4:             Connect to ipv4:", "",
                    "Address         Port        Address         Port",
                    "--------------- ----------  --------------- ----------"]


def _portproxy(*rows: str) -> dict:
    return {"portproxyLines": [*PORTPROXY_HEADER, *rows, ""]}


@pytest.mark.parametrize(
    ("row", "needle"),
    [
        pytest.param("0.0.0.0         50052       10.77.0.2       50052", "listen 0.0.0.0:50052", id="listen-port"),
        pytest.param("192.168.1.20    8080        10.77.0.2       51052", "connect 10.77.0.2:51052",
                     id="connect-comm-port"),
        pytest.param("*               51052       127.0.0.1       9000", "listen *:51052", id="listen-comm-port"),
    ],
)
def test_portproxy_entry_for_an_rpc_port_refuses(tmp_path: Path, row: str, needle: str) -> None:
    outcome = _run(tmp_path, [PEER], extra=_portproxy(row))
    _assert_refused(outcome, "netsh interface portproxy", needle, "[v4tov4]",
                    "netsh interface portproxy delete v4tov4")
    assert outcome.log("netsh.jsonl") == [{"Args": ["interface", "portproxy", "show", "all"]}]


def test_portproxy_entry_in_a_v6_section_refuses(tmp_path: Path) -> None:
    lines = ["", "Listen on ipv6:             Connect to ipv4:", "",
             "Address         Port        Address         Port",
             "--------------- ----------  --------------- ----------",
             "::              50052       10.77.0.2       50052", ""]
    outcome = _run(tmp_path, [PEER], extra={"portproxyLines": lines})
    _assert_refused(outcome, "[v6tov4] listen :::50052", "portproxy delete v6tov4")


def test_unrelated_portproxy_entry_allows_start(tmp_path: Path) -> None:
    outcome = _run(tmp_path, [PEER], extra=_portproxy("0.0.0.0         8080        127.0.0.1       80"))
    assert outcome.launched, outcome.error or outcome.text


def test_no_portproxy_entries_allows_start(tmp_path: Path) -> None:
    outcome = _run(tmp_path, [PEER])
    assert outcome.launched, outcome.error or outcome.text
    assert outcome.log("netsh.jsonl"), "portproxy was never listed"


@pytest.mark.parametrize(
    ("extra", "needle"),
    [
        pytest.param({"portproxyExit": 1, "portproxyLines": ["The following command was not found."]},
                     "exited with code 1", id="exit-code"),
        pytest.param({"portproxyThrows": True}, "cannot run 'netsh interface portproxy show all'", id="not-runnable"),
        pytest.param(_portproxy("0.0.0.0 http 127.0.0.1 80"), "cannot parse", id="named-port"),
        pytest.param({"portproxyLines": ["Something unexpected"]}, "cannot parse", id="unknown-text"),
    ],
)
def test_portproxy_unreadable_refuses(tmp_path: Path, extra: dict, needle: str) -> None:
    outcome = _run(tmp_path, [PEER], extra=extra)
    _assert_refused(outcome, "cannot verify that no port proxy forwards to rpc-server", needle)


# --- The firewall service must be running ---


@pytest.mark.parametrize("status", ["Stopped", "StartPending", "Paused"])
def test_firewall_service_not_running_refuses(tmp_path: Path, status: str) -> None:
    outcome = _run(tmp_path, [PEER], extra={"serviceStatus": status})
    _assert_refused(outcome, "(mpssvc) is not running", f"Status={status}", "Start-Service mpssvc")


def test_firewall_service_unreadable_refuses(tmp_path: Path) -> None:
    outcome = _run(tmp_path, [PEER], extra={"serviceFails": True})
    _assert_refused(outcome, "cannot read the state of the Windows Defender Firewall service (mpssvc)")


def test_firewall_service_is_checked_on_a_healthy_start(tmp_path: Path) -> None:
    outcome = _run(tmp_path, [PEER])
    assert outcome.launched, outcome.error or outcome.text
    assert outcome.log("get-service.jsonl") == [{"Name": "mpssvc"}]
