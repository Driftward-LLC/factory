#!/bin/sh
# Configured application roles validate identity and policy before starting any writer.
set -eu
if [ -n "${FACTORY_ORGANIZATION_CONFIG:-}${HATCHET_INTAKE_ROUTING_REGISTRY_PATH:-}${HATCHET_VERIFIER_POLICY_PATH:-}${HATCHET_INDEPENDENT_REVIEW_POLICY_PATH:-}" ]; then
  python3 -m factory_organization
fi
exec "$@"
