"""Explicit synthetic deployment configuration for runtime tests."""
import os
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / 'hatchet/workers/test-fixtures'


def configure():
    context = patch.dict(os.environ, {
        'FACTORY_ORGANIZATION_CONFIG': str(FIXTURES / 'organization.json'),
        'HATCHET_INTAKE_ROUTING_REGISTRY_PATH': str(FIXTURES / 'routing-registry.json'),
        'HATCHET_VERIFIER_POLICY_PATH': str(FIXTURES / 'verifier-policy.json'),
    })
    return context.start, lambda: context.stop()
