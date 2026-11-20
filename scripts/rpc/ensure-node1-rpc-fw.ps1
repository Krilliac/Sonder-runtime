#Requires -Version 5.1
<#
.SYNOPSIS
  Ensure peer-only inbound Windows Firewall allow for Node1 llama.cpp rpc-server (default :50052).
.PARAMETER Port
  TCP port rpc-server listens on (must match start-node1-rpc.ps1 -Port). Default 50052.
.NOTES
  Private-link Ethernet often shows NetworkCategory Public, so the rule uses
  -Profile Any. IP lock remains LocalAddress 10.77.0.2 / RemoteAddress 10.77.0.1.
#>
[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 50052
)

$ErrorActionPreference = 'Stop'

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
  -Description "Peer-only llama.cpp ggml-rpc worker on 10.77.0.0/30 private link (Main 10.77.0.1 -> Node1 10.77.0.2:$Port)" | Out-Null

Get-NetFirewallRule -DisplayName $displayName | ForEach-Object {
  $p = $_ | Get-NetFirewallPortFilter
  $a = $_ | Get-NetFirewallAddressFilter
  [pscustomobject]@{
    Name      = $_.DisplayName
    Enabled   = $_.Enabled
    Profile   = "$($_.Profile)"
    LocalPort = $p.LocalPort
    Local     = "$($a.LocalAddress)"
    Remote    = "$($a.RemoteAddress)"
  }
} | Format-List
