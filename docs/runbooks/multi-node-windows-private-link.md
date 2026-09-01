# Windows multi-node private-link setup

This runbook turns a second Windows PC into a private compute and inference
worker for a primary workstation. It consolidates the setup, validation, and
failure recovery lessons from a completed two-PC deployment.

The result is a **remote worker**, not one larger computer. Windows does not
combine the two PCs' CPU, RAM, and GPU into one transparent resource pool.
Send complete jobs to the worker instead: inference requests, builds, tests,
indexing, rendering, or other bounded workloads. Sonder can pool independent
Ollama requests across workers, but it does not split one model across hosts.

For an Ubuntu or other Linux worker, use
[install-server-private.md](install-server-private.md) for the service layer.
The topology and trust principles here still apply, but the Windows commands
do not.

## 1. Choose the topology

The validated two-PC topology used separate networks for separate purposes:

```text
                          ordinary LAN / Internet
                                    |
                    Wi-Fi or primary Ethernet
                                    |
                +-------------------+-------------------+
                |                                       |
        Primary workstation                         Worker PC
        interactive work                         background work
                |                                       |
        dedicated 2.5 GbE ---------------- dedicated 2.5 GbE
             10.77.0.1/30                          10.77.0.2/30
             no gateway                             no gateway
             no DNS                                 no DNS
```

Use one of these layouts:

- **One dedicated cable:** use a `/30` point-to-point subnet, as shown above.
- **Several workers on a private switch or VLAN:** assign every host a unique
  address on a larger private subnet. Add TLS or a VPN before configuring
  remote Sonder workers.
- **Workers across an ordinary LAN or the Internet:** use a VPN or
  authenticated TLS reverse proxy. Never expose raw Ollama HTTP or SSH to the
  Internet.

Raw HTTP is acceptable only as a narrow connectivity smoke test on a
physically controlled, point-to-point cable with address-, interface-, and
profile-scoped firewall rules. Sonder intentionally requires HTTPS for a
configured remote Ollama worker.

## 2. Record the baseline before changing anything

Run an elevated PowerShell window on each PC and save the output somewhere
outside the application directory. Record adapter names, interface indexes,
MAC addresses, current addressing, routes, DNS, profile, MTU, and link speed.
Those details make rollback deterministic and prevent configuring the wrong
NIC.

```powershell
$snapshot = [ordered]@{
    computer = $env:COMPUTERNAME
    captured_at = (Get-Date).ToString('o')
    adapters = @(Get-NetAdapter -IncludeHidden | Select-Object Name, InterfaceDescription, ifIndex, MacAddress, Status, LinkSpeed)
    addresses = @(Get-NetIPAddress | Select-Object InterfaceIndex, AddressFamily, IPAddress, PrefixLength, PrefixOrigin)
    routes = @(Get-NetRoute | Select-Object InterfaceIndex, AddressFamily, DestinationPrefix, NextHop, RouteMetric)
    dns = @(Get-DnsClientServerAddress | Select-Object InterfaceIndex, AddressFamily, ServerAddresses)
    profiles = @(Get-NetConnectionProfile | Select-Object InterfaceIndex, InterfaceAlias, NetworkCategory)
}
$snapshot | ConvertTo-Json -Depth 6 | Set-Content -Encoding utf8 '.\network-before.json'
```

Confirm which adapter is cabled by matching at least two stable identifiers,
such as MAC address plus interface description or hardware ID. Do not identify
it only by a mutable display name such as `Ethernet`.

Also confirm:

- The ordinary LAN adapter has the default route and working Internet access.
- The dedicated adapter negotiates the expected speed.
- The worker has enough storage, RAM, and GPU memory for the intended jobs.
- OpenSSH Server and the inference runtimes are installed from trusted sources.

## 3. Configure the dedicated link

The examples use the tested `10.77.0.0/30` subnet. Replace the adapter aliases
if necessary, but keep the two ends in the same subnet.

On the primary workstation:

```powershell
$privateAdapter = 'Ethernet'
New-NetIPAddress -InterfaceAlias $privateAdapter -IPAddress '10.77.0.1' -PrefixLength 30
Set-NetConnectionProfile -InterfaceAlias $privateAdapter -NetworkCategory Private
Set-DnsClientServerAddress -InterfaceAlias $privateAdapter -ResetServerAddresses
```

