#!/usr/bin/env bash
# Production installer for the server-private profile (SPEC-2 WP8).
#
# Unlike deploy_sonder.sh --serve, this installer:
#   - never binds publicly and never advertises a plaintext URL,
#   - never prints the API key,
#   - installs into an immutable versioned release directory,
#   - keeps secrets and state outside the release tree,
#   - installs a dedicated OS identity and hardened systemd units.
#
# Build the audited payload as an unprivileged user first:
#   python3 scripts/package_local_system.py --out dist/local-system
# Then install only that manifest-verified payload:
#   sudo packaging/install_sonder.sh --package-source dist/local-system \
#       [--version-tag <tag>]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PACKAGE_SOURCE="$SCRIPT_DIR/../dist/local-system"
VERSION_TAG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --package-source|--source) PACKAGE_SOURCE="$2"; shift 2 ;;
    --version-tag) VERSION_TAG="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  echo "this installer must run as root" >&2
  exit 1
fi
PACKAGE_SOURCE="$(cd "$PACKAGE_SOURCE" 2>/dev/null && pwd)" || {
  echo "package source does not exist: $PACKAGE_SOURCE" >&2
  echo "build it first: python3 scripts/package_local_system.py --out dist/local-system" >&2
  exit 1
}
if [ ! -f "$PACKAGE_SOURCE/PACKAGE-MANIFEST.json" ] || \
   [ ! -f "$PACKAGE_SOURCE/scripts/package_local_system.py" ]; then
  echo "--package-source must name an audited local-system package, not a checkout" >&2
  echo "build it first: python3 scripts/package_local_system.py --out dist/local-system" >&2
  exit 1
fi
if [ -z "$VERSION_TAG" ]; then
  VERSION_TAG="$(date -u +%Y%m%dT%H%M%SZ)"
fi
case "$VERSION_TAG" in
  [A-Za-z0-9]*) ;;
  *)
    echo "invalid --version-tag: use 1-64 letters, digits, dots, underscores, or hyphens" >&2
    exit 2
    ;;
esac
case "$VERSION_TAG" in
  *[!A-Za-z0-9._-]*)
    echo "invalid --version-tag: use 1-64 letters, digits, dots, underscores, or hyphens" >&2
    exit 2
    ;;
esac
if [ "${#VERSION_TAG}" -gt 64 ]; then
  echo "invalid --version-tag: use 1-64 letters, digits, dots, underscores, or hyphens" >&2
  exit 2
fi

RELEASE_DIR="/opt/sonder/releases/$VERSION_TAG"
STAGING="$RELEASE_DIR.staging"
echo "installing release $VERSION_TAG"

# 1. Service identity and directories.
id sonder >/dev/null 2>&1 || useradd --system --home /var/lib/sonder --shell /usr/sbin/nologin sonder
install -d -o sonder -g sonder -m 0700 /var/lib/sonder /var/log/sonder /var/backups/sonder
install -d -o sonder -g sonder -m 0750 /srv/sonder/workspaces
install -d -m 0755 /etc/sonder /opt/sonder/releases

# 2. Immutable versioned release with its own virtualenv.
if [ -e "$RELEASE_DIR" ]; then
  echo "release $RELEASE_DIR already exists; refusing to overwrite in place" >&2
  exit 1
fi
if [ -e "$STAGING" ]; then
  echo "staging path $STAGING already exists; inspect and remove it explicitly" >&2
  exit 1
fi
mkdir -p "$STAGING"

