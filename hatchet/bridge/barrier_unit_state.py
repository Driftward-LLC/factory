#!/usr/bin/env python3
"""Strict systemd LoadState proof shared by safe-down barrier verifiers."""
from __future__ import annotations

import argparse
import subprocess
import sys
from typing import Callable


class UnitStateError(RuntimeError):
    pass


Runner = Callable[..., subprocess.CompletedProcess[str]]


def systemctl_value(unit: str, property_name: str, *, single_line: bool = True, runner: Runner | None = None) -> str:
    if runner is None:
        runner = subprocess.run
    result = runner(
        ["systemctl", "show", unit, "-p", property_name, "--value"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or result.stderr or (single_line and (not result.stdout.endswith("\n") or "\n" in result.stdout[:-1])):
        raise UnitStateError(f"cannot prove systemd {property_name}")
    return result.stdout[:-1] if single_line else result.stdout


def unit_is_absent(unit: str, *, runner: Runner | None = None) -> bool:
    """Accept only the exact systemd absence spelling; all else fails closed."""
    state = systemctl_value(unit, "LoadState", runner=runner)
    if state == "not-found":
        return True
    if state == "loaded":
        return False
    raise UnitStateError("unsafe systemd LoadState")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("unit")
    args = parser.parse_args()
    try:
        print("absent" if unit_is_absent(args.unit) else "loaded")
    except UnitStateError as exc:
        print(f"maintenance barrier unit-state proof failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
