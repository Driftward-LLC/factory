#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
runtime_root="${DRIFTWARD_FACTORY_RUNTIME_ROOT:-/docker/driftward/factory}"
service_unit="${DRIFTWARD_FACTORY_SYSTEMD_UNIT:-/etc/systemd/system/driftward-factory.service}"
service_name="$(basename "$service_unit")"
hermes_gateway_unit="${DRIFTWARD_HERMES_FACTORY_GATEWAY_SYSTEMD_UNIT:-/etc/systemd/system/driftward-hermes-factory-gateway.service}"
hermes_gateway_service_name="$(basename "$hermes_gateway_unit")"
factory_image_dropin_dir="${service_unit}.d"
factory_image_dropin="$factory_image_dropin_dir/10-image-tag.conf"
hatchet_env_file="${DRIFTWARD_HATCHET_ENV_FILE:-/etc/driftward/hatchet.env}"
bridge_executor_env_file="${DRIFTWARD_HERMES_BRIDGE_EXECUTOR_ENV_FILE:-/etc/driftward/hatchet-worker-bridge-executor.env}"
legacy_hatchet_unit="${DRIFTWARD_LEGACY_HATCHET_UNIT:-driftward-hatchet.service}"
factory_cli_source="$repo_root/factory/factory"
factory_cli_target="${DRIFTWARD_FACTORY_CLI_TARGET:-/usr/local/bin/factory}"
factory_share_dir="${DRIFTWARD_FACTORY_SHARE_DIR:-/usr/local/share/driftward-factory}"
node_bin="${DRIFTWARD_FACTORY_NODE_BIN:-/usr/bin/node}"
factory_hatchet_doctor_source="$repo_root/tools/factory-hatchet-doctor.py"
factory_hatchet_doctor_target="$factory_share_dir/factory-hatchet-doctor.py"
factory_hatchet_desired_state_source="$repo_root/hatchet/desired-state/factory-hatchet.json"
factory_hatchet_desired_state_target="$factory_share_dir/desired-state/factory-hatchet.json"
hermes_gateway_source="$repo_root/hatchet/workers/bridge-executor-worker/src/hermes/hermes-gateway-server.ts"
hermes_gateway_bundle_target="$factory_share_dir/hermes-gateway/hermes-gateway-server.js"
hermes_gateway_proto_source="$repo_root/hatchet/workers/bridge-executor-worker/src/hermes/hermes-gateway.proto"
hermes_gateway_proto_target="$factory_share_dir/hermes-gateway/hermes-gateway.proto"
verifier_policy_source="$repo_root/hatchet/workers/verifier-policy.json"
verifier_policy_target="$factory_share_dir/verifier-policy.json"
factory_image_tag="${FACTORY_IMAGE_TAG:-local}"
factory_image_override="${FACTORY_IMAGE:-}"
smoke_timeout="${DRIFTWARD_FACTORY_SMOKE_TIMEOUT:-90}"
smoke_interval="${DRIFTWARD_FACTORY_SMOKE_INTERVAL:-5}"
start=0

