"""Organization settings are inert by default and exact at trust boundaries."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import factory_organization as organization


class OrganizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'organization.json'
        self.value = {'schema': 'factory-organization-v1', 'linear': {
            'workspace_id': '11111111-1111-4111-8111-111111111111',
            'team_id': '22222222-2222-4222-8222-222222222222',
            'team_key': 'ACME', 'workspace_slug': 'example',
            'actor_id': '33333333-3333-4333-8333-333333333333', 'delegate_name': 'Example Executor'},
            'github': {'enabled': True}}
        self.path.write_text(json.dumps(self.value))
        self.env = patch.dict(os.environ, {'FACTORY_ORGANIZATION_CONFIG': str(self.path)})
        self.env.start(); self.addCleanup(self.env.stop)

    def test_second_organization_and_historical_identifier(self):
        self.assertTrue(organization.matches_issue('ACME-42'))
        self.assertFalse(organization.matches_issue('ENG-42'))
        self.assertTrue(organization.ISSUE.fullmatch('ENG-42'))
        self.assertEqual(organization.issue_url('ACME-42'), 'https://linear.app/example/issue/ACME-42')
        self.assertIsNone(organization.issue_url('ENG-42'))
        observation = {'identifier': 'ACME-42', 'teamKey': 'ACME', 'teamId': self.value['linear']['team_id'], 'workspaceId': self.value['linear']['workspace_id']}
        self.assertTrue(organization.matches_observation(observation))
        self.assertFalse(organization.matches_observation({**observation, 'workspaceId': '44444444-4444-4444-8444-444444444444'}))

    def test_missing_configuration_disables_integrations(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(organization.load()['github'], {'enabled': False})
            self.assertFalse(organization.matches_issue('ACME-42'))
            with self.assertRaises(ValueError): organization.linear_identity()

    def test_invalid_configured_file_rejected(self):
        for malformed in ['{', '{}', json.dumps({**self.value, 'github': {'enabled': 'true'}}), json.dumps({**self.value, 'linear': {**self.value['linear'], 'workspace_slug': '../evil'}})]:
            with self.subTest(value=malformed):
                self.path.write_text(malformed)
                with self.assertRaises(ValueError): organization.load()
        self.path.unlink()
        with self.assertRaises(ValueError): organization.load()

    def test_invalid_deployment_policy_fails_startup(self):
        policy = Path(self.temp.name) / 'routing.json'
        policy.write_text(json.dumps({'version': 2, 'routes': [{'project_id': '11111111-1111-4111-8111-111111111111', 'repo': 'example/widgets', 'execution_enabled': 'yes'}]}))
        with patch.dict(os.environ, {'HATCHET_INTAKE_ROUTING_REGISTRY_PATH': str(policy)}):
            with self.assertRaises(ValueError): organization.validate_startup()
        for kind, value in [('verifier', {'version': 1, 'policies': [{'repo': 'example/widgets'}]}), ('review', {'schema': 'factory-independent-review-policy-v1', 'enabled': True, 'automatic_repairs_allowed': False, 'max_passes': 3, 'repositories': ['example/widgets']})]:
            with self.subTest(kind=kind), self.assertRaises(ValueError): organization.validate_policy(value, kind)

    def test_disabled_github_rejects_bridge_real_work_despite_credentials(self):
        from hatchet_hermes_bridge import store
        self.value['github']['enabled'] = False
        self.path.write_text(json.dumps(self.value))
        request = {'idempotency_key': 'fixture', 'requested_action': 'create_draft_pr', 'payload': {"organization_binding": {"workspace_id": "11111111-1111-4111-8111-111111111111", "team_id": "22222222-2222-4222-8222-222222222222", "team_key": "ENG"}, 'repo': 'example/widgets', 'linear_issue': 'ACME-42', 'dry_run': False}}
        with patch.dict(os.environ, {'DRIFTWARD_HERMES_ALLOWED_REPOS': 'example/widgets', 'GITHUB_TOKEN': 'fixture-unusable-token'}):
            with self.assertRaisesRegex(store.BridgeValidationError, 'disabled'): store._validate_submit(request)
        request['payload']['dry_run'] = True
        store._validate_submit(request)

    def test_fresh_bridge_initializes_with_empty_default_routing(self):
        import sqlite3
        from hatchet_hermes_bridge import store
        from hatchet_hermes_bridge.legacy_artifacts import routing_repositories
        with patch.dict(os.environ, {}, clear=True):
            conn = sqlite3.connect(':memory:'); conn.row_factory = sqlite3.Row
            try:
                store.init_db(conn)
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM factory_attempts').fetchone()[0], 0)
                self.assertFalse(store._canonical_enabled_repo('example/widgets'))
            finally:
                conn.close()
        self.assertEqual(routing_repositories({'version': 2, 'routes': []}), (set(), set()))

    def test_routing_identity_uniqueness_and_disabled_only_catalog(self):
        first = {"project_id": "11111111-1111-4111-8111-111111111111", "repo": "example/widgets", "execution_enabled": False}
        second = {"project_id": "22222222-2222-4222-8222-222222222222", "repo": "example/control-plane", "execution_enabled": False}
        organization.validate_policy({"version": 2, "routes": [first, second]}, "routing")
        for duplicate in ({**second, "project_id": first["project_id"]}, {**second, "repo": first["repo"]}, {**second, "repo": "EXAMPLE/Widgets"}):
            with self.subTest(route=duplicate), self.assertRaises(ValueError):
                organization.validate_policy({"version": 2, "routes": [first, duplicate]}, "routing")

    def test_retired_repository_catalog_matches_runtime_constraints(self):
        from hatchet_hermes_bridge.legacy_artifacts import routing_repositories
        route = {"project_id": "11111111-1111-4111-8111-111111111111", "repo": "example/widgets", "execution_enabled": False}
        for retired in ("example/platform", ["bad repo"], ["EXAMPLE/Widgets"], ["example/platform", "EXAMPLE/Platform"], [f"example/repo{i}" for i in range(33)]):
            policy = {"version": 2, "routes": [route], "retired_repositories": retired}
            with self.subTest(retired=retired):
                with self.assertRaises(ValueError): organization.validate_policy(policy, "routing")
                with self.assertRaises(ValueError): routing_repositories(policy)
        policy = {"version": 2, "routes": [route], "retired_repositories": ["example/platform"]}
        organization.validate_policy(policy, "routing")
        self.assertEqual(routing_repositories(policy), ({"example/widgets", "example/platform"}, set()))
