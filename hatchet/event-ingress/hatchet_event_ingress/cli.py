from __future__ import annotations

import argparse
import json
import sys

from .normalizer import IngressValidationError, normalize_github_pull_request, normalize_linear_issue
from .public_proxy import serve_public_proxy
from .server import serve
from .store import DEFAULT_DB_PATH, IngressConflict, IngressNotFound, connect, get_event, list_events, record_event


def print_json(payload) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hatchet event ingress")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="SQLite database path")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Initialize the ingress database")

    serve_parser = sub.add_parser("serve", help="Serve local HTTP ingress API")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=19878)

    public_proxy_parser = sub.add_parser("serve-public-linear-proxy", help="Serve public-safe Linear webhook proxy")
    public_proxy_parser.add_argument("--host", default="127.0.0.1")
    public_proxy_parser.add_argument("--port", type=int, default=19879)

    status = sub.add_parser("status", help="List recent ingress events")
    status.add_argument("--limit", type=int, default=20)

    get = sub.add_parser("get", help="Get an ingress event by ID or event key")
    get.add_argument("ref")

    normalize = sub.add_parser("normalize-github-pr", help="Normalize a GitHub pull_request payload")
    normalize.add_argument("--payload-file", required=True)
    normalize.add_argument("--delivery-id")

    record = sub.add_parser("record-github-pr", help="Record a GitHub pull_request payload without publishing")
    record.add_argument("--payload-file", required=True)
    record.add_argument("--delivery-id")

    normalize_linear = sub.add_parser("normalize-linear-issue", help="Normalize a Linear Issue payload")
    normalize_linear.add_argument("--payload-file", required=True)
    normalize_linear.add_argument("--delivery-id")

    record_linear = sub.add_parser("record-linear-issue", help="Record a Linear Issue payload without publishing")
    record_linear.add_argument("--payload-file", required=True)
    record_linear.add_argument("--delivery-id")

    return parser


def read_json_file(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("payload file must contain a JSON object")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "serve":
            serve(args.db, args.host, args.port)
            return 0
        if args.command == "serve-public-linear-proxy":
            serve_public_proxy(args.host, args.port)
            return 0

        conn = connect(args.db)
        try:
            if args.command == "init":
                print_json({"ok": True, "db": args.db})
            elif args.command == "status":
                print_json({"events": list_events(conn, args.limit)})
            elif args.command == "get":
                print_json(get_event(conn, args.ref))
            elif args.command == "normalize-github-pr":
                payload = read_json_file(args.payload_file)
                print_json(normalize_github_pull_request(payload, delivery_id=args.delivery_id))
            elif args.command == "record-github-pr":
                payload = read_json_file(args.payload_file)
                normalized = normalize_github_pull_request(payload, delivery_id=args.delivery_id)
                event, created = record_event(conn, normalized=normalized, raw_payload=payload)
                print_json({"created": created, "event": event})
            elif args.command == "normalize-linear-issue":
                payload = read_json_file(args.payload_file)
                print_json(normalize_linear_issue(payload, delivery_id=args.delivery_id))
            elif args.command == "record-linear-issue":
                payload = read_json_file(args.payload_file)
                normalized = normalize_linear_issue(payload, delivery_id=args.delivery_id)
                event, created = record_event(conn, normalized=normalized, raw_payload=payload)
                print_json({"created": created, "event": event})
            else:
                parser.error(f"unknown command {args.command}")
        finally:
            conn.close()
    except (IngressConflict, IngressNotFound, IngressValidationError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
