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
             10.0.0.1/24                          10.0.0.2/24
             no gateway                             no gateway
             no DNS                                 no DNS
```

Use one of these layouts:

- **One dedicated cable:** use a small point-to-point subnet. The example
  uses a `/24`; a `/30` is the minimal point-to-point choice.
- **Several workers on a private switch or VLAN:** assign every host a unique
  address on a larger private subnet. Put TLS in front of every worker before
  configuring it in Sonder (section 8).
- **Workers across an ordinary LAN or the Internet:** use a TLS reverse proxy
  restricted to the coordinator's source IP, optionally over a VPN as an extra
  network layer. A VPN alone is not enough: Sonder rejects a non-loopback
  worker without HTTPS and an explicit port on any network
  (`sonder_runtime/platform/config.py:2114-2130`). Do not add proxy
  authentication: Sonder sends no credentials to Ollama, so the worker would
  get 401 or 403 and stay in probation. Never expose raw Ollama HTTP or SSH
  to the Internet.

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

The examples use the example `10.0.0.0/24` subnet. Replace the adapter aliases
if necessary, but keep the two ends in the same subnet.

On the primary workstation:

```powershell
$privateAdapter = 'Ethernet'
New-NetIPAddress -InterfaceAlias $privateAdapter -IPAddress '10.0.0.1' -PrefixLength 24
Set-NetConnectionProfile -InterfaceAlias $privateAdapter -NetworkCategory Private
Set-DnsClientServerAddress -InterfaceAlias $privateAdapter -ResetServerAddresses
```

On the worker:

```powershell
$privateAdapter = 'Ethernet'
New-NetIPAddress -InterfaceAlias $privateAdapter -IPAddress '10.0.0.2' -PrefixLength 24
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
Get-NetNeighbor -IPAddress '10.0.0.2'
Test-NetConnection '10.0.0.2' -Port 22
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
$knownHosts = Join-Path $PWD 'worker-known-hosts'
ssh-keyscan 10.0.0.2 | Set-Content -Encoding ascii $knownHosts
ssh-keygen -lf $knownHosts
```

Compare the fingerprints exactly. Only after they match, connect with strict
checking and the dedicated key:

```powershell
$sshKey = "$env:USERPROFILE\.ssh\worker-ed25519"
ssh -i $sshKey `
    -o BatchMode=yes `
    -o StrictHostKeyChecking=yes `
    -o "UserKnownHostsFile=$knownHosts" `
    <admin-user>@10.0.0.2 hostname
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

Install Ollama on the worker and pull the exact model aliases the coordinator
will request. For the isolated-link smoke test only, serve on the private
address. `OLLAMA_HOST` on `ollama serve` is the bind address; on `ollama pull`
it is only the server to connect to. Models are stored by the server, so the
smoke-test server must use the same models directory as the task script that
will run Ollama in the final configuration (`OLLAMA_MODELS`, for example
`C:\OllamaModels` in [multi-node-ollama.md](multi-node-ollama.md)). Otherwise
the aliases you pull land in your interactive user's default directory
(`%USERPROFILE%\.ollama\models`), the task server never sees them, and Sonder
gets model-not-found after section 8. Stop the Ollama tray app and any other
Ollama task first, so two servers do not compete for GPU memory, then in one
shell:

```powershell
$env:OLLAMA_HOST = '10.0.0.2:11434'
$env:OLLAMA_MODELS = 'C:\OllamaModels'   # same value as the task script
ollama serve
```

In a second shell, pull against that server:

```powershell
$env:OLLAMA_HOST = '10.0.0.2:11434'
ollama pull qwen2.5-coder:7b          # a real library tag: your base model
ollama cp qwen2.5-coder:7b sonder:latest
```

