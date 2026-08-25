#!/usr/bin/env bash
# The complete, fixed set of bridge-installer publication targets.  This file
# is sourced by the installer, recovery proof and their harnesses so a new
# target cannot be published without being included in rollback/proof.

publication_inventory="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/publication-targets.json"
mapfile -t publication_units < <(python3 - "$publication_inventory" <<'PY'
import json,sys
for value in json.load(open(sys.argv[1]))["units"]: print(value)
PY
)

# Files that can be read, verified, copied or installed as part of a bridge
# publication.  Secret files are listed because their pre-attempt existence
# and metadata are evidence, but this installer never copies their contents.
mapfile -t publication_targets < <(python3 - "$publication_inventory" <<'PY'
import json,sys
for value in json.load(open(sys.argv[1]))["targets"]: print(value)
PY
)
mapfile -t publication_directories < <(python3 - "$publication_inventory" <<'PY'
import json,sys
for value in json.load(open(sys.argv[1]))["directories"]: print(value)
PY
)
mapfile -t publication_transients < <(python3 - "$publication_inventory" <<'PY'
import json,sys
for value in json.load(open(sys.argv[1]))["transients"]: print(value)
PY
)

# The production inventory stays absolute.  A hermetic test can only remap it
# after the inherited capability has been validated by its caller.
if [[ "${BRIDGE_TEST_MODE:-}" == 1 ]]; then
  for index in "${!publication_targets[@]}"; do publication_targets[index]="$BRIDGE_TEST_ROOT${publication_targets[index]}"; done
  for index in "${!publication_directories[@]}"; do publication_directories[index]="$BRIDGE_TEST_ROOT${publication_directories[index]}"; done
  for index in "${!publication_transients[@]}"; do publication_transients[index]="$BRIDGE_TEST_ROOT${publication_transients[index]}"; done
fi
for publication_unit in "${publication_units[@]}"; do
  publication_targets+=("/etc/systemd/system/$publication_unit")
done

publication_target_paths() { printf '%s\n' "${publication_targets[@]}"; }
