#!/usr/bin/env bash
set -euo pipefail

# Historical source rollouts cannot prove compatibility across naming generations.
# Refuse before locks, publication, service control, or state mutation.
printf '%s\n' 'Source installation is disabled for this naming generation. Use a fresh portable installation; existing state requires a separately reviewed migration. See deploy/NAMING-MIGRATION.md.' >&2
exit 64
