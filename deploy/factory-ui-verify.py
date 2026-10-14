#!/usr/bin/env python3
"""Run an explicit, isolated UI-verification pilot; never changes live routing."""
import argparse
import json
from pathlib import Path
import sys

import bootstrap
import ui_verification


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True, help="existing private Factory installation")
    parser.add_argument("--request", type=Path, required=True, help="operator-approved synthetic preview recipe")
    parser.add_argument("--runner-image", required=True, help="immutable local verifier image ID or digest")
    parser.add_argument("--plugin-directory", type=Path, required=True, help="installed, reviewed Driftward Engineering plugin")
    parser.add_argument("--api-key-file", type=Path, help="dedicated private OpenAI API key file; never shared operator auth")
    parser.add_argument("--fixture", action="store_true", help="test real browser transport without calling an LLM")
    parser.add_argument("--apply", action="store_true", help="start disposable containers; otherwise read-only preview")
    args = parser.parse_args()
    try:
        raw = bootstrap.checked_path(args.request).read_bytes()
        if len(raw) > 32768:
            raise ValueError("request too large")
        if args.fixture and args.api_key_file:
            raise ValueError("fixture runs must not receive provider credentials")
        pilot = ui_verification.Pilot(args.directory, json.loads(raw), args.runner_image,
                                      args.plugin_directory, fixture=args.fixture)
        if not args.apply:
            print("Preview only: disposable app, browser and Codex runner; no live routing or ledger changes.")
            return 0
        if not args.fixture and not args.api_key_file:
            raise ValueError("real Codex run requires its own --api-key-file")
        key = b"" if args.fixture else bootstrap._read_private(args.api_key_file).strip().encode()
        if not args.fixture and (not key or len(key) > 1024 or b"\n" in key):
            raise ValueError("invalid dedicated API credential")
        report = pilot.run(key)
        print(json.dumps({"run_id": report["run_id"], "status": report["status"], "reason": report["reason"],
                          "cleanup_complete": report["cleanup_complete"], "factory_verified": False,
                          "evidence": str(pilot.root / "report.json")}))
        return 0 if report["status"] in {"fixture_passed", "observed"} and report["cleanup_complete"] else 1
    except (OSError, ValueError, RuntimeError, ui_verification.subprocess.TimeoutExpired):
        print("UI verification blocked: check private configuration, immutable images and prerequisite readiness; no raw output disclosed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
