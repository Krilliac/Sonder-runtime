# Sonder Distributed RPC Sharding (`llama.cpp --rpc` across Main + Node1)

This directory contains launch scripts and firewall helpers for running a single
large GGUF model sharded across **Main** (`10.77.0.1`, RTX 5070 Ti 16 GB, CUDA)
and **Node1** (`10.77.0.2`, Vulkan `rpc-server`) over the dedicated
`10.77.0.0/30` private link, exposed on Main strictly on loopback
(`http://127.0.0.1:11438/v1`) for Sonder's `openai-compatible` gateway.

## Dedicated Binary Directories

Pinned official `ggml-org/llama.cpp` release (`b12494`, matching `ggml-rpc` protocol on both hosts):

- **Main**: `%LOCALAPPDATA%\sonder\llama-rpc\` (`llama-b12494-bin-win-cuda-12.4-x64.zip` + `cudart-llama-bin-win-cuda-12.4-x64.zip`)
- **Node1**: `C:\NewBuild\llama-rpc\` (`llama-b12494-bin-win-vulkan-x64.zip`)

## Security & Network Policy

- Node1 `rpc-server` binds **strictly** to `10.77.0.2:50052` (never `0.0.0.0` or public LAN).
- Node1 Windows Firewall rule `Node1 llama-rpc 50052 from Main peer` allows inbound TCP `50052` **only** from `10.77.0.1` to `10.77.0.2` (`-Profile Any` because private-link Ethernet can classify as `Public`). `ensure-node1-rpc-fw.ps1` also pins the rule with `-InterfaceAlias` to the interface that owns `10.77.0.2` (resolved with `Get-NetIPAddress`), and creates no rule when that interface cannot be resolved to exactly one alias without wildcard characters. Start still accepts an existing peer rule that is not pinned.
- **Comm port (`<Port>` + 1000, `51052` by default).** In the pinned `b12494` build, when Main's client has two or
  more distinct RPC endpoints and `GGML_RPC_NO_COMM` is unset, it sends `RPC_CMD_COMM_INIT` with port = (rank-0
  port + 1000) (`ggml/src/ggml-rpc/ggml-rpc.cpp:3258`), and the rank-0 `rpc-server` then opens a second, unauthenticated
  listener on `0.0.0.0:<that port>` (`ggml-rpc.cpp:2352`). Only the main listener keeps `--host`. Start therefore
  protects the comm port like `<Port>` (see below), and refuses a `<Port>` above 64535 because its comm port would
  not fit. Defence in depth: setting `GGML_RPC_NO_COMM=1` in the environment of Main's `llama-server` turns
  COMM_INIT off (`ggml-rpc.cpp:3229`), so no comm listener is opened; the Main launchers do not set it.
- **The comm port is closed to Main too.** No rule admits `<Port>` + 1000 from `10.77.0.1` (the peer rule is for
  `<Port>` only), so when Node1's `rpc-server` is rank 0 a rank on Main cannot reach its comm listener: that rank's
  connect retries fail (`ggml-rpc.cpp:2376-2385`), it answers COMM_INIT with failure, and the client logs
  "rank ... failed to initialize" and gives up on the communicator (`ggml-rpc.cpp:3283-3295`). Nothing is admitted,
  so this is safe, but note (static reading of the source, not a live test) that rank 0 waits in `accept()` with no
  timeout (`ggml-rpc.cpp:2361`, `transport.cpp:726-727`), so Main's `llama-server` can stall at COMM_INIT instead of
  failing fast. For splits over two or more RPC endpoints, set `GGML_RPC_NO_COMM=1` in the environment of Main's
  `llama-server` so COMM_INIT is never sent.
- Main `llama-server` binds **strictly** to `127.0.0.1:11438` (loopback only, no cloud egress).
- `start-node1-rpc.ps1 -Action Start` **always** checks the effective Windows Firewall policy before it launches
  `rpc-server`, with or without `-EnsureFirewall`, and refuses to start (throws, non-zero exit) when:
  - no enabled inbound Allow rule has exactly TCP `<Port>`, LocalAddress `<HostAddress>` and RemoteAddress `10.77.0.1`;
  - any other enabled inbound Allow rule admits TCP `<Port>` or the comm port TCP `<Port>` + 1000 (directly, through
    `Any`, a range that contains it, a port list, or a port keyword) or an `rpc-server.exe` / `ggml-rpc-server.exe`
    program, from any remote address other than exactly `10.77.0.1` (`Any`, `LocalSubnet`, a LAN range, ...). A
    program rule counts as an rpc-server rule by its file name (case-insensitive) in any directory or path form (8.3, junction, `\Device\HarddiskVolumeN`), and an 8.3
    short file name (`RPC-SE~1.EXE`) counts too because it cannot be compared. Rules bound to another program or to a
    packaged (Store) app are ignored, because they cannot admit `rpc-server` traffic;
  - such a rule (including one shaped exactly like the peer rule) has a non-empty `RemoteDynamicKeywordAddresses`.
    A rule admits its `RemoteAddress` plus every address its dynamic keywords resolve to, so a rule with any keyword
    is never peer-only: it is never accepted as the peer rule, and it refuses as wide when it covers TCP `<Port>` or
    the comm port. The refusal lists the keyword ids. The property is read from the rule object; if it cannot be
    read, Start refuses;
  - the Windows Defender Firewall service (`mpssvc`) is not `Running`, or its state cannot be read;
  - `netsh interface portproxy show all` lists an entry that listens on or connects to TCP `<Port>` or the comm port
    (a port proxy relays connections as local traffic, which the inbound rules do not filter), or `netsh` fails or
    prints anything that cannot be parsed. The refusal gives the `netsh interface portproxy delete` command;
  - a firewall profile is off or allows inbound by default, or a rule's filters cannot be read;
  - any profile (Domain, Private or Public) lists any entry other than `NotConfigured` in `DisabledInterfaceAliases`,
    whatever interface it names (a friendly name, a GUID, a renamed adapter). An excluded interface is not filtered
    at all, and the comm listener binds `0.0.0.0`, so no exclusion is safe. The lists are read from both the
    `ActiveStore` (effective policy, including GPO) and the `PersistentStore` (local setting) and united; if either
    store cannot be read, or a profile's list cannot be read, Start refuses.

  The refusal message names the fix: run `ensure-node1-rpc-fw.ps1 -Port <Port>` (or Start with `-EnsureFirewall`)
  from an elevated shell, `Set-NetFirewallProfile -Name <profile> -DisabledInterfaceAliases NotConfigured` for an
  interface exclusion (or the GPO / MDM policy when the entry is only in the `ActiveStore`), and, for each wider rule
  it lists (with its `PolicyStoreSource`), `Disable-NetFirewallRule -Name '<rule>'` for a local rule (a `'` in the
  name is doubled so the command pastes as is), or the GPO (then `gpupdate /force`) or MDM policy the rule comes from.
