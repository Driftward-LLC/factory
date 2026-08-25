#!/usr/bin/env bash
# Shared production-only maintenance barrier for the bridge installer/recovery.
# It deliberately has no environment or CLI path overrides.

if [[ "${BRIDGE_TEST_MODE:-}" == 1 ]]; then
  python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/test_capability.py" assert
  maintenance_root="$BRIDGE_TEST_ROOT/run/driftward-hatchet-bridge"
  systemd_dropin_root="$BRIDGE_TEST_ROOT/run/systemd/system"
  maintenance_owner="$BRIDGE_TEST_UID:$BRIDGE_TEST_GID"
else
  maintenance_root=/run/driftward-hatchet-bridge
  systemd_dropin_root=/run/systemd/system
  maintenance_owner=0:0
fi
maintenance_lock="$maintenance_root/maintenance.lock"
maintenance_active="$maintenance_root/maintenance.active"
maintenance_dropin_name=90-driftward-bridge-maintenance.conf
barrier_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

barrier_dropin_path() { printf '%s/%s.d/%s\n' "$systemd_dropin_root" "$1" "$maintenance_dropin_name"; }

ensure_maintenance_lock() {
  if [[ ! -e "$maintenance_lock" ]]; then
    install -m 0600 -o root -g root /dev/null "$maintenance_lock" || return 1
  fi
  [[ -f "$maintenance_lock" && ! -L "$maintenance_lock" ]] || return 1
  [[ "$(stat -c '%u:%g:%a:%h' "$maintenance_lock")" == "$maintenance_owner:600:1" ]] || return 1
}

barrier_make_active() {
  local tmp
  install -d -m 0750 -o root -g root "$maintenance_root" || return 1
  ensure_maintenance_lock || return 1
  tmp="$(mktemp "$maintenance_root/.active.XXXXXX")" || return 1
  install -m 0600 -o root -g root /dev/null "$tmp" || { rm -f -- "$tmp"; return 1; }
  mv -f -- "$tmp" "$maintenance_active" || return 1
  sync -f "$maintenance_active" && sync -d "$maintenance_root"
}

acquire_maintenance_lock() {
  install -d -m 0750 -o root -g root "$maintenance_root" || return 1
  ensure_maintenance_lock || return 1
  exec 3>"$maintenance_lock"
  flock -n 3 || { echo "bridge maintenance lock is held" >&2; return 1; }
  barrier_make_active || { flock -u 3; exec 3>&-; return 1; }
  lock_acquired=1
}

adopt_inherited_maintenance_lock() {
  python3 - "$maintenance_lock" <<'PY'
import fcntl, os, sys
expected=os.stat(sys.argv[1]); actual=os.fstat(3)
if (expected.st_dev, expected.st_ino)!=(actual.st_dev, actual.st_ino): raise SystemExit('inherited maintenance capability mismatch')
fcntl.flock(3, fcntl.LOCK_EX | fcntl.LOCK_NB)
PY
  barrier_make_active || return 1
  lock_acquired=1
}

verify_inherited_maintenance_lock_readonly() {
  python3 - "$maintenance_lock" <<'PY'
import fcntl, os, stat, sys
expected=os.stat(sys.argv[1], follow_symlinks=False); actual=os.fstat(3)
if not stat.S_ISREG(actual.st_mode) or (expected.st_dev, expected.st_ino)!=(actual.st_dev, actual.st_ino): raise SystemExit('inherited maintenance capability mismatch')
fcntl.flock(3, fcntl.LOCK_EX | fcntl.LOCK_NB)
PY
}

release_maintenance_lock() {
  (( lock_acquired )) || return 0
  flock -u 3 || return 1
  exec 3>&-
  lock_acquired=0
}

write_maintenance_dropin() {
  local unit="$1" dir path tmp
  dir="$systemd_dropin_root/$unit.d"; path="$(barrier_dropin_path "$unit")"
  install -d -m 0755 -o root -g root "$dir" || return 1
  tmp="$(mktemp "$dir/.${maintenance_dropin_name}.XXXXXX")" || return 1
  printf '[Unit]\n# Installed only while the fixed bridge maintenance barrier is active.\nConditionPathExists=!%s\n' "$maintenance_active" >"$tmp" || { rm -f -- "$tmp"; return 1; }
  chown root:root "$tmp" && chmod 0644 "$tmp" && mv -f -- "$tmp" "$path"
}

install_maintenance_dropins() {
  local unit
  for unit in "${writer_units[@]}"; do write_maintenance_dropin "$unit" || return 1; done
  systemctl daemon-reload || return 1
  # Validate the real systemd condition parser. The active sentinel must make
  # the negated condition false before any writer is stopped.
  if systemd-analyze condition "ConditionPathExists=!$maintenance_active" >/dev/null 2>&1; then
    echo "maintenance condition did not block activation" >&2; return 1
  fi
}

unit_load_state() {
  local state
  state="$(PYTHONPATH="$barrier_script_dir${PYTHONPATH:+:$PYTHONPATH}" python3 "$barrier_script_dir/barrier_unit_state.py" "$1")" || return 1
  case "$state" in
    absent) printf 'not-found\n';;
    loaded) printf 'loaded\n';;
    *) return 1;;
  esac
}

