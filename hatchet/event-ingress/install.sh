#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
unit_src="$repo_root/hatchet/systemd/driftward-hatchet-event-ingress.service"
unit_dst="/etc/systemd/system/driftward-hatchet-event-ingress.service"
env_file="/etc/driftward/hatchet-event-ingress.env"
state_dir="/var/lib/driftward-hatchet-event-ingress"
db_path="$state_dir/ingress.db"
workers_root="$repo_root/hatchet/workers"

if [[ "$(id -u)" != "0" ]]; then
  echo "Hatchet event ingress install requires root" >&2
  exit 1
fi

if [[ ! -f "$unit_src" ]]; then
  echo "missing systemd source: $unit_src" >&2
  exit 1
fi

install -d -m 0755 -o root -g root /etc/driftward
install -d -m 0755 -o root -g root "$state_dir"

if [[ ! -e "$env_file" ]]; then
  install -m 0600 -o root -g root /dev/null "$env_file"
else
  chown root:root "$env_file"
  chmod 0600 "$env_file"
fi

npm run build --prefix "$workers_root"
PYTHONPATH="$repo_root/hatchet/event-ingress" \
  python3 -m hatchet_event_ingress.cli --db "$db_path" init >/dev/null

tmp="$(mktemp)"
sed "s#__REPO_ROOT__#$repo_root#g" "$unit_src" >"$tmp"
install -D -m 0644 "$tmp" "$unit_dst"
rm -f "$tmp"

systemctl daemon-reload
systemctl enable --now driftward-hatchet-event-ingress.service
systemctl restart driftward-hatchet-event-ingress.service