`sonder:latest` itself cannot be pulled: it is a local alias that
`setup_alias.py` builds with `ollama create`, and it is in no registry. Create
it on the worker as above (`ollama cp <base-tag> sonder:latest`, or
`ollama create sonder:latest -f <Modelfile>`); the full options, including the
Modelfile that matches Sonder's own, are in
[multi-pc-ollama.md](multi-pc-ollama.md#prepare-each-worker-pc). Both commands
talk to the server at `OLLAMA_HOST`, and `ollama pull` does not need
`OLLAMA_MODELS`: the server writes the files.
If the server is already running from the task script described in
[multi-node-ollama.md](multi-node-ollama.md) and bound to the private address,
skip `ollama serve` and run only the pull. A pull against a server that is not
listening on that address fails with connection refused.

For unattended operation, run Ollama as a scheduled task that starts at boot.
The task script, restart loop, and `OLLAMA_HOST` handling are documented in
[multi-node-ollama.md](multi-node-ollama.md); do not copy them here. Persist
the bind address in that task's script; setting it only in an interactive
shell is not sufficient. Use the private address for the smoke test and
loopback (`127.0.0.1:11434`) for the final TLS configuration in section 8.

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
& '<worker-root>\llama\llama-server.exe' `
    --host '10.0.0.2' `
    --port 18080 `
    --model '<worker-root>\models\model.gguf' `
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
while (Test-Path '<worker-root>\state\llama-server.json') {
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
    -LocalAddress '10.0.0.2' `
    -RemoteAddress '10.0.0.1' `
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
curl.exe --noproxy '*' 'http://10.0.0.2:11434/api/version'
curl.exe --noproxy '*' 'http://10.0.0.2:11434/api/tags'
```

For PowerShell automation, use a dedicated `HttpClientHandler` with
`UseProxy = $false` for these probes.

Run a real, deterministic generation against each runtime. A TCP connection
alone is not enough. For llama.cpp, poll `/health` until it returns HTTP 200;
the TCP port may already be open while model loading still returns 503.

Validate in this order:

1. Adapter link and expected IP configuration.
2. Strict SSH identity and key-based login.
3. Smoke-test listener bound only to the private address (section 8 later
   moves it to loopback behind Caddy).
4. Runtime version or health endpoint.
5. Model inventory contains the expected alias.
6. Deterministic inference returns the requested marker.
7. Logs or loaded modules prove the intended GPU backend.
8. Worker Wi-Fi/LAN address refuses the inference ports.
9. Primary PC still has its ordinary Internet route.

When switching between Ollama and llama.cpp in a test, use `try`/`finally`:
stop Ollama, start and validate llama.cpp, then always stop llama.cpp and
restore Ollama even if a probe fails.

## 8. Move to the TLS end state

The direct HTTP checks above validate the physical and runtime layers only.
Sonder rejects a remote Ollama worker that is not `https` with an explicit
port (`[ollama].workers remote entries must use https`,
`sonder_runtime/platform/config.py:2130`), so a raw
`http://10.0.0.2:11434` worker cannot be configured. Finish with this
procedure (checked against the code on 2026-11-20). The Caddy configuration
and the coordinator settings each have one home in
[remote-ollama-caddy.md](remote-ollama-caddy.md); follow it rather than
copying values from here.

1. **Rebind the runtimes to loopback (worker).** Change `OLLAMA_HOST` in the
   scheduled-task script to `127.0.0.1:11434` (see
   [multi-node-ollama.md](multi-node-ollama.md)) and restart the task. If you
   use llama.cpp, restart it with `--host 127.0.0.1`. Expected:

   ```powershell
   Get-NetTCPConnection -LocalPort 11434 -State Listen |
       Select-Object LocalAddress, LocalPort
   ```

   `LocalAddress` is `127.0.0.1` only. Then confirm the restarted task
   server (which uses `OLLAMA_MODELS` from its script) lists the alias the
   coordinator will request; if it is missing, recreate it against this
   server (`ollama cp <base-tag> sonder:latest`, see section 5; never
   `ollama pull sonder:latest`) or fix the `OLLAMA_MODELS` mismatch
   (section 5):

   ```powershell
   (Invoke-RestMethod 'http://127.0.0.1:11434/api/tags').models.name
   ```

   The output must include `sonder:latest` (or your alias). The checks in
   remote-ollama-caddy.md#verify do not detect a missing model.

2. **Install Caddy on the worker and apply the worker configuration.**
   Follow [remote-ollama-caddy.md](remote-ollama-caddy.md) for the Caddyfile.
   Running Caddy as a Windows service is covered by the official Caddy
   documentation (`https://caddyserver.com/docs/running`); use the method
   described there for your Caddy version rather than a copy of it here.
   The reference Caddyfile has no `bind` directive, so Caddy listens on
   every address (`:443`); `10.0.0.2` appears only as the site address, host
   matcher and certificate name. Confirm it is listening with
   `Get-NetTCPConnection -LocalPort 443 -State Listen`; expect `0.0.0.0` or
   `::`, not `10.0.0.2`. Step 3's address- and interface-scoped firewall rule
   and the Caddyfile's `remote_ip` matcher are therefore the only restriction
   on who can reach 443. Whether Windows Defender Firewall prompts the first time `caddy.exe`
   listens depends on the profile, Group Policy and any existing rules, so a
   prompt may or may not appear. If one does, the choice matters: Allow creates
   program rules for `caddy.exe` that open every port it listens on, for any
   remote address, on the ticked profiles (bypassing step 3's scoped rule), and
   declining may create Block rules, which take precedence over step 3's Allow
   rule and make the coordinator's HTTPS time out. Either way, check for such
   program rules afterwards. List them with
   `Get-NetFirewallApplicationFilter | Where-Object Program -like '*caddy.exe' |
   Get-NetFirewallRule`, review the output, then remove them by piping the same
   command to `Remove-NetFirewallRule`. If the coordinator times out on 443
   while the local listener is up, look for a `caddy.exe` Block rule.

3. **Replace the firewall rule.** Remove the raw-port rule from section 6 and
   allow only HTTPS from the coordinator address. Use this rule instead of the
   broader 443 rule shown in remote-ollama-caddy.md (it has no
   `-LocalAddress`, `-InterfaceAlias` or `-Profile`); do not create both:

   ```powershell
   Remove-NetFirewallRule -DisplayName 'Compute link - Ollama from coordinator'
   New-NetFirewallRule `
       -DisplayName 'Compute link - Caddy HTTPS from coordinator' `
       -Direction Inbound -Action Allow -Protocol TCP `
       -LocalPort 443 `
       -LocalAddress '10.0.0.2' `
       -RemoteAddress '10.0.0.1' `
       -InterfaceAlias 'Ethernet' `
       -Profile Private
   ```

   Also remove any llama.cpp `18080` rule you no longer need. Sonder sends no
   credentials to Ollama in static mode, so the source-address firewall rule
   is the access restriction; keep it narrow.

4. **Distribute the Caddy root CA to the coordinator.** The coordinator
   verifies the certificate chain and hostname, so the CA that issued the
   worker certificate must be trusted there, and the certificate must name the
   exact address used in the worker URL. Follow the CA steps in
   [remote-ollama-caddy.md](remote-ollama-caddy.md#certificate-trust); copy only the public root
   certificate, never a private key.

5. **Set the coordinator worker URL** to `https://10.0.0.2:443` and set
   `SONDER_ALLOW_REMOTE_OLLAMA=1`, as described in
   [remote-ollama-caddy.md](remote-ollama-caddy.md#coordinator-settings).
   The URL must be an origin only (scheme, host, explicit port).

6. **Verify.** Run the checks in
   [remote-ollama-caddy.md](remote-ollama-caddy.md#verify), then repeat the
   listener and negative Wi-Fi/LAN checks from section 7 against the final
   configuration: `11434` and `18080` must be refused on every address
   except loopback. `443` is bound on all addresses (step 2), so it must
   answer only for the coordinator because of the firewall scope and the
   `remote_ip` matcher. Test it two ways: from the coordinator, against the
   worker's Wi-Fi/LAN address (expect a timeout), and from a non-coordinator
   host against `10.0.0.2` (expect a timeout or 403). Do not run the Wi-Fi/LAN
   test on the worker itself: local traffic bypasses the firewall, and Caddy
   has no certificate for that address, so you would see a TLS error.

Rollback: follow
[remote-ollama-caddy.md](remote-ollama-caddy.md#rollback), then remove the
firewall rule named `Compute link - Caddy HTTPS from coordinator` yourself
(that runbook's "close port 443" does not name it). If you need the raw
smoke-test state back, rebind Ollama to `10.0.0.2:11434` and
recreate the section 6 rule. Return to section 10 for full link rollback.

Typical final topology:

```text
Sonder coordinator -> https://10.0.0.2:443 -> Caddy -> 127.0.0.1:11434
```

For the consent gate, scheduling, circuit breaking, and health checks, see
[multi-pc-ollama.md](multi-pc-ollama.md).

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
A raw Ollama or llama.cpp listener on `0.0.0.0` must be rebound to the private
address or loopback; do not rely on a firewall rule for it. Caddy on 443 is the
one expected all-address listener (section 8, step 2), restricted only by the
scoped firewall rule and the `remote_ip` matcher.

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
- TLS certificate name and issuing CA used by the coordinator (and the VPN, if
  one is also used).
- Validation date, deterministic test result, and observed latency.
- Known limitations and the person or team responsible for the node.

This record is what makes the next node repeatable: hardware and addresses may
change, but the identity, least-privilege, lifecycle, validation, and rollback
contracts stay the same.
