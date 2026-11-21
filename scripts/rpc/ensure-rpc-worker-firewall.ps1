#Requires -Version 5.1
<#
.SYNOPSIS
  Ensure a peer-only inbound Windows Firewall rule for a llama.cpp rpc-server worker.
.DESCRIPTION
  Run as administrator on the worker. Allows inbound TCP on the RPC port only
  from the main machine's private-link address to the worker's private-link
  address. Both addresses must be literal private (RFC 1918) or link-local IPv4
  addresses; the rule is recreated on every run so it always matches the
  arguments.
.NOTES
  A dedicated private-link adapter often reports NetworkCategory Public, so the
  rule uses -Profile Any. The address lock (LocalAddress / RemoteAddress) is
  what limits it.
.EXAMPLE
  powershell -NoProfile -File .\ensure-rpc-worker-firewall.ps1 -LocalAddress <worker-ip> -RemoteAddress <main-ip>
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$LocalAddress,
    [Parameter(Mandatory = $true)]
    [string]$RemoteAddress,
    [string]$Port = $env:SONDER_RPC_PORT,
    [string]$DisplayName = 'Sonder llama-rpc worker (main peer only)'
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'rpc-policy.ps1')

$null = Assert-RpcFirewallDisplayName -DisplayName $DisplayName
$local = ConvertTo-RpcPrivateIPv4 -Address $LocalAddress -Label 'Worker (local) address'
$remote = ConvertTo-RpcPrivateIPv4 -Address $RemoteAddress -Label 'Main (remote) address'
if ($local -eq $remote) { throw 'LocalAddress and RemoteAddress must differ.' }
$rpcPort = if (([string]$Port).Trim()) { ConvertTo-RpcPort -Value $Port -Label 'Port' } else { $script:RpcDefaultPort }

# Match the exact name (case-sensitive) after a full listing: -DisplayName would
# treat any wildcard as a pattern.
@(Get-NetFirewallRule -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -ceq $DisplayName }) |
  Remove-NetFirewallRule

New-NetFirewallRule -DisplayName $DisplayName `
  -Direction Inbound `
  -Action Allow `
  -Protocol TCP `
  -LocalPort $rpcPort `
  -LocalAddress $local `
  -RemoteAddress $remote `
  -Profile Any `
  -Description "Peer-only llama.cpp rpc-server worker: $remote -> ${local}:$rpcPort" | Out-Null

@(Get-NetFirewallRule | Where-Object { $_.DisplayName -ceq $DisplayName }) | ForEach-Object {
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