# Verify and copy only files listed in PACKAGE-MANIFEST.json, using the
# installer's OWN verifier (the tree this script ships in), never the copy
# inside --package-source: importing the package's module would execute its
# code as root before a single byte had been checked.  `python3 -I` ignores
# PYTHONPATH and user site-packages and running from / keeps the working
# directory off sys.path, so nothing in the package can shadow the verifier.
# Ignored/untracked checkout state and unlisted files can never enter the
# privileged release directory through this path.
TRUSTED_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
verify_and_stage_payload() {
  local trusted_root="$1" package_source="$2" staging="$3"
  if [ ! -f "$trusted_root/scripts/package_local_system.py" ]; then
    echo "installer verifier missing: $trusted_root/scripts/package_local_system.py" >&2
    return 1
  fi
  (cd / && python3 -I - "$trusted_root" "$package_source" "$staging" <<'PY'
import importlib.util
import sys
from pathlib import Path

trusted_root, package_source, staging = (Path(arg) for arg in sys.argv[1:4])
verifier_path = trusted_root / "scripts" / "package_local_system.py"
spec = importlib.util.spec_from_file_location("sonder_package_verifier", verifier_path)
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)
verifier.copy_verified_payload(package_source, staging)
PY
  )
}
verify_and_stage_payload "$TRUSTED_ROOT" "$PACKAGE_SOURCE" "$STAGING"
python3 -m venv "$STAGING/venv"
"$STAGING/venv/bin/pip" install --quiet --upgrade pip
"$STAGING/venv/bin/pip" install --quiet -r "$STAGING/requirements-runtime.txt"
# requirements-runtime.txt pins the top-level releases only; pip resolves the
# transitive closure at install time and there is no hash lock yet (see
# docs/runbooks/install-server-private.md).  Refuse an inconsistent
# resolution before the release becomes current, and record exactly what was
# installed next to the release for audit and reproduction.
"$STAGING/venv/bin/python" -m pip check
"$STAGING/venv/bin/python" -m pip freeze --all > "$STAGING/INSTALLED-REQUIREMENTS.txt"
mv "$STAGING" "$RELEASE_DIR"
ln -sfn "$RELEASE_DIR" /opt/sonder/current

# 3. Configuration and secrets (secrets file mode 0600, key never printed).
if [ ! -f /etc/sonder/sonder.toml ]; then
  install -m 0644 "$RELEASE_DIR/packaging/sonder.toml.example" /etc/sonder/sonder.toml
  echo "wrote /etc/sonder/sonder.toml — review before first start"
fi
if [ ! -f /etc/sonder/sonder.env ]; then
  umask 077
  KEY="$("$RELEASE_DIR/venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(32))')"
  AUTH="$("$RELEASE_DIR/venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(32))')"
  cat > /etc/sonder/sonder.env <<EOF
SONDER_API_KEY=$KEY
SONDER_AUTH_SECRET=$AUTH
EOF
  chmod 0600 /etc/sonder/sonder.env
  unset KEY AUTH
  echo "generated secrets in /etc/sonder/sonder.env (mode 0600; keys are not printed)"
fi

# 4. Hardened service units and timers.
install -m 0644 "$RELEASE_DIR/packaging/systemd/sonder.service" /etc/systemd/system/
install -m 0644 "$RELEASE_DIR/packaging/systemd/sonder-backup.service" /etc/systemd/system/
install -m 0644 "$RELEASE_DIR/packaging/systemd/sonder-backup.timer" /etc/systemd/system/
install -m 0644 "$RELEASE_DIR/packaging/systemd/sonder-restore-smoke.service" /etc/systemd/system/
install -m 0644 "$RELEASE_DIR/packaging/systemd/sonder-restore-smoke.timer" /etc/systemd/system/
systemctl daemon-reload

# 5. Preflight before anything is enabled; the listener never opens on failure.
if ! sudo -u sonder "$RELEASE_DIR/venv/bin/python" -m sonder_runtime preflight \
    --config /etc/sonder/sonder.toml --secrets /etc/sonder/sonder.env --skip-ollama; then
  echo "preflight reported problems — fix them, then: systemctl enable --now sonder" >&2
  exit 1
fi

cat <<'EOF'
Install complete.

Next steps:
  1. Review /etc/sonder/sonder.toml (loopback bind is the default and the
     only supported direct bind).
  2. Ensure Ollama is running, then: systemctl enable --now sonder
  3. Enable backups: systemctl enable --now sonder-backup.timer sonder-restore-smoke.timer
  4. For remote access install a TLS reverse proxy; see
     packaging/reverse-proxy/nginx-sonder.conf and docs/runbooks/secure-remote-access.md.

The API key is in /etc/sonder/sonder.env — it is never printed or logged.
EOF
