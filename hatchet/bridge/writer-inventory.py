#!/usr/bin/env python3
"""Emit the shared writer inventory for the root installer without secrets."""
from __future__ import annotations
import argparse
from hatchet_hermes_bridge.runtime import DIRECT_DB_WRITERS, HTTP_MUTATION_CLIENTS, WRITER_UNITS

p = argparse.ArgumentParser(); p.add_argument("--kind", choices=("direct", "http", "all"), default="all")
args = p.parse_args()
units = {"direct": DIRECT_DB_WRITERS, "http": HTTP_MUTATION_CLIENTS, "all": WRITER_UNITS}[args.kind]
print("\n".join(units))
