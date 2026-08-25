#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import pprint
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "hatchet/contracts/events.json"
CATALOG_PATH = ROOT / "hatchet/workers/catalog.json"
PYTHON_OUTPUT = ROOT / "hatchet/event-ingress/hatchet_event_ingress/contracts.py"
TYPESCRIPT_OUTPUT = ROOT / "hatchet/workers/shared/src/contracts.ts"

ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
WIRE_RE = re.compile(r"^driftward\.[a-z0-9_.]+$")


class ContractError(Exception):
    pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and generate Hatchet contract constants.")
    parser.add_argument("command", choices=("validate", "generate", "check"))
    args = parser.parse_args()

    contract = load_json(CONTRACT_PATH)
    catalog = load_json(CATALOG_PATH)
    validate_contract(contract, catalog)

    if args.command == "validate":
        print("Hatchet contracts are valid.")
        return 0

    outputs = generate_outputs(contract)
    if args.command == "generate":
        for path, content in outputs.items():
            path.write_text(content, encoding="utf-8")
        print("Generated Hatchet contract constants.")
        return 0

    stale = [path for path, content in outputs.items() if not path.exists() or path.read_text(encoding="utf-8") != content]
    if stale:
        for path in stale:
            print(f"stale generated contract: {path.relative_to(ROOT)}", file=sys.stderr)
        print("Run: python3 tools/hatchet-contracts.py generate", file=sys.stderr)
        return 1

    print("Hatchet generated contract constants are current.")
    return 0


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ContractError(f"{path.relative_to(ROOT)} must be a JSON object")
    return data


def validate_contract(contract: dict[str, Any], catalog: dict[str, Any]) -> None:
    if contract.get("version") != 1:
        raise ContractError("contract version must be 1")

    events = required_object(contract, "events")
    decisions = required_object(contract, "decisions")
    reason_codes = required_object(contract, "reason_codes")
    bridge_statuses = required_object(contract, "bridge_lookup_statuses")
    provider_mappings = required_object(contract, "provider_action_mappings")

    for section_name, section in (
        ("events", events),
        ("decisions", decisions),
        ("reason_codes", reason_codes),
        ("bridge_lookup_statuses", bridge_statuses),
    ):
        for item_id, item in section.items():
            validate_id(section_name, item_id)
            if not isinstance(item, dict):
                raise ContractError(f"{section_name}.{item_id} must be an object")
            label = item.get("label")
            if not isinstance(label, str) or not label.strip():
                raise ContractError(f"{section_name}.{item_id}.label must be a non-empty string")

    wire_names: dict[str, str] = {}
    for event_id, event in events.items():
        wire_name = event.get("wire_name")
        if not isinstance(wire_name, str) or not WIRE_RE.match(wire_name):
            raise ContractError(f"events.{event_id}.wire_name must be a driftward.* string")
        if wire_name in wire_names:
            raise ContractError(f"duplicate wire_name {wire_name!r} for {wire_names[wire_name]} and {event_id}")
        wire_names[wire_name] = event_id

        default_decision = event.get("default_decision_id")
        if default_decision is not None and default_decision not in decisions:
            raise ContractError(f"events.{event_id}.default_decision_id references unknown decision {default_decision!r}")

        default_reason = event.get("default_reason_code_id")
        if default_reason is not None and default_reason not in reason_codes:
            raise ContractError(f"events.{event_id}.default_reason_code_id references unknown reason code {default_reason!r}")

        event_key_suffix = event.get("event_key_suffix")
        if event_key_suffix is not None and not isinstance(event_key_suffix, str):
            raise ContractError(f"events.{event_id}.event_key_suffix must be a string")

    for provider_key, event_id in provider_mappings.items():
        if not isinstance(provider_key, str) or not provider_key.strip():
            raise ContractError("provider_action_mappings keys must be non-empty strings")
        if event_id not in events:
            raise ContractError(f"provider_action_mappings.{provider_key} references unknown event {event_id!r}")

    validate_catalog(catalog, wire_names)


def validate_catalog(catalog: dict[str, Any], wire_names: dict[str, str]) -> None:
    catalog_events = catalog.get("events")
    if not isinstance(catalog_events, list) or not all(isinstance(event, str) for event in catalog_events):
        raise ContractError("hatchet/workers/catalog.json events must be a string array")

    missing_contract = sorted(set(catalog_events) - set(wire_names))
    if missing_contract:
        raise ContractError(f"catalog events missing from contract: {', '.join(missing_contract)}")

    missing_catalog = sorted(set(wire_names) - set(catalog_events))
    if missing_catalog:
        raise ContractError(f"contract events missing from catalog: {', '.join(missing_catalog)}")

    workers = catalog.get("workers")
    if not isinstance(workers, list):
        raise ContractError("hatchet/workers/catalog.json workers must be an array")
    for worker in workers:
        if not isinstance(worker, dict):
            raise ContractError("catalog workers must be objects")
        worker_name = worker.get("name") or "<unknown>"
        for field in ("produces_events", "consumes_events"):
            values = worker.get(field)
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                raise ContractError(f"catalog worker {worker_name}.{field} must be a string array")
            unknown = sorted(set(values) - set(wire_names))
            if unknown:
                raise ContractError(f"catalog worker {worker_name}.{field} references unknown events: {', '.join(unknown)}")
            missing_global = sorted(set(values) - set(catalog_events))
            if missing_global:
                raise ContractError(f"catalog worker {worker_name}.{field} missing from global events: {', '.join(missing_global)}")


