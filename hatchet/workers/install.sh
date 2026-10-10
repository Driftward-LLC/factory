#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
unit_src="$repo_root/hatchet/workers/systemd"
unit_dst="/etc/systemd/system"
workers_root="$repo_root/hatchet/workers"
profile_name="${DRIFTWARD_HATCHET_PROFILE:-prod}"
common_env="/etc/driftward/hatchet-workers.env"
bridge_executor_env="/etc/driftward/hatchet-worker-bridge-executor.env"
reconciliation_env="/etc/driftward/hatchet-worker-reconciliation.env"
bridge_env="/etc/driftward/hatchet-bridge.env"
verifier_env="/etc/driftward/hatchet-verifier.env"
operator_env="/etc/driftward/factory-operator.env"
policy_src="$workers_root/verifier-policy.json"
policy_dst="$policy_src"
units=(
  driftward-hatchet-hermes-gateway.service
  driftward-hatchet-worker-intake.service
  driftward-hatchet-worker-execution-dispatch.service
  driftward-hatchet-worker-bridge-executor.service
  driftward-hatchet-worker-pr-event.service
  driftward-hatchet-worker-reconciliation.service
)

if [[ "$(id -u)" != "0" ]]; then
  echo "Hatchet worker install requires root" >&2
  exit 1
fi

install -d -m 0750 -o root -g root /etc/driftward
install -d -m 0755 -o root -g root "$unit_dst"

ensure_env_file() {
  local file="$1"
  if [[ ! -e "$file" ]]; then
    install -m 0600 -o root -g root /dev/null "$file"
  else
    chown root:root "$file"
    chmod 0600 "$file"
  fi
}

upsert_env() {
  local file="$1"
  local key="$2"
  local value="$3"
  local tmp

  [[ -n "$value" ]] || return 0

  tmp="$(mktemp)"
  if [[ -s "$file" ]]; then
    awk -F= -v key="$key" '$1 != key { print }' "$file" >"$tmp"
  fi
  printf '%s=%s\n' "$key" "$value" >>"$tmp"
  install -m 0600 -o root -g root "$tmp" "$file"
  rm -f "$tmp"
}

env_file_value() {
  local file="$1"
  local key="$2"

  awk -F= -v key="$key" '$1 == key { print substr($0, length(key) + 2); exit }' "$file" 2>/dev/null || true
}

remove_env() {
  local file="$1"
  local key="$2"
  local tmp

  [[ -e "$file" ]] || return 0

  tmp="$(mktemp)"
  awk -F= -v key="$key" '$1 != key { print }' "$file" >"$tmp"
  install -m 0600 -o root -g root "$tmp" "$file"
  rm -f "$tmp"
}

analytics_label_secret_shaped() {
  local value="${1,,}"
  [[ "$value" =~ (^|[._+-])(sk-|gh[pousr]_|github_pat_|xox[baprs]-|hf_|gsk_|lin_api_|glpat-|npm_|pypi-|akia[a-z0-9]|asia[a-z0-9]|aiza[a-z0-9]|bearer([._+-]|$)) ]] \
    || [[ "$value" =~ eyj[a-z0-9_-]{2,}\.[a-z0-9_-]{2,}\.[a-z0-9_-]{2,} ]] \
    || [[ "$value" =~ [a-z0-9_.-]{128,} ]]
}

analytics_label_valid() {
  local kind="$1"
  local value="$2"
  analytics_label_secret_shaped "$value" && return 1
  case "$kind" in
    auth) [[ "$value" == "chatgpt_codex_oauth" || "$value" == "api_key" || "$value" == "unknown" ]] ;;
    dimension) [[ "$value" =~ ^[a-z][a-z0-9_.-]{0,79}$ ]] ;;
    version) [[ "$value" != "unknown" && "$value" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]{0,119}$ ]] ;;
    *) return 1 ;;
  esac
}

