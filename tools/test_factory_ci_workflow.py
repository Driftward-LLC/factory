"""Keep CI savings scoped to redundant PR runs and dependency downloads."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class QualityWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = (ROOT / '.github/workflows/hatchet-quality.yml').read_text()

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
            job = self.workflow.split(f'\n  {name}:\n', 1)[1].split('\n  validate:\n', 1)[0]
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
        catalog = self.workflow.split('      - name: Upload shared component catalog\n', 1)[1].split('      - name:', 1)[0]
        screenshots = self.workflow.split('      - name: Upload fixture screenshots\n', 1)[1].split('\n  validate:', 1)[0]
        for step in [catalog, screenshots]:
            self.assertIn('uses: actions/upload-artifact@v4\n', step)
            self.assertIn('          retention-days: 7\n', step)
        self.assertIn('        if: always()\n', screenshots)
        for path in ['factory-dashboard', 'factory-control', 'factory-storybook']:
            self.assertIn(f'/tmp/{path}-*.png', screenshots)
        self.assertIn('path: factory/console/storybook-static/\n', catalog)


if __name__ == '__main__':
    unittest.main()
