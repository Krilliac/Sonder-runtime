# Optional isolated execution

`isolated_run` is a direct MCP-only tool for running an already-installed Linux
container image through Docker or Podman. It is disabled when neither runtime is
detected and can be forced off with `SONDER_ISOLATED_RUNTIME=off`. The setting
also accepts only `auto`, `docker`, or `podman`; it cannot name an executable.

The local permission default is `ask`. The tool is deliberately absent from the
Sonder agent, project-agent, workbench, and autopilot tool lists. This preserves
a host decision boundary for the only integrity-expanding option:
`writable_workspace=true`. The exact absolute project directory is the only host
bind. It is read-only by default.

## Fixed policy

The caller supplies an OCI image reference, a JSON array containing the
container command argv, the exact project path, optional stdin, and bounded
resource requests. The runtime command is always launched with `shell=False`.
The caller cannot supply runtime flags, extra mounts, a Docker socket, devices,
environment variables, a container user, an entrypoint, capabilities, or
privileged mode. Images are never pulled implicitly (`--pull=never`).

Every launch has:

- networking disabled;
- a read-only root filesystem;
- all capabilities dropped and `no-new-privileges` enabled;
- a fixed unprivileged UID/GID (`65534:65534`);
- a 64 MiB `noexec,nosuid,nodev` tmpfs at `/tmp`;
- a minimal environment created by `/usr/bin/env -i`;
- PID, memory, CPU, wall-time, stdin, and combined-output caps;
- one generated container name so timeout/output-limit cleanup can issue a
  bounded argv-only `docker|podman rm -f`.

Defaults are 30 seconds, 512 MiB, 1 CPU, 64 PIDs, 64 KiB stdin, and 128 KiB
combined output. Hard maxima are 120 seconds, 4096 MiB, 4 CPUs, 256 PIDs,
64 KiB stdin, and 256 KiB combined output.

Windows paths must be absolute, drive-qualified local paths. UNC paths, device
paths, relative paths, control characters, and the comma delimiter used by the
runtime mount syntax fail closed. The resolved path is mounted directly; Sonder
does not translate it through WSL/MSYS conventions.

## Security boundary and limitations

This is defense in depth, not an escape-proof sandbox. Its guarantees depend on
the external Docker/Podman CLI, daemon or Podman machine, OCI runtime, image,
virtualization layer, and host kernel being correctly configured and patched.
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
