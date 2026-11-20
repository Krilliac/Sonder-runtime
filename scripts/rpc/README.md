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
- Node1 Windows Firewall rule `Node1 llama-rpc 50052 from Main peer` allows inbound TCP `50052` **only** from `10.77.0.1` to `10.77.0.2` (`-Profile Any` because private-link Ethernet can classify as `Public`).
- Main `llama-server` binds **strictly** to `127.0.0.1:11438` (loopback only, no cloud egress).
- `start-node1-rpc.ps1 -Action Start` **always** checks the effective Windows Firewall policy before it launches
  `rpc-server`, with or without `-EnsureFirewall`, and refuses to start (throws, non-zero exit) when:
  - no enabled inbound Allow rule has exactly TCP `<Port>`, LocalAddress `<HostAddress>` and RemoteAddress `10.77.0.1`;
  - any other enabled inbound Allow rule admits TCP `<Port>` (directly, through `Any`, a range that contains it, or a
    port keyword) or an `rpc-server.exe` / `ggml-rpc-server.exe` program, from any remote address other than exactly
    `10.77.0.1` (`Any`, `LocalSubnet`, a LAN range, ...). A program rule counts as an rpc-server rule by its file
    name (case-insensitive) in any directory or path form (8.3, junction, `\Device\HarddiskVolumeN`), and an 8.3
    short file name (`RPC-SE~1.EXE`) counts too because it cannot be compared. Rules bound to another program or to a
    packaged (Store) app are ignored, because they cannot admit `rpc-server` traffic;
  - a firewall profile is off or allows inbound by default, or a rule's filters cannot be read;
  - any profile lists the interface that owns `<HostAddress>` (resolved with `Get-NetIPAddress`) in
    `DisabledInterfaceAliases`, which turns the firewall off for that interface; or, when that interface cannot be
    resolved, any profile lists any interface there; or the profiles' `DisabledInterfaceAliases` cannot be read.

  The refusal message names the fix: run `ensure-node1-rpc-fw.ps1 -Port <Port>` (or Start with `-EnsureFirewall`)
  from an elevated shell, `Set-NetFirewallProfile -Name <profile> -DisabledInterfaceAliases NotConfigured` for an
  interface exclusion, and, for each wider rule it lists (with its `PolicyStoreSource`), `Disable-NetFirewallRule
  -Name '<rule>'` for a local rule, or the GPO (then `gpupdate /force`) or MDM policy the rule comes from.
- If Start refuses while an instance is already running, that instance is exposed by the same firewall state, so
  Start stops it (the same steps as `-Action Stop`) and says so in the refusal; if it cannot be stopped, the message
  says so and gives the PID.
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