normalize_analytics_label() {
  local file="$1"
  local key="$2"
  local kind="$3"
  local configured="$4"
  local value="$configured"
  [[ -n "$value" ]] || value="$(env_file_value "$file" "$key")"
  if [[ -n "$value" ]] && analytics_label_valid "$kind" "$value"; then
    # Always rewrite the key, even when the existing first value is valid.
    # upsert_env removes every occurrence before appending one canonical value,
    # so a later duplicate cannot become the effective secret-shaped label.
    upsert_env "$file" "$key" "$value"
  elif [[ "$kind" == "version" ]]; then
    remove_env "$file" "$key"
  else
    upsert_env "$file" "$key" unknown
  fi
  value="$(env_file_value "$file" "$key")"
  if [[ -z "$value" || "$value" == "unknown" ]]; then
    echo "warning: $key is unknown or invalid; execution attribution will be incomplete" >&2
  fi
}

ensure_env_secret() {
  local file="$1"
  local key="$2"
  local value

  value="$(awk -F= -v key="$key" '$1 == key { print substr($0, length(key) + 2); exit }' "$file" 2>/dev/null || true)"
  if [[ -z "$value" ]]; then
    upsert_env "$file" "$key" "$(openssl rand -hex 32)"
  fi
}

read_profile_value() {
  local key="$1"
  python3 - "$profile_name" "$key" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

profile_name, requested_key = sys.argv[1], sys.argv[2]
path = Path.home() / ".hatchet" / "profiles.yaml"
if not path.exists():
    raise SystemExit(0)

in_profiles = False
in_profile = False
for raw in path.read_text().splitlines():
    if raw.strip() == "profiles:":
        in_profiles = True
        continue
    if not in_profiles:
        continue

    if raw.startswith("    ") and raw.strip() == f"{profile_name}:":
        in_profile = True
        continue

    if in_profile and raw.startswith("    ") and not raw.startswith("        "):
        break

    if in_profile and raw.startswith("        ") and ":" in raw:
        key, value = raw.strip().split(":", 1)
        if key == requested_key:
            print(value.strip())
            break
PY
}

ensure_env_file "$common_env"
ensure_env_file "$bridge_executor_env"
ensure_env_file "$reconciliation_env"
ensure_env_file "$verifier_env"
ensure_env_file "$operator_env"
ensure_env_secret "$operator_env" HATCHET_FACTORY_OPERATOR_TOKEN
HATCHET_VERIFIER_ENV_PATH="$verifier_env" HATCHET_VERIFIER_POLICY_PATH="$policy_dst" HATCHET_VERIFIER_POLICY_SOURCE="$policy_src" "$repo_root/hatchet/bridge/verifier-env.sh" provision
HATCHET_VERIFIER_ENV_PATH="$verifier_env" HATCHET_VERIFIER_POLICY_PATH="$policy_dst" HATCHET_VERIFIER_POLICY_SOURCE="$policy_src" "$repo_root/hatchet/bridge/verifier-env.sh" status

upsert_env "$common_env" HATCHET_CLIENT_API_URL "$(read_profile_value apiserverurl)"
upsert_env "$common_env" HATCHET_CLIENT_HOST_PORT "$(read_profile_value grpchostport)"
upsert_env "$common_env" HATCHET_CLIENT_TLS_STRATEGY "$(read_profile_value tlsstrategy)"
upsert_env "$common_env" HATCHET_CLIENT_TOKEN "$(read_profile_value token)"
upsert_env "$common_env" HATCHET_HERMES_BRIDGE_URL "http://127.0.0.1:19877"
if [[ -f "$bridge_env" ]]; then
  bridge_token="$(awk -F= '$1 == "HATCHET_HERMES_BRIDGE_TOKEN" { print $2; exit }' "$bridge_env")"
  upsert_env "$common_env" HATCHET_HERMES_BRIDGE_TOKEN "$bridge_token"
  inspection_token="$(awk -F= '$1 == "HATCHET_HERMES_INSPECTION_TOKEN" { print $2; exit }' "$bridge_env")"
  recovery_token="$(awk -F= '$1 == "HATCHET_HERMES_RECOVERY_TOKEN" { print $2; exit }' "$bridge_env")"
  upsert_env "$bridge_executor_env" HATCHET_HERMES_INSPECTION_TOKEN "$inspection_token"
  upsert_env "$reconciliation_env" HATCHET_HERMES_RECOVERY_TOKEN "$recovery_token"
  # Scoped credentials are not copied into unrelated workers.
