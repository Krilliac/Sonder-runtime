#Requires -Version 5.1
<#
.SYNOPSIS
  Ensure peer-only inbound Windows Firewall allow for Node1 llama.cpp rpc-server (default :50052).
.PARAMETER Port
  TCP port rpc-server listens on (must match start-node1-rpc.ps1 -Port). Default 50052.
.NOTES
  Private-link Ethernet often shows NetworkCategory Public, so the rule uses
  -Profile Any. IP lock remains LocalAddress 10.77.0.2 / RemoteAddress 10.77.0.1.
  The rule is also pinned (-InterfaceAlias) to the interface that owns
  10.77.0.2, so a packet spoofed as 10.77.0.1 on another NIC does not match it.
  If that interface cannot be resolved, no rule is created.
#>
[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 50052
)

$ErrorActionPreference = 'Stop'

$localAddress = '10.77.0.2'  # must match -LocalAddress below
try {
  $aliases = @(Get-NetIPAddress -IPAddress $localAddress -ErrorAction Stop |
    ForEach-Object { ([string]$_.InterfaceAlias).Trim() } | Where-Object { $_ -ne '' } | Select-Object -Unique)
}
catch {
  throw "Cannot resolve the interface that owns ${localAddress}: $($_.Exception.Message). Assign $localAddress to the private-link adapter first; no firewall rule was created."
}
if ($aliases.Count -ne 1) {
  $found = if ($aliases.Count -eq 0) { 'none' } else { "'" + ($aliases -join "', '") + "'" }
  throw "Cannot resolve exactly one interface that owns ${localAddress} (found: $found). Assign $localAddress to the private-link adapter only; no firewall rule was created."
}
$interfaceAlias = $aliases[0]
if ([System.Management.Automation.WildcardPattern]::ContainsWildcardCharacters($interfaceAlias)) {
  # -InterfaceAlias takes a wildcard pattern; a literal '*', '?' or '[' in the name would widen it.
  throw "The interface that owns $localAddress is named '$interfaceAlias', which contains wildcard characters (* ? [ ]), so the rule cannot be pinned to it exactly. Rename the adapter (Rename-NetAdapter); no firewall rule was created."
}

$displayName = "Node1 llama-rpc $Port from Main peer"
Get-NetFirewallRule -DisplayName $displayName -ErrorAction SilentlyContinue | Remove-NetFirewallRule

New-NetFirewallRule -DisplayName $displayName `
  -Direction Inbound `
  -Action Allow `
  -Protocol TCP `
  -LocalPort $Port `
  -LocalAddress 10.77.0.2 `
  -RemoteAddress 10.77.0.1 `
  -Profile Any `
  -InterfaceAlias $interfaceAlias `
  -Description "Peer-only llama.cpp ggml-rpc worker on 10.77.0.0/30 private link (Main 10.77.0.1 -> Node1 10.77.0.2:$Port)" | Out-Null

Get-NetFirewallRule -DisplayName $displayName | ForEach-Object {
  $p = $_ | Get-NetFirewallPortFilter
  $a = $_ | Get-NetFirewallAddressFilter
  [pscustomobject]@{
    Name      = $_.DisplayName
    Enabled   = $_.Enabled
    Profile   = "$($_.Profile)"
    LocalPort = $p.LocalPort
    Interface = $interfaceAlias
    Local     = "$($a.LocalAddress)"
    Remote    = "$($a.RemoteAddress)"
  }
} | Format-List