- **A refused Start stops anything listening on `<HostAddress>:<Port>`**, not only an instance this script started:
  whatever is up there is exposed by the same firewall state. It uses the same steps as `-Action Stop` (state-file
  PID, its wrapper, then every process listening on that endpoint) and says so in the refusal; if it cannot be
  stopped, the message says so and gives the PID. This includes a refusal caused by a transient error, for example
  running Start from a non-elevated shell that cannot read a rule's filters, so a working instance can be taken down.
- If `rpc-server` does not start listening within 20 s, Start stops the whole process tree it launched (the
  PowerShell wrapper and any `rpc-server` child, which may have bound somewhere unexpected), not only the wrapper.
- Accepted deviation from the design doc's "Profile Any" for the peer rule: Start does not check the peer rule's
  profile. A peer rule scoped to a narrower profile can only admit fewer connections than `Any`, so this is
  fail-safe (at worst Main cannot connect); `ensure-node1-rpc-fw.ps1` still creates it with `-Profile Any`.
- `-EnsureFirewall` runs `ensure-node1-rpc-fw.ps1 -Port <Port>` first (needs admin) and refuses to start if that
  helper is missing from the folder holding `start-node1-rpc.ps1`, or fails. The helper always uses LocalAddress
  `10.77.0.2`, so `-EnsureFirewall` only satisfies the check for the default `-HostAddress 10.77.0.2`.

## Current Hardware Usage (780M iGPU on Node1)

1. Ensure Node1 firewall rule (once as admin on Node1; add `-Port <Port>` if rpc-server uses a non-default port):
   ```powershell
   powershell -NoProfile -File C:\NewBuild\scripts\ensure-node1-rpc-fw.ps1
   ```
2. Start Node1 Vulkan RPC worker. From an elevated shell, `-EnsureFirewall` refreshes the peer-only rule first:
   ```powershell
   powershell -NoProfile -File C:\NewBuild\scripts\start-node1-rpc.ps1 -Action Start -EnsureFirewall
   ```
   From a non-admin shell, omit `-EnsureFirewall`: Start still verifies the firewall and refuses if step 1 was not done.
3. Start Main sharded `llama-server` (using an Ollama tag or explicit `.gguf` path):
   ```powershell
   powershell -NoProfile -File %LOCALAPPDATA%\sonder\deployment\start-rpc-sharded.ps1 `
     -Action Start -OllamaModel "R4C3R/qwen3-0.6b-heretic:q4_k_m" -CtxSize 4096
   ```

## One-Step Flip When RX 9060 XT 16 GB Arrives & BIOS UMA Is Lowered

1. Install the **RX 9060 XT 16 GB** in Node1 and lower the **780M iGPU BIOS UMA** frame buffer to minimum (`512M` or `Auto`) so system RAM is freed and Vulkan device selection prioritizes the discrete 16 GB GPU.
2. Verify the Vulkan device name on Node1 (`Vulkan0` vs `Vulkan1`):
   ```powershell
   powershell -NoProfile -File C:\NewBuild\scripts\start-node1-rpc.ps1 -Action Devices
   ```
3. Start Node1 `rpc-server` pinned to the RX 9060 XT Vulkan device (with `-UseCache` optional; from an elevated
   shell, or drop `-EnsureFirewall` when not admin):
   ```powershell
   powershell -NoProfile -File C:\NewBuild\scripts\start-node1-rpc.ps1 -Action Stop
   powershell -NoProfile -File C:\NewBuild\scripts\start-node1-rpc.ps1 -Action Start -Device Vulkan0 -UseCache -EnsureFirewall
   ```
4. Flip Main launch flags to balanced 16 GB + 16 GB VRAM sharding (`-TensorSplit 16,16 -CtxSize 16384`):
   ```powershell
   powershell -NoProfile -File %LOCALAPPDATA%\sonder\deployment\start-rpc-sharded.ps1 `
     -Action Start `
     -ModelPath "D:\ollama\gguf\unsloth\Qwen3.8-27B-GGUF\Qwen3.8-27B-UD-Q6_K_M.gguf" `
     -TensorSplit "16,16" -CtxSize 16384 -GpuLayers 999 -FlashAttn on -CacheTypeK q8_0 -CacheTypeV q8_0
   ```
   Wire Sonder tiers via env:
   ```powershell
   $env:SONDER_OPENAI_BASE_URL = "http://127.0.0.1:11438"
   $env:SONDER_OPENAI_MODEL = "sonder-rpc"
   $env:SONDER_GENERAL_PROVIDER = "openai-compatible"
   $env:SONDER_REASONING_PROVIDER = "openai-compatible"
   ```