fi
upsert_env "$common_env" HATCHET_EVENT_INGRESS_URL "${HATCHET_EVENT_INGRESS_URL:-http://127.0.0.1:19878}"
upsert_env "$common_env" RECONCILIATION_OBSERVATION_TIMEOUT_MS "${RECONCILIATION_OBSERVATION_TIMEOUT_MS:-1000}"
upsert_env "$common_env" RECONCILIATION_OBSERVATION_MAX_AGE_MS "${RECONCILIATION_OBSERVATION_MAX_AGE_MS:-86400000}"
upsert_env "$common_env" HATCHET_CLIENT_LOG_LEVEL "${HATCHET_CLIENT_LOG_LEVEL:-INFO}"
remove_env "$common_env" HATCHET_INTAKE_DEFAULT_REPO
upsert_env "$common_env" HATCHET_INTAKE_ROUTING_REGISTRY_PATH "${HATCHET_INTAKE_ROUTING_REGISTRY_PATH:-$workers_root/routing-registry.json}"
upsert_env "$common_env" HATCHET_INTAKE_ALLOWED_REPOS "${HATCHET_INTAKE_ALLOWED_REPOS:-Driftward-LLC/driftward-host,Driftward-LLC/capykit}"
upsert_env "$common_env" HATCHET_INTAKE_DRY_RUN "${HATCHET_INTAKE_DRY_RUN:-true}"
upsert_env "$common_env" HATCHET_VERIFIER_POLICY_PATH "$policy_dst"

# The root-only operator and outbox dispatcher share only the Hatchet event
# client settings; neither receives bridge or verifier credentials.
for key in HATCHET_CLIENT_API_URL HATCHET_CLIENT_HOST_PORT HATCHET_CLIENT_TLS_STRATEGY HATCHET_CLIENT_TOKEN; do
  value="$(awk -F= -v key="$key" '$1 == key { print substr($0, length(key) + 2); exit }' "$common_env")"
  upsert_env "$operator_env" "$key" "$value"
done

