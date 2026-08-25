#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
unit_src="$repo_root/hatchet/systemd/driftward-hatchet-linear-webhook-proxy.service"
unit_dst="/etc/systemd/system/driftward-hatchet-linear-webhook-proxy.service"
env_file="/etc/driftward/hatchet-event-ingress.env"
public_port="${HATCHET_PUBLIC_LINEAR_WEBHOOK_FUNNEL_PORT:-443}"
proxy_target="${HATCHET_PUBLIC_LINEAR_WEBHOOK_PROXY_TARGET:-http://127.0.0.1:19879}"

if [[ "$(id -u)" != "0" ]]; then
  echo "Hatchet public webhook install requires root" >&2
  exit 1
fi

if [[ ! -f "$unit_src" ]]; then
  echo "missing systemd source: $unit_src" >&2
  exit 1
fi

install -d -m 0755 -o root -g root /etc/driftward
if [[ ! -e "$env_file" ]]; then
  install -m 0600 -o root -g root /dev/null "$env_file"
else
  chown root:root "$env_file"
  chmod 0600 "$env_file"
fi

tmp="$(mktemp)"
sed "s#__REPO_ROOT__#$repo_root#g" "$unit_src" >"$tmp"
install -D -m 0644 "$tmp" "$unit_dst"
rm -f "$tmp"

systemctl daemon-reload
systemctl enable --now driftward-hatchet-linear-webhook-proxy.service
systemctl restart driftward-hatchet-linear-webhook-proxy.service

tailscale funnel --bg --yes --https="$public_port" "$proxy_target" >/dev/null

printf 'Linear webhook public URL: https://%s/events/linear/issue\n' "$(tailscale status --json | jq -r '.Self.DNSName' | sed 's/[.]$//')"
