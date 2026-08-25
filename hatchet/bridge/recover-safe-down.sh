#!/usr/bin/env bash
set -euo pipefail

# One-way recovery for the pre-capture incident. It does not restore or start
# the mutable checkout runtime: it proves safe-down, archives the marker, and
# execs the transactional installer with its inherited exclusive capability.
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 "$script_dir/test_capability.py" guard
if test_shell="$(python3 "$script_dir/test_capability.py" maybe-shell)"; then
  eval "$test_shell"
  PATH="$BRIDGE_TEST_FAKE_BIN:$PATH"
fi
backup=""; expected_checksum=""; expected_marker=""; expected_marker_mtime=""; supersede=0
parent_transition_sha=""; parent_generation=""; parent_code_commit=""; successor_commit=""; successor_pr=""; approved_pr_head=""
attest_descendant_marker=0; old_marker_sha=""; old_marker_dev=""; old_marker_ino=""; old_marker_mtime_ns=""; marker_dev=""; marker_ino=""; marker_mtime_ns=""; terminal_marker_reason=""
original_args=("$@")
while (( $# )); do
  case "$1" in
    --yes) yes=1;; --supersede) supersede=1;; --backup) backup="$2"; shift;; --checksum) expected_checksum="$2"; shift;; --marker-sha256) expected_marker="$2"; shift;; --marker-mtime) expected_marker_mtime="$2"; shift;;
    --parent-transition-sha256) parent_transition_sha="$2"; shift;; --parent-generation) parent_generation="$2"; shift;; --parent-code-commit) parent_code_commit="$2"; shift;; --successor-commit) successor_commit="$2"; shift;; --successor-pr) successor_pr="$2"; shift;; --approved-pr-head) approved_pr_head="$2"; shift;;
    --attest-descendant-marker) attest_descendant_marker=1;; --old-marker-sha256) old_marker_sha="$2"; shift;; --old-marker-dev) old_marker_dev="$2"; shift;; --old-marker-ino) old_marker_ino="$2"; shift;; --old-marker-mtime-ns) old_marker_mtime_ns="$2"; shift;; --marker-dev) marker_dev="$2"; shift;; --marker-ino) marker_ino="$2"; shift;; --marker-mtime-ns) marker_mtime_ns="$2"; shift;; --terminal-marker-reason) terminal_marker_reason="$2"; shift;;
    *) echo "usage: recover-safe-down.sh --yes [--supersede --parent-transition-sha256 <sha256> --parent-generation <n> --parent-code-commit <sha> --successor-commit <sha> --successor-pr <number> --approved-pr-head <sha> [--attest-descendant-marker --old-marker-sha256 <sha256> --old-marker-dev <dev> --old-marker-ino <ino> --old-marker-mtime-ns <ns> --marker-dev <dev> --marker-ino <ino> --marker-mtime-ns <ns> --terminal-marker-reason safe-down\\ fresh-install-failed]] --backup <root backup> --checksum <sha256> --marker-sha256 <sha256> --marker-mtime <epoch>" >&2; exit 64;;
  esac; shift
done
[[ "${yes:-0}" == 1 ]] || { echo "explicit --yes approval required" >&2; exit 1; }
if ! python3 "$script_dir/maintenance_lock_exec.py" check; then
  mode=exec; (( supersede )) && mode=exec-existing
  exec python3 "$script_dir/maintenance_lock_exec.py" "$mode" bash "$script_dir/recover-safe-down.sh" "${original_args[@]}"
fi
if (( supersede )) && ! python3 "$script_dir/barrier_evidence_exec.py" check; then
  exec python3 "$script_dir/barrier_evidence_exec.py" exec bash "$script_dir/recover-safe-down.sh" "${original_args[@]}"
