# Install: server-private profile

Target: self-hosted Linux server, single private owner, systemd.
Sonder binds loopback only; remote access goes through the TLS reverse
proxy (see `packaging/reverse-proxy/nginx-sonder.conf`).

## 1. Build the audited package

```bash
git clone https://github.com/Krilliac/Sonder-runtime.git
cd Sonder-runtime
git checkout <reviewed-tag-or-sha>
python3 scripts/package_local_system.py --out dist/local-system
```

Build as the unprivileged checkout owner. The packager includes only tracked,
allowlisted runtime files, privacy-scans them, and writes their sizes and
SHA-256 hashes to `PACKAGE-MANIFEST.json`.

## 2. Install the verified release

This runbook covers the initial source-checkout install. Install from a reviewed
tag or exact commit through the manifest-verified package path — never copy or
run production from a mutable developer checkout. SPEC-4 signed bundles are
the separate update/distribution path described in `publish-release.md`.

```bash
VERSION_TAG=$(git rev-parse --verify HEAD)
sudo packaging/install_sonder.sh \
  --package-source dist/local-system \
  --version-tag "$VERSION_TAG"
```

The installer validates the version tag before constructing root-owned paths,
verifies every manifest entry before copying it, ignores every unlisted file,
and refuses to overwrite an existing release or staging directory.

## 3. Configuration and secrets

The installer creates the service identity and directories, writes the default
loopback configuration if absent, and generates `/etc/sonder/sonder.env` with
mode 0600. Edit `/etc/sonder/sonder.toml` for the host. The API key is never
printed; read it from the secrets file when configuring a client.

## 4. Validate before first start

```bash
sudo -u sonder /opt/sonder/current/venv/bin/python -m sonder_runtime preflight \
    --config /etc/sonder/sonder.toml --secrets /etc/sonder/sonder.env
sudo -u sonder /opt/sonder/current/venv/bin/python -m sonder_runtime migrate \
    --config /etc/sonder/sonder.toml --secrets /etc/sonder/sonder.env
```

Preflight must pass (Ollama must be running, or use `--skip-ollama` to
inspect the remaining checks).

## 5. Enable the service

```bash
sudo cp /opt/sonder/current/packaging/systemd/sonder.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sonder
sudo -u sonder /opt/sonder/current/venv/bin/python -m sonder_runtime status --json
```

## 6. Remote access (optional)

Never expose the runtime port directly. Install the reverse-proxy
reference from `packaging/reverse-proxy/nginx-sonder.conf` with real
certificates, and keep `[server].host = "127.0.0.1"`.
