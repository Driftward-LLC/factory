#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
runtime_root="${DRIFTWARD_FACTORY_RUNTIME_ROOT:-/docker/driftward/factory}"
service_unit="${DRIFTWARD_FACTORY_SYSTEMD_UNIT:-/etc/systemd/system/driftward-factory.service}"
hatchet_env_file="${DRIFTWARD_HATCHET_ENV_FILE:-/etc/driftward/hatchet.env}"
legacy_hatchet_unit="${DRIFTWARD_LEGACY_HATCHET_UNIT:-driftward-hatchet.service}"
factory_cli_source="$repo_root/factory/factory"
factory_cli_target="${DRIFTWARD_FACTORY_CLI_TARGET:-/usr/local/bin/factory}"
factory_share_dir="${DRIFTWARD_FACTORY_SHARE_DIR:-/usr/local/share/driftward-factory}"
factory_hatchet_doctor_source="$repo_root/tools/factory-hatchet-doctor.py"
factory_hatchet_doctor_target="$factory_share_dir/factory-hatchet-doctor.py"
factory_hatchet_desired_state_source="$repo_root/hatchet/desired-state/factory-hatchet.json"
factory_hatchet_desired_state_target="$factory_share_dir/desired-state/factory-hatchet.json"
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

for source in "$factory_cli_source" "$factory_hatchet_doctor_source" "$factory_hatchet_desired_state_source"; do
  if [[ ! -f "$source" ]]; then
    echo "missing Factory install source: $source" >&2
    exit 1
  fi
done

install -d -m 0750 -o root -g root "$runtime_root"
install -d -m 0750 -o root -g root \
  "$runtime_root/data/bridge" \
  "$runtime_root/data/hatchet/config" \
  "$runtime_root/data/hatchet/postgres" \
  "$runtime_root/data/ingress" \
  "$runtime_root/data/lifecycle" \
  "$runtime_root/data/linear-token" \
  "$runtime_root/keys/hatchet"
install -d -m 0755 -o root -g root /etc/driftward
if [[ ! -e /etc/driftward/factory-lifecycle-writer.env ]]; then
  install -m 0600 -o root -g root /dev/null /etc/driftward/factory-lifecycle-writer.env
else
  chown root:root /etc/driftward/factory-lifecycle-writer.env
  chmod 0600 /etc/driftward/factory-lifecycle-writer.env
fi
install -d -m 0755 -o root -g root /run/driftward-factory-operator

install -m 0644 "$script_dir/docker-compose.yml" "$runtime_root/docker-compose.yml"
install -D -m 0755 "$script_dir/factory-docker-smoke" /usr/local/bin/factory-docker-smoke
install -D -m 0755 "$factory_cli_source" "$factory_cli_target"
install -d -m 0755 -o root -g root "$factory_share_dir/desired-state"
install -m 0755 "$factory_hatchet_doctor_source" "$factory_hatchet_doctor_target"
install -m 0644 "$factory_hatchet_desired_state_source" "$factory_hatchet_desired_state_target"

tmp_unit="$(mktemp)"
sed \
  -e "s#__RUNTIME_ROOT__#$runtime_root#g" \
  -e "s#__SOURCE_ROOT__#$repo_root#g" \
  "$script_dir/systemd/driftward-factory.service" >"$tmp_unit"
install -D -m 0644 "$tmp_unit" "$service_unit"
rm -f "$tmp_unit"

compose_args=(--project-directory "$runtime_root")
if [[ -r "$hatchet_env_file" ]]; then
  compose_args=(--env-file "$hatchet_env_file" "${compose_args[@]}")
fi
factory_image_tag="${FACTORY_IMAGE_TAG:-local}"
factory_image="${FACTORY_IMAGE:-driftward/factory:$factory_image_tag}"

FACTORY_SOURCE_ROOT="$repo_root" docker compose "${compose_args[@]}" config >/dev/null
systemd-analyze verify "$service_unit"
systemctl daemon-reload

echo "Factory Docker runtime installed: $runtime_root"
echo "Factory systemd unit installed: $service_unit"
echo "Factory CLI installed: $factory_cli_target"

if (( start )); then
  if systemctl is-active --quiet "$legacy_hatchet_unit"; then
    echo "$legacy_hatchet_unit is still active; stop it after migrating Hatchet data into $runtime_root/data/hatchet" >&2
    exit 1
  fi
  for key_file in master.key private_ec256.key public_ec256.key; do
    if [[ ! -r "$runtime_root/keys/hatchet/$key_file" ]]; then
      echo "Hatchet key file is missing: $runtime_root/keys/hatchet/$key_file" >&2
      echo "Migrate existing Hatchet keys before starting the standalone Factory runtime." >&2
      exit 1
    fi
  done
  if [[ ! -f "$runtime_root/data/hatchet/postgres/PG_VERSION" ]]; then
    echo "Hatchet Postgres data is missing under $runtime_root/data/hatchet/postgres" >&2
    echo "Migrate historical Hatchet data before starting the standalone Factory runtime." >&2
    exit 1
  fi
  if [[ ! -r /etc/driftward/factory-linear-token.env ]]; then
    echo "Factory Linear token credentials are missing: /etc/driftward/factory-linear-token.env" >&2
    echo "Create this protected env file before starting the standalone Factory runtime." >&2
    exit 1
  fi

  docker build -f "$repo_root/deploy/Dockerfile" -t "$factory_image" "$repo_root"
  systemctl enable --now "$(basename "$service_unit")"
  factory-docker-smoke "$runtime_root"
else
  echo "Start with: systemctl enable --now $(basename "$service_unit")"
  echo "Smoke with: factory-docker-smoke $runtime_root"
fi