def generate_outputs(contract: dict[str, Any]) -> dict[Path, str]:
    events = required_object(contract, "events")
    decisions = required_object(contract, "decisions")
    reason_codes = required_object(contract, "reason_codes")
    bridge_statuses = required_object(contract, "bridge_lookup_statuses")
    provider_mappings = required_object(contract, "provider_action_mappings")

    event_ids = upper_map(events)
    decision_ids = upper_map(decisions)
    reason_code_ids = upper_map(reason_codes)
    bridge_status_ids = upper_map(bridge_statuses)
    event_wire_names = {constant: events[event_id]["wire_name"] for constant, event_id in event_ids.items()}
    event_ids_by_wire_name = {event["wire_name"]: event_id for event_id, event in events.items()}
    default_reason_by_wire_name = {
        event["wire_name"]: event["default_reason_code_id"]
        for event in events.values()
        if "default_reason_code_id" in event
    }

    python_content = render_python(
        contract=contract,
        event_ids=event_ids,
        event_wire_names=event_wire_names,
        event_ids_by_wire_name=event_ids_by_wire_name,
        default_reason_by_wire_name=default_reason_by_wire_name,
        decisions=decision_ids,
        reason_codes=reason_code_ids,
        bridge_statuses=bridge_status_ids,
        provider_mappings=provider_mappings,
    )
    typescript_content = render_typescript(
        contract=contract,
        event_ids=event_ids,
        event_wire_names=event_wire_names,
        event_ids_by_wire_name=event_ids_by_wire_name,
        default_reason_by_wire_name=default_reason_by_wire_name,
        decisions=decision_ids,
        reason_codes=reason_code_ids,
        bridge_statuses=bridge_status_ids,
        provider_mappings=provider_mappings,
    )
    return {
        PYTHON_OUTPUT: python_content,
        TYPESCRIPT_OUTPUT: typescript_content,
    }


def render_python(**values: Any) -> str:
    lines = [
        "# Generated by tools/hatchet-contracts.py. Do not edit by hand.",
        "from __future__ import annotations",
        "",
    ]
    for name, value in (
        ("HATCHET_CONTRACT", values["contract"]),
        ("EVENT_IDS", values["event_ids"]),
        ("EVENTS", values["contract"]["events"]),
        ("HATCHET_EVENTS", values["event_wire_names"]),
        ("EVENT_IDS_BY_WIRE_NAME", values["event_ids_by_wire_name"]),
        ("EVENT_DEFAULT_REASON_CODES_BY_WIRE_NAME", values["default_reason_by_wire_name"]),
        ("DECISIONS", values["decisions"]),
        ("REASON_CODES", values["reason_codes"]),
        ("BRIDGE_LOOKUP_STATUSES", values["bridge_statuses"]),
        ("PROVIDER_ACTION_MAPPINGS", values["provider_mappings"]),
    ):
        lines.append(f"{name} = {pprint.pformat(value, sort_dicts=False, width=120)}")
        lines.append("")
    return "\n".join(lines)


def render_typescript(**values: Any) -> str:
    lines = [
        "// Generated by tools/hatchet-contracts.py. Do not edit by hand.",
        "",
    ]
    for name, value in (
        ("HATCHET_CONTRACT", values["contract"]),
        ("EVENT_IDS", values["event_ids"]),
        ("EVENTS", values["contract"]["events"]),
        ("HATCHET_EVENT_WIRE_NAMES", values["event_wire_names"]),
        ("HATCHET_EVENT_IDS_BY_WIRE_NAME", values["event_ids_by_wire_name"]),
        ("HATCHET_EVENT_DEFAULT_REASON_CODES_BY_WIRE_NAME", values["default_reason_by_wire_name"]),
        ("DECISIONS", values["decisions"]),
        ("REASON_CODES", values["reason_codes"]),
        ("BRIDGE_LOOKUP_STATUSES", values["bridge_statuses"]),
        ("PROVIDER_ACTION_MAPPINGS", values["provider_mappings"]),
    ):
        lines.append(f"export const {name} = {json.dumps(value, indent=2)} as const;")
        lines.append("")
    lines.extend(
        [
            "export type EventId = keyof typeof EVENTS;",
            "export type HatchetEventWireName = (typeof HATCHET_EVENT_WIRE_NAMES)[keyof typeof HATCHET_EVENT_WIRE_NAMES];",
            "export type DecisionId = (typeof DECISIONS)[keyof typeof DECISIONS];",
            "export type ReasonCodeId = (typeof REASON_CODES)[keyof typeof REASON_CODES];",
            "export type BridgeLookupStatus = (typeof BRIDGE_LOOKUP_STATUSES)[keyof typeof BRIDGE_LOOKUP_STATUSES];",
            "",
        ]
    )
    return "\n".join(lines)


def required_object(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise ContractError(f"{key} must be an object")
    return value


def validate_id(section_name: str, item_id: str) -> None:
    if not isinstance(item_id, str) or not ID_RE.match(item_id):
        raise ContractError(f"{section_name}.{item_id} must use snake_case")


def upper_map(values: dict[str, Any]) -> dict[str, str]:
    return {item_id.upper(): item_id for item_id in values}


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ContractError as error:
        print(f"hatchet contract error: {error}", file=sys.stderr)
        raise SystemExit(1)