unit_is_absent() {
  local state
  state="$(unit_load_state "$1")" || return 2
  [[ "$state" == not-found ]] && return 0
  [[ "$state" == loaded ]] && return 1
  return 2
}

stop_loaded_writers() {
  local unit state failures=0
  for unit in "${writer_units[@]}"; do
    if unit_is_absent "$unit"; then continue; else state=$?; fi
    (( state == 1 )) || { echo "cannot prove writer load state: $unit" >&2; failures=1; continue; }
    systemctl stop "$unit" || { echo "failed to stop loaded writer: $unit" >&2; failures=1; }
  done
  return "$failures"
}

verify_maintenance_barrier() {
  local unit path active dropins state failures=0
  [[ -f "$maintenance_active" ]] || { echo "maintenance activation sentinel missing" >&2; return 1; }
  for unit in "${writer_units[@]}"; do
    path="$(barrier_dropin_path "$unit")"
    [[ -f "$path" && ! -L "$path" ]] || { echo "maintenance drop-in missing: $unit" >&2; failures=1; continue; }
    [[ "$(stat -c '%u:%g:%a' "$path")" == "$maintenance_owner:644" ]] || { echo "maintenance drop-in ownership/mode invalid: $unit" >&2; failures=1; }
    if unit_is_absent "$unit"; then continue; else state=$?; fi
    (( state == 1 )) || { echo "cannot prove writer load state: $unit" >&2; failures=1; continue; }
    active="$(systemctl show "$unit" -p ActiveState --value 2>/dev/null)" || { failures=1; continue; }
    [[ "$active" == inactive ]] || { echo "writer remains active: $unit" >&2; failures=1; }
    dropins="$(systemctl show "$unit" -p DropInPaths --value 2>/dev/null)" || { failures=1; continue; }
    [[ "$dropins" == *"$path"* ]] || { echo "maintenance drop-in not loaded: $unit" >&2; failures=1; }
  done
  return "$failures"
}

verify_existing_maintenance_barrier_readonly() {
  local unit path active dropins state failures=0
  verify_inherited_maintenance_lock_readonly || return 1
  [[ -f "$maintenance_active" && ! -L "$maintenance_active" ]] || { echo "maintenance activation sentinel missing" >&2; return 1; }
  [[ "$(stat -c '%u:%g:%a' "$maintenance_active")" == "$maintenance_owner:600" ]] || { echo "maintenance activation sentinel metadata invalid" >&2; return 1; }
  for unit in "${writer_units[@]}"; do
    path="$(barrier_dropin_path "$unit")"
    [[ -f "$path" && ! -L "$path" && "$(stat -c '%u:%g:%a' "$path")" == "$maintenance_owner:644" ]] || { echo "maintenance drop-in missing or unsafe: $unit" >&2; failures=1; continue; }
    if unit_is_absent "$unit"; then continue; else state=$?; fi
    (( state == 1 )) || { echo "cannot prove writer load state: $unit" >&2; failures=1; continue; }
    active="$(systemctl show "$unit" -p ActiveState --value 2>/dev/null || printf unknown)"
    [[ "$active" == inactive || "$active" == failed || "$active" == inactive\ * ]] || { echo "writer remains active: $unit" >&2; failures=1; }
    dropins="$(systemctl show "$unit" -p DropInPaths --value 2>/dev/null || printf '')"
    [[ "$dropins" == *"$path"* ]] || { echo "maintenance drop-in not loaded: $unit" >&2; failures=1; }
  done
  for unit in driftward-hatchet-bridge-reconciler.service driftward-hatchet-bridge-reconciler.timer; do
    [[ "$(systemctl show "$unit" -p ActiveState --value 2>/dev/null || printf unknown)" == inactive ]] || { echo "reconciler remains active: $unit" >&2; failures=1; }
  done
  command -v lsof >/dev/null || { echo "lsof is required for safe-down supersession" >&2; return 1; }
  # lsof exits one only when none of the exact ledger files is open.  Any
  # other result is either a holder or an inspection error, both fail closed.
  local lsof_status
  if lsof "$state_dir/bridge.db" "$state_dir/bridge.db-wal" "$state_dir/bridge.db-shm" >/dev/null 2>&1; then
    lsof_status=0
  else
    lsof_status=$?
  fi
  [[ "$lsof_status" == 1 ]] || { [[ "$lsof_status" == 0 ]] && echo "bridge DB has an open holder" >&2 || echo "cannot prove bridge DB has no holder" >&2; return 1; }
  return "$failures"
}

remove_maintenance_dropins() {
  local unit path dir failures=0
  for unit in "${writer_units[@]}"; do
    path="$(barrier_dropin_path "$unit")"; dir="${path%/*}"
    [[ -e "$path" || -L "$path" ]] && rm -f -- "$path" || true
    rmdir --ignore-fail-on-non-empty "$dir" 2>/dev/null || true
  done
  systemctl daemon-reload || failures=1
  return "$failures"
}

clear_maintenance_barrier() {
  remove_maintenance_dropins || return 1
  rm -f -- "$maintenance_active" || return 1
  sync -d "$maintenance_root" || return 1
  release_maintenance_lock
}
