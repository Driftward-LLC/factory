#!/usr/bin/env bash
# Shared by the production bridge installer and its executable test harness.
# It is intentionally source-only: production installation paths and targets
# remain fixed in install.sh.

readonly -a BRIDGE_INSTALL_FAILPOINT_STAGES=(
  predeploy
  staging
  writer-quiesce
  backup
  disposable-migration
  live-migration
  config-publication
  policy-publication
  routing-publication
  unit-publication
  release-publication
  symlink-switch
  daemon-reload
  restart
  health
)

supported_bridge_install_failpoint() {
  local candidate
  for candidate in "${BRIDGE_INSTALL_FAILPOINT_STAGES[@]}"; do
    [[ "$candidate" == "$1" ]] && return 0
  done
  return 1
}

validate_install_failpoint() {
  local configured="${HATCHET_BRIDGE_INSTALL_FAIL_AT:-}"
  [[ -z "$configured" ]] && return 0
  if ! supported_bridge_install_failpoint "$configured"; then
    printf 'unknown bridge installer failpoint: %s\n' "$configured" >&2
    return 2
  fi
  return 0
}

fail_at() {
  local stage="${1:-}"
  if [[ $# -ne 1 ]] || ! supported_bridge_install_failpoint "$stage"; then
    printf 'invalid bridge installer failpoint stage: %s\n' "$stage" >&2
    return 64
  fi
  if [[ -n "${HATCHET_BRIDGE_INSTALL_FAIL_AT:-}" && "${HATCHET_BRIDGE_INSTALL_FAIL_AT}" == "$stage" ]]; then
    printf 'injected installer failure: %s\n' "$stage" >&2
    return 97
  fi
  return 0
}
