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
$global:MockEnsureFails = [bool]$scenario.ensureFails

function Write-MockLog([string]$Name, $Value) {
    Add-Content -LiteralPath (Join-Path $global:MockOut $Name) -Value ($Value | ConvertTo-Json -Compress -Depth 6)
}

function ConvertTo-MockRule($r) {
    [pscustomobject]@{
        Name = [string]$r.name; InstanceID = [string]$r.name; DisplayName = [string]$r.displayName
        Enabled = [string]$r.enabled; Direction = [string]$r.direction; Action = [string]$r.action
        PackageFamilyName = [string]$r.packageFamilyName
    }
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
          [string[]]$LocalAddress, [string[]]$RemoteAddress, [string]$Profile, [string]$Description, [string]$Program)
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
}
function Invoke-CimMethod {
    [CmdletBinding()]
    param([string]$ClassName, [string]$MethodName, [hashtable]$Arguments)
    Write-MockLog 'launch.json' @{ ClassName = $ClassName; MethodName = $MethodName }
    [pscustomobject]@{ ReturnValue = 9; ProcessId = 0 }
}
function New-Object {
    # Only the TcpClient used by Test-TcpPort is mocked: it never connects.
    param([Parameter(Position = 0)][string]$TypeName)
    if ($TypeName -ne 'System.Net.Sockets.TcpClient') { throw "mock New-Object: unexpected type $TypeName" }
    $client = [pscustomobject]@{}
    $client | Add-Member -MemberType ScriptMethod -Name BeginConnect -Value { Write-MockLog 'tcp.jsonl' @{ Host = $args[0]; Port = $args[1] }; throw 'mock: no network' }
    $client | Add-Member -MemberType ScriptMethod -Name Close -Value { }
    $client
}

# Refuse to run the real script unless every side-effecting command is the mock.
foreach ($cmd in 'Get-NetFirewallRule', 'Get-NetFirewallPortFilter', 'Get-NetFirewallAddressFilter',
                 'Get-NetFirewallApplicationFilter', 'Get-NetFirewallProfile', 'Remove-NetFirewallRule',
                 'New-NetFirewallRule', 'Get-NetTCPConnection', 'Invoke-CimMethod', 'New-Object') {
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
}

PROFILES_ON = [
    {"Name": n, "Enabled": "True", "DefaultInboundAction": "Block"} for n in ("Domain", "Private", "Public")
]


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
    ensure_fails: bool = False,
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
        "ensureFails": ensure_fails,
        "args": script_args,
    }
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
    wrapper = tmp_path / "wrapper.ps1"
    wrapper.write_text(WRAPPER, encoding="utf-8")
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-File", str(wrapper), "-ScenarioPath", str(scenario_path)],
        capture_output=True,
        text=True,
        timeout=120,
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
