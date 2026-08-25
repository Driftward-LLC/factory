from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_DB_PATH = "/var/lib/driftward-hatchet-event-ingress/ingress.db"
_UTC_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class IngressConflict(Exception):
    """Raised when an event key is reused with different payload."""


class IngressNotFound(Exception):
    """Raised when an ingress event cannot be found."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_lifecycle_time(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def payload_hash(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def connect(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    parent = Path(db_path).parent
    if parent and str(parent) != ".":
        parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ingress_events (
            event_id TEXT PRIMARY KEY,
            event_key TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL,
            provider_event TEXT NOT NULL,
            action TEXT NOT NULL,
            delivery_id TEXT,
            subject TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            raw_payload_json TEXT NOT NULL,
            normalized_json TEXT NOT NULL,
            hatchet_event_name TEXT NOT NULL,
            hatchet_event_id TEXT,
            hatchet_published INTEGER NOT NULL DEFAULT 0,
            decision TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            provider_updated_at_us INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_ingress_events_created
            ON ingress_events(created_at);
        CREATE INDEX IF NOT EXISTS idx_ingress_events_lifecycle_cursor
            ON ingress_events(source, created_at, event_id);
        """
    )
    _migrate_provider_updated_at(conn)
    _migrate_lifecycle_times(conn)
    conn.executescript(
        """
        DROP INDEX IF EXISTS idx_ingress_events_latest_observation;
        DROP INDEX IF EXISTS idx_ingress_events_reconciliation_order;

        CREATE INDEX IF NOT EXISTS idx_ingress_events_latest_observation_v2
            ON ingress_events(source, subject, provider_updated_at_us DESC, event_id DESC);

        CREATE INDEX IF NOT EXISTS idx_ingress_events_reconciliation_order_v2
            ON ingress_events(provider_updated_at_us DESC, event_id DESC);
        """
    )
    conn.commit()


def _migrate_lifecycle_times(conn: sqlite3.Connection) -> None:
    for column in ("created_at", "updated_at"):
        updates = []
        for row in conn.execute(f"SELECT rowid, {column} FROM ingress_events").fetchall():
            try:
                canonical = _canonical_lifecycle_time(row[column])
            except (TypeError, ValueError):
                continue
            if canonical != row[column]:
                updates.append((canonical, row["rowid"]))
        if updates:
            conn.executemany(f"UPDATE ingress_events SET {column} = ? WHERE rowid = ?", updates)


