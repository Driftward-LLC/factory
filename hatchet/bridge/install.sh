#!/usr/bin/env bash
set -euo pipefail

# This is an approved-rollout command, never a development helper.  It keeps a
# bounded quiesced migration window and deliberately leaves Factory mode and
# the incident-disabled reconciler untouched.
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 "$script_dir/test_capability.py" guard
if test_shell="$(python3 "$script_dir/test_capability.py" maybe-shell)"; then
  eval "$test_shell"
  PATH="$BRIDGE_TEST_FAKE_BIN:$PATH"
fi
# Validate test-only failpoint input before taking the production capability so
# an unknown name has no filesystem or lock side effect.
# shellcheck source=installer-failpoints.sh
source "$script_dir/installer-failpoints.sh"
validate_install_failpoint
if ! python3 "$script_dir/maintenance_lock_exec.py" check; then
  exec python3 "$script_dir/maintenance_lock_exec.py" exec bash "$script_dir/install.sh" "$@"
fi
source "$script_dir/installer-barrier.sh"
source "$script_dir/publication-targets.sh"
recover_safe_down=0
if [[ "${1:-}" == --recover-safe-down && $# -eq 1 ]]; then recover_safe_down=1; elif [[ $# -ne 0 ]]; then echo "usage: install.sh [--recover-safe-down]" >&2; exit 64; fi
# Reject an unsupported injected failpoint before staging state or touching a
# rollout target. The helper explicitly returns zero for normal operation.
repo_root="$(cd "$script_dir/../.." && pwd)"
state_dir="/var/lib/driftward-hatchet-bridge"; db="$state_dir/bridge.db"
routing_registry="/etc/driftward/hatchet-routing-registry.json"
bridge_env="/etc/driftward/hatchet-bridge.env"
# Production targets are fixed.  Test indirection belongs to the dedicated
# harness, never to the production installer invocation.
release_root="/docker/driftward/hatchet/releases/bridge"
if [[ "${BRIDGE_TEST_MODE:-}" == 1 ]]; then
  repo_root="$BRIDGE_TEST_REPOSITORY"
  state_dir="$BRIDGE_TEST_ROOT/var/lib/driftward-hatchet-bridge"; db="$state_dir/bridge.db"
  release_root="$BRIDGE_TEST_ROOT/docker/driftward/hatchet/releases/bridge"
  routing_registry="$BRIDGE_TEST_ROOT/etc/driftward/hatchet-routing-registry.json"
  bridge_env="$BRIDGE_TEST_ROOT/etc/driftward/hatchet-bridge.env"
fi
if [[ "${BRIDGE_TEST_MODE:-}" == 1 ]]; then release_version="$BRIDGE_TEST_RELEASE_VERSION"; else release_version="$(git -C "$repo_root" rev-parse HEAD)"; fi
stage="$(mktemp -d /var/tmp/driftward-bridge-install.XXXXXX)"; chmod 0700 "$stage"
backup=""; new_release_dir=""; quiesced=0; lock_released=0; live_migrated=0; publication_started=0; rollback_failed=0
mapfile -t direct_writers < <(PYTHONPATH="$script_dir" python3 "$script_dir/writer-inventory.py" --kind direct)
mapfile -t http_clients < <(PYTHONPATH="$script_dir" python3 "$script_dir/writer-inventory.py" --kind http)
mapfile -t writer_units < <(PYTHONPATH="$script_dir" python3 "$script_dir/writer-inventory.py" --kind all)
lock_acquired=1
units=("${publication_units[@]}")
targets=("${publication_targets[@]}")
if [[ "${BRIDGE_TEST_MODE:-}" == 1 ]]; then release_source_tree="$release_version"; else release_source_tree="$(git -C "$repo_root" rev-parse "$release_version^{tree}")"; fi

verify_staged_release() {
  # Python imports from the staged release must never make its immutable
  # manifest stale.  Reprove the exact already-approved tree after each
  # migration boundary and immediately before publication touches a target.
  PYTHONDONTWRITEBYTECODE=1 python3 -B "$script_dir/release_hygiene.py" validate --tree "$stage/release"
  PYTHONDONTWRITEBYTECODE=1 python3 -B "$script_dir/release_hygiene.py" scan --tree "$stage/release"
  PYTHONDONTWRITEBYTECODE=1 python3 -B "$script_dir/release_hygiene.py" verify --tree "$stage/release" --commit "$release_version" --source-tree "$release_source_tree"
}

idle_counts() {
  python3 - "$db" <<'PY'
import sqlite3,sys
c=sqlite3.connect(f'file:{sys.argv[1]}?mode=ro',uri=True)
queries=("SELECT COUNT(*) FROM bridge_jobs WHERE state IN ('accepted','running','effect_committed','verification_pending')", "SELECT COUNT(*) FROM factory_attempts WHERE status IN ('reserved','accepted','running','effect_authorized','effect_committed','verification_pending','cancellation_requested','effect_outcome_unknown')", "SELECT COUNT(*) FROM factory_execution_leases WHERE released_at IS NULL", "SELECT COUNT(*) FROM factory_attempt_outbox WHERE acknowledged_at IS NULL", "SELECT COUNT(*) FROM factory_inspection_outbox WHERE acknowledged_at IS NULL")
values=[c.execute(q).fetchone()[0] for q in queries]; c.close()
if any(values): raise SystemExit('bridge migration requires zero active jobs/attempts/leases/outboxes')
PY
}
ledger_snapshot() {
  python3 - "$db" <<'PY'
import hashlib,json,sqlite3,sys
c=sqlite3.connect(f'file:{sys.argv[1]}?mode=ro',uri=True)
tables={row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
for table in ('bridge_jobs','bridge_events','factory_attempts','factory_execution_leases','factory_attempt_outbox','factory_inspection_outbox','factory_attempt_events','factory_import_conflicts','factory_import_conflict_resolutions','factory_import_conflict_attestations'):
 if table not in tables:
  print(f'{table}=missing'); continue
 rows=c.execute("SELECT * FROM "+table).fetchall()
 # The values are never emitted.  Per-row canonical hashes make ordering and
 # SQLite rowid irrelevant while detecting any immutable/effect-table change.
 row_hashes=sorted(hashlib.sha256(json.dumps(tuple(row),default=str,separators=(',',':')).encode()).hexdigest() for row in rows)
 digest=hashlib.sha256(''.join(row_hashes).encode()).hexdigest()
 print(f'{table}={len(rows)}:{digest}')
c.close()
PY
}
assert_migration_ledger() {
  local before="$1" after="$2" expected_delta="$3"
  python3 - "$before" "$after" "$expected_delta" <<'PY'
import sys
def rows(path):
 return dict(line.strip().split('=',1) for line in open(path) if '=' in line)
before,after=rows(sys.argv[1]),rows(sys.argv[2]); expected=int(sys.argv[3])
attest='factory_import_conflict_attestations'
for key,value in before.items():
 if key == attest or value == 'missing': continue
 if after.get(key) != value: raise SystemExit(f'immutable/effect ledger changed during migration: {key}')
def count(value): return 0 if value in (None,'missing') else int(value.split(':',1)[0])
if count(after.get(attest)) - count(before.get(attest)) != expected:
 raise SystemExit('unexpected semantic attestation delta during migration')
PY
}
attestation_delta_required() {
  PYTHONDONTWRITEBYTECODE=1 DRIFTWARD_BRIDGE_RELEASE_VERSION="$release_version" HATCHET_INTAKE_ROUTING_REGISTRY_PATH="$stage/etc/driftward/hatchet-routing-registry.json" PYTHONPATH="$stage/release" python3 -B - "$db" <<'PY'
import sqlite3,sys
from pathlib import Path
from hatchet_hermes_bridge.store import classify_factory_import_conflicts
c=sqlite3.connect(f'file:{sys.argv[1]}?mode=ro',uri=True); c.row_factory=sqlite3.Row
try: print(classify_factory_import_conflicts(c, routing_registry=Path(__import__('os').environ['HATCHET_INTAKE_ROUTING_REGISTRY_PATH']), legacy_only=True)['attestation_required'])
finally: c.close()
PY
}
record_services() {
  : >"$stage/services.tsv"
  for unit in "${writer_units[@]}"; do
    load="$(unit_load_state "$unit")"
    presence=absent; [[ "$load" != not-found ]] && presence=present
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$unit" "$presence" "$load" "$(systemctl show "$unit" -p UnitFileState --value 2>/dev/null || printf absent)" "$(systemctl show "$unit" -p ActiveState --value 2>/dev/null || printf absent)" "$([[ "$unit" == driftward-hatchet-bridge-reconciler.* ]] && printf incident-disabled || printf ordinary)" >>"$stage/services.tsv"
  done
}
persist_recovery_state() {
  local tmp
  install -d -m 0700 -o root -g root "$state_dir" || return 1
  tmp="$(mktemp "$state_dir/.recovery-state.XXXXXX")" || return 1
  {
    printf 'release=%s\n' "$release_version"
    printf 'schema=%s\n' "$(python3 - "$db" <<'PY'
import sqlite3,sys
c=sqlite3.connect(f'file:{sys.argv[1]}?mode=ro',uri=True); print(c.execute('pragma user_version').fetchone()[0]); c.close()
PY
)"
    ledger_snapshot
    cat "$stage/services.tsv"
  } >"$tmp" || { rm -f -- "$tmp"; return 1; }
  install -m 0600 -o root -g root "$tmp" "$state_dir/recovery-state.tsv" && rm -f -- "$tmp"
}
quiesce_writers() {
  [[ "$(factory mode)" == "dry-run" ]] || { echo "Factory must be dry-run before migration" >&2; return 1; }
  idle_counts; record_services; persist_recovery_state; fail_at writer-quiesce
  # Executor/reconciliation workers only use bridge HTTP, but stopping them
  # avoids queued mutation attempts while the API is intentionally unavailable.
  quiesced=1
  install_maintenance_dropins
  stop_loaded_writers
  verify_maintenance_barrier
  ! systemctl is-active --quiet driftward-hatchet-bridge-reconciler.service || { echo "reconciler service must remain incident-disabled" >&2; return 1; }
  ! systemctl is-active --quiet driftward-hatchet-bridge-reconciler.timer || { echo "reconciler timer must remain incident-disabled" >&2; return 1; }
  command -v lsof >/dev/null || { echo "lsof is required for migration holder checks" >&2; return 1; }
  lsof "$db" "$db-wal" "$db-shm" >/dev/null 2>&1 && { echo "bridge DB still has an open writer" >&2; return 1; } || true
  curl --silent --fail --max-time 2 http://127.0.0.1:19877/health >/dev/null && { echo "bridge API remains available during migration" >&2; return 1; } || true
  idle_counts
}
snapshot_targets() {
  python3 "$script_dir/publication_commit.py" snapshot --snapshot "$stage/rollback/publication.json" || return 1
}
restore_targets() {
  python3 "$script_dir/publication_commit.py" rollback --journal "$state_dir/publication-$release_version.journal"
}
restore_database() {
  [[ -n "$backup" && -f "$backup" ]] || return 1
  [[ "$(stat -c '%a' "$backup")" == 600 ]] || return 1
  expected_checksum="$(awk '{print $1}' "${backup}.sha256")"
  lsof "$db" "$db-wal" "$db-shm" >/dev/null 2>&1 && return 1 || true
  PYTHONPATH="$script_dir" BACKUP="$backup" DB="$db" CHECKSUM="$expected_checksum" python3 - <<'PY'
import os
from hatchet_hermes_bridge.recovery import restore_verified_backup
restore_verified_backup(os.environ['BACKUP'], os.environ['DB'], os.environ['CHECKSUM'])
PY
}
restore_services() {
  local failures=0 unit presence load unit_file active incident
  restore_op() { "$@" || { echo "restore operation failed: $*" >&2; failures=1; }; }
  restore_op systemctl daemon-reload
  while IFS=$'\t' read -r unit presence load unit_file active incident; do
    # Both reconciler activators are an explicit incident-response exception:
    # neither rollback nor success may revive them before external smoke.
    if [[ "$unit" == driftward-hatchet-bridge-reconciler.service || "$unit" == driftward-hatchet-bridge-reconciler.timer ]]; then
      restore_op systemctl stop "$unit"
      [[ "$unit" == *.timer ]] && restore_op systemctl disable "$unit"
      continue
    fi
    [[ "$presence" == absent ]] && continue
    [[ "$unit_file" == enabled || "$unit_file" == enabled-runtime ]] && restore_op systemctl enable "$unit"
    [[ "$unit_file" == disabled ]] && restore_op systemctl disable "$unit"
    [[ "$active" == active ]] && restore_op systemctl start "$unit"
  done <"$stage/services.tsv"
  # The incident response state is authoritative for this first rollout.
  restore_op systemctl disable --now driftward-hatchet-bridge-reconciler.timer
  return "$failures"
}
hard_block_writers() {
  local failures=0 failed_stages="$1"
  (( lock_acquired )) || acquire_maintenance_lock || failures=1
  install_maintenance_dropins || failures=1
  stop_loaded_writers || failures=1
  verify_maintenance_barrier || failures=1
  # Never redirect into the existing marker: a terminal rollback reason must
  # retain both identities and an fsynced prepared/committed lineage record.
  # The lineage writer is internal-only: barrier_evidence_exec retains the
  # sentinel/drop-in descriptors and reproves quiescence/no DB holders across
  # this child boundary before the marker can move.
  python3 "$script_dir/barrier_evidence_exec.py" exec env PYTHONPATH="$script_dir" \
    python3 "$script_dir/safe_down_recovery.py" update-marker-lineage --reason "$failed_stages" || failures=1
  return "$failures"
}
rollback() {
  local failures=0 failed_stages=()
  (( quiesced || publication_started || live_migrated )) || return 0
  # The pre-capture incident has no safe old runtime to restore.  Its only
  # rollback target is the verified safe-down baseline: restore data/targets
  # if they changed, then hard-block every writer.  Never start checkout code.
  if (( recover_safe_down )); then
    if (( live_migrated )); then restore_database || { failures=1; failed_stages+=(restore-database); }; fi
    if (( publication_started )); then restore_targets || { failures=1; failed_stages+=(restore-targets); }; fi
    hard_block_writers "safe-down ${failed_stages[*]:-fresh-install-failed}" || failures=1
    rollback_failed=1
    return 1
  fi
  if (( lock_released )); then
    if acquire_maintenance_lock; then lock_released=0; else failures=1; failed_stages+=(reacquire-lock); fi
  fi
  if stop_loaded_writers; then :; else failures=1; failed_stages+=(stop-writers); fi
  if (( live_migrated )); then restore_database || { failures=1; failed_stages+=(restore-database); }; fi
  if (( publication_started )); then restore_targets || { failures=1; failed_stages+=(restore-targets); }; fi
  if [[ -n "$new_release_dir" && -d "$new_release_dir" ]]; then
    printf '%s\n' "$new_release_dir" >"${backup}.inactive-release" || { failures=1; failed_stages+=(record-orphan); }
    chmod 0600 "${backup}.inactive-release" || { failures=1; failed_stages+=(record-orphan); }
  fi
  clear_maintenance_barrier || { failures=1; failed_stages+=(clear-maintenance-barrier); }
  restore_services || { failures=1; failed_stages+=(restore-services); }
  if (( live_migrated )) && ! systemctl is-active --quiet driftward-hatchet-bridge.service; then failures=1; failed_stages+=(bridge-postcondition); fi
  if (( failures )); then
    hard_block_writers "${failed_stages[*]}" || failed_stages+=(hard-block)
    rollback_failed=1
    printf 'rollback failures: %s\n' "${failed_stages[*]}" >&2
    return 1
  fi
  return 0
}
on_exit() {
  status=$?; if (( status )); then rollback || { echo "ROLLBACK BLOCKED: writers remain stopped; operator action required" >&2; status=1; }; fi
  (( rollback_failed || lock_released )) || release_maintenance_lock 2>/dev/null || true
  rm -rf "$stage"; exit "$status"
}
on_signal() {
  local signal="$1" status=143
  [[ "$signal" == INT ]] && status=130
  # Do not let a second delivery interrupt rollback.  EXIT owns the recovery
  # decision and deliberately leaves the safe-down marker/barrier in place.
  trap - TERM INT
  exit "$status"
}
trap on_exit EXIT
trap 'on_signal TERM' TERM
trap 'on_signal INT' INT
[[ "$(id -u)" == 0 || "${BRIDGE_TEST_MODE:-}" == 1 ]] || { echo "Hatchet bridge install requires root" >&2; exit 1; }

# Safe-down adoption is a capability check, not a marker-path check.  It must
# happen before even private staging so a direct invocation cannot perform any
# installer work without the wrapper's inherited FD3/FD4-plus-evidence proof.
adopt_inherited_maintenance_lock
if (( recover_safe_down )) && [[ -f "$state_dir/publication-$release_version.journal" ]] && [[ "$(PYTHONPATH="$script_dir" python3 "$script_dir/safe_down_recovery.py" adopted-transition-version)" == 1 ]]; then
  # An interrupted safe-down publish is always rolled back to the immutable
  # v1 transition baseline before strict adoption can continue.  v2+ records
  # never receive this exception: their strict adoption proof rejects any
  # publication drift or incomplete journal.  This helper only validates the
  # held v1 capability; publication_commit.py owns the descriptor-safe journal
  # reconciliation and rollback below.
  PYTHONPATH="$script_dir" python3 "$script_dir/safe_down_recovery.py" v1-publication-replay-needed
  backup="$(python3 - <<'PY'
import json,os
os.lseek(4, 0, os.SEEK_SET)
print(json.loads(os.read(4, 1 << 20).decode('utf-8'))['backup']['path'])
PY
)"
  restore_database
  python3 "$script_dir/publication_commit.py" rollback --journal "$state_dir/publication-$release_version.journal" --recovery-evidence
fi
if (( recover_safe_down )); then PYTHONPATH="$script_dir" python3 "$script_dir/safe_down_recovery.py" adopt; fi
if ! python3 "$script_dir/publication_commit.py" check-incomplete; then
  hard_block_writers "incomplete-publication-journal" || true
  echo "incomplete publication journal requires explicit recovery" >&2
  exit 1
fi

# Read-only gates and private committed-tree staging happen before quiescence.
fail_at predeploy
python3 "$script_dir/predeploy-check.py" --db "$db" --routing-registry "$repo_root/hatchet/workers/routing-registry.json" --require-zero-legacy-ready --require-zero-import-conflicts --validate-legacy-import
ledger_snapshot >"$stage/ledger-before.txt"
fail_at staging
HATCHET_BRIDGE_STAGE_ROOT="$stage" DRIFTWARD_BRIDGE_RELEASE_VERSION="$release_version" "$script_dir/publish-release.sh" --stage-only >/dev/null
verify_staged_release
mkdir -p "$stage/etc/driftward" "$stage/units" "$stage/rollback"
if [[ "${BRIDGE_TEST_MODE:-}" == 1 ]]; then
  cp "$repo_root/hatchet/workers/routing-registry.json" "$stage/etc/driftward/hatchet-routing-registry.json"
  for unit in "${units[@]}"; do cp "$repo_root/hatchet/systemd/$unit" "$stage/units/$unit"; done
else
  git -C "$repo_root" show "$release_version:hatchet/workers/routing-registry.json" >"$stage/etc/driftward/hatchet-routing-registry.json"
  for unit in "${units[@]}"; do git -C "$repo_root" show "$release_version:hatchet/systemd/$unit" >"$stage/units/$unit"; done
fi
chmod 0640 "$stage/etc/driftward/"*.json; chmod 0644 "$stage/units/"*
grep -R -E '/root/driftward-host|__REPO_ROOT__' "$stage/units" && { echo "staged unit references mutable checkout" >&2; exit 1; } || true
python3 -m json.tool "$stage/release/verifier-policy.json" >/dev/null; python3 -m json.tool "$stage/etc/driftward/hatchet-routing-registry.json" >/dev/null
python3 "$script_dir/release_hygiene.py" scan --tree "$stage"
expected_attestation_delta="$(attestation_delta_required)"

# Quiescence creates the single state from which both backup migrations run.
if (( recover_safe_down )); then
  : # Capability and proof were accepted before any staging.
else
  : # fixed exclusive capability was acquired before the installer exec.
fi
quiesce_writers
if python3 "$script_dir/test_capability.py" signal-if writer-quiesce; then kill -TERM "$$"; fi
backup_dir="/root/driftward-backups/hatchet"; [[ "${BRIDGE_TEST_MODE:-}" == 1 ]] && backup_dir="$BRIDGE_TEST_ROOT/root/driftward-backups/hatchet"; install -d -m 0700 -o root -g root "$backup_dir"
backup="$backup_dir/bridge-$(date -u +%Y%m%dT%H%M%SZ)-before-${release_version}.sqlite"
fail_at backup
BACKUP_PATH="$backup" DB_PATH="$db" BRIDGE_TEST_UID="${BRIDGE_TEST_UID:-0}" BRIDGE_TEST_GID="${BRIDGE_TEST_GID:-0}" python3 - <<'PY'
import hashlib,os,re,sqlite3
from pathlib import Path
s=sqlite3.connect(f"file:{os.environ['DB_PATH']}?mode=ro",uri=True); d=sqlite3.connect(os.environ['BACKUP_PATH'])
try:
 s.backup(d)
 if d.execute('PRAGMA integrity_check').fetchone()[0] != 'ok': raise SystemExit('bridge backup integrity check failed')
finally: d.close(); s.close()
path=Path(os.environ['BACKUP_PATH'])
if not re.fullmatch(r'bridge-\d{8}T\d{6}Z-before-[0-9a-f]{40}\.sqlite',path.name): raise SystemExit('invalid backup filename')
os.chown(path,int(os.environ['BRIDGE_TEST_UID']),int(os.environ['BRIDGE_TEST_GID'])); os.chmod(path,0o600)
fd=os.open(path,os.O_RDONLY); os.fsync(fd); os.close(fd)
fd=os.open(path.parent,os.O_RDONLY); os.fsync(fd); os.close(fd)
PY
sha256sum "$backup" >"$backup.sha256"; chown root:root "$backup.sha256"; chmod 0600 "$backup.sha256"
BRIDGE_TEST_UID="${BRIDGE_TEST_UID:-0}" BRIDGE_TEST_GID="${BRIDGE_TEST_GID:-0}" python3 - "$backup" <<'PY'
import os,re,sys
from pathlib import Path
backup=Path(sys.argv[1]); checksum=backup.with_suffix(backup.suffix+'.sha256')
line=checksum.read_text().strip()
if not re.fullmatch(r'[0-9a-f]{64}\s+\*?'+re.escape(str(backup))+r'$',line): raise SystemExit('invalid backup checksum format')
uid=int(os.environ['BRIDGE_TEST_UID']); gid=int(os.environ['BRIDGE_TEST_GID'])
for path in (backup,checksum):
 stat=path.stat()
 if stat.st_uid != uid or stat.st_gid != gid or stat.st_mode & 0o777 != 0o600: raise SystemExit('backup ownership or mode invalid')
 fd=os.open(path,os.O_RDONLY); os.fsync(fd); os.close(fd)
fd=os.open(backup.parent,os.O_RDONLY); os.fsync(fd); os.close(fd)
PY
cp -- "$backup" "$stage/disposable.db"
db="$stage/disposable.db" ledger_snapshot >"$stage/disposable-before.txt"
fail_at disposable-migration
PYTHONDONTWRITEBYTECODE=1 DRIFTWARD_BRIDGE_RELEASE_VERSION="$release_version" HATCHET_INTAKE_ROUTING_REGISTRY_PATH="$stage/etc/driftward/hatchet-routing-registry.json" PYTHONPATH="$stage/release" python3 -B -m hatchet_hermes_bridge.cli --db "$stage/disposable.db" init >/dev/null
db="$stage/disposable.db" ledger_snapshot >"$stage/disposable-after.txt"; assert_migration_ledger "$stage/disposable-before.txt" "$stage/disposable-after.txt" "$expected_attestation_delta"
PYTHONDONTWRITEBYTECODE=1 DRIFTWARD_BRIDGE_RELEASE_VERSION="$release_version" HATCHET_INTAKE_ROUTING_REGISTRY_PATH="$stage/etc/driftward/hatchet-routing-registry.json" PYTHONPATH="$stage/release" python3 -B "$stage/release/predeploy-check.py" --db "$stage/disposable.db" --routing-registry "$stage/etc/driftward/hatchet-routing-registry.json" --require-zero-legacy-ready --require-zero-import-conflicts --require-current-attestations --validate-legacy-import
verify_staged_release

fail_at live-migration
idle_counts
PYTHONDONTWRITEBYTECODE=1 DRIFTWARD_BRIDGE_RELEASE_VERSION="$release_version" HATCHET_INTAKE_ROUTING_REGISTRY_PATH="$stage/etc/driftward/hatchet-routing-registry.json" PYTHONPATH="$stage/release" python3 -B -m hatchet_hermes_bridge.cli --db "$db" init >/dev/null
ledger_snapshot >"$stage/ledger-migrated.txt"; assert_migration_ledger "$stage/ledger-before.txt" "$stage/ledger-migrated.txt" "$expected_attestation_delta"
live_migrated=1; snapshot_targets; verify_staged_release; publication_started=1
fail_at config-publication
python3 "$script_dir/publication_commit.py" commit --snapshot "$stage/rollback/publication.json" --stage "$stage" --release-version "$release_version" --transaction "$release_version"
fail_at policy-publication
fail_at routing-publication
fail_at unit-publication
new_release_dir="$release_root/$release_version"
fail_at release-publication
fail_at symlink-switch
systemctl daemon-reload; fail_at daemon-reload
clear_maintenance_barrier; lock_released=1
systemctl start driftward-hatchet-bridge.service; fail_at restart
token="$(awk -F= '$1 == "HATCHET_HERMES_BRIDGE_TOKEN" {print $2; exit}' "$bridge_env")"; [[ -n "$token" ]] || { echo "bridge auth token is required for production health" >&2; exit 1; }
for _ in {1..20}; do curl --silent --fail --max-time 2 -H "Authorization: Bearer $token" http://127.0.0.1:19877/health >/dev/null && break; sleep 1; done
fail_at health
health="$(curl --silent --fail --max-time 2 -H "Authorization: Bearer $token" http://127.0.0.1:19877/health)"
HEALTH="$health" RELEASE="$release_version" python3 - <<'PY'
import json,os
health=json.loads(os.environ['HEALTH'])
assert health == {'ok':True,'schema_version':6,'release_commit':os.environ['RELEASE']}
PY
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$release_root/current" DB="$db" python3 -B - <<'PY'
import os
from hatchet_hermes_bridge.store import connect_runtime
c=connect_runtime(os.environ['DB']); c.close()
PY
PYTHONDONTWRITEBYTECODE=1 DRIFTWARD_BRIDGE_RELEASE_VERSION="$release_version" HATCHET_INTAKE_ROUTING_REGISTRY_PATH="$routing_registry" PYTHONPATH="$release_root/current" python3 -B "$release_root/current/predeploy-check.py" --db "$db" --routing-registry "$routing_registry" --require-zero-legacy-ready --require-zero-import-conflicts --require-current-attestations --validate-legacy-import
idle_counts
ledger_snapshot >"$stage/ledger-after.txt"; cmp --silent "$stage/ledger-migrated.txt" "$stage/ledger-after.txt" || { echo "unexpected bridge ledger mutation after migration" >&2; exit 1; }
restore_services
if (( recover_safe_down )); then
  PYTHONPATH="$script_dir" python3 "$script_dir/safe_down_recovery.py" complete
fi
echo "bridge release committed: $release_version (Factory mode unchanged; reconciler remains disabled)"
