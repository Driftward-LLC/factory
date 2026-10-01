#!/usr/bin/env bash
set -euo pipefail

# Recover only a previously captured, failed installer window. This command is
# intentionally separate from install.sh: it never calls init, predeploy, or
# release publication and refuses incomplete evidence.
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
state_dir=/var/lib/driftward-hatchet-bridge
db="$state_dir/bridge.db"
marker="$state_dir/recovery-blocked"
state="$state_dir/recovery-state.tsv"
source "$script_dir/installer-barrier.sh"
mapfile -t writer_units < <(PYTHONPATH="$script_dir" python3 "$script_dir/writer-inventory.py" --kind all)
lock_acquired=0

[[ "$(id -u)" == 0 ]] || { echo "bridge blocked recovery requires root" >&2; exit 1; }
for path in "$marker" "$state"; do
  [[ -f "$path" && ! -L "$path" ]] || { echo "recovery evidence missing: $path" >&2; exit 1; }
  [[ "$(stat -c '%u:%g:%a' "$path")" == 0:0:600 ]] || { echo "recovery evidence ownership/mode invalid: $path" >&2; exit 1; }
done
grep -Eq '^rollback recovery blocked: [[:alnum:][:space:]_-]+$' "$marker" || { echo "recovery marker format invalid" >&2; exit 1; }

ledger_snapshot() {
  python3 - "$db" <<'PY'
import hashlib,json,sqlite3,sys
c=sqlite3.connect(f'file:{sys.argv[1]}?mode=ro',uri=True)
tables={row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
for table in ('bridge_jobs','factory_attempts','factory_execution_leases','factory_attempt_outbox','factory_inspection_outbox','factory_attempt_events','factory_issue_controls','factory_issue_control_commands'):
 if table not in tables and table in ('factory_issue_controls','factory_issue_control_commands'):
  print(f'{table}=missing'); continue
 rows=c.execute('SELECT * FROM '+table).fetchall()
 hashes=sorted(hashlib.sha256(json.dumps(tuple(row),default=str,separators=(',',':')).encode()).hexdigest() for row in rows)
 print(f'{table}={len(rows)}:{hashlib.sha256("".join(hashes).encode()).hexdigest()}')
c.close()
PY
}
expected_schema="$(awk -F= '$1=="schema" {print $2; exit}' "$state")"
[[ "$expected_schema" =~ ^[0-9]+$ ]] || { echo "recovery schema evidence invalid" >&2; exit 1; }
actual_schema="$(python3 - "$db" <<'PY'
import sqlite3,sys
c=sqlite3.connect(f'file:{sys.argv[1]}?mode=ro',uri=True); print(c.execute('pragma user_version').fetchone()[0]); c.close()
PY
)"
[[ "$actual_schema" == "$expected_schema" ]] || { echo "recovery refuses changed bridge schema" >&2; exit 1; }
current_ledger="$(ledger_snapshot)" || { echo "recovery could not read immutable ledger" >&2; exit 1; }
for line in $current_ledger; do
  # Older recovery snapshots predate issue ownership. Only absent-in-both is
  # compatible; an existing table must have the captured, unchanged digest.
  if [[ "$line" == factory_issue_controls=missing || "$line" == factory_issue_control_commands=missing ]] && ! grep -q "^${line%%=*}=" "$state"; then continue; fi
  grep -Fqx "$line" "$state" || { echo "recovery refuses changed immutable ledger" >&2; exit 1; }
done

acquire_maintenance_lock
recovered=0
on_exit() {
  local status=$?
  if (( ! recovered )); then
    (( lock_acquired )) || acquire_maintenance_lock || true
    install_maintenance_dropins || true
    stop_loaded_writers || true
    verify_maintenance_barrier || true
  fi
  (( recovered || ! lock_acquired )) || release_maintenance_lock || true
  exit "$status"
}
trap on_exit EXIT
install_maintenance_dropins
stop_loaded_writers
verify_maintenance_barrier

# The failed pre-ENG-67 installer left ineffective /run masks. Remove only the
# exact owned symlinks after the condition barrier has been loaded.
for unit in "${writer_units[@]}"; do
  path="/run/systemd/system/$unit"
  [[ -L "$path" && "$(readlink "$path")" == /dev/null ]] && rm -f -- "$path"
done
systemctl daemon-reload
verify_maintenance_barrier

# Recovery is deliberately proof-gated. The installer state records exact
# prior states; absence is a valid prior state. Reconciler activation remains
# disabled until the separately approved post-smoke step.
while IFS=$'\t' read -r unit presence load unit_file active incident; do
  [[ "$unit" == *=* || -z "$unit" ]] && continue
  if [[ "$incident" == incident-disabled ]]; then
    unit_present "$unit" && systemctl stop "$unit"
    [[ "$unit" == *.timer ]] && systemctl disable "$unit"
    continue
  fi
  [[ "$presence" == present ]] || continue
  [[ "$unit_file" == enabled || "$unit_file" == enabled-runtime ]] && systemctl enable "$unit"
  [[ "$unit_file" == disabled ]] && systemctl disable "$unit"
done <"$state"

# Remove conditions and release the exclusive capability only once every
# mutable target and the immutable ledger proved unchanged. Starting services
# follows the captured active state; a failure leaves marker/drop-ins intact.
clear_maintenance_barrier
while IFS=$'\t' read -r unit presence load unit_file active incident; do
  [[ "$incident" == incident-disabled || "$presence" != present || "$active" != active ]] && continue
  systemctl start "$unit"
done <"$state"

systemctl is-active --quiet driftward-hatchet-bridge.service || { echo "old bridge did not recover healthy" >&2; exit 1; }
token="$(awk -F= '$1 == "HATCHET_HERMES_BRIDGE_TOKEN" {print $2; exit}' /etc/driftward/hatchet-bridge.env)"
[[ -n "$token" ]] || { echo "old bridge recovery token missing" >&2; exit 1; }
curl --silent --fail --max-time 2 -H "Authorization: Bearer $token" http://127.0.0.1:19877/health >/dev/null || { echo "old bridge health failed" >&2; exit 1; }
rm -f -- "$marker" "$state"
sync -d "$state_dir"
recovered=1
echo "bridge blocked recovery complete; no migration was performed"
