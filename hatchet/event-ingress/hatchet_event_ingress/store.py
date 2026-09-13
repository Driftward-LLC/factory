from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DEFAULT_DB_PATH = "/var/lib/driftward-hatchet-event-ingress/ingress.db"
INGRESS_SCHEMA_VERSION = 3
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
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        _guard_supported_schema(conn)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        init_db(conn)
        return conn
    except Exception:
        conn.close()
        raise


def init_db(conn: sqlite3.Connection) -> None:
    # Reject a database written by a newer runtime before DDL, migrations, or
    # lifecycle timestamp rewrites can change it.
    _guard_supported_schema(conn)
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
            published_at TEXT,
            publish_attempts INTEGER NOT NULL DEFAULT 0,
            publish_lease_id TEXT,
            publish_lease_expires_at TEXT,
            publish_last_error TEXT,
            decision TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            provider_updated_at_us INTEGER,
            provider_timestamp_source TEXT CHECK (
                provider_timestamp_source IN ('provider', 'receipt_fallback')
            )
        );

        CREATE TABLE IF NOT EXISTS ingress_schema_meta (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            schema_version INTEGER NOT NULL,
            migrated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_ingress_events_created
            ON ingress_events(created_at);
        CREATE INDEX IF NOT EXISTS idx_ingress_events_lifecycle_cursor
            ON ingress_events(source, created_at, event_id);

        CREATE TABLE IF NOT EXISTS ingress_receipts (
            receipt_id TEXT PRIMARY KEY,
            event_key TEXT NOT NULL,
            source TEXT NOT NULL,
            provider_event TEXT NOT NULL,
            delivery_id TEXT,
            payload_hash TEXT NOT NULL,
            signature_verified INTEGER NOT NULL DEFAULT 0 CHECK (
                signature_verified IN (0, 1)
            ),
            disposition TEXT NOT NULL CHECK (
                disposition IN ('accepted', 'duplicate', 'payload_conflict')
            ),
            received_at TEXT NOT NULL,
            FOREIGN KEY (event_key) REFERENCES ingress_events(event_key)
        );

        CREATE INDEX IF NOT EXISTS idx_ingress_receipts_received
            ON ingress_receipts(received_at, receipt_id);
        CREATE INDEX IF NOT EXISTS idx_ingress_receipts_event
            ON ingress_receipts(event_key, received_at, receipt_id);

        CREATE TRIGGER IF NOT EXISTS ingress_receipts_no_update
        BEFORE UPDATE ON ingress_receipts
        BEGIN
            SELECT RAISE(ABORT, 'ingress receipts are append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS ingress_receipts_no_delete
        BEFORE DELETE ON ingress_receipts
        BEGIN
            SELECT RAISE(ABORT, 'ingress receipts are append-only');
        END;
        """
    )
    _migrate_publication_columns(conn)
    _migrate_receipt_provenance(conn)
    _migrate_provider_updated_at(conn)
    _migrate_lifecycle_times(conn)
    schema = conn.execute(
        "SELECT schema_version FROM ingress_schema_meta WHERE singleton = 1"
    ).fetchone()
    if schema is None:
        conn.execute(
            "INSERT INTO ingress_schema_meta(singleton, schema_version, migrated_at) "
            "VALUES (1, ?, ?)",
            (INGRESS_SCHEMA_VERSION, utc_now()),
        )
    elif int(schema["schema_version"]) < INGRESS_SCHEMA_VERSION:
        conn.execute(
            "UPDATE ingress_schema_meta SET schema_version = ?, migrated_at = ? "
            "WHERE singleton = 1",
            (INGRESS_SCHEMA_VERSION, utc_now()),
        )
    elif int(schema["schema_version"]) > INGRESS_SCHEMA_VERSION:
        raise RuntimeError("ingress database schema is newer than this runtime")
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


def _guard_supported_schema(conn: sqlite3.Connection) -> None:
    meta_table = conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type = 'table' AND name = 'ingress_schema_meta'"
    ).fetchone()
    if meta_table is None:
        return
    try:
        schema = conn.execute(
            "SELECT schema_version FROM ingress_schema_meta WHERE singleton = 1"
        ).fetchone()
        version = None if schema is None else int(schema[0])
    except (sqlite3.DatabaseError, TypeError, ValueError) as exc:
        raise RuntimeError("ingress database schema metadata is invalid") from exc
    if version is not None and version > INGRESS_SCHEMA_VERSION:
        raise RuntimeError("ingress database schema is newer than this runtime")


def _migrate_publication_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(ingress_events)")}
    for definition in (
        "publish_attempts INTEGER NOT NULL DEFAULT 0",
        "publish_lease_id TEXT",
        "publish_lease_expires_at TEXT",
        "publish_last_error TEXT",
        "published_at TEXT",
    ):
        if definition.split()[0] not in columns:
            conn.execute(f"ALTER TABLE ingress_events ADD COLUMN {definition}")


def _migrate_receipt_provenance(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(ingress_receipts)")}
    if "signature_verified" not in columns:
        conn.execute(
            "ALTER TABLE ingress_receipts ADD COLUMN "
            "signature_verified INTEGER NOT NULL DEFAULT 0"
        )


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
    if "provider_timestamp_source" not in columns:
        conn.execute("ALTER TABLE ingress_events ADD COLUMN provider_timestamp_source TEXT")

    rows = conn.execute(
        """
        SELECT event_id, normalized_json, created_at, provider_updated_at_us,
               provider_timestamp_source
        FROM ingress_events
        WHERE provider_updated_at_us IS NULL OR provider_timestamp_source IS NULL
        """
    ).fetchall()
    updates = []
    for row in rows:
        try:
            normalized = json.loads(row["normalized_json"])
        except json.JSONDecodeError:
            continue
        provider_time = _provider_timestamp(
            normalized.get("updatedAt") if isinstance(normalized, dict) else None
        )
        ordering_time = provider_time or _provider_timestamp(_row_value(row, "created_at"))
        if ordering_time:
            updates.append((
                row["provider_updated_at_us"] or ordering_time[1],
                "provider" if provider_time else "receipt_fallback",
                row["event_id"],
            ))
    if updates:
        conn.executemany(
            "UPDATE ingress_events SET provider_updated_at_us = ?, "
            "provider_timestamp_source = ? WHERE event_id = ?",
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
        "normalized": json.loads(row["normalized_json"]),
        "hatchet_event_name": row["hatchet_event_name"],
        "hatchet_event_id": row["hatchet_event_id"],
        "hatchet_published": bool(row["hatchet_published"]),
        "published_at": row["published_at"],
        "publish_attempts": row["publish_attempts"],
        "publish_last_error": row["publish_last_error"],
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
    signature_verified: bool = False,
) -> tuple[dict[str, Any], bool]:
    if not isinstance(signature_verified, bool):
        raise ValueError("signature_verified must be a boolean")
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
    provider_time = _provider_timestamp(normalized.get("updatedAt"))
    ordering_time = provider_time or _provider_timestamp(now)

    with conn:
        existing = conn.execute(
            "SELECT * FROM ingress_events WHERE event_key = ?",
            (event_key,),
        ).fetchone()
        if existing is None:
            # A pre-digest runtime keyed canonical rows by an unsigned delivery
            # header. Retain that row when the same signed body is replayed after
            # upgrade so it cannot be published as a second canonical event.
            existing = conn.execute(
                """
                SELECT * FROM ingress_events
                WHERE source = ? AND provider_event = ? AND payload_hash = ?
                ORDER BY hatchet_published DESC, created_at, event_id
                LIMIT 1
                """,
                (source, provider_event, digest),
            ).fetchone()

        created = False
        if existing is None:
            inserted = conn.execute(
                """
                INSERT INTO ingress_events (
                    event_id, event_key, source, provider_event, action, delivery_id,
                    subject, payload_hash, raw_payload_json, normalized_json,
                    hatchet_event_name, decision, reason_code, created_at, updated_at,
                    provider_updated_at_us, provider_timestamp_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_key) DO NOTHING
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
                    # The digest is sufficient for idempotency/conflict evidence.
                    # Do not retain provider bodies, review text, identities, or
                    # any secret-shaped fields that normalization intentionally omits.
                    "{}",
                    canonical_json(normalized),
                    event_name,
                    decision,
                    reason_code,
                    now,
                    now,
                    ordering_time[1],
                    "provider" if provider_time else "receipt_fallback",
                ),
            )
            existing = conn.execute(
                "SELECT * FROM ingress_events WHERE event_key = ?",
                (event_key,),
            ).fetchone()
            created = inserted.rowcount == 1
        if not existing:  # Defensive: the insert or its conflicting row must exist.
            raise RuntimeError("ingress event insert did not produce a durable row")
        event = row_to_event(existing)
        disposition = (
            "accepted"
            if created
            else "duplicate"
            if event["payload_hash"] == digest
            else "payload_conflict"
        )
        conn.execute(
            """
            INSERT INTO ingress_receipts (
                receipt_id, event_key, source, provider_event, delivery_id,
                payload_hash, signature_verified, disposition, received_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                event["event_key"],
                source,
                provider_event,
                normalized.get("deliveryId"),
                digest,
                int(signature_verified),
                disposition,
                now,
            ),
        )

    # Raise only after the transaction commits so the conflicting receipt remains
    # observable without mutating the canonical event.
    if disposition == "payload_conflict":
        raise IngressConflict("event_key already exists with different payload")
    return event, created


def list_ingress_receipts(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    """Return receipt metadata without webhook bodies or normalized content."""
    if limit < 1 or limit > 250:
        raise ValueError("limit must be between 1 and 250")
    rows = conn.execute(
        """
        SELECT receipt_id, event_key, source, provider_event, delivery_id,
               payload_hash, signature_verified, disposition, received_at
        FROM ingress_receipts
        ORDER BY received_at DESC, receipt_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        {**dict(row), "signature_verified": bool(row["signature_verified"])}
        for row in rows
    ]


