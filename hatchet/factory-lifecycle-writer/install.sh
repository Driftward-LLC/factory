#!/usr/bin/env bash
set -euo pipefail

[[ "$(id -u)" == "0" ]] || { echo "Factory lifecycle writer install requires root" >&2; exit 1; }
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
env_file=/etc/driftward/factory-lifecycle-writer.env
state_dir=/var/lib/driftward-factory-lifecycle-writer
bridge_env=/etc/driftward/hatchet-bridge.env
install_root=/usr/local/lib/driftward-factory-lifecycle-writer

install -d -m 0755 -o root -g root /etc/driftward /etc/systemd/system
if ! id -u driftward-factory-writer >/dev/null 2>&1; then
  useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin driftward-factory-writer
fi
install -d -m 0700 -o driftward-factory-writer -g driftward-factory-writer "$state_dir"
chown -R driftward-factory-writer:driftward-factory-writer "$state_dir"
install -d -m 0755 -o root -g root "$install_root/factory_lifecycle_writer"
install -m 0644 "$script_dir"/factory_lifecycle_writer/*.py "$install_root/factory_lifecycle_writer/"
install -m 0755 "$script_dir/factory-lifecycle-token-refresh" "$script_dir/factory-lifecycle-service" "$install_root/"
[[ -e "$env_file" ]] || install -m 0600 -o root -g root /dev/null "$env_file"
chown root:root "$env_file"; chmod 0600 "$env_file"
bridge_token="$(awk -F= '$1 == "HATCHET_HERMES_BRIDGE_TOKEN" { print $2; exit }' "$bridge_env")"
[[ -n "$bridge_token" ]] || { echo "missing bridge token" >&2; exit 1; }
tmp="$(mktemp)"
watermark="$(awk -F= '$1 == "FACTORY_LIFECYCLE_ACTIVATION_WATERMARK" { value = $2 } END { print value }' "$env_file")"
[[ -n "$watermark" ]] || watermark="$(awk -F= '$1 == "FACTORY_LIFECYCLE_DRY_RUN_WATERMARK" { value = $2 } END { print value }' "$env_file")"
[[ -n "$watermark" ]] || watermark="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
awk -F= '$1 != "HATCHET_HERMES_BRIDGE_TOKEN" && $1 != "FACTORY_LIFECYCLE_DRY_RUN_WATERMARK" && $1 != "FACTORY_LIFECYCLE_ACTIVATION_WATERMARK" { print }' "$env_file" >"$tmp"
printf 'HATCHET_HERMES_BRIDGE_TOKEN=%s\n' "$bridge_token" >>"$tmp"
printf 'FACTORY_LIFECYCLE_ACTIVATION_WATERMARK=%s\n' "$watermark" >>"$tmp"
install -m 0600 -o root -g root "$tmp" "$env_file"; rm -f "$tmp"
for unit in "$script_dir"/systemd/*; do
  tmp="$(mktemp)"; sed -e "s#__REPO_ROOT__#$repo_root#g" -e "s#__INSTALL_ROOT__#$install_root#g" "$unit" >"$tmp"; install -m 0644 "$tmp" "/etc/systemd/system/$(basename "$unit")"; rm -f "$tmp"
done
runuser -u driftward-factory-writer -- env PYTHONPATH="$install_root" FACTORY_LIFECYCLE_DB="$state_dir/outbox.db" python3 - <<'PY'
from factory_lifecycle_writer.outbox import Outbox
import os
outbox = Outbox(os.environ['FACTORY_LIFECYCLE_DB'])
outbox.close()
PY
chown -R driftward-factory-writer:driftward-factory-writer "$state_dir"
systemctl daemon-reload
systemctl enable --now driftward-factory-linear-token.timer
systemctl start driftward-factory-linear-token.service
systemctl enable --now driftward-factory-lifecycle-writer.service
systemctl restart driftward-factory-lifecycle-writer.service
echo "Factory lifecycle writer installed"
