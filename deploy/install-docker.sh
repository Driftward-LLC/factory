#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
runtime_root="${DRIFTWARD_FACTORY_RUNTIME_ROOT:-/docker/driftward/factory}"
service_unit="${DRIFTWARD_FACTORY_SYSTEMD_UNIT:-/etc/systemd/system/driftward-factory.service}"
start=0

usage() {
  cat <<'USAGE'
usage: deploy/install-docker.sh [--start]

Installs the Docker Compose deployment wrapper for Driftward Factory.
It does not create or print secrets. Required env files stay under /etc/driftward.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --start) start=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 64 ;;
  esac
done

if [[ "$(id -u)" != "0" ]]; then
  echo "Factory Docker install requires root" >&2
  exit 1
fi

install -d -m 0750 -o root -g root "$runtime_root"
install -d -m 0750 -o root -g root "$runtime_root/data/bridge" "$runtime_root/data/ingress" "$runtime_root/data/lifecycle"
install -d -m 0755 -o root -g root /run/driftward-factory-operator

install -m 0644 "$script_dir/docker-compose.yml" "$runtime_root/docker-compose.yml"
install -D -m 0755 "$script_dir/factory-docker-smoke" /usr/local/bin/factory-docker-smoke

tmp_unit="$(mktemp)"
sed \
  -e "s#__RUNTIME_ROOT__#$runtime_root#g" \
  -e "s#__SOURCE_ROOT__#$repo_root#g" \
  "$script_dir/systemd/driftward-factory.service" >"$tmp_unit"
install -D -m 0644 "$tmp_unit" "$service_unit"
rm -f "$tmp_unit"

FACTORY_SOURCE_ROOT="$repo_root" docker compose --project-directory "$runtime_root" config >/dev/null
systemd-analyze verify "$service_unit"
systemctl daemon-reload

echo "Factory Docker runtime installed: $runtime_root"
echo "Factory systemd unit installed: $service_unit"

if (( start )); then
  FACTORY_SOURCE_ROOT="$repo_root" docker compose --project-directory "$runtime_root" build
  systemctl enable --now "$(basename "$service_unit")"
  factory-docker-smoke "$runtime_root"
else
  echo "Start with: systemctl enable --now $(basename "$service_unit")"
  echo "Smoke with: factory-docker-smoke $runtime_root"
fi