def list_pending_publications(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    if limit < 1 or limit > 250:
        raise ValueError("limit must be between 1 and 250")
    rows = conn.execute(
        "SELECT * FROM ingress_events WHERE hatchet_published = 0 ORDER BY created_at, event_id LIMIT ?",
        (limit,),
    ).fetchall()
    return [row_to_event(row) for row in rows]


def publication_status(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        "SELECT COUNT(*), MIN(created_at), MAX(publish_attempts) FROM ingress_events WHERE hatchet_published = 0"
    ).fetchone()
    return {
        "unpublished": int(row[0]),
        "oldest_unpublished_at": row[1],
        "max_publish_attempts": int(row[2] or 0),
    }


def claim_pending_publication(conn: sqlite3.Connection, event_key: str, *, lease_seconds: int = 60) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc)
    now_text = now.isoformat(timespec="microseconds").replace("+00:00", "Z")
    lease_id = str(uuid.uuid4())
    expires = (now + timedelta(seconds=max(1, min(lease_seconds, 300)))).isoformat(timespec="microseconds").replace("+00:00", "Z")
    with conn:
        changed = conn.execute(
            """
            UPDATE ingress_events
            SET publish_attempts = publish_attempts + 1,
                publish_lease_id = ?, publish_lease_expires_at = ?,
                publish_last_error = NULL, updated_at = ?
            WHERE event_key = ? AND hatchet_published = 0
              AND (publish_lease_expires_at IS NULL OR publish_lease_expires_at <= ?)
            """,
            (lease_id, expires, now_text, event_key, now_text),
        )
    return get_event(conn, event_key) | {"publish_lease_id": lease_id} if changed.rowcount == 1 else None


