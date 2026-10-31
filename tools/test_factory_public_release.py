"""Published release evidence must bind tested source to the exact public image."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_publisher', ROOT / 'deploy/auto_deploy_artifact.py')
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)
COMMIT = 'a' * 40
IMAGE = 'sha256:' + 'b' * 64
CONFIG = 'sha256:' + 'c' * 64
VERSION = 'v2026.10.29'
REFERENCE = publisher.RELEASE_IMAGE + '@' + IMAGE


class PublicReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / 'release'
        self.environment = {'GITHUB_REPOSITORY': 'Driftward-LLC/factory',
                            'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_REF': 'refs/heads/main',
                            'GITHUB_SHA': COMMIT, 'GITHUB_RUN_ID': '34', 'GITHUB_RUN_ATTEMPT': '2'}
        self.package = {'name': 'factory', 'package_type': 'container', 'visibility': 'public'}
        self.info = {'Id': CONFIG, 'Architecture': 'amd64', 'Os': 'linux', 'Config': {'Labels': {
            'org.opencontainers.image.revision': COMMIT,
            'org.opencontainers.image.version': VERSION,
            'org.opencontainers.image.source': 'https://github.com/Driftward-LLC/factory'}}}
        self.manifest = {'schemaVersion': 2, 'config': {'digest': CONFIG}}
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, self.environment, clear=True).start()
        self.commands = patch.object(publisher.subprocess, 'check_output', side_effect=self.command).start()
        self.api = patch.object(publisher, 'release_api', return_value=self.package).start()
        patch.object(publisher, 'compatibility', return_value='d' * 64).start()

    def command(self, args, **kwargs):
        if args[0] == 'git':
            return COMMIT
        if args[:3] == ['docker', 'image', 'inspect']:
            return json.dumps([self.info])
        self.assertEqual(args, ['docker', 'manifest', 'inspect', REFERENCE])
        return json.dumps(self.manifest)

    def publish_metadata(self, version=VERSION, reference=REFERENCE):
        publisher.release_metadata(ROOT, self.output, 'tested:ci', version, reference)

    def test_metadata_binds_version_digest_compatibility_and_exact_producer(self):
        for local_id in (CONFIG, IMAGE):
            with self.subTest(local_id=local_id):
                self.info['Id'] = local_id
                self.publish_metadata()
                value = json.loads((self.output / 'deployment.json').read_text())
                self.assertEqual(value, {'format': 3, 'release_tag': VERSION, 'commit': COMMIT,
                    'image': REFERENCE, 'image_config': CONFIG, 'compatibility': 'd' * 64,
                    'run_id': 34, 'run_attempt': 2})
                with self.assertRaises(FileExistsError):
                    self.publish_metadata()
                (self.output / 'deployment.json').unlink()

    def test_private_or_foreign_package_is_refused_before_metadata(self):
        for change in ({'visibility': 'private'}, {'visibility': 'internal'}, {'name': 'foreign'},
                       {'package_type': 'npm'}, {'owner': {'login': 'another'}},
                       {'repository': {'full_name': 'another/repo'}}, {'repository': 'invalid'}):
            with self.subTest(change=change):
                self.api.return_value = self.package | change
                with self.assertRaises(RuntimeError):
                    self.publish_metadata()
                self.assertFalse(self.output.exists())

    def test_local_parent_index_is_not_used_to_identify_the_tested_platform(self):
        command = self.command

        def indexed_image(args, **kwargs):
            if args == ['docker', 'image', 'inspect', 'tested:ci']:
                return json.dumps([self.info | {'Id': 'sha256:' + 'e' * 64}])
            if args[:3] == ['docker', 'image', 'inspect']:
                self.assertEqual(args[3:], ['--platform', 'linux/amd64', 'tested:ci'])
            return command(args, **kwargs)

        self.commands.side_effect = indexed_image
        self.publish_metadata()
        self.assertEqual(json.loads((self.output / 'deployment.json').read_text())['image_config'], CONFIG)

    def test_workflow_resolves_the_published_manifest_instead_of_local_repo_digests(self):
        workflow = (ROOT / '.github/workflows/factory-release.yml').read_text()
        publication = workflow.split('      - name: Publish validated runtime image\n', 1)[1]
        selection = publication.split('          docker push --platform linux/amd64 "$image_tag"\n', 1)[1]
        selection = selection.split('          python3 deploy/auto_deploy_artifact.py', 1)[0]
        fake_docker = Path(self.temp.name) / 'docker'
        fake_docker.write_text('#!/bin/sh\n'
            'if [ "$1 $2 $3" = "manifest inspect --verbose" ]; then\n'
            '  printf \'%s\\n\' "$REGISTRY_MANIFEST"\n'
            'else\n'
            '  echo ghcr.io/driftward-llc/factory@sha256:' + 'e' * 64 + '\n'
            'fi\n')
        fake_docker.chmod(0o755)
        env = {'PATH': self.temp.name + ':/usr/bin:/bin', 'IMAGE_REPOSITORY': publisher.RELEASE_IMAGE,
               'image_tag': publisher.RELEASE_IMAGE + ':' + VERSION}
        script = 'set -euo pipefail\n' + selection + '\nprintf "%s" "$image_ref"\n'
        for descriptor, expected in (({'Descriptor': {'digest': IMAGE}}, REFERENCE),
                                     ({'Descriptor': {'digest': 'invalid'}}, None),
                                     ({}, None), ([{'Descriptor': {'digest': IMAGE}}], None)):
            with self.subTest(descriptor=descriptor):
                # Use the real subprocess entry point: the publisher mocks only check_output.
                result = subprocess.run(['bash', '-c', script], env=env | {
                    'REGISTRY_MANIFEST': json.dumps(descriptor)}, capture_output=True, text=True)
                if expected:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, expected)
                else:
                    self.assertNotEqual(result.returncode, 0)

    def test_wrong_event_branch_checkout_or_run_identity_cannot_publish(self):
        for key, value in [('GITHUB_REPOSITORY', 'another/repo'), ('GITHUB_EVENT_NAME', 'pull_request'),
                           ('GITHUB_REF', 'refs/heads/feature'), ('GITHUB_SHA', 'e' * 40),
                           ('GITHUB_RUN_ID', '0'), ('GITHUB_RUN_ATTEMPT', '-1')]:
            with self.subTest(key=key), patch.dict(os.environ, {key: value}):
                with self.assertRaises(RuntimeError):
                    self.publish_metadata()
                self.assertFalse(self.output.exists())
        for version, reference in [('latest', REFERENCE), (VERSION, publisher.RELEASE_IMAGE + ':latest'),
                                   (VERSION, 'ghcr.io/foreign/factory@' + IMAGE)]:
            with self.subTest(version=version, reference=reference), self.assertRaises(RuntimeError):
                self.publish_metadata(version, reference)

    def test_changed_image_or_multi_platform_manifest_is_rejected(self):
        original = copy.deepcopy(self.info)
        for field, value in [('Id', 'sha256:' + 'f' * 64), ('Architecture', 'arm64'), ('Os', 'windows')]:
            self.info = original | {field: value}
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                self.publish_metadata()
        for label in original['Config']['Labels']:
            self.info = copy.deepcopy(original)
            self.info['Config']['Labels'][label] = 'wrong'
            with self.subTest(label=label), self.assertRaises(RuntimeError):
                self.publish_metadata()
        self.info = original
        for change in ({'schemaVersion': 1}, {'manifests': []}, {'config': {'digest': 'invalid'}},
                       {'config': {'digest': 'sha256:' + 'f' * 64}}):
            self.manifest = {'schemaVersion': 2, 'config': {'digest': CONFIG}} | change
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                self.publish_metadata()
        self.assertFalse(self.output.exists())

    def test_quality_requires_same_commit_full_success_from_canonical_main_push(self):
        run = {'path': '.github/workflows/hatchet-quality.yml', 'head_sha': COMMIT,
               'head_branch': 'main', 'event': 'push', 'status': 'completed', 'conclusion': 'success',
               'repository': {'full_name': 'Driftward-LLC/factory'},
               'head_repository': {'full_name': 'Driftward-LLC/factory'}}
        self.api.return_value = {'workflow_runs': [run]}
        publisher.release_quality(ROOT)
        for change in ({'head_sha': 'f' * 40}, {'event': 'pull_request'}, {'head_branch': 'feature'},
                       {'status': 'in_progress'}, {'conclusion': 'failure'}, {'conclusion': 'skipped'},
                       {'conclusion': 'cancelled'}, {'path': 'another.yml'},
                       {'head_repository': {'full_name': 'foreign/fork'}}):
            self.api.return_value = {'workflow_runs': [run | change]}
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                publisher.release_quality(ROOT)
        for malformed in ({}, [], {'workflow_runs': []}):
            self.api.return_value = malformed
            with self.assertRaises(RuntimeError):
                publisher.release_quality(ROOT)

    def test_workflow_keeps_release_metadata_as_a_checksummed_release_asset(self):
        workflow = (ROOT / '.github/workflows/factory-release.yml').read_text()
        self.assertIn('sha256sum deployment.json >> SHA256SUMS', workflow)
        self.assertIn('"$RUNNER_TEMP/factory-release/deployment.json"', workflow)
        self.assertIn('docker push --platform linux/amd64', workflow)
        self.assertIn('uses: actions/attest@v4', workflow)
        self.assertIn('id-token: write', workflow)
        self.assertIn('actions: read', workflow)
        self.assertIn('attestations: write', workflow)
        self.assertIn("subject-path: '${{ runner.temp }}/factory-release/*'", workflow)
        self.assertLess(workflow.index('uses: actions/attest@v4'), workflow.index('gh "${args[@]}"'))
        self.assertLess(workflow.index('--verify-public-package'), workflow.index('docker push'))
        self.assertLess(workflow.index('--release "$RUNNER_TEMP/factory-release"'), workflow.index('gh "${args[@]}"'))
        self.assertIn("github.repository == 'Driftward-LLC/factory' && github.ref == 'refs/heads/main'", workflow)
        self.assertNotIn('factory-production', workflow)


if __name__ == '__main__':
    unittest.main()
