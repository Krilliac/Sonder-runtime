# llama.cpp RPC sharding scripts (one model across two machines)

These scripts run a single GGUF model split across the **main** machine's GPU
(`llama-server --rpc`) and one or more **workers** running llama.cpp
`rpc-server` over a private link. Sonder reaches the result through its
existing `openai-compatible` provider on a loopback URL. Sharding is opt-in and
off by default: nothing starts and the runtime probes nothing until
`SONDER_RPC_PEERS` (or `-RpcPeers`) names a worker.

Full procedure, prerequisites and troubleshooting:
[docs/runbooks/rpc-sharding.md](../../docs/runbooks/rpc-sharding.md).
Configuration reference:
[docs/wiki/03-configuration.md](../../docs/wiki/03-configuration.md#cross-pc-model-sharding-llamacpp-rpc-opt-in).

| Script | Runs on | Purpose |
|---|---|---|
| `rpc-policy.ps1` | both | Shared address/port policy, dot-sourced by the other three. Copy it with them. |
| `ensure-rpc-worker-firewall.ps1` | worker (admin) | Inbound allow rule for the RPC port, only from the main machine's private address to the worker's. |
| `start-rpc-worker.ps1` | worker | `Start`/`Stop`/`Status`/`Health`/`Devices` for `rpc-server` bound to the worker's private address. |
| `start-rpc-sharded.ps1` | main | `Start`/`Stop`/`Status`/`Health`/`Env` for the loopback `llama-server --rpc`, optionally launching Sonder with the matching environment. |

## Address and port policy (fail closed)

- Workers come only from an explicit allowlist: `SONDER_RPC_PEERS` or
  `-RpcPeers`, comma- or semicolon-separated `ip` or `ip:port` entries, at most
  16. There is no default worker.
- Every entry must be a literal IPv4 address in `10.0.0.0/8`, `172.16.0.0/12`,
  `192.168.0.0/16` or `169.254.0.0/16`. Hostnames, IPv6, loopback, `0.0.0.0`
  and public or documentation addresses are refused with an error.
- Every entry uses the one configured RPC port: `SONDER_RPC_PORT` or
  `-RpcPort` (default `50052`). An entry naming another port is refused.
- `rpc-server` binds to the worker's private address (`-BindAddress` or
  `SONDER_RPC_WORKER_ADDRESS`), never `0.0.0.0`. It has no authentication, so
  keep it on the private link and lock it with the firewall script.
- The main machine's `llama-server` binds to loopback only (`-BindHost`,
  default `127.0.0.1`; port `-Port`, default `11438`).

The same rules are enforced by the runtime
(`sonder_runtime/platform/rpc_sharding_config.py`) when it loads configuration.

## Binaries

Install the same official `ggml-org/llama.cpp` release on every machine (the
`ggml-rpc` protocol must match) into a dedicated folder:
`%LOCALAPPDATA%\sonder\llama-rpc` by default, or the folder named by
`SONDER_RPC_INSTALL_DIR` / `-InstallDir`. Pick the build that matches each
machine's GPU (for example CUDA on the main machine, Vulkan on the worker).

## Quick start

Replace `<worker-ip>` and `<main-ip>` with the two machines' addresses on the
private link (both must be private or link-local IPv4 addresses, as above).

On the worker (once, as administrator, then whenever you start it):

```powershell
powershell -NoProfile -File .\ensure-rpc-worker-firewall.ps1 -LocalAddress <worker-ip> -RemoteAddress <main-ip>
powershell -NoProfile -File .\start-rpc-worker.ps1 -Action Devices
powershell -NoProfile -File .\start-rpc-worker.ps1 -Action Start -BindAddress <worker-ip> -Device Vulkan0
```

On the main machine:

```powershell
$env:SONDER_RPC_PEERS = '<worker-ip>'
powershell -NoProfile -File .\start-rpc-sharded.ps1 -Action Start `
  -ModelPath '<path-to-model.gguf>' -TensorSplit '1,1' -CtxSize 16384
powershell -NoProfile -File .\start-rpc-sharded.ps1 -Action Env
```

`-Action Env` prints the variables Sonder needs (`SONDER_OPENAI_BASE_URL`,
`SONDER_OPENAI_MODEL`, `SONDER_RPC_PEERS`, `SONDER_RPC_PORT` and the
`SONDER_<TIER>_PROVIDER` entries for `-SonderTiers`). `-LaunchSonder serve`
sets them for the child process and starts Sonder from the repository root
(`-SonderRuntimeRoot`) with `-PythonExe`, `SONDER_PYTHON`, or `python`.
`-OllamaModel <tag>` uses an already-pulled Ollama model's GGUF blob instead of
`-ModelPath`.

`-TensorSplit` weights are relative: `1,1` splits evenly, `7,1` keeps most
layers on the main GPU (for a worker with a small or shared-memory GPU).
Named presets live in `sonder_runtime/adapters/data/rpc_sharding_profiles.json`
(override with `SONDER_RPC_PROFILES_FILE`).

## Launch behaviour

Both launchers start the server through `Win32_Process.Create` (via `Start-RpcDetached`), so the process is detached from the caller: it holds none of the caller's pipe handles and is outside the caller's job object. `Start` therefore returns promptly even under a one-shot `ssh host powershell -File ...` or a piped CI/agent wrapper, and the server survives the session closing. Trade-off: stdout is not captured. `start-rpc-sharded.ps1` passes `--log-file` to `llama-server` (the `stderr` path in the state file); `rpc-server` has no log capture.
