"""Keep cheap checks first and every quality/evidence/release gate blocking."""
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import re
import tempfile
import textwrap
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class QualityWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = (ROOT / '.github/workflows/hatchet-quality.yml').read_text()
        # Read only the indentation-delimited job/step blocks used by these
        # contracts; the tool suite must not require a YAML or Node installation.
        cls.jobs = dict(re.findall(r'^  ([a-z-]+):\n(.*?)(?=^  [a-z-]+:\n|\Z)',
                                  cls.workflow.split('\njobs:\n', 1)[1], re.M | re.S))

    def step(self, job, name):
        return self.jobs[job].split(f'      - name: {name}\n', 1)[1].split('      - ', 1)[0]

    def test_cheap_preflight_gates_parallel_code_and_ui_then_release(self):
        expected = {
            'preflight': [], 'validate': ['preflight'], 'console-browser': ['preflight'],
            'required-evidence': ['console-browser'],
            'release-readiness': ['preflight', 'validate', 'console-browser', 'required-evidence'],
        }
        self.assertEqual(set(self.jobs), set(expected))
        for name, dependencies in expected.items():
            job = self.jobs[name]
            found = re.search(r'^    needs: \[([^\]]*)\]$', job, re.M)
            self.assertEqual(found.group(1).split(', ') if found else [], dependencies)
            if name == 'required-evidence':
                self.assertIn('    if: ${{ always() }}\n', job)
            else:
                # GitHub's default success() requires every dependency to pass;
                # no always()/custom condition may bypass this gate for releases.
                self.assertNotRegex(job, re.compile(r'^    if:', re.M), name)
            self.assertNotRegex(job, re.compile(r'^    continue-on-error:', re.M), name)
        preflight = self.jobs['preflight']
        self.assertIn('fetch-depth: 0', preflight)
        self.assertIn('git diff --check "$base" HEAD', preflight)
        self.assertIn('GITLEAKS_VERSION: 8.30.1', preflight)
        self.assertIn('551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb', preflight)
        self.assertIn('sha256sum --check -', preflight)
        self.assertIn('/tmp/gitleaks git --redact --verbose .', preflight)
        self.assertNotIn('setup-node', preflight)
        self.assertNotIn('docker build', preflight)

    def evidence(self, **changes):
        step = self.step('required-evidence', 'Require both browser evidence artifacts')
        script = textwrap.dedent(step.split('        run: |\n', 1)[1])
        self.assertTrue(script.startswith("python3 - <<'PY'\n"))
        script = script.split('\n', 1)[1].rsplit('\nPY', 1)[0]
        values = dict(BROWSER_RESULT='success', CATALOG_OUTCOME='success', CATALOG_ARTIFACT_ID='123',
                      SCREENSHOTS_OUTCOME='success', SCREENSHOTS_ARTIFACT_ID='456')
        values.update(changes)
        with tempfile.TemporaryDirectory() as directory:
            summary = Path(directory) / 'summary'
            with patch.dict(os.environ, {**values, 'GITHUB_STEP_SUMMARY': str(summary)}, clear=True), redirect_stdout(io.StringIO()):
                try:
                    exec(compile(script, 'required-evidence', 'exec'), {})
                    success = True
                except SystemExit:
                    success = False
            return success, summary.read_text()

    def test_evidence_executes_actual_gate_and_fails_closed(self):
        self.assertTrue(self.evidence()[0])
        for field in ['CATALOG_OUTCOME', 'SCREENSHOTS_OUTCOME', 'BROWSER_RESULT']:
            for result in ['failure', 'skipped', 'cancelled', '', 'neutral']:
                with self.subTest(field=field, result=result):
                    self.assertFalse(self.evidence(**{field: result})[0])
        for field in ['CATALOG_ARTIFACT_ID', 'SCREENSHOTS_ARTIFACT_ID']:
            for value in ['', '0', '-1', 'not-an-id', '123\n', 'raw-output\n| injected |']:
                with self.subTest(field=field, value=value):
                    passed, summary = self.evidence(**{field: value})
                    self.assertFalse(passed)
                    self.assertNotIn('raw-output', summary)

    def test_upload_failure_has_own_red_gate_while_ui_checks_can_pass(self):
        passed, summary = self.evidence(CATALOG_OUTCOME='failure', CATALOG_ARTIFACT_ID='')
        self.assertFalse(passed)
        self.assertIn('| Browser and UI checks | success |', summary)
        self.assertIn('| Required catalog evidence | missing or upload failed |', summary)
        self.assertIn('| Required screenshots evidence | published |', summary)
        self.assertIn('Release readiness is blocked', summary)
        self.assertIn('Retry all jobs', summary)
        browser = self.jobs['console-browser']
        self.assertEqual(self.workflow.count('continue-on-error:'), 2)
        for name, identifier in [('Upload shared component catalog', 'catalog'), ('Upload fixture screenshots', 'screenshots')]:
            upload = self.step('console-browser', name)
            self.assertIn(f'        id: {identifier}\n', upload)
            self.assertIn('        continue-on-error: true\n', upload)
            self.assertIn(f'${{{{ steps.{identifier}.outcome }}}}', browser)
            self.assertIn(f'${{{{ steps.{identifier}.outputs.artifact-id }}}}', browser)
            self.assertNotIn(f'${{{{ steps.{identifier}.conclusion }}}}', browser)
        gate = self.jobs['required-evidence']
        for name in ['catalog', 'screenshots']:
            for field in ['outcome', 'artifact-id']:
                self.assertIn(f'${{{{ needs.console-browser.outputs.{name}-{field} }}}}', gate)
        self.assertIn('${{ needs.console-browser.result }}', gate)

    def test_release_keeps_exact_image_rehearsals_and_main_only_publisher(self):
        release = self.jobs['release-readiness']
        names = ['Verify production package read-only for internal pull requests', 'Validate Docker deployment',
                 'Prove isolated fresh install, restart, and restore', 'Rehearse stopped production-state relocation',
                 'Authenticate private production image publication', 'Publish the tested production image and package metadata',
                 'Retain immutable production image metadata']
        self.assertEqual(sorted(names, key=release.index), names)
        for command in ['docker compose -f deploy/docker-compose.yml config >/dev/null',
                        'FACTORY_RELEASE_COMMIT="$(git rev-parse HEAD)"', 'FACTORY_RELEASE_VERSION=ci',
                        '-f deploy/Dockerfile -t driftward/factory:ci .',
                        'python3 tools/check_factory_portable.py --image driftward/factory:ci --telemetry',
                        'python3 tools/check_factory_cutover.py --image driftward/factory:ci',
                        'python3 deploy/auto_deploy_artifact.py "$RUNNER_TEMP/factory-production" driftward/factory:ci']:
            self.assertIn(command, release)
        self.assertEqual(self.workflow.count('packages: write'), 1)
        self.assertIn('      packages: write\n', release)
        self.assertNotIn('permissions:', self.jobs['validate'])
        self.assertNotIn('download-artifact', self.workflow)
        self.assertNotIn('continue-on-error:', release)
        for name in names[-3:-1]:
            step = self.step('release-readiness', name)
            self.assertIn("if: github.event_name == 'push' && github.ref == 'refs/heads/main' && github.repository == 'Driftward-LLC/factory'", step)
        metadata = self.step('release-readiness', names[-1])
        self.assertIn("if: github.event_name == 'push' && github.ref == 'refs/heads/main'", metadata)
        self.assertIn('name: factory-production-${{ github.sha }}', metadata)
        self.assertIn('if-no-files-found: error', metadata)
        self.assertIn('retention-days: 7', metadata)
        verification = self.step('release-readiness', names[0])
        self.assertIn("github.event.pull_request.head.repo.full_name == 'Driftward-LLC/factory'", verification)
        self.assertIn('python3 deploy/auto_deploy_artifact.py --verify-package', verification)

    def test_code_and_browser_assertions_remain_blocking(self):
        for name in ['validate', 'console-browser']:
            checks = self.jobs[name].split('      - name: Upload shared component catalog', 1)[0]
            self.assertNotIn('continue-on-error:', checks)
            self.assertNotRegex(checks, re.compile(r'^        if:', re.M))
            self.assertNotIn('|| true', checks)
        for command in ['tools/hatchet-contracts.py generate', 'tools/hatchet-contracts.py check',
                        'git diff --exit-code -- hatchet/event-ingress/hatchet_event_ingress/contracts.py hatchet/workers/shared/src/contracts.ts',
                        "python3 -m unittest discover -s tools -p 'test_*.py'",
                        'tools.test_factory_cli.CanonicalCliTests.test_operator_round_trip_uses_private_parsed_credentials_and_cleans_up tools.test_factory_operator_cli',
                        'npm run build --prefix hatchet/workers', 'npm run test --prefix hatchet/workers/intake-worker',
                        'npm run test --prefix hatchet/workers/bridge-executor-worker', 'npm run test --prefix hatchet/workers/reconciliation-worker',
                        "discover -s hatchet/bridge/tests", "discover -s hatchet/event-ingress/tests", "discover -s hatchet/factory-lifecycle-writer/tests"]:
            self.assertIn(command, self.jobs['validate'])
        for script in ['test_factory_console_browser.cjs', 'test_factory_issue_control_browser.cjs',
                       'test_factory_issue_control_backend_browser.cjs', 'test_factory_ui_package.cjs', 'test_factory_storybook.cjs']:
            self.assertIn('node tools/' + script, self.jobs['console-browser'])
        for command in ['npm run check:ui --prefix factory/console', 'npm run build-storybook --prefix factory/console']:
            self.assertIn(command, self.jobs['console-browser'])

    def test_deployment_requires_entire_quality_success_and_release_stays_serial(self):
        deploy = (ROOT / '.github/workflows/factory-deploy.yml').read_text()
        self.assertIn('name: Hatchet Quality\n', self.workflow)
        self.assertIn('workflows: [Hatchet Quality]', deploy)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", deploy)
        self.assertIn("github.event.workflow_run.event == 'push'", deploy)
        release = (ROOT / '.github/workflows/factory-release.yml').read_text()
        self.assertIn('  group: factory-release\n  cancel-in-progress: false', release)

    def test_concurrency_is_pr_only_and_main_pushes_have_unique_groups(self):
        concurrency = self.workflow.split('\nconcurrency:\n', 1)[1].split('\njobs:', 1)[0]
        self.assertIn("  group: ${{ github.workflow }}-${{ github.event_name }}-${{ github.event_name == 'pull_request' && github.event.pull_request.number || github.run_id }}\n", concurrency)
        self.assertIn("  cancel-in-progress: ${{ github.event_name == 'pull_request' }}\n", concurrency)
        self.assertNotIn('concurrency:', self.workflow.split('\njobs:', 1)[1])
        push = self.workflow.split('\n  push:\n', 1)[1].split('\npermissions:', 1)[0]
        self.assertIn('branches: [main]', push)
        self.assertNotIn('paths:', push)
        self.assertNotIn('paths-ignore:', push)

    def test_download_cache_keys_cover_each_jobs_installed_dependencies(self):
        for name, lockfiles in [
            ('console-browser', ['factory/console/package-lock.json']),
            ('validate', ['hatchet/workers/package-lock.json', 'factory/console/package-lock.json']),
        ]:
            job = self.jobs[name]
            node = job.split('uses: actions/setup-node@v4\n', 1)[1].split('      - ', 1)[0]
            python = job.split('uses: actions/setup-python@v5\n', 1)[1].split('      - ', 1)[0]
            self.assertIn('          cache: npm\n', node)
            for path in lockfiles:
                self.assertIn(path, node)
            self.assertIn('          cache: pip\n', python)
            self.assertIn('          cache-dependency-path: hatchet/bridge/requirements.txt\n', python)
            for path in [*lockfiles, 'hatchet/bridge/requirements.txt']:
                self.assertTrue((ROOT / path).is_file(), path)
            self.assertIn('npm ci --prefix factory/console', job)
            self.assertIn('pip install --requirement hatchet/bridge/requirements.txt', job)

    def test_routine_evidence_keeps_seven_days_and_screenshots_on_failure(self):
        catalog = self.step('console-browser', 'Upload shared component catalog')
        screenshots = self.step('console-browser', 'Upload fixture screenshots')
        for step in [catalog, screenshots]:
            self.assertIn('uses: actions/upload-artifact@v4\n', step)
            self.assertIn('          retention-days: 7\n', step)
            self.assertIn('          if-no-files-found: error\n', step)
        self.assertIn('        if: always()\n', screenshots)
        for path in ['factory-dashboard', 'factory-control', 'factory-storybook']:
            self.assertIn(f'/tmp/{path}-*.png', screenshots)
        self.assertIn('path: factory/console/storybook-static/\n', catalog)


if __name__ == '__main__':
    unittest.main()
