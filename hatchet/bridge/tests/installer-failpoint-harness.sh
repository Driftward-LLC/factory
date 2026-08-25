#!/usr/bin/env bash
# Test-only state-machine harness. It exercises the production failpoint
# helper under strict shell options without exposing production path overrides.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../installer-failpoints.sh
source "$script_dir/../installer-failpoints.sh"

validate_install_failpoint

cleanup() {
  local status=$?
  printf 'cleanup:%s\n' "$status"
  exit "$status"
}
on_term() {
  printf 'signal:TERM\n'
  exit 143
}
trap cleanup EXIT
trap on_term TERM

run_stage() {
  local stage="$1"
  printf 'enter:%s\n' "$stage"
  fail_at "$stage"
  printf 'pass:%s\n' "$stage"
  if [[ "${HATCHET_BRIDGE_HARNESS_SIGNAL_AT:-}" == "$stage" ]]; then
    kill -TERM "$$"
  fi
}

run_conditional_contexts() {
  fail_at predeploy
  if fail_at staging; then printf 'conditional:staging:pass\n'; fi
  wrapped_failpoint() { fail_at backup; return $?; }
  if wrapped_failpoint; then printf 'conditional-function:backup:pass\n'; fi
  wrapped_failpoint || printf 'conditional-function:backup:fail:%s\n' "$?"
}

case "${HATCHET_BRIDGE_HARNESS_MODE:-state-machine}" in
  state-machine)
    for stage in "${BRIDGE_INSTALL_FAILPOINT_STAGES[@]}"; do run_stage "$stage"; done
    printf 'complete\n'
    ;;
  conditional-contexts)
    run_conditional_contexts
    printf 'complete\n'
    ;;
  *)
    printf 'unknown harness mode\n' >&2
    exit 64
    ;;
esac