On the worker:

```powershell
$privateAdapter = 'Ethernet'
New-NetIPAddress -InterfaceAlias $privateAdapter -IPAddress '10.77.0.2' -PrefixLength 30
Set-NetConnectionProfile -InterfaceAlias $privateAdapter -NetworkCategory Private
Set-DnsClientServerAddress -InterfaceAlias $privateAdapter -ResetServerAddresses
```

Do not add a gateway or DNS server to the dedicated adapter. The ordinary LAN
adapter should remain the only default route. Preserve IPv6 unless the local
network design explicitly requires changing it.

Verify both machines:

```powershell
Get-NetIPAddress -InterfaceAlias $privateAdapter
Get-NetRoute -InterfaceAlias $privateAdapter
Get-DnsClientServerAddress -InterfaceAlias $privateAdapter
Get-NetConnectionProfile -InterfaceAlias $privateAdapter
Get-NetAdapter -Name $privateAdapter | Select-Object Name, Status, LinkSpeed, MacAddress
```

Do not treat a failed ping as proof that the link is broken. ICMP may be
blocked while TCP is healthy. Check the neighbor table and the actual service
port as well:

```powershell
Get-NetNeighbor -IPAddress '10.77.0.2'
Test-NetConnection '10.77.0.2' -Port 22
```

## 4. Establish SSH without trusting an unverified host key

Create a dedicated administrative account on the worker and use public-key
authentication. A guessed or default password is not a setup method. Do not
copy private keys to the worker, repository, chat history, or shared setup
directory.

First obtain the worker's SSH host-key fingerprint locally on the worker or
through another already trusted channel:

```powershell
Get-ChildItem 'C:\ProgramData\ssh\ssh_host_*_key.pub' |
    ForEach-Object { ssh-keygen -lf $_.FullName }
```

On the primary PC, capture the candidate key into a task-scoped file:

```powershell
$knownHosts = Join-Path $PWD 'node-known-hosts'
ssh-keyscan 10.77.0.2 | Set-Content -Encoding ascii $knownHosts
ssh-keygen -lf $knownHosts
```

Compare the fingerprints exactly. Only after they match, connect with strict
checking and the dedicated key:

```powershell
$sshKey = "$env:USERPROFILE\.ssh\compute-node-ed25519"
ssh -i $sshKey `
    -o BatchMode=yes `
    -o StrictHostKeyChecking=yes `
    -o "UserKnownHostsFile=$knownHosts" `
    node-admin@10.77.0.2 hostname
```

Avoid `StrictHostKeyChecking=no` and do not automatically accept a changed
key. A changed fingerprint is an identity failure to investigate.

After connecting, verify the account and elevation context before attempting
administrative changes:

```powershell
whoami
whoami /groups
```

Some SSH environments do not expose the same `PATH` as an interactive desktop
session. Discover required executables with `Get-Command` or bounded
`Test-Path` checks, then invoke their absolute paths. Do not hardcode a tool
path copied from another machine's cache.

## 5. Install and bind the inference runtimes

Use separate lifecycle controls for the always-on Ollama service and an
on-demand llama.cpp server. Keep them mutually exclusive by default so two
runtimes do not unexpectedly compete for GPU memory.

### Ollama

Install Ollama on the worker, pull the exact model aliases the coordinator
will request, and bind only to the private address:

```powershell
$env:OLLAMA_HOST = '10.77.0.2:11434'
ollama pull sonder:latest
```

For unattended operation, create a supported Windows service or a scheduled
task that runs as a service account at startup. The completed deployment used
a `SYSTEM` scheduled task because it provided a stable noninteractive process
lifecycle. Persist the bind address in that task's environment or wrapper;
setting it only in an interactive shell is not sufficient.

Check the listener and process owner:

```powershell
Get-NetTCPConnection -LocalPort 11434 -State Listen |
    Select-Object LocalAddress, LocalPort, OwningProcess
Get-Process -Id (Get-NetTCPConnection -LocalPort 11434 -State Listen).OwningProcess
```