fi
state_dir=/var/lib/driftward-hatchet-bridge
[[ "${BRIDGE_TEST_MODE:-}" == 1 ]] && state_dir="$BRIDGE_TEST_ROOT/var/lib/driftward-hatchet-bridge"
source "$script_dir/installer-barrier.sh"
mapfile -t writer_units < <(PYTHONPATH="$script_dir" python3 "$script_dir/writer-inventory.py" --kind all)
lock_acquired=1
on_signal() {
  # The wrapper may be interrupted before it execs the installer.  Preserve
  # the active barrier and marker; the next explicitly approved invocation
  # will reopen and reprove the same archived transition.
  trap - TERM INT
  exit "$([[ "$1" == INT ]] && printf 130 || printf 143)"
}
trap 'on_signal TERM' TERM
trap 'on_signal INT' INT

# Ordinary recovery installs the condition barrier before opening evidence.
# Supersession instead proves an already-installed barrier read-only and uses
# O_NOFOLLOW descriptors solely to carry that proof into safe_down_recovery.
if (( supersede )); then
  [[ -n "$parent_transition_sha" && -n "$parent_generation" && -n "$parent_code_commit" && -n "$successor_commit" && -n "$successor_pr" && -n "$approved_pr_head" ]] || { echo "supersession identity and reviewed successor provenance are required" >&2; exit 64; }
  if (( attest_descendant_marker )); then
    [[ -n "$old_marker_sha" && -n "$old_marker_dev" && -n "$old_marker_ino" && -n "$old_marker_mtime_ns" && -n "$marker_dev" && -n "$marker_ino" && -n "$marker_mtime_ns" && -n "$terminal_marker_reason" ]] || { echo "complete descendant marker attestation identity is required" >&2; exit 64; }
  elif [[ -n "$old_marker_sha$old_marker_dev$old_marker_ino$old_marker_mtime_ns$marker_dev$marker_ino$marker_mtime_ns$terminal_marker_reason" ]]; then
    echo "descendant marker values require --attest-descendant-marker" >&2; exit 64
  fi
  verify_existing_maintenance_barrier_readonly
  python3 "$script_dir/test_capability.py" audit recover-wrapper-readonly
  # This is deliberately standalone: it creates only immutable transition
  # evidence and swaps the auxiliary active link.  It never execs install.sh.
  cmd=(env "PYTHONPATH=$script_dir" python3 "$script_dir/safe_down_recovery.py" supersede
    --parent-transition-sha256 "$parent_transition_sha" --parent-generation "$parent_generation" --parent-code-commit "$parent_code_commit"
    --backup "$backup" --checksum "$expected_checksum" --marker-sha256 "$expected_marker" --marker-mtime "$expected_marker_mtime"
    --successor-commit "$successor_commit" --successor-pr "$successor_pr" --approved-pr-head "$approved_pr_head")
  if (( attest_descendant_marker )); then
    cmd+=(--attest-descendant-marker --old-marker-sha256 "$old_marker_sha" --old-marker-dev "$old_marker_dev" --old-marker-ino "$old_marker_ino" --old-marker-mtime-ns "$old_marker_mtime_ns" --marker-dev "$marker_dev" --marker-ino "$marker_ino" --marker-mtime-ns "$marker_mtime_ns" --terminal-marker-reason "$terminal_marker_reason")
  fi
  exec "${cmd[@]}"
fi
adopt_inherited_maintenance_lock; install_maintenance_dropins; stop_loaded_writers; verify_maintenance_barrier
python3 "$script_dir/test_capability.py" audit recover-wrapper
for u in "${writer_units[@]}"; do p="$systemd_dropin_root/$u"; [[ -L "$p" && "$(readlink "$p")" == /dev/null ]] && rm -f -- "$p"; done
systemctl daemon-reload; verify_maintenance_barrier
exec env PYTHONPATH="$script_dir" python3 "$script_dir/safe_down_recovery.py" prepare --exec-install --backup "$backup" --checksum "$expected_checksum" --marker-sha256 "$expected_marker" --marker-mtime "$expected_marker_mtime"
