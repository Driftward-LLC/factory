#!/usr/bin/env bash
set -euo pipefail

env_file="${HATCHET_VERIFIER_ENV_PATH:-/etc/driftward/hatchet-verifier.env}"
policy_path="${HATCHET_VERIFIER_POLICY_PATH:-/etc/driftward/hatchet-verifier-policy.json}"
policy_source="${HATCHET_VERIFIER_POLICY_SOURCE:-}"

test_mode="${HATCHET_VERIFIER_TEST_MODE:-0}"
[[ "$(id -u)" == "0" || "$test_mode" == "1" ]] || { echo "verifier env operation requires root" >&2; exit 1; }

token_ok() {
  [[ -f "$env_file" ]] && [[ "$(stat -c '%a' "$env_file")" == "600" ]] && \
    { [[ "$test_mode" == "1" ]] || [[ "$(stat -c '%U:%G' "$env_file")" == "root:root" ]]; } && \
    awk -F= '$1 == "HATCHET_HERMES_VERIFIER_TOKEN" && $2 ~ /^[A-Fa-f0-9]{64}$/ { found=1 } END { exit !found }' "$env_file"
}

provision() {
  if [[ "$test_mode" == "1" ]]; then
    install -d -m 0750 "$(dirname "$env_file")"
  else
    install -d -m 0750 -o root -g root "$(dirname "$env_file")"
  fi
  if token_ok; then return 0; fi
  tmp="$(mktemp "${env_file}.tmp.XXXXXX")"
  trap 'rm -f "${tmp:-}"' EXIT
  if [[ -f "$env_file" ]]; then awk -F= '$1 != "HATCHET_HERMES_VERIFIER_TOKEN" { print }' "$env_file" >"$tmp"; fi
  printf 'HATCHET_HERMES_VERIFIER_TOKEN=%s\n' "$(openssl rand -hex 32)" >>"$tmp"
  if [[ "$test_mode" != "1" ]]; then chown root:root "$tmp"; fi
  chmod 0600 "$tmp"
  mv -f "$tmp" "$env_file"; sync -d "$(dirname "$env_file")" 2>/dev/null || true
  token_ok || { echo "verifier token provisioning failed" >&2; exit 1; }
}

status() {
  token_ok || { echo "verifier token is absent or invalid" >&2; exit 1; }
  [[ -r "$policy_path" ]] || { echo "verifier policy is unreadable" >&2; exit 1; }
  if [[ -n "$policy_source" ]]; then
    [[ "$(sha256sum "$policy_source" | awk '{print $1}')" == "$(sha256sum "$policy_path" | awk '{print $1}')" ]] || { echo "verifier policy digest mismatch" >&2; exit 1; }
  fi
  echo "verifier preflight ok"
}

case "${1:-status}" in provision) provision ;; status) status ;; *) echo "usage: $0 {provision|status}" >&2; exit 2 ;; esac
