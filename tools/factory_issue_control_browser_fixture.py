"""Disposable actual-console fixture for test_factory_issue_control_backend_browser.cjs."""
from __future__ import annotations

import json
import hashlib
import os
import sys
import threading
from contextlib import closing

from hatchet_hermes_bridge import store
from hatchet_hermes_bridge.console import make_console_server
from test_console_issue_control import ACTOR, ROOT, IssueControlFixture


def main() -> None:
    fixture = IssueControlFixture()
    fixture.setUp()
    os.environ["FACTORY_CONSOLE_PUBLIC_ORIGIN"] = sys.argv[1]
    server = make_console_server(fixture.db, "127.0.0.1", 0, ROOT / "factory/console", ingress_db_path=fixture.ingress)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        print(json.dumps({"port": server.server_port, "actor": ACTOR}), flush=True)
        if sys.stdin.readline().strip() == "verify":
            with closing(store.connect_readonly(fixture.db)) as conn:
                control = store.get_factory_issue_control(conn, "ENG-8")
                assert control["mode"] == "factory", control
                assert control["automatic_intake_blocked"] is True
                assert control["actor"] == ACTOR
                assert [item["action"] for item in control["history"]] == ["return_control", "take_control"]
                assert conn.execute("SELECT COUNT(*) FROM factory_attempts").fetchone()[0] == 1
                assert conn.execute("SELECT COUNT(*) FROM factory_attempt_outbox").fetchone()[0] == 1
                attempt = store.list_factory_attempts(conn, "ENG-8")[0]
                job = store.get_job(conn, attempt["job_id"])
                assert job["payload"]["repo"] == "Driftward-LLC/capykit"
                assert "actor" not in job["payload"]
            print(json.dumps({"verified": True, "attempts": 1, "outbox": 1, "history": 2}), flush=True)
        if sys.stdin.readline().strip() == "seed_revision":
            # Fixture-only durable evidence: no GitHub, Hatchet or executor call.
            with closing(store.connect_runtime(fixture.db)) as conn, conn:
                source = store.list_factory_attempts(conn, "ENG-8")[0]
                job = store.get_job(conn, source["job_id"])
                pr = "https://github.com/Driftward-LLC/capykit/pull/80"
                head = "a" * 40
                conn.execute("UPDATE bridge_jobs SET state='ready_for_human_review',pr_url=?,commit_sha=? WHERE job_id=?", (pr, head, job["job_id"]))
                conn.execute("UPDATE factory_attempts SET status='verified_ready',prior_pr_url=? WHERE attempt_id=?", (pr, source["attempt_id"]))
                result = {"repository": "Driftward-LLC/capykit", "number": 80, "url": pr, "state": "open", "merged": False, "draft": True,
                          "head_branch": job["branch"], "head_sha": head, "base_ref": "main", "reason": "ok"}
                document = store.canonical_json(result)
                stamp = store.utc_now()
                conn.execute("INSERT INTO factory_inspection_requests (request_key,repository,number,url,job_id,work_item_key,prior_attempt_id,capability,state,result_json,result_digest,observed_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             ("fixture-revision-inspection", result["repository"], 80, pr, job["job_id"], "ENG-8", source["attempt_id"], "github_pr_inspection_v1", "completed", document, hashlib.sha256(document.encode()).hexdigest(), stamp, stamp, stamp))
                original = dict(conn.execute("SELECT * FROM factory_attempts WHERE attempt_id=?", (source["attempt_id"],)).fetchone())
            print(json.dumps({"source_attempt_id": source["attempt_id"], "branch": job["branch"], "pr_url": pr, "head_sha": head}), flush=True)
        if sys.stdin.readline().strip() == "verify_revision":
            with closing(store.connect_readonly(fixture.db)) as conn:
                attempts = store.list_factory_attempts(conn, "ENG-8")
                assert len(attempts) == 2
                revised = attempts[0]
                assert revised["prior_attempt_id"] == source["attempt_id"]
                assert revised["prior_pr_url"] == pr
                assert dict(conn.execute("SELECT * FROM factory_attempts WHERE attempt_id=?", (source["attempt_id"],)).fetchone()) == original
                assert conn.execute("SELECT COUNT(*) FROM factory_attempt_outbox").fetchone()[0] == 2
                revision_job = store.get_job(conn, revised["job_id"])
                assert revision_job["branch"] == job["branch"]
                assert revision_job["payload"]["factory_revision"]["approval_kind"] == "operator_attestation"
                assert revision_job["payload"]["factory_revision"]["reviewer"] is None
                assert revision_job["payload"]["factory_revision"]["review_model"] is None
                assert revision_job["payload"]["factory_revision"]["feedback"] == "Apply the approved design feedback. Preserve keyboard navigation."
            print(json.dumps({"verified_revision": True, "attempts": 2, "outbox": 2, "source_immutable": True}), flush=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        fixture.doCleanups()


if __name__ == "__main__":
    main()
