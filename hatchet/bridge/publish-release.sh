#!/usr/bin/env bash
set -euo pipefail

# Publish only a committed tree.  This command deliberately refuses a dirty
# relevant checkout: a release must always be reproducible from its SHA.
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 "$script_dir/test_capability.py" guard
repo_root="$(cd "$script_dir/../.." && pwd)"
release_root="/docker/driftward/hatchet/releases/bridge"
if test_shell="$(python3 "$script_dir/test_capability.py" maybe-shell)"; then
  eval "$test_shell"
  repo_root="$BRIDGE_TEST_REPOSITORY"
  release_root="$BRIDGE_TEST_ROOT/docker/driftward/hatchet/releases/bridge"
fi
release_version="${DRIFTWARD_BRIDGE_RELEASE_VERSION:-$(git -C "$repo_root" rev-parse HEAD)}"
stage_root="${HATCHET_BRIDGE_STAGE_ROOT:-}"
stage_only=0
[[ "${1:-}" == "--stage-only" ]] && stage_only=1
verify_existing=0
[[ "${1:-}" == "--verify-existing" ]] && verify_existing=1
[[ "$release_version" =~ ^[0-9a-f]{40}$ ]] || { echo "bridge release version must be a commit SHA" >&2; exit 1; }
if [[ "${BRIDGE_TEST_MODE:-}" != 1 ]]; then
  git -C "$repo_root" cat-file -e "$release_version^{commit}" || { echo "bridge release commit is unavailable" >&2; exit 1; }
  relevant=(hatchet/bridge hatchet/workers/bridge-executor-worker hatchet/workers/package.json hatchet/workers/package-lock.json hatchet/systemd)
  if [[ -n "$(git -C "$repo_root" status --porcelain -- "${relevant[@]}")" ]]; then
    echo "refusing production release from dirty relevant source" >&2; exit 1
  fi
fi

if (( verify_existing )); then
  target="$release_root/$release_version"
  [[ -d "$target" ]] || { echo "release target is absent or not a directory" >&2; exit 1; }
  python3 "$script_dir/release_hygiene.py" scan --tree "$target"
  python3 "$script_dir/release_hygiene.py" verify --tree "$target" --commit "$release_version" --source-tree "$(git -C "$repo_root" rev-parse "$release_version^{tree}")"
  printf '%s\n' "$target"; exit 0
fi

if [[ -n "$stage_root" ]]; then
  stage="$stage_root/release"
  mkdir -p "$stage"
else
  stage="$(mktemp -d "${TMPDIR:-/var/tmp}/driftward-bridge-release.${release_version}.XXXXXX")"
fi
cleanup=1
trap '[[ "$cleanup" == 1 ]] && rm -rf "$stage"' EXIT
source_root="$stage/source"
mkdir -p "$source_root"
# The subprocess recovery harness has already authenticated a private
# capability and copied a checked-in dispatcher build into it.  Its release
# fixture needs the same minimal immutable payload the production archive
# produces, not a second archive/scan of the entire worker source tree for
# every SIGKILL point.  Root production never accepts this branch.
if [[ "${BRIDGE_TEST_MODE:-}" == 1 ]]; then
  install -d -m 0755 "$stage/hatchet_hermes_bridge" "$stage/worker-triggers"
  install -m 0644 "$repo_root/hatchet/bridge/hatchet_hermes_bridge/"*.py "$stage/hatchet_hermes_bridge/"
  install -m 0644 "$repo_root/hatchet/bridge/test_capability.py" "$stage/test_capability.py"
  install -m 0644 "$repo_root/hatchet/bridge/README.md" "$stage/README.md"
  install -m 0644 "$repo_root/hatchet/bridge/predeploy-check.py" "$stage/predeploy-check.py"
  install -m 0644 "$repo_root/hatchet/workers/verifier-policy.json" "$stage/verifier-policy.json"
  install -m 0644 "$repo_root/hatchet/workers/independent-review-policy.json" "$stage/independent-review-policy.json"
  install -m 0644 "$BRIDGE_TEST_PREBUILT_TRIGGER" "$stage/worker-triggers/push-accepted-event.js"
