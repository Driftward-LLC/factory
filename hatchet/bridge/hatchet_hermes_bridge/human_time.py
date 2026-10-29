"""Append-only, self-reported human time on a real Factory attempt."""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import nullcontext
from typing import Any

from . import store

EVENT = "human_time_recorded"
CATEGORIES = frozenset({"intervention", "review", "maintenance"})
ATTEMPT_RE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
REQUEST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
ACTOR_RE = re.compile(r"uid:0;pid:[0-9]+;account:[A-Za-z0-9_-]+")


def record_human_time(
    conn: sqlite3.Connection, attempt_id: str, *, request_id: str,
    duration_ms: int, category: str, actor: str,
) -> dict[str, Any]:
    """Record one entry; an exact retry preserves the original actor and time."""
    if not isinstance(actor, str) or not ACTOR_RE.fullmatch(actor):
        raise store.BridgeValidationError("human time requires a root Unix peer")
    if not isinstance(attempt_id, str) or not ATTEMPT_RE.fullmatch(attempt_id):
        raise store.BridgeValidationError("human time requires an exact attempt UUID")
    if (not isinstance(request_id, str) or not REQUEST_RE.fullmatch(request_id)
            or store._secret_shaped(request_id)):
        raise store.BridgeValidationError("human time request_id must be a safe identifier of at most 128 characters")
    if type(duration_ms) is not int or not 0 <= duration_ms <= 86_400_000:
        raise store.BridgeValidationError("human time duration_ms must be an integer between 0 and 86400000")
    if not isinstance(category, str) or category not in CATEGORIES:
        raise store.BridgeValidationError("human time category must be intervention, review, or maintenance")
    details = {"schema_version": 1, "request_id": request_id, "duration_ms": duration_ms,
               "category": category, "actor": actor}
    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
        f"driftward.factory.attempt-event:{attempt_id}:{EVENT}:{request_id}"))
    with nullcontext() if conn.in_transaction else conn:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        attempt = store.get_factory_attempt(conn, attempt_id)
        if (attempt["mode"] != "real" or attempt["actor"] == "legacy-import"
                or attempt["status"].startswith("legacy_")):
            raise store.BridgeValidationError("human time requires a real, nonlegacy attempt")
        row = conn.execute("SELECT * FROM factory_attempt_events WHERE event_id=?", (event_id,)).fetchone()
        if row:
            recorded = json.loads(row["details_json"])
            # A different CLI process can retry the same intent; retain its first actor.
            if (row["attempt_id"] != attempt_id or row["event_type"] != EVENT
                    or {key: value for key, value in recorded.items() if key != "actor"}
                    != {key: value for key, value in details.items() if key != "actor"}):
                raise store.BridgeConflict("human time request_id conflicts with existing intent")
            return {"created": False, "event": {"event_id": event_id, "attempt_id": attempt_id,
                    "event_type": EVENT, "details": recorded, "created_at": row["created_at"]}}
        now = store.utc_now()
        conn.execute(
            "INSERT INTO factory_attempt_events (event_id,attempt_id,event_type,details_json,created_at) VALUES (?,?,?,?,?)",
            (event_id, attempt_id, EVENT, store.canonical_json(details), now),
        )
        return {"created": True, "event": {"event_id": event_id, "attempt_id": attempt_id,
                "event_type": EVENT, "details": details, "created_at": now}}