The expected local address is the private-link address, not `0.0.0.0` and not
the Wi-Fi/LAN address.

### llama.cpp with Vulkan

Keep the Vulkan build, model, logs, and runtime state in distinct directories.
Start the server with an explicit private bind and GPU offload:

```powershell
& 'C:\Compute\llama\llama-server.exe' `
    --host '10.77.0.2' `
    --port 18080 `
    --model 'C:\Compute\models\model.gguf' `
    -ngl 999
```

Verify acceleration from runtime evidence rather than assuming that a GPU was
used. Check the startup log for the Vulkan device and offloaded layers, and
confirm that Vulkan modules are loaded into the live process:

```powershell
$server = Get-Process -Name 'llama-server'
$server.Modules | Where-Object ModuleName -Match 'vulkan|ggml-vulkan'
```

### Windows OpenSSH process-lifetime trap

A process launched with `Start-Process` inside a one-shot Windows SSH command
may be terminated when the SSH session closes. A command can therefore report
successful startup while the listener disappears seconds later.

Use a Windows service, scheduled task, or another supported supervisor for a
permanent service. For an on-demand validation session, keep the strict SSH
session alive while a state file exists:

```powershell
# Run after the lifecycle script has started the server.
while (Test-Path 'C:\Compute\state\llama-server.json') {
    Start-Sleep -Seconds 1
}
```

Have the stop action remove the state file so the holder exits cleanly.

## 6. Restrict the worker firewall

Create one inbound rule per service. Bind each rule to all of these dimensions:

- Exact worker-local address.
- Exact primary-PC remote address.
- Dedicated interface alias.
- `Private` network profile.
- Exact TCP port.

Example for Ollama:

```powershell
New-NetFirewallRule `
    -DisplayName 'Compute link - Ollama from coordinator' `
    -Direction Inbound `
    -Action Allow `
    -Protocol TCP `
    -LocalPort 11434 `
    -LocalAddress '10.77.0.2' `
    -RemoteAddress '10.77.0.1' `
    -InterfaceAlias 'Ethernet' `
    -Profile Private