upsert_env "$bridge_executor_env" DRIFTWARD_HERMES_CREATE_DRAFT_PR_ENABLED "${DRIFTWARD_HERMES_CREATE_DRAFT_PR_ENABLED:-1}"
upsert_env "$bridge_executor_env" DRIFTWARD_HERMES_GRPC_TARGET "${DRIFTWARD_HERMES_GRPC_TARGET:-unix:///run/driftward-hermes-factory/hermes.sock}"
normalize_analytics_label "$bridge_executor_env" DRIFTWARD_HERMES_AUTH_MODE auth "${DRIFTWARD_HERMES_AUTH_MODE:-}"
remove_env "$bridge_executor_env" DRIFTWARD_HERMES_PROVIDER
remove_env "$bridge_executor_env" DRIFTWARD_HERMES_MODEL
normalize_analytics_label "$bridge_executor_env" DRIFTWARD_HERMES_VERSION version "${DRIFTWARD_HERMES_VERSION:-}"
normalize_analytics_label "$bridge_executor_env" FACTORY_RELEASE_VERSION version "${FACTORY_RELEASE_VERSION:-}"
upsert_env "$bridge_executor_env" DRIFTWARD_HERMES_COMMAND "${DRIFTWARD_HERMES_COMMAND:-driftward-hermes}"
upsert_env "$bridge_executor_env" DRIFTWARD_HERMES_ALLOWED_REPOS "${DRIFTWARD_HERMES_ALLOWED_REPOS:-Driftward-LLC/driftward-host,Driftward-LLC/capykit}"
upsert_env "$bridge_executor_env" DRIFTWARD_HERMES_EXPECTED_GITHUB_LOGIN "${DRIFTWARD_HERMES_EXPECTED_GITHUB_LOGIN:-factory-hermes[bot]}"
upsert_env "$bridge_executor_env" DRIFTWARD_HERMES_EXPECTED_GIT_AUTHOR_NAME "${DRIFTWARD_HERMES_EXPECTED_GIT_AUTHOR_NAME:-factory-hermes[bot]}"
upsert_env "$bridge_executor_env" DRIFTWARD_HERMES_EXPECTED_GIT_AUTHOR_EMAIL "${DRIFTWARD_HERMES_EXPECTED_GIT_AUTHOR_EMAIL:-4491454+factory-hermes[bot]@users.noreply.github.com}"
upsert_env "$bridge_executor_env" DRIFTWARD_HERMES_FORBIDDEN_GITHUB_LOGINS "${DRIFTWARD_HERMES_FORBIDDEN_GITHUB_LOGINS:-derek-nguyen}"
upsert_env "$bridge_executor_env" DRIFTWARD_HERMES_FORBIDDEN_GIT_AUTHOR_EMAILS "${DRIFTWARD_HERMES_FORBIDDEN_GIT_AUTHOR_EMAILS:-derek@driftward.io}"
upsert_env "$bridge_executor_env" DRIFTWARD_HERMES_CREATE_DRAFT_PR_TIMEOUT_MS "${DRIFTWARD_HERMES_CREATE_DRAFT_PR_TIMEOUT_MS:-1200000}"
upsert_env "$bridge_executor_env" DRIFTWARD_HERMES_ASYNC_RECEIPTS_ENABLED "${DRIFTWARD_HERMES_ASYNC_RECEIPTS_ENABLED:-1}"
upsert_env "$bridge_executor_env" DRIFTWARD_HERMES_ASYNC_CREATE_DRAFT_PR_TIMEOUT_MS "${DRIFTWARD_HERMES_ASYNC_CREATE_DRAFT_PR_TIMEOUT_MS:-14400000}"
upsert_env "$bridge_executor_env" DRIFTWARD_HERMES_RECEIPT_WAIT_DURATION "${DRIFTWARD_HERMES_RECEIPT_WAIT_DURATION:-4h}"
upsert_env "$bridge_executor_env" DRIFTWARD_HERMES_RECEIPT_REQUIRE_SYSTEMD "${DRIFTWARD_HERMES_RECEIPT_REQUIRE_SYSTEMD:-1}"
upsert_env "$bridge_executor_env" DRIFTWARD_HERMES_MAX_BUFFER_BYTES "${DRIFTWARD_HERMES_MAX_BUFFER_BYTES:-2097152}"

if ! grep -q '^HATCHET_CLIENT_TOKEN=' "$common_env"; then
  echo "HATCHET_CLIENT_TOKEN missing from $common_env; run hatchet profile add --name $profile_name or set it manually" >&2
  exit 1
fi

if [[ -z "${bridge_token:-}" ]] || ! curl --silent --fail --max-time 2 -H "Authorization: Bearer $bridge_token" http://127.0.0.1:19877/health >/dev/null; then
  echo "Hatchet bridge is unavailable; run hatchet/bridge/install.sh before installing workers" >&2
  exit 1
fi

npm install --prefix "$workers_root"
npm run build --prefix "$workers_root"

for unit in "${units[@]}"; do
  tmp="$(mktemp)"
  sed "s#__REPO_ROOT__#$repo_root#g" "$unit_src/$unit" >"$tmp"
  install -D -m 0644 "$tmp" "$unit_dst/$unit"
  rm -f "$tmp"
done

systemctl daemon-reload
systemctl enable --now "${units[@]}"
systemctl restart "${units[@]}"
systemctl restart driftward-factory-operator.service