usage() {
  cat <<'USAGE'
usage: deploy/install-docker.sh [--start] [--image-tag TAG] [--image IMAGE]

Installs the Docker Compose deployment wrapper for Driftward Factory.
It does not create or print secrets. Required env files stay under /etc/driftward.

Options:
  --start          Build the selected Factory image and restart the systemd unit.
  --image-tag TAG  Persist the Factory image tag used by systemd and Compose.
  --image IMAGE    Persist a full Factory image reference instead of a tag.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --start) start=1; shift ;;
    --image-tag)
      [[ $# -ge 2 ]] || { echo "--image-tag requires a value" >&2; exit 64; }
      factory_image_tag="$2"
      factory_image_override=""
      shift 2
      ;;
    --image)
      [[ $# -ge 2 ]] || { echo "--image requires a value" >&2; exit 64; }
      factory_image_override="$2"
      shift 2
      ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 64 ;;
  esac
done

if [[ -n "$factory_image_override" && ! "$factory_image_override" =~ ^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$ ]]; then
  echo "invalid Factory image reference: $factory_image_override" >&2
  exit 64
fi
if [[ -z "$factory_image_override" ]]; then
  if [[ ! "$factory_image_tag" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]]; then
    echo "invalid Factory image tag: $factory_image_tag" >&2
    exit 64
  fi
  factory_image="driftward/factory:$factory_image_tag"
else
  factory_image="$factory_image_override"
fi

if [[ ! "$smoke_timeout" =~ ^[1-9][0-9]*$ ]]; then
  echo "invalid smoke timeout: $smoke_timeout" >&2
  exit 64
fi
if [[ ! "$smoke_interval" =~ ^[1-9][0-9]*$ ]]; then
  echo "invalid smoke interval: $smoke_interval" >&2
  exit 64
fi

require_clean_release_checkout() {
  local release_tag="$factory_image_tag"
  if [[ -n "$factory_image_override" ]]; then
    local image_basename="${factory_image_override##*/}"
    [[ "$image_basename" == *:* ]] || return 0
    release_tag="${image_basename##*:}"
  fi
  [[ "$release_tag" =~ ^v[0-9]{4}\.[0-9]{1,2}\.[0-9]{1,2}($|[.-]) ]] || return 0

  if ! git -C "$repo_root" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "release Factory image tag requires a git checkout: $release_tag" >&2
    exit 64
  fi

  local tag_commit
  local head_commit
  tag_commit="$(git -C "$repo_root" rev-parse -q --verify "refs/tags/$release_tag^{commit}" || true)"
  head_commit="$(git -C "$repo_root" rev-parse HEAD)"
  if [[ -z "$tag_commit" || "$tag_commit" != "$head_commit" ]]; then
    echo "release Factory image tag $release_tag does not match the current checkout" >&2
    echo "checkout the release tag before installing it" >&2
    exit 64
  fi

  git -C "$repo_root" update-index -q --refresh || true
  if ! git -C "$repo_root" diff-index --quiet HEAD --; then
    echo "release Factory image tag $release_tag requires a clean working tree" >&2
    exit 64
  fi
  if [[ -n "$(git -C "$repo_root" ls-files --others --exclude-standard)" ]]; then
    echo "release Factory image tag $release_tag requires no untracked files" >&2
    exit 64
  fi
}

require_clean_release_checkout

wait_for_factory_smoke() {
  local deadline=$((SECONDS + smoke_timeout))
  local output

  while true; do
    if output="$(factory-docker-smoke "$runtime_root" 2>&1)"; then
      printf '%s\n' "$output"
      return 0
    fi

    printf '%s\n' "$output" >&2
    if (( SECONDS >= deadline )); then
      echo "Factory Docker smoke did not pass within ${smoke_timeout}s" >&2
      return 1
    fi

    echo "Factory Docker smoke is not ready; retrying in ${smoke_interval}s" >&2
    sleep "$smoke_interval"
  done
}

if [[ "$(id -u)" != "0" ]]; then
  echo "Factory Docker install requires root" >&2
  exit 1
fi

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

ensure_env_default() {
  local file="$1"
  local key="$2"
  local value="$3"

  if ! grep -q "^${key}=" "$file" 2>/dev/null; then
    upsert_env "$file" "$key" "$value"
  fi
}

for source in "$factory_cli_source" "$factory_hatchet_doctor_source" "$factory_hatchet_desired_state_source" "$hermes_gateway_source" "$hermes_gateway_proto_source" "$verifier_policy_source" "$script_dir/systemd/driftward-factory.service" "$script_dir/systemd/driftward-hermes-factory-gateway.service"; do
  if [[ ! -f "$source" ]]; then
    echo "missing Factory install source: $source" >&2
    exit 1
  fi
done
if [[ ! -x "$node_bin" ]]; then
  echo "Hermes Factory gateway requires an executable Node.js binary at $node_bin" >&2
  exit 1
fi
node_major="$("$node_bin" -p 'Number(process.versions.node.split(".")[0])' 2>/dev/null || true)"
if [[ ! "$node_major" =~ ^[0-9]+$ ]] || (( node_major < 18 )); then
  echo "Hermes Factory gateway requires Node.js 18 or newer at $node_bin" >&2
  exit 1
fi
if ! command -v npm >/dev/null 2>&1; then
  echo "Hermes Factory gateway bundle requires npm on the host" >&2
  exit 1
fi

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
ensure_env_file "$bridge_executor_env_file"
ensure_env_default "$bridge_executor_env_file" DRIFTWARD_HERMES_GRPC_TARGET "${DRIFTWARD_HERMES_GRPC_TARGET:-unix:///run/driftward-hermes-factory/hermes.sock}"
if [[ ! -e /etc/driftward/factory-lifecycle-writer.env ]]; then
  install -m 0600 -o root -g root /dev/null /etc/driftward/factory-lifecycle-writer.env
else
  chown root:root /etc/driftward/factory-lifecycle-writer.env
  chmod 0600 /etc/driftward/factory-lifecycle-writer.env
fi
install -d -m 0755 -o root -g root /run/driftward-factory-operator
install -d -m 0755 -o root -g root /run/driftward-hermes-factory

install -m 0644 "$script_dir/docker-compose.yml" "$runtime_root/docker-compose.yml"
install -D -m 0755 "$script_dir/factory-docker-smoke" /usr/local/bin/factory-docker-smoke
install -D -m 0755 "$factory_cli_source" "$factory_cli_target"
install -d -m 0755 -o root -g root "$factory_share_dir/desired-state"
install -d -m 0755 -o root -g root "$factory_share_dir/hermes-gateway"
install -m 0755 "$factory_hatchet_doctor_source" "$factory_hatchet_doctor_target"
install -m 0644 "$factory_hatchet_desired_state_source" "$factory_hatchet_desired_state_target"
install -m 0644 "$hermes_gateway_proto_source" "$hermes_gateway_proto_target"
install -m 0644 "$verifier_policy_source" "$verifier_policy_target"

npm ci --prefix "$repo_root/hatchet/workers" --ignore-scripts >/dev/null
npm exec --prefix "$repo_root/hatchet/workers" -- esbuild \
  "$hermes_gateway_source" \
  --bundle \
  --platform=node \
  --format=cjs \
  --target=node18 \
  --line-limit=120 \
  --legal-comments=none \
  --outfile="$hermes_gateway_bundle_target" >/dev/null

tmp_unit="$(mktemp)"
sed \
  -e "s#__RUNTIME_ROOT__#$runtime_root#g" \
  -e "s#__SOURCE_ROOT__#$repo_root#g" \
  "$script_dir/systemd/driftward-factory.service" >"$tmp_unit"
install -D -m 0644 "$tmp_unit" "$service_unit"
rm -f "$tmp_unit"

tmp_gateway_unit="$(mktemp)"
sed \
  -e "s#__SOURCE_ROOT__#$repo_root#g" \
  -e "s#__FACTORY_SHARE_DIR__#$factory_share_dir#g" \
  -e "s#__NODE_BIN__#$node_bin#g" \
  "$script_dir/systemd/driftward-hermes-factory-gateway.service" >"$tmp_gateway_unit"
install -D -m 0644 "$tmp_gateway_unit" "$hermes_gateway_unit"
rm -f "$tmp_gateway_unit"

install -d -m 0755 -o root -g root "$factory_image_dropin_dir"
tmp_dropin="$(mktemp)"
if [[ -n "$factory_image_override" ]]; then
  printf '[Service]\nEnvironment=FACTORY_IMAGE=%s\n' "$factory_image_override" >"$tmp_dropin"
else
  printf '[Service]\nEnvironment=FACTORY_IMAGE_TAG=%s\n' "$factory_image_tag" >"$tmp_dropin"
fi
install -D -m 0644 "$tmp_dropin" "$factory_image_dropin"
rm -f "$tmp_dropin"

compose_args=(--project-directory "$runtime_root")
if [[ -r "$hatchet_env_file" ]]; then
  compose_args=(--env-file "$hatchet_env_file" "${compose_args[@]}")
fi

compose_env=(env -u FACTORY_IMAGE -u FACTORY_IMAGE_TAG FACTORY_SOURCE_ROOT="$repo_root")
if [[ -n "$factory_image_override" ]]; then
  compose_env+=(FACTORY_IMAGE="$factory_image_override")
else
  compose_env+=(FACTORY_IMAGE_TAG="$factory_image_tag")
fi
"${compose_env[@]}" docker compose "${compose_args[@]}" config >/dev/null
systemd-analyze verify "$service_unit" "$hermes_gateway_unit"
systemctl daemon-reload

echo "Factory Docker runtime installed: $runtime_root"
echo "Factory systemd unit installed: $service_unit"
echo "Hermes Factory gateway systemd unit installed: $hermes_gateway_unit"
echo "Factory image pinned: $factory_image"
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
  systemctl enable "$hermes_gateway_service_name"
  systemctl restart "$hermes_gateway_service_name"
  systemctl enable "$service_name"
  systemctl restart "$service_name"
  wait_for_factory_smoke
else
  echo "Start the Hermes Factory gateway with: systemctl enable --now $hermes_gateway_service_name"
  echo "Start after the selected image exists with: systemctl enable --now $service_name"
  echo "Smoke with: factory-docker-smoke $runtime_root"
fi
