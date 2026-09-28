#!/usr/bin/env bash
# Package only public deployment inputs; runtime state and credentials never belong here.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
version=""
image=""
output_dir=""
source_commit=""
validation_only=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --version|--image|--output-dir|--source-commit)
      [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 64; }
      case "$1" in
        --version) version="$2" ;;
        --image) image="$2" ;;
        --output-dir) output_dir="$2" ;;
        --source-commit) source_commit="$2" ;;
      esac
      shift 2
      ;;
    --validation-only) validation_only=true; shift ;;
    *) echo "unknown argument: $1" >&2; exit 64 ;;
  esac
done

[[ "$version" =~ ^v[0-9]{4}\.[0-9]{1,2}\.[0-9]{1,2}(\.[0-9]+)?$ ]] || {
  echo "--version must match vYYYY.M.D or vYYYY.M.D.N" >&2; exit 64;
}
[[ "$image" =~ ^ghcr\.io/driftward-llc/factory@sha256:[a-f0-9]{64}$ ]] || {
  echo "--image must be a digest-pinned ghcr.io/driftward-llc/factory reference" >&2; exit 64;
}
[[ -n "$output_dir" ]] || { echo "--output-dir is required" >&2; exit 64; }
source_commit="${source_commit:-$(git -C "$repo_root" rev-parse HEAD)}"
[[ "$source_commit" =~ ^[a-f0-9]{40}$ ]] || { echo "invalid source commit" >&2; exit 64; }
source_epoch="$(git -C "$repo_root" show -s --format=%ct "$source_commit")"
files=(deploy/docker-compose.yml deploy/compose.portable.yml deploy/factory-portable.py deploy/bootstrap.py deploy/portable_state.py deploy/PORTABLE.md)
for file in "${files[@]}"; do
  [[ -f "$repo_root/$file" && ! -L "$repo_root/$file" ]] || {
    echo "missing or symlinked release input: $file" >&2; exit 66;
  }
done

bundle="factory-$version-linux-amd64"
archive="$bundle"
if [[ "$validation_only" == true ]]; then archive+="-validation-only"; fi
mkdir -p "$output_dir"
output_dir="$(cd "$output_dir" && pwd)"
[[ ! -e "$output_dir/$archive.tar.gz" && ! -e "$output_dir/SHA256SUMS" ]] || {
  echo "refusing to overwrite release artifacts" >&2; exit 73;
}
staging="$(mktemp -d)"
trap 'rm -r -- "$staging"' EXIT
mkdir -p "$staging/$bundle/deploy"
for file in "${files[@]}"; do
  install -m 0644 "$repo_root/$file" "$staging/$bundle/$file"
done
printf '%s\n' \
  'FACTORY_RELEASE_SCHEMA=1' \
  'FACTORY_RELEASE_PLATFORM=linux/amd64' \
  "FACTORY_RELEASE_VALIDATION_ONLY=$validation_only" \
  "FACTORY_IMAGE=$image" \
  "FACTORY_RELEASE_VERSION=$version" \
  "FACTORY_RELEASE_COMMIT=$source_commit" > "$staging/$bundle/factory.env"
tar --sort=name --mtime="@$source_epoch" --owner=0 --group=0 --numeric-owner \
  -C "$staging" -czf "$output_dir/$archive.tar.gz" "$bundle"
(cd "$output_dir" && sha256sum "$archive.tar.gz" > SHA256SUMS)
printf 'Packaged %s\n' "$output_dir/$archive.tar.gz"
if [[ "$validation_only" == true ]]; then
  echo 'Validation-only artifact: image reference is not published; do not install.'
fi
