#!/usr/bin/env python3
"""Verify historical cutover application refuses before host or state access.

This is a naming compatibility boundary check, not a cold-copy rehearsal.
"""
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="factory-cutover-refusal-") as temporary:
        root = Path(temporary)
        missing = root / "missing"
        result = subprocess.run([
            sys.executable, str(ROOT / "deploy/factory-cutover.py"),
            "--source-project", "factory-source", "--source-env-dir", str(missing),
            "--source-keys", str(missing), "--directory", str(root / "candidate"),
            "--project", "factory-target", "--snapshot", str(root / "snapshot"), "--apply",
        ], capture_output=True, text=True, timeout=10)
        if result.returncode != 1 or "source cutover is disabled for this naming generation" not in result.stderr:
            raise RuntimeError("cutover did not refuse at its naming compatibility boundary")
        if list(root.iterdir()):
            raise RuntimeError("cutover refusal created unexpected state")
    print("Cutover naming boundary verified; no state copied or services started.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
