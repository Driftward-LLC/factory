import tempfile
import unittest
from datetime import timedelta

from hatchet_hermes_bridge.reconciler import GitHubBranch, GitHubPullRequest, reconcile_jobs
from hatchet_hermes_bridge.store import get_job, list_events, submit_job, transition_job
from test_support import connect_migrate as connect


class FakeGitHubInspector:
    def __init__(self, *, pr=None, branch=None):
        self.pr = pr
        self.branch = branch

    def find_pr_for_branch(self, repo, branch):
        return self.pr

    def find_branch(self, repo, branch):
        return self.branch


class BridgeReconcilerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile()
        self.conn = connect(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        self.tmp.close()

    def test_running_job_stops_at_verification_pending_when_pr_exists(self):
        job = self._running_job("hatchet:run-1:create-pr")
        inspector = FakeGitHubInspector(
            pr=GitHubPullRequest(
                number=99,
                url="https://github.com/Driftward-LLC/driftward-host/pull/99",
                state="OPEN",
                is_draft=True,
                head_ref_name=job["branch"],
            ),
            branch=GitHubBranch(name=job["branch"], commit_sha="abc123"),
        )

        results = reconcile_jobs(
            self.conn,
            inspector=inspector,
            stale_after=timedelta(seconds=0),
        )

        self.assertEqual(results[0]["action"], "verification_pending")
        pending = get_job(self.conn, job["job_id"])
        self.assertEqual(pending["state"], "verification_pending")
        self.assertEqual(pending["commit_sha"], "abc123")
        self.assertEqual(pending["pr_url"], "https://github.com/Driftward-LLC/driftward-host/pull/99")
        self.assertEqual(
            [event["event_type"] for event in list_events(self.conn, job["job_id"])],
            ["accepted", "transition:running", "transition:effect_committed", "transition:verification_pending"],
        )

    def test_effect_committed_job_never_promotes_to_completed_without_verification(self):
        job = self._running_job("hatchet:run-pending:create-pr")
        committed = transition_job(
            self.conn,
            job["job_id"],
            state="effect_committed",
            details={"commit_sha": "abc123", "pr_url": "https://github.com/Driftward-LLC/driftward-host/pull/99"},
        )

        results = reconcile_jobs(self.conn, inspector=FakeGitHubInspector(), stale_after=timedelta(seconds=0))

        self.assertEqual(results[0]["action"], "verification_pending")
        self.assertEqual(get_job(self.conn, committed["job_id"])["state"], "verification_pending")

    def test_running_job_records_branch_only_side_effect_without_completing(self):
        job = self._running_job("hatchet:run-2:create-pr")
        inspector = FakeGitHubInspector(
            branch=GitHubBranch(name=job["branch"], commit_sha="branchsha"),
        )

        results = reconcile_jobs(
            self.conn,
            inspector=inspector,
            stale_after=timedelta(seconds=0),
        )

        self.assertEqual(results[0]["action"], "effect_committed")
        committed = get_job(self.conn, job["job_id"])
        self.assertEqual(committed["state"], "effect_committed")
        self.assertEqual(committed["commit_sha"], "branchsha")
        self.assertIsNone(committed["pr_url"])
        self.assertEqual(committed["reason_code"], "reconciled_branch_found_without_pr")

    def test_running_job_fails_when_no_side_effect_exists_after_timeout(self):
        job = self._running_job("hatchet:run-3:create-pr")
        inspector = FakeGitHubInspector()

        results = reconcile_jobs(
            self.conn,
            inspector=inspector,
            stale_after=timedelta(seconds=0),
        )

        self.assertEqual(results[0]["action"], "failed")
        failed = get_job(self.conn, job["job_id"])
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["reason_code"], "reconciled_no_side_effect_after_timeout")

    def test_not_stale_job_is_skipped(self):
        job = self._running_job("hatchet:run-4:create-pr")

        results = reconcile_jobs(
            self.conn,
            inspector=FakeGitHubInspector(),
            stale_after=timedelta(days=1),
        )

        self.assertEqual(results[0]["action"], "skipped")
        self.assertEqual(results[0]["reason_code"], "not_stale")
        self.assertEqual(get_job(self.conn, job["job_id"])["state"], "running")

    def _running_job(self, key):
        job, _ = submit_job(
            self.conn,
            {
                "idempotency_key": key,
                "requested_action": "create_draft_pr",
                "payload": {"repo": "Driftward-LLC/driftward-host"},
            },
        )
        return transition_job(self.conn, job["job_id"], state="running")


if __name__ == "__main__":
    unittest.main()
