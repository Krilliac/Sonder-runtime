# Unsafe lab model testing

Unsafe lab mode exists for evaluating models with no behavioral guardrails on
an environment whose loss is expected. It deliberately removes Sonder's
agent/autopilot host-tool policy. It does **not** create, configure, verify, or
claim an operating-system isolation boundary.

## Required environment

Prepare the isolation outside Sonder:

- a disposable VM, or a hardened disposable container with a separate kernel
  risk accepted by the operator;
- a dedicated unprivileged user, never root/Administrator/elevated;
- no host mounts, shared home directories, forwarded agents, host credential
  stores, cloud credentials, or secrets;
- no Docker/Podman/containerd/VM-management socket or API;
- no privileged container, host PID/network namespace, device passthrough, or
  broad Linux capabilities;
- loopback-only Sonder service exposure and restricted outbound network;
- strict CPU, memory, process, disk, and execution-time limits at the VM or
  container boundary; and
- a clean snapshot that can be destroyed after the experiment.

Containerization alone is not proof of isolation. Kernel/runtime flaws and
unsafe mounts or sockets can still expose the host. Prefer a disposable VM for
actively hostile model testing.

## Activate

Set the acknowledgement as one exact value before starting Sonder:

```powershell
$env:SONDER_UNSAFE_LAB_ACK = 'I UNDERSTAND SONDER UNSAFE LAB MODE GIVES MODELS UNRESTRICTED HOST TOOL ACCESS AND I AM RUNNING IN A DISPOSABLE ISOLATED ENVIRONMENT'
$env:SONDER_HOST = '127.0.0.1'
python server.py
```

On POSIX, export the same two values and run under the dedicated non-root
account. Do not use `sudo`.

Sonder refuses misspellings/truthy shorthand, non-loopback binding, and
root/elevated execution. A successful activation prints the warning through
`status()` and `diagnostics()` and durably appends it to
`$SONDER_HOME/audit/unsafe-lab.jsonl`. `SONDER_UNSAFE_LAB_AUDIT_PATH` may point
that record at a persistent evidence volume, but never at a host-mounted
credential or source directory.

## What changes

Within the acknowledged process, model-driven agent and autopilot runs are no
longer confined by their normal tool allowlist, project root, read-only mode,
web/location consent flag, argument-aware executable/language list, or file
approval token. Model-directed subprocesses still receive a secret/control
scrubbed environment. Direct tool timeout/output limits and operating-system
permissions remain, but neither is a security guarantee.

When the variable is absent, all normal policies are unchanged.

## Finish

Preserve only sanitized test evidence, then destroy/revert the whole guest.
Do not reuse it for normal development, and do not move generated executables
or scripts onto a trusted host without separate malware-oriented inspection.
