"""Durable lifecycle outbox with leased, restart-safe delivery."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def future(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical_timestamp(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class Outbox:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.executescript("""
          CREATE TABLE IF NOT EXISTS lifecycle_outbox (
            outbox_key TEXT PRIMARY KEY, phase TEXT NOT NULL, delivery_state TEXT NOT NULL,
            issue_id TEXT, identifier TEXT, issue_url TEXT, job_id TEXT, idempotency_key TEXT,
            source_event_key TEXT, source_delivery_id TEXT, repo TEXT, pr_url TEXT,
            reason_code TEXT, execution_mode TEXT NOT NULL, source_kind TEXT NOT NULL,
            source_recorded_at TEXT NOT NULL, marker TEXT NOT NULL, body TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT, first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL, delivered_at TEXT, suppressed_at TEXT, last_error_code TEXT,
            lease_owner TEXT, lease_until TEXT
          );
          CREATE INDEX IF NOT EXISTS idx_lifecycle_delivery ON lifecycle_outbox(delivery_state, next_attempt_at);
          CREATE TABLE IF NOT EXISTS lifecycle_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        self._ensure_columns()
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_lifecycle_lease ON lifecycle_outbox(lease_until)")
        self._migrate_timestamps()

    def _ensure_columns(self) -> None:
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(lifecycle_outbox)")}
        for name in ("lease_owner", "lease_until"):
            if name not in columns:
                self.conn.execute(f"ALTER TABLE lifecycle_outbox ADD COLUMN {name} TEXT")

    def _migrate_timestamps(self) -> None:
        """Normalize parseable legacy instants before any lexical comparisons."""
        columns = (
            "source_recorded_at", "next_attempt_at", "first_seen_at", "last_seen_at",
            "delivered_at", "suppressed_at", "lease_until",
        )
        for column in columns:
            rows = self.conn.execute(
                f"SELECT rowid, {column} FROM lifecycle_outbox WHERE {column} IS NOT NULL"
            ).fetchall()
            for row in rows:
                normalized = canonical_timestamp(row[column])
                if normalized and normalized != row[column]:
                    self.conn.execute(
                        f"UPDATE lifecycle_outbox SET {column} = ? WHERE rowid = ?",
                        (normalized, row["rowid"]),
                    )

    def close(self) -> None:
        self.conn.close()

    def record(self, item: dict[str, Any]) -> bool:
        timestamp = now()
        values = {**item, "first_seen_at": timestamp, "last_seen_at": timestamp, "lease_owner": None, "lease_until": None}
        columns = list(values)
        placeholders = ",".join("?" for _ in columns)
        with self.conn:
            cursor = self.conn.execute(f"INSERT OR IGNORE INTO lifecycle_outbox ({','.join(columns)}) VALUES ({placeholders})", [values[column] for column in columns])
            if not cursor.rowcount:
                self.conn.execute("UPDATE lifecycle_outbox SET last_seen_at = ? WHERE outbox_key = ?", (timestamp, item["outbox_key"]))
        return bool(cursor.rowcount)

    def claim_one(self, owner: str, *, lease_seconds: int = 120) -> dict[str, Any] | None:
        """Atomically reserve one row, bounding the external-call lease window."""
        timestamp = now()
        with self.conn:
            self.conn.execute("UPDATE lifecycle_outbox SET delivery_state = 'retrying', lease_owner = NULL, lease_until = NULL WHERE delivery_state = 'claimed' AND lease_until <= ?", (timestamp,))
            row = self.conn.execute("""
              SELECT outbox_key FROM lifecycle_outbox
              WHERE delivery_state IN ('pending', 'retrying')
                AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
              ORDER BY first_seen_at LIMIT 1
            """, (timestamp,)).fetchone()
            if not row:
                return None
            key = row["outbox_key"]
            cursor = self.conn.execute("""
              UPDATE lifecycle_outbox SET delivery_state = 'claimed', lease_owner = ?, lease_until = ?
              WHERE outbox_key = ? AND delivery_state IN ('pending', 'retrying')
            """, (owner, future(lease_seconds), key))
            if not cursor.rowcount:
                return None
            claimed = self.conn.execute("SELECT * FROM lifecycle_outbox WHERE outbox_key = ? AND lease_owner = ?", (key, owner)).fetchone()
        return dict(claimed) if claimed else None

    def renew(self, key: str, owner: str, *, lease_seconds: int = 120) -> bool:
        """Fence each provider call: a reclaimed or expired lease cannot proceed."""
        with self.conn:
            cursor = self.conn.execute("""
              UPDATE lifecycle_outbox SET lease_until = ?
              WHERE outbox_key = ? AND delivery_state = 'claimed' AND lease_owner = ? AND lease_until > ?
            """, (future(lease_seconds), key, owner, now()))
        return bool(cursor.rowcount)

    def delivered(self, key: str, owner: str, *, recovered: bool = False) -> bool:
        with self.conn:
            cursor = self.conn.execute("""
              UPDATE lifecycle_outbox SET delivery_state = ?, delivered_at = ?, last_error_code = NULL,
                lease_owner = NULL, lease_until = NULL
              WHERE outbox_key = ? AND delivery_state = 'claimed' AND lease_owner = ? AND lease_until > ?
            """, ("recovered" if recovered else "delivered", now(), key, owner, now()))
        return bool(cursor.rowcount)

    def retry(self, key: str, owner: str, reason: str, *, max_attempts: int = 8) -> bool:
        row = self.conn.execute("SELECT attempts FROM lifecycle_outbox WHERE outbox_key = ? AND delivery_state = 'claimed' AND lease_owner = ? AND lease_until > ?", (key, owner, now())).fetchone()
        if not row:
            return False
        attempts = int(row["attempts"]) + 1
        state = "dead_letter" if attempts >= max_attempts else "retrying"
        delay = min(3600, 30 * (2 ** min(attempts - 1, 7)))
        with self.conn:
            cursor = self.conn.execute("""
              UPDATE lifecycle_outbox SET attempts = ?, delivery_state = ?, next_attempt_at = ?, last_error_code = ?,
                lease_owner = NULL, lease_until = NULL
              WHERE outbox_key = ? AND delivery_state = 'claimed' AND lease_owner = ? AND lease_until > ?
            """, (attempts, state, future(delay) if state != "dead_letter" else None, reason, key, owner, now()))
        return bool(cursor.rowcount)

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM lifecycle_meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO lifecycle_meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def suppress_legacy_pre_activation_unknown_modes(self, watermark: str) -> int:
        with self.conn:
            cursor = self.conn.execute("""
              UPDATE lifecycle_outbox SET delivery_state = 'suppressed', suppressed_at = ?,
                reason_code = 'pre_activation_unknown_mode_suppressed'
              WHERE delivery_state = 'dead_letter' AND reason_code = 'execution_mode_unknown'
                AND source_recorded_at <= ?
            """, (now(), watermark))
        return int(cursor.rowcount)

    def status(self) -> dict[str, Any]:
        connection = sqlite3.connect(self.path)
        try:
            states = {str(row[0]): int(row[1]) for row in connection.execute("SELECT delivery_state, COUNT(*) FROM lifecycle_outbox GROUP BY delivery_state")}
            row = connection.execute("SELECT MIN(first_seen_at) FROM lifecycle_outbox WHERE delivery_state IN ('pending', 'retrying', 'claimed')").fetchone()
            oldest = row[0] if row and row[0] else None
            age = None
            if oldest:
                age = max(0, int((datetime.now(timezone.utc) - datetime.fromisoformat(oldest.replace("Z", "+00:00"))).total_seconds()))
            return {"states": states, "oldest_pending_at": oldest, "oldest_queue_age_seconds": age, "dead_letters": states.get("dead_letter", 0)}
        finally:
            connection.close()
