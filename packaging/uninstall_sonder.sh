#!/usr/bin/env bash
# Uninstaller (SPEC-2 WP8). Preserves private state by default; removing
# state and backups requires the explicit flag.
#
# Usage: sudo packaging/uninstall_sonder.sh [--purge-private-state]
set -euo pipefail

PURGE=0
[ "${1:-}" = "--purge-private-state" ] && PURGE=1

if [ "$(id -u)" -ne 0 ]; then
  echo "this uninstaller must run as root" >&2
  exit 1
fi

systemctl disable --now sonder.service sonder-backup.timer sonder-restore-smoke.timer 2>/dev/null || true
rm -f /etc/systemd/system/sonder.service \
      /etc/systemd/system/sonder-backup.service /etc/systemd/system/sonder-backup.timer \
      /etc/systemd/system/sonder-restore-smoke.service /etc/systemd/system/sonder-restore-smoke.timer
systemctl daemon-reload

rm -rf /opt/sonder
echo "removed /opt/sonder (releases)"

if [ "$PURGE" -eq 1 ]; then
  rm -rf /var/lib/sonder /var/log/sonder /var/backups/sonder /etc/sonder
  userdel sonder 2>/dev/null || true
  echo "PURGED private state, backups, secrets, and the sonder user"
else
  cat <<'EOF'
Preserved (remove manually or rerun with --purge-private-state):
  /var/lib/sonder       private runtime state
  /var/backups/sonder   backups
  /etc/sonder           configuration and secrets
  /var/log/sonder       logs
EOF
fi