def mark_publication_failed(conn: sqlite3.Connection, event_key: str, lease_id: str, reason: str = "publisher_failed") -> None:
    with conn:
        conn.execute(
            """
            UPDATE ingress_events
            SET publish_lease_id = NULL, publish_lease_expires_at = NULL,
                publish_last_error = ?, updated_at = ?
            WHERE event_key = ? AND hatchet_published = 0 AND publish_lease_id = ?
            """,
            (reason[:120], utc_now(), event_key, lease_id),
        )


def mark_published(conn: sqlite3.Connection, event_key: str, hatchet_event_id: str | None, *, lease_id: str | None = None) -> dict[str, Any]:
    now = utc_now()
    with conn:
        row = conn.execute(
            "SELECT * FROM ingress_events WHERE event_key = ?",
            (event_key,),
        ).fetchone()
        if not row:
            raise IngressNotFound(f"event not found: {event_key}")
        changed = conn.execute(
            """
            UPDATE ingress_events
            SET hatchet_event_id = ?,
                hatchet_published = 1,
                published_at = COALESCE(published_at, ?),
                publish_lease_id = NULL,
                publish_lease_expires_at = NULL,
                publish_last_error = NULL,
                updated_at = ?
            WHERE event_key = ? AND (? IS NULL OR publish_lease_id = ?)
            """,
            (hatchet_event_id, now, now, event_key, lease_id, lease_id),
        )
        if lease_id is not None and changed.rowcount != 1:
            raise IngressConflict("publication lease is no longer current")
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