```

Create the equivalent rule for llama.cpp on port `18080`. Keep SSH scoped to
the management network appropriate for the deployment. Do not add a broad
`Any`-address inference rule.

Confirm that inference ports are reachable over the private cable and are not
reachable through the worker's ordinary Wi-Fi/LAN address.

## 7. Validate from the primary PC

Bypass only the HTTP client's proxy for the direct private address. Some
Windows environments send RFC1918 addresses through a configured proxy, which
can make a healthy listener look unavailable. Do not disable the global proxy.

```powershell
curl.exe --noproxy '*' 'http://10.77.0.2:11434/api/version'
curl.exe --noproxy '*' 'http://10.77.0.2:11434/api/tags'
```

For PowerShell automation, use a dedicated `HttpClientHandler` with
`UseProxy = $false` for these probes.

Run a real, deterministic generation against each runtime. A TCP connection
alone is not enough. For llama.cpp, poll `/health` until it returns HTTP 200;
the TCP port may already be open while model loading still returns 503.

Validate in this order:

1. Adapter link and expected IP configuration.
2. Strict SSH identity and key-based login.
3. Listener bound only to the private address.
4. Runtime version or health endpoint.
5. Model inventory contains the expected alias.
6. Deterministic inference returns the requested marker.
7. Logs or loaded modules prove the intended GPU backend.
8. Worker Wi-Fi/LAN address refuses the inference ports.
9. Primary PC still has its ordinary Internet route.

When switching between Ollama and llama.cpp in a test, use `try`/`finally`:
stop Ollama, start and validate llama.cpp, then always stop llama.cpp and
restore Ollama even if a probe fails.

## 8. Connect the worker to Sonder

The direct HTTP checks above validate the physical and runtime layers. Before
adding a remote worker to Sonder, place a trusted TLS reverse proxy in front
of Ollama or connect the hosts through a VPN. Follow
[multi-pc-ollama.md](multi-pc-ollama.md) for the consent gate, HTTPS worker
origins, scheduling, circuit breaking, and health checks.

After the isolated-link smoke test, rebind Ollama to loopback, remove the raw
`11434` private-link firewall rule, and expose only the TLS proxy's port to the
coordinator. Re-run the listener and negative Wi-Fi/LAN checks against the
final configuration.

Typical final topology:

```text
Sonder coordinator -> HTTPS worker name -> TLS proxy -> 127.0.0.1:11434
```

Use the same model alias on every worker that may receive that request. Start
with `worker_max_inflight = 1`; measure latency, VRAM use, and queueing before
raising concurrency.

Keep arbitrary remote command execution out of the inference pool. General
remote compute belongs in a separate authenticated, catalog-bound execution
lane; see [compute-fabric.md](compute-fabric.md).

## 9. Scale beyond one worker

Repeat the worker checklist rather than cloning secrets or machine identity:

- Assign a unique hostname and private IP.
- Generate a unique SSH user key or certificate mapping.
- Verify and pin that worker's host key independently.
- Issue a unique TLS certificate whose name matches the configured origin.
- Apply address-scoped firewall rules for the coordinator only.
- Install the required model aliases locally.
- Record capability, VRAM, expected concurrency, and rollback state.
- Run the complete validation sequence before admitting the worker to the pool.

Prefer whole-job scheduling. Route work according to capability and current
load; do not assume that adding machines accelerates a single job. Limit
concurrent native builds according to RAM and commit headroom on each host.

## 10. Rollback

Keep rollback scripts beside the setup scripts and make preview or dry-run the
default. A complete rollback should:

1. Stop and disable the worker inference task or service.
2. Stop any on-demand server and remove only its owned state file.
3. Remove the narrowly named firewall rules.
4. Remove the static IPv4 address from the dedicated adapter.
5. Restore the saved DHCP, DNS, route metric, MTU, and network profile values.
6. Leave unrelated adapters, IPv6, models, logs, and user data untouched.
7. Re-run the baseline inventory and compare it with the saved snapshot.

Never make rollback depend on a guessed original configuration. Use the saved
pre-change snapshot.

## Troubleshooting

### SSH says the host key is unknown or changed

Check that the intended task-scoped `UserKnownHostsFile` is being used. Verify
the current fingerprint locally on the worker. Do not bypass the mismatch.

### SSH login works but administrative commands fail

Verify the actual account, Administrators-group membership, integrity level,
and elevation behavior. The desktop user, SSH user, and service account may be
different identities.

### A command works interactively but not over SSH

Inspect the SSH session's `PATH` and use the executable's verified absolute
path. Cached runtime paths are machine-specific and may change after upgrades.

### The HTTP probe reports connection refused but `curl --noproxy` works

The client is probably using a proxy for the private IP. Bypass the proxy only
for the dedicated probe client or add the private subnet to the approved local
proxy-bypass policy.

### llama.cpp starts and immediately disappears

The Windows SSH session likely owned the child process. Use a service,
scheduled task, supported supervisor, or retained SSH holder.

### The port is open but health returns 503

The process is still loading the model. Poll with a bounded timeout and inspect
the server log; do not treat listener creation as readiness.

### Inference is reachable through Wi-Fi

Stop the service. Inspect both the listener address and firewall rule scope.
Binding to `0.0.0.0` cannot be repaired by relying on an overly broad firewall
rule.

### The dedicated cable breaks Internet access

Remove any gateway or DNS configuration from the dedicated adapter and inspect
the route table. The ordinary LAN adapter should own the default route.

## Deployment record

For each worker, retain a secret-free record containing:

- Hostname, OS, adapter identity, private IP, and link speed.
- Saved pre-change network snapshot and rollback procedure.
- Verified SSH host-key fingerprints, but no private keys.
- Runtime versions, model aliases, bind addresses, and lifecycle owner.
- Firewall rule names and exact scope.
- TLS or VPN identity used by the coordinator.
- Validation date, deterministic test result, and observed latency.
- Known limitations and the person or team responsible for the node.

This record is what makes the next node repeatable: hardware and addresses may
change, but the identity, least-privilege, lifecycle, validation, and rollback
contracts stay the same.