def _migrate_provider_updated_at(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(ingress_events)")}
    if "provider_updated_at_us" not in columns:
        conn.execute("ALTER TABLE ingress_events ADD COLUMN provider_updated_at_us INTEGER")

    rows = conn.execute(
        """
        SELECT event_id, normalized_json, created_at
        FROM ingress_events
        WHERE provider_updated_at_us IS NULL
        """
    ).fetchall()
    updates = []
    for row in rows:
        try:
            normalized = json.loads(row["normalized_json"])
        except json.JSONDecodeError:
            continue
        provider_time = _provider_timestamp(normalized.get("updatedAt") if isinstance(normalized, dict) else None)
        if not provider_time:
            provider_time = _provider_timestamp(_row_value(row, "created_at"))
        if provider_time:
            updates.append((provider_time[1], row["event_id"]))
    if updates:
        conn.executemany(
            "UPDATE ingress_events SET provider_updated_at_us = ? WHERE event_id = ?",
            updates,
        )


def row_to_event(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "event_id": row["event_id"],
        "event_key": row["event_key"],
        "source": row["source"],
        "provider_event": row["provider_event"],
        "action": row["action"],
        "delivery_id": row["delivery_id"],
        "subject": row["subject"],
        "payload_hash": row["payload_hash"],
        "raw_payload": json.loads(row["raw_payload_json"]),
        "normalized": json.loads(row["normalized_json"]),
        "hatchet_event_name": row["hatchet_event_name"],
        "hatchet_event_id": row["hatchet_event_id"],
        "hatchet_published": bool(row["hatchet_published"]),
        "decision": row["decision"],
        "reason_code": row["reason_code"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def record_event(
    conn: sqlite3.Connection,
    *,
    normalized: dict[str, Any],
    raw_payload: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    event_key = _required(normalized, "eventKey")
    source = _required(normalized, "source")
    provider_event = _required(normalized, "providerEvent")
    action = _required(normalized, "action")
    event_name = _required(normalized, "eventName")
    decision = _required(normalized, "decision")
    reason_code = _required(normalized, "reasonCode")
    subject = normalized.get("subject")
    if not isinstance(subject, str) or not subject.strip():
        repo = _required(normalized, "repo")
        pr_number = normalized.get("prNumber")
        if not isinstance(pr_number, int):
            raise ValueError("subject or repo/prNumber is required")
        subject = f"{repo}#{pr_number}"
    subject = subject.strip()

    digest = payload_hash(raw_payload)
    now = utc_now()
    provider_time = _provider_timestamp(normalized.get("updatedAt")) or _provider_timestamp(now)

    with conn:
        existing = conn.execute(
            "SELECT * FROM ingress_events WHERE event_key = ?",
            (event_key,),
        ).fetchone()
        if existing:
            event = row_to_event(existing)
            if event["payload_hash"] != digest:
                raise IngressConflict("event_key already exists with different payload")
            return event, False

        conn.execute(
            """
            INSERT INTO ingress_events (
                event_id, event_key, source, provider_event, action, delivery_id,
                subject, payload_hash, raw_payload_json, normalized_json,
                hatchet_event_name, decision, reason_code, created_at, updated_at,
                provider_updated_at_us
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                event_key,
                source,
                provider_event,
                action,
                normalized.get("deliveryId"),
                subject,
                digest,
                canonical_json(raw_payload),
                canonical_json(normalized),
                event_name,
                decision,
                reason_code,
                now,
                now,
                provider_time[1],
            ),
        )

    return get_event(conn, event_key), True


def mark_published(conn: sqlite3.Connection, event_key: str, hatchet_event_id: str | None) -> dict[str, Any]:
    with conn:
        row = conn.execute(
            "SELECT * FROM ingress_events WHERE event_key = ?",
            (event_key,),
        ).fetchone()
        if not row:
            raise IngressNotFound(f"event not found: {event_key}")
        conn.execute(
            """
            UPDATE ingress_events
            SET hatchet_event_id = ?,
                hatchet_published = 1,
                updated_at = ?
            WHERE event_key = ?
            """,
            (hatchet_event_id, utc_now(), event_key),
        )
    return get_event(conn, event_key)


def get_event(conn: sqlite3.Connection, ref: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT * FROM ingress_events
        WHERE event_key = ? OR event_id = ?
        """,
        (ref, ref),
    ).fetchone()
    if not row:
        raise IngressNotFound(f"event not found: {ref}")
    return row_to_event(row)


def list_events(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM ingress_events
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [row_to_event(row) for row in rows]


def latest_observation(conn: sqlite3.Connection, *, source: str, subject: str) -> dict[str, Any] | None:
    if source not in {"github", "linear"}:
        raise ValueError("source must be github or linear")
    if not subject or len(subject) > 300:
        raise ValueError("subject must be between 1 and 300 characters")
    row = conn.execute(
        """
        SELECT event_key, source, subject, normalized_json, created_at, provider_updated_at_us
        FROM ingress_events
        WHERE source = ? AND subject = ?
        ORDER BY provider_updated_at_us DESC, event_id DESC
        LIMIT 1
        """,
        (source, subject),
    ).fetchone()
    if not row:
        return None
    return _sanitize_observation_row(row)


def list_reconciliation_observations(conn: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    if limit < 1 or limit > 250:
        raise ValueError("limit must be between 1 and 250")
    rows = conn.execute(
        """
        SELECT event_key, source, subject, normalized_json, hatchet_published,
               decision, reason_code, created_at, provider_updated_at_us
        FROM ingress_events
        ORDER BY provider_updated_at_us DESC, event_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    observations = []
    for row in rows:
        observation = _sanitize_observation_row(row)
        observation["hatchetPublished"] = bool(row["hatchet_published"])
        observation["decision"] = row["decision"]
        observation["reasonCode"] = row["reason_code"]
        observations.append(observation)
    return observations


def list_lifecycle_observations(conn: sqlite3.Connection, limit: int = 250, *, after_created_at: str | None = None, after_event_id: str | None = None, before_created_at: str | None = None, before_event_id: str | None = None) -> list[dict[str, Any]]:
    """Return the narrow Linear projection consumed by the lifecycle writer.

    This deliberately never returns descriptions, comments, raw webhook bodies,
    or provider error text.  The writer only needs stable correlation and the
    ingress decision to decide whether a routing failure is operator-visible.
    """
    if limit < 1 or limit > 250:
        raise ValueError("limit must be between 1 and 250")
    if bool(after_created_at) != bool(after_event_id):
        raise ValueError("lifecycle cursor requires both created_at and event_id")
    if bool(before_created_at) != bool(before_event_id):
        raise ValueError("lifecycle bound requires both created_at and event_id")
    where, values = "WHERE source = 'linear'", []
    if after_created_at and after_event_id:
        where += " AND (created_at > ? OR (created_at = ? AND event_id > ?))"
        values.extend([after_created_at, after_created_at, after_event_id])
    if before_created_at and before_event_id:
        where += " AND (created_at < ? OR (created_at = ? AND event_id <= ?))"
        values.extend([before_created_at, before_created_at, before_event_id])
    rows = conn.execute(
        f"""
        SELECT event_key, delivery_id, subject, normalized_json, action, decision,
               reason_code, created_at, event_id
        FROM ingress_events
        {where}
        ORDER BY created_at ASC, event_id ASC
        LIMIT ?
        """, (*values, limit),
    ).fetchall()
    result = []
    for row in rows:
        normalized = json.loads(row["normalized_json"])
        result.append({
            "eventId": row["event_id"],
            "eventKey": row["event_key"],
            "deliveryId": row["delivery_id"],
            "subject": row["subject"],
            "issueId": normalized.get("issueId"),
            "identifier": normalized.get("identifier"),
            "issueUrl": normalized.get("issueUrl"),
            "projectId": normalized.get("projectId"),
            "projectName": normalized.get("projectName"),
            "team": normalized.get("team"),
            "teamKey": normalized.get("teamKey"),
            "status": normalized.get("status"),
            "action": row["action"],
            "decision": row["decision"],
            "reasonCode": row["reason_code"],
            "createdAt": row["created_at"],
        })
    return result


def count_reconciliation_observations(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM ingress_events").fetchone()[0])


def _sanitize_observation(event: dict[str, Any]) -> dict[str, Any]:
    return _sanitize_observation_fields(
        event_key=event["event_key"],
        source=event["source"],
        subject=event["subject"],
        normalized=event["normalized"],
        created_at=event["created_at"],
    )


def _sanitize_observation_row(row: sqlite3.Row) -> dict[str, Any]:
    return _sanitize_observation_fields(
        event_key=row["event_key"],
        source=row["source"],
        subject=row["subject"],
        normalized=json.loads(row["normalized_json"]),
        created_at=row["created_at"],
    )


def _sanitize_observation_fields(*, event_key: str, source: str, subject: str, normalized: dict[str, Any], created_at: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "eventKey": event_key, "source": source, "subject": subject,
        "observedAt": created_at,
    }
    provider_time = _provider_timestamp(normalized.get("updatedAt"))
    if provider_time:
        result["providerUpdatedAt"] = provider_time[0]
    if isinstance(normalized.get("deliveryId"), str):
        result["deliveryId"] = normalized["deliveryId"]
    if source == "github":
        result["github"] = {key: normalized.get(key) for key in (
            "repo", "prNumber", "prUrl", "state", "merged", "isDraft", "headRefName", "headSha", "linearIssues"
        )}
    else:
        blocked_by = normalized.get("blockedBy")
        result["linear"] = {
            "projectId": normalized.get("projectId"), "status": normalized.get("status"), "dispatchEligible": normalized.get("dispatchEligible"),
            "blockedByCount": len(blocked_by) if isinstance(blocked_by, list) else 0,
        }
    return result


def _required(value: dict[str, Any], key: str) -> str:
    found = value.get(key)
    if not isinstance(found, str) or not found.strip():
        raise ValueError(f"{key} is required")
    return found.strip()


def _row_value(row: sqlite3.Row, key: str) -> Any:
    return row[key] if key in row.keys() else None


def _provider_timestamp(value: Any) -> tuple[str, int] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    utc = parsed.astimezone(timezone.utc)
    delta = utc - _UTC_EPOCH
    microseconds = (
        delta.days * 24 * 60 * 60 * 1_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )
    return utc.isoformat(timespec="microseconds").replace("+00:00", "Z"), microseconds
