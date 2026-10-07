"""Read-only, bounded pre-execution view of recorded Linear observations."""
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path


ISSUE = re.compile(r"ENG-[1-9][0-9]*")
STATUSES = {name.lower(): name for name in (
    "Backlog", "Triage", "Todo", "To Do", "Ready", "Blocked", "Needs Operator",
    "In Progress", "In Review", "Done", "Canceled", "Cancelled", "Duplicate",
)}


def _timestamp(value):
    try:
        return value if isinstance(value, str) and datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo else None
    except ValueError:
        return None


def observed_intake(bridge, ingress_path, issue=None):
    result = {"available": False, "items": [], "truncated": False, "scan_limit": 500, "limit": 100}
    if not ingress_path:
        return result
    try:
        conn = sqlite3.connect(Path(ingress_path).resolve().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            # Select latest provider state BEFORE filtering statuses, so a delayed
            # Ready delivery cannot resurrect a subsequently completed issue.
            rows = conn.execute("""
                SELECT subject, normalized_json, created_at FROM ingress_events e
                WHERE source = 'linear' AND subject GLOB 'ENG-[0-9]*'
                  AND (? IS NULL OR subject = ?)
                  AND NOT EXISTS (
                    SELECT 1 FROM ingress_events newer
                    WHERE newer.source = e.source AND newer.subject = e.subject
                      AND (newer.provider_updated_at_us > e.provider_updated_at_us
                        OR (newer.provider_updated_at_us = e.provider_updated_at_us
                            AND newer.event_id > e.event_id)))
                ORDER BY provider_updated_at_us DESC, event_id DESC LIMIT 501
            """, (issue, issue)).fetchall()
        finally:
            conn.close()
        result["truncated"] = len(rows) > 500
        for row in rows[:500]:
            key = row["subject"]
            if not ISSUE.fullmatch(key):
                continue
            value = json.loads(row["normalized_json"])
            if not isinstance(value, dict):
                raise ValueError("invalid observation")
            status = STATUSES.get(str(value.get("status", "")).lower(), "Unknown")
            if not issue and (
                value.get("action") == "remove"
                or status in {"Done", "Canceled", "Cancelled", "Duplicate"}
                or value.get("statusType") in {"completed", "canceled"}
                or bridge.execute("SELECT 1 FROM factory_attempts WHERE work_item_key = ? LIMIT 1", (key,)).fetchone()
            ):
                continue
            blockers = value.get("blockedBy")
            blocker_ids = sorted({b["id"] for b in blockers if isinstance(b, dict)
                                  and isinstance(b.get("id"), str) and ISSUE.fullmatch(b["id"])}) if isinstance(blockers, list) else []
            result["items"].append({
                "issue": key, "linear_status": status,
                "removed": value.get("action") == "remove",
                "blocked_by": blocker_ids, "blocked_by_count": len(blockers) if isinstance(blockers, list) else None,
                "dispatch_eligible": value.get("dispatchEligible") if isinstance(value.get("dispatchEligible"), bool) else None,
                "observed_at": _timestamp(row["created_at"]),
                "provider_updated_at": _timestamp(value.get("updatedAt")),
            })
        result["truncated"] |= len(result["items"]) > 100
        result["items"] = result["items"][:100]
        result["available"] = True
        return result
    except (OSError, sqlite3.Error, ValueError, TypeError):
        # Never turn unreadable/legacy/malformed evidence into "nothing waiting".
        return {**result, "available": False, "items": []}
