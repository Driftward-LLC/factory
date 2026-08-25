#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
smoke_source="$script_dir/driftward-hatchet-smoke"
smoke_target="${DRIFTWARD_HATCHET_SMOKE_TARGET:-/usr/local/bin/driftward-hatchet-smoke}"
factory_cli_source="$repo_root/factory/factory"
factory_cli_target="${DRIFTWARD_FACTORY_CLI_TARGET:-/usr/local/bin/factory}"
factory_share_dir="${DRIFTWARD_FACTORY_SHARE_DIR:-/usr/local/share/driftward-factory}"
factory_hatchet_doctor_source="$repo_root/tools/factory-hatchet-doctor.py"
factory_hatchet_doctor_target="$factory_share_dir/factory-hatchet-doctor.py"
factory_hatchet_desired_state_source="$script_dir/desired-state/factory-hatchet.json"
factory_hatchet_desired_state_target="$factory_share_dir/desired-state/factory-hatchet.json"

if [[ "$(id -u)" != "0" ]]; then
  echo "Hatchet install requires root" >&2
  exit 1
fi

for source in "$smoke_source" "$factory_cli_source" "$factory_hatchet_doctor_source" "$factory_hatchet_desired_state_source"; do
  if [[ ! -f "$source" ]]; then
    echo "missing Factory install source: $source" >&2
    exit 1
  fi
done

install -D -m 0755 "$factory_cli_source" "$factory_cli_target"
cmp -s "$factory_cli_source" "$factory_cli_target" || {
  echo "installed Factory CLI does not match source" >&2
  exit 1
}

install -d -m 0755 -o root -g root "$factory_share_dir/desired-state"
install -m 0755 "$factory_hatchet_doctor_source" "$factory_hatchet_doctor_target"
install -m 0644 "$factory_hatchet_desired_state_source" "$factory_hatchet_desired_state_target"
cmp -s "$factory_hatchet_doctor_source" "$factory_hatchet_doctor_target" || {
  echo "installed Factory Hatchet doctor does not match source" >&2
  exit 1
}
cmp -s "$factory_hatchet_desired_state_source" "$factory_hatchet_desired_state_target" || {
  echo "installed Factory Hatchet desired state does not match source" >&2
  exit 1
}

# Publish the exact checked-in smoke helper before managed services are refreshed.
install -D -m 0755 "$smoke_source" "$smoke_target"
cmp -s "$smoke_source" "$smoke_target" || {
  echo "installed Hatchet smoke helper does not match source" >&2
  exit 1
}

bash "$script_dir/bridge/install.sh"
bash "$script_dir/event-ingress/install.sh"
bash "$script_dir/workers/install.sh"
bash "$script_dir/factory-lifecycle-writer/install.sh"

echo "Factory CLI installed: $factory_cli_target"
echo "Factory desired-state assets installed: $factory_share_dir"
echo "Hatchet runtime and smoke helper installed: $smoke_target"
