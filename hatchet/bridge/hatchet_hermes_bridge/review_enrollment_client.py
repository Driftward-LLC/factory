"""Installation-local authenticated client, executed only in the operator role."""
from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("issue")
    parser.add_argument("phase", choices=("inspect", "preview", "confirm"))
    parser.add_argument("--source-attempt", required=True)
    for field in ("repo", "pr", "branch", "reviewed-head", "reason", "request-id", "snapshot-digest"):
        parser.add_argument("--" + field)
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    body = {"issue": args.issue, "source_attempt_id": args.source_attempt}
    artifact = {"repository": args.repo, "pr_url": args.pr, "branch": args.branch,
                "reviewed_head_sha": args.reviewed_head, "reason": args.reason}
    if args.phase == "inspect":
        if not args.yes or any(artifact.values()) or args.request_id or args.snapshot_digest:
            parser.error("inspect requires --yes and only the source attempt")
    else:
        if not all(artifact.values()):
            parser.error("preview and confirm require the exact artifact and reason")
        body.update(artifact)
        if args.phase == "confirm":
            if not args.yes or not args.request_id or not args.snapshot_digest:
                parser.error("confirm requires --yes, --request-id and --snapshot-digest")
            body.update(request_id=args.request_id, snapshot_digest=args.snapshot_digest)
        elif args.yes or args.request_id or args.snapshot_digest:
            parser.error("preview does not accept confirmation options")
    token = os.environ.get("HATCHET_FACTORY_OPERATOR_TOKEN")
    if os.geteuid() != 0 or not token:
        parser.error("operator role requires root and its existing operator credential")
    connection = http.client.HTTPConnection("localhost", timeout=30)
    try:
        connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.sock.settimeout(30)
        connection.sock.connect(os.environ.get("FACTORY_OPERATOR_SOCKET", "/run/factory-operator/operator.sock"))
        connection.request("POST", "/v1/review-enrollment/" + args.phase, json.dumps(body),
                           {"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        response = connection.getresponse()
        value = json.loads(response.read(1048577))
        if response.status not in {200, 201}:
            error = str(value.get("error", "operator request failed")).replace(token, "[redacted]")
            print("Factory: " + "".join(c if c.isprintable() else " " for c in error)[:512], file=sys.stderr)
            return 1
        print(json.dumps(value, sort_keys=True))
        return 0
    except (OSError, ValueError, http.client.HTTPException):
        print("Factory: operator request unavailable", file=sys.stderr)
        return 1
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
