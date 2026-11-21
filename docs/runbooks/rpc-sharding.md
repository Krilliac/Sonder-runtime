# Cross-PC model sharding with llama.cpp RPC (opt-in)

This runbook runs **one** model split across two machines: the main
workstation's GPU and a worker's GPU, joined by llama.cpp's RPC backend over a
dedicated private link. Use it when a model does not fit on the main GPU alone
and a second machine has spare VRAM. When the model fits on one machine, run it
there instead: every token crosses the link, so a sharded model is slower than
the same model on one GPU.

Sharding is **off by default**. Nothing in the runtime changes until
`SONDER_RPC_PEERS` names at least one worker.

How it fits together:

```text
 Sonder (openai-compatible provider)
        |  http://127.0.0.1:11438  (loopback only)
 main:  llama-server --rpc <worker-ip>:50052   (local GPU + remote layers)
        |  private link, TCP 50052, firewall: main -> worker only
 worker: rpc-server --host <worker-ip> --port 50052   (worker GPU)
```

The scripts are in [`scripts/rpc/`](../../scripts/rpc/README.md); the
variables are listed in the
[configuration reference](../wiki/03-configuration.md#cross-pc-model-sharding-llamacpp-rpc-opt-in).

## 1. Prerequisites

- A private link between the two machines, with a static private (RFC 1918) or
  link-local IPv4 address on each end. [multi-node-windows-private-link.md](multi-node-windows-private-link.md)
  covers setting one up on Windows. Below, `<main-ip>` and `<worker-ip>` are
  those two addresses.
- The same official `ggml-org/llama.cpp` release on both machines (the RPC
  protocol changes between releases). Install it in a dedicated folder,
  `%LOCALAPPDATA%\sonder\llama-rpc` by default or the folder named by
  `SONDER_RPC_INSTALL_DIR`. Use the build that matches each GPU (for example a
  CUDA build on the main machine and a Vulkan build on the worker).
- Copy `scripts/rpc/` to the worker. The scripts dot-source `rpc-policy.ps1`,
  so keep the files together.

## 2. Security rules (fail closed)

- `rpc-server` has **no authentication or encryption**. Anyone who can reach
  its port can run work on the worker's GPU and read the tensors sent to it.
  Bind it only to the worker's private-link address and allow inbound traffic
  only from `<main-ip>`.
- Workers come only from the explicit allowlist (`SONDER_RPC_PEERS` or
  `-RpcPeers`). Each entry must be a literal IPv4 address in `10.0.0.0/8`,
  `172.16.0.0/12`, `192.168.0.0/16` or `169.254.0.0/16`, on the one configured
  RPC port (`SONDER_RPC_PORT`, default `50052`). Hostnames, IPv6, loopback,
  `0.0.0.0`, public and documentation addresses, and any other port are
  refused, both by the scripts and by the runtime at startup.
- The main machine's `llama-server` listens on loopback only
  (`127.0.0.1:11438` by default). The launcher refuses any other bind address
  and refuses `extra_args` that would override `--host`, `--port` or `--rpc`.

## 3. Prepare and start the worker

As administrator on the worker, create the peer-only firewall rule (rerun it
whenever an address or the port changes; it replaces the previous rule):

```powershell
powershell -NoProfile -File .\ensure-rpc-worker-firewall.ps1 -LocalAddress <worker-ip> -RemoteAddress <main-ip>
```

List the GPUs llama.cpp can see and pick the device for `-Device`:

```powershell
powershell -NoProfile -File .\start-rpc-worker.ps1 -Action Devices
```

Start the worker (state goes to `%LOCALAPPDATA%\sonder\run`, logs to
`%LOCALAPPDATA%\sonder\logs`; `-StateRoot` changes both):

```powershell
powershell -NoProfile -File .\start-rpc-worker.ps1 -Action Start -BindAddress <worker-ip> -Device Vulkan0
powershell -NoProfile -File .\start-rpc-worker.ps1 -Action Status -BindAddress <worker-ip>
```

`-UseCache` enables `rpc-server`'s local tensor cache, which shortens later
model loads. `-EnsureFirewall -AllowedPeer <main-ip>` runs the firewall step
as part of `Start`.

## 4. Start the sharded server on the main machine

```powershell
$env:SONDER_RPC_PEERS = '<worker-ip>'
powershell -NoProfile -File .\scripts\rpc\start-rpc-sharded.ps1 -Action Start `
  -ModelPath '<path-to-model.gguf>' -TensorSplit '1,1' -CtxSize 16384
```

`Start` refuses to launch when a worker is unreachable, waits up to
`-ReadyWaitSeconds` (default 180) for `/health` to report `ok`, and records its
state in `%LOCALAPPDATA%\sonder\run\llama-rpc-server.json`. `-OllamaModel <tag>`
uses an already-pulled Ollama model's GGUF file instead of `-ModelPath`.

Choosing `-TensorSplit`: the weights are relative shares of layers per device,
main GPU first. `1,1` suits two GPUs with similar free VRAM; `7,1` keeps most
layers on the main GPU when the worker has a small or shared-memory GPU. Named
presets are in `sonder_runtime/adapters/data/rpc_sharding_profiles.json`;
point `SONDER_RPC_PROFILES_FILE` at your own file (same shape) to use others.

## 5. Point Sonder at it

```powershell
powershell -NoProfile -File .\scripts\rpc\start-rpc-sharded.ps1 -Action Env
```

prints the variables to set for Sonder:

| Variable | Value |
|---|---|
| `SONDER_RPC_PEERS` | the worker allowlist, e.g. `<worker-ip>:50052` |
| `SONDER_RPC_PORT` | the RPC port (default `50052`) |
| `SONDER_OPENAI_BASE_URL` | the loopback server, `http://127.0.0.1:11438` |
| `SONDER_OPENAI_MODEL` | the `-Alias` (default `sonder-rpc`) |
| `SONDER_<TIER>_PROVIDER` | `openai-compatible` for each tier in `-SonderTiers` (`rpc-sharded` and `distributed-rpc` are accepted aliases) |

Set them in the environment Sonder starts from, or pass `-LaunchSonder serve`
to have the script start Sonder with them.

## 6. Verify

- **Configuration.** With `SONDER_RPC_PEERS` set, Sonder refuses to start
  when an entry breaks the rules above, when `SONDER_RPC_PORT` is not a port,
  or when `SONDER_OPENAI_BASE_URL` is missing or not a loopback URL. The error
  names the variable and the reason.
- **Startup probe.** Once listening, `sonder serve` probes the sharded server
  once in the background and logs
  `RPC sharded backend: healthy=True status=ok models=['sonder-rpc'] error=-`.
  `healthy=False` means the server is not up yet or a worker dropped; the log
  line carries the error. Without `SONDER_RPC_PEERS` no probe runs.
- **By hand.** `start-rpc-sharded.ps1 -Action Status` reports the server's
  health and each worker's reachability; `-Action Health` exits non-zero when
  the server is not healthy.

## 7. Stop

```powershell
powershell -NoProfile -File .\scripts\rpc\start-rpc-sharded.ps1 -Action Stop
powershell -NoProfile -File .\start-rpc-worker.ps1 -Action Stop -BindAddress <worker-ip>
```

To turn sharding off for Sonder, unset `SONDER_RPC_PEERS` and point the tiers
back at their previous providers.

## 8. Troubleshooting

| Symptom | Likely cause |
|---|---|
| `RPC peer <worker-ip>:50052 is unreachable` | Worker not started, firewall rule missing or naming the wrong addresses, or the link is down. Run `start-rpc-worker.ps1 -Action Health` on the worker and `Test-NetConnection <worker-ip> -Port 50052` on the main machine. |
| `... is not a private ... or link-local ... IPv4 address` | The allowlist names a hostname, public address or IPv6 address. Use the worker's private-link IPv4 address. |
| `... uses port N, but the configured RPC port is M` | An entry's `:port` differs from `SONDER_RPC_PORT` / `-RpcPort`. Use one port for every worker. |
| `llama-server exited during startup` | Mismatched llama.cpp releases, not enough VRAM for the split, or a bad model path. The stderr tail is in the error and in `%LOCALAPPDATA%\sonder\logs`. |
| Very slow generation | Expected to a degree; check the link speed and move layers to the main GPU with a larger first `-TensorSplit` weight. |
