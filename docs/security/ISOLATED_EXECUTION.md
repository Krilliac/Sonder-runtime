# Optional isolated execution

`isolated_run` is a direct MCP-only tool for running an already-installed Linux
container image through Docker or Podman. It is **off by default**. Set
`SONDER_ISOLATED_RUNTIME=auto|docker|podman` to enable it explicitly; the setting
cannot name an executable. Detection requires a responsive Linux engine through
a local Unix socket, named pipe, loopback endpoint, or loopback Podman-machine
connection. An installed but stopped engine is skipped and `auto` tries the
other engine. Remote contexts are rejected. The verified Docker endpoint is
pinned into execution with `--host`; Podman-machine connections are pinned with
the verified local `--url` and configured identity rather than re-resolving a
mutable default at launch time.

The local permission default is `ask`, but metadata is not the authorization
boundary. Every call must also provide a valid developer token, the secret from
`SONDER_ISOLATED_APPROVAL_CODE`, and `acknowledge_isolation_limits=true`.
Writable execution additionally requires the distinct secret configured in
`SONDER_ISOLATED_WRITE_APPROVAL_CODE`. The tool is deliberately absent from the
Sonder agent, project-agent, workbench, and autopilot tool lists. This preserves
a host decision boundary for the only integrity-expanding option:
`writable_workspace=true`. The exact absolute project directory is the only host
bind. It is read-only by default. `SONDER_ISOLATED_ROOTS` must explicitly list
one or more authorized parent directories; arbitrary host paths are rejected.

## Fixed policy

The caller supplies an OCI image reference, a JSON array containing the
container command argv, the exact project path, optional stdin, and bounded
resource requests. The runtime command is always launched with `shell=False`.
The caller cannot supply runtime flags, extra mounts, a Docker socket, devices,
environment variables, a container user, an entrypoint, capabilities, or
privileged mode. Images are never pulled implicitly (`--pull=never`).

Every launch has:

- networking disabled;
- daemon logging disabled with `--log-driver=none`, so engine/journald storage
  cannot grow independently of the MCP output cap;
- image health checks disabled with `--no-healthcheck`, preventing an image
  metadata command from becoming a second execution path;
- a read-only root filesystem;
- all capabilities dropped and `no-new-privileges` enabled;
- a fixed unprivileged UID/GID (`65534:65534`);
- a 64 MiB `noexec,nosuid,nodev` tmpfs at `/tmp`;
- a minimal environment created by `/usr/bin/env -i`;
- PID, memory, CPU, wall-time, stdin, and combined-output caps;
- one generated container name so timeout/output-limit cleanup can issue a
  bounded argv-only `docker|podman rm -f`, retry it, and verify the exact name
  is absent through a successful exact-name container-list query. Command
  failure is uncertainty, never evidence of absence;
- memory plus swap set to the same total limit, preventing extra swap allowance.
  Docker uses its fixed assignment form; Podman receives separate arguments and
  must reject the launch if its cgroup/runtime cannot honor them;
- nested mounts rejected before launch and recursion disabled in the actual
  bind: Docker uses `bind-recursive=disabled`, while Podman uses
  `bind-nonrecursive=true`. Read-only is then applied with the engine-specific
  mount option.

Before launch, Sonder runs a pinned-endpoint, five-second, 64 KiB-capped local
`image inspect`. It rejects missing images, malformed metadata, and every image
whose OCI `Config.Volumes` is non-empty. The immutable inspected image ID—not
the caller's mutable tag—is passed to `run`. Podman additionally receives
`--image-volume=ignore` as defense in depth. Inspection never pulls an image;
the eventual run remains fixed at `--pull=never` and `--network=none`.

Defaults are 30 seconds, 512 MiB, 1 CPU, 64 PIDs, 64 KiB stdin, and 128 KiB
combined output. Hard maxima are 120 seconds, 4096 MiB, 4 CPUs, 256 PIDs,
64 KiB stdin, and 256 KiB combined output.

Windows paths must be absolute, drive-qualified local paths. UNC paths, device
paths, relative paths, control characters, and the comma delimiter used by the
runtime mount syntax fail closed. The resolved path is mounted directly; Sonder
does not translate it through WSL/MSYS conventions.

The selected project must be inside `SONDER_ISOLATED_ROOTS`. Filesystem roots,
Windows drive roots, `/proc`, `/sys`, `/dev`, `/run`, submounts, symlinks,
Windows reparse points, Unix sockets, FIFOs, devices, and other special entries
are rejected before launch. This prevents a nominal project bind from exposing
runtime sockets, devices, or writable nested mounts. The safety walk is bounded
at 100,000 entries and fails closed when it cannot inspect an entry. A project
that is itself a mount must exactly equal an authorized root. Sonder captures
its filesystem identity after scanning, rechecks the scan and identity directly
before launch, and checks identity again immediately after starting the CLI.

## Security boundary and limitations

This is defense in depth, not an escape-proof sandbox. Its guarantees depend on
the external Docker/Podman CLI, daemon or Podman machine, OCI runtime, image,
virtualization layer, bind-recursion implementation, and host kernel being
correctly configured and patched.
A compromised daemon or kernel defeats these controls. Docker Desktop and
Podman machine may also implement the bind through a VM/filesharing layer.

The fixed `/usr/bin/env` entrypoint means minimal or `scratch` images without
that executable fail rather than falling back to the image entrypoint. The
fixed unprivileged user may be unable to read restrictive project files or
write a host-approved writable workspace; loosen host file permissions only
after reviewing that consequence. Runtime registry credentials and proxy
variables are scrubbed, so only already-installed images are supported.

Do not mount the Docker socket into the project or place sensitive device nodes,
sockets, or credentials in a writable workspace. The tool cannot distinguish a
special file already present inside the one approved project bind.

The Docker- and Podman-specific argv contracts are covered by unit tests. No
ready container engine was present on the implementation host, so a live engine
smoke test remains unverified; availability probes and the launch itself fail
closed rather than weakening flags when an engine rejects an option.
