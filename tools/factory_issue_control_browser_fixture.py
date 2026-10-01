"""Disposable actual-console fixture for test_factory_issue_control_backend_browser.cjs."""
from __future__ import annotations

import json
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
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        fixture.doCleanups()


if __name__ == "__main__":
    main()