else
  git -C "$repo_root" archive --format=tar "$release_version" -- hatchet/bridge hatchet/workers | tar -xf - -C "$source_root"
  # Test fixtures are never runtime/build inputs for the immutable bridge
  # release. Remove only the known test locations before scanning the committed
  # production archive; generated runtime output is scanned again after build.
  rm -rf "$source_root/hatchet/bridge/tests"
  find "$source_root/hatchet/workers" -type f -name '*.test.ts' -delete
  python3 "$script_dir/release_hygiene.py" scan --tree "$source_root"
  npm ci --prefix "$source_root/hatchet/workers" --ignore-scripts >/dev/null
  npm run build --prefix "$source_root/hatchet/workers" >/dev/null
  install -d -m 0755 "$stage/hatchet_hermes_bridge" "$stage/worker-triggers"
  install -m 0644 "$source_root/hatchet/bridge"/hatchet_hermes_bridge/*.py "$stage/hatchet_hermes_bridge/"
  install -m 0644 "$source_root/hatchet/bridge/test_capability.py" "$stage/test_capability.py"
  install -m 0644 "$source_root/hatchet/bridge/README.md" "$stage/README.md"
  install -m 0644 "$source_root/hatchet/bridge/predeploy-check.py" "$stage/predeploy-check.py"
  install -m 0644 "$source_root/hatchet/workers/verifier-policy.json" "$stage/verifier-policy.json"
  install -m 0644 "$source_root/hatchet/workers/independent-review-policy.json" "$stage/independent-review-policy.json"
  npm exec --prefix "$source_root/hatchet/workers" -- esbuild \
    "$source_root/hatchet/workers/bridge-executor-worker/src/triggers/push-accepted-event.ts" \
    --bundle \
    --platform=node \
    --format=cjs \
    --target=node18 \
    --line-limit=120 \
    --legal-comments=none \
    --outfile="$stage/worker-triggers/push-accepted-event.js" >/dev/null
fi
rm -rf "$source_root"
PYTHONPATH="$stage" python3 -B - "$stage" <<'PY'
import sys
from pathlib import Path
root = Path(sys.argv[1])
for path in sorted((root / "hatchet_hermes_bridge").glob("*.py")):
    compile(path.read_text(encoding="utf-8"), str(path), "exec", dont_inherit=True)
compile((root / "predeploy-check.py").read_text(encoding="utf-8"), str(root / "predeploy-check.py"), "exec", dont_inherit=True)
PY
find "$stage" -type f -exec chmod 0644 {} +
find "$stage" -type d -exec chmod 0755 {} +
if [[ "${BRIDGE_TEST_MODE:-}" == 1 ]]; then tree_sha="$release_version"; else tree_sha="$(git -C "$repo_root" rev-parse "$release_version^{tree}")"; fi
python3 "$script_dir/release_hygiene.py" validate --tree "$stage"
python3 "$script_dir/release_hygiene.py" scan --tree "$stage"
python3 "$script_dir/release_hygiene.py" write-manifest --tree "$stage" --commit "$release_version" --source-tree "$tree_sha"
chmod 0644 "$stage"/MANIFEST.json "$stage"/RELEASE_METADATA.json

verify_release() {
  python3 "$script_dir/release_hygiene.py" verify --tree "$1" --commit "$release_version" --source-tree "$tree_sha"
}
verify_release "$stage" || { echo "staged release manifest verification failed" >&2; exit 1; }
if (( stage_only )); then
  cleanup=0; printf '%s\n' "$stage"; exit 0
fi
install -d -m 0755 -o root -g root "$release_root"
target="$release_root/$release_version"
if [[ -e "$target" ]]; then
  [[ -d "$target" ]] && verify_release "$target" || { echo "existing release fails immutable manifest verification" >&2; exit 1; }
else
  chown -R root:root "$stage"; mv "$stage" "$target"; cleanup=0
fi
ln -s "$target" "$release_root/.current.new"
mv -Tf "$release_root/.current.new" "$release_root/current"
sync -d "$release_root" 2>/dev/null || true
printf '%s\n' "$target"
