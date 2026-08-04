# Install: server-private profile

Target: self-hosted Linux server, single private owner, systemd.
Sonder binds loopback only; remote access goes through the TLS reverse
proxy (see `packaging/reverse-proxy/nginx-sonder.conf`).

## 1. Create the service identity and directories

```bash
sudo useradd --system --home /var/lib/sonder --shell /usr/sbin/nologin sonder
sudo install -d -o sonder -g sonder -m 0700 /var/lib/sonder /var/log/sonder /var/backups/sonder
sudo install -d -o sonder -g sonder -m 0750 /srv/sonder/workspaces
sudo install -d -m 0755 /etc/sonder /opt/sonder/releases
```

## 2. Install a release

Until the SPEC-4 signed distribution exists, install from a checked-out
tag into a versioned directory — never run production from a mutable
checkout:

```bash
VERSION_DIR=/opt/sonder/releases/$(git -C Sonder-runtime describe --always)
sudo rsync -a --exclude .git Sonder-runtime/ "$VERSION_DIR/"
sudo python3 -m venv "$VERSION_DIR/venv"
sudo "$VERSION_DIR/venv/bin/pip" install -r "$VERSION_DIR/requirements-runtime.txt"
sudo ln -sfn "$VERSION_DIR" /opt/sonder/current
```

## 3. Configuration and secrets

```bash
sudo cp /opt/sonder/current/packaging/sonder.toml.example /etc/sonder/sonder.toml
sudo cp /opt/sonder/current/packaging/sonder.env.example /etc/sonder/sonder.env
sudo chmod 0600 /etc/sonder/sonder.env
python3 -c "import secrets; print('SONDER_API_KEY=' + secrets.token_urlsafe(32))" | sudo tee -a /etc/sonder/sonder.env >/dev/null
```

Edit `/etc/sonder/sonder.toml` for the host. The API key is never
printed by any install step; read it from the secrets file when
configuring a client.

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
