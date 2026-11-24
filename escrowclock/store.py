"""Durable SQLite event log and idempotent command dispatcher."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from . import model
from .canonical import canonical_text, short_hash
from .errors import DeterministicCrash, EscrowError, ReplayIntegrityError
from .protocol import error_envelope, replay_envelope, request_hash, success_envelope

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    event_time INTEGER,
    request_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS request_records (
    request_id TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    command TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('applied', 'rejected')),
    error_code TEXT,
    event_seq INTEGER,
    head_seq_before INTEGER NOT NULL DEFAULT 0,
    response_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL,
    state_hash TEXT NOT NULL,
    state_json TEXT NOT NULL,
    created_at_request TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

CRASH_POINTS = (
    "before_write",
    "before_row_write",
    "after_write",
    "after_commit",
)


class Store:
    def __init__(
        self,
        path: str | Path = "escrowclock.db",
        *,
        crash_point: str | None = None,
        crash_mode: str = "exception",
    ) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self.crash_point = crash_point
        self.crash_mode = crash_mode
        self.conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=30000")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA_SQL)
        self._migrate_schema()

    def _migrate_schema(self) -> None:
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(request_records)")}
        if "head_seq_before" not in columns:
            self.conn.execute(
                "ALTER TABLE request_records ADD COLUMN head_seq_before INTEGER NOT NULL DEFAULT 0"
            )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Crash injection
    # ------------------------------------------------------------------

    def trigger_crash(self, point: str, context: dict[str, Any] | None = None) -> None:
        if self.crash_point != point:
            return
        marker = {
            "ok": False,
            "crash": True,
            "point": point,
            "context": context or {},
        }
        if self.crash_mode == "process":
            sys.stdout.write(json.dumps(marker, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            # os._exit is the closest local simulation of a hard process/host
            # crash: no finally blocks, buffers, or interpreter shutdown.
            os._exit(86)
        raise DeterministicCrash(point)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def dispatch(self, command: dict[str, Any]) -> dict[str, Any]:
        request_id = command.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("command.request_id is required")
        digest = request_hash(command)

        self.trigger_crash("before_write", {"request_id": request_id})

        self.conn.execute("BEGIN IMMEDIATE")
        response: dict[str, Any]
        try:
            row = self.conn.execute(
                "SELECT * FROM request_records WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is not None:
                self.conn.execute("ROLLBACK")
                if row["request_hash"] != digest:
                    return error_envelope(
                        {
                            "code": "IDEMPOTENCY_KEY_CONFLICT",
                            "message": "request_id was already used with different arguments",
                            "details": {"request_id": request_id},
                            "retryable": False,
                        },
                        request_id,
                    )
                stored = json.loads(row["response_json"])
                return replay_envelope(stored)

            self.trigger_crash("before_row_write", {"request_id": request_id})
            state = self.replay_state()
            head_seq_before = int(state["head_seq"])

            event: dict[str, Any] | None = None
            event_seq: int | None = None
            error_code: str | None = None
            status: str

            try:
                next_state, event = model.apply_command(state, command)
                receipt = model.build_receipt(command, event, next_state)
                response = success_envelope(receipt)
                status = "applied"
                event_seq = int(event["seq"])
                self._insert_event(event)
            except EscrowError as exc:
                response = error_envelope(exc, request_id)
                status = "rejected"
                error_code = exc.code

            self.conn.execute(
                """
                INSERT INTO request_records(
                    request_id, request_hash, command, status,
                    error_code, event_seq, head_seq_before, response_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    digest,
                    command.get("command", "unknown"),
                    status,
                    error_code,
                    event_seq,
                    head_seq_before,
                    canonical_text(response),
                ),
            )

            self.trigger_crash(
                "after_write",
                {"request_id": request_id, "status": status, "event_seq": event_seq},
            )
            self.conn.execute("COMMIT")
        except BaseException:
            # DeterministicCrash is raised before commit in exception mode.
            # Rollback makes the simulated crash equivalent to a dead process
            # whose uncommitted SQLite transaction is recovered later.
            try:
                self.conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

        self.trigger_crash("after_commit", {"request_id": request_id, "event_seq": event_seq})
        return response

    def _insert_event(self, event: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO events(
                seq, event_id, event_type, event_time, request_id,
                payload_json, prev_hash, event_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event["seq"],
                event["event_id"],
                event["event_type"],
                event["event_time"],
                event["request_id"],
                canonical_text(event["payload"]),
                event["prev_hash"],
                event["event_hash"],
            ),
        )

    # ------------------------------------------------------------------
    # Replay / snapshots / reads
    # ------------------------------------------------------------------

    def _event_rows(self, up_to_seq: int | None = None) -> list[sqlite3.Row]:
        if up_to_seq is None:
            return list(self.conn.execute("SELECT * FROM events ORDER BY seq"))
        return list(
            self.conn.execute("SELECT * FROM events WHERE seq <= ? ORDER BY seq", (up_to_seq,))
        )

    @staticmethod
    def row_to_event(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        if isinstance(row, sqlite3.Row):
            return {
                "seq": row["seq"],
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "event_time": row["event_time"],
                "request_id": row["request_id"],
                "payload": json.loads(row["payload_json"]),
                "prev_hash": row["prev_hash"],
                "event_hash": row["event_hash"],
            }
        return dict(row)

    def replay_state(
        self, up_to_seq: int | None = None, at: int | None = None
    ) -> dict[str, Any]:
        # Always verify the complete contiguous prefix known to the snapshot.
        prefix = model.initial_state()
        for row in self._event_rows(up_to_seq):
            prefix = model.reduce_event(prefix, self.row_to_event(row), verify=True)

        if at is None:
            return prefix

        limit = prefix["head_seq"]
        rows = self.conn.execute(
            """
            SELECT * FROM events
            WHERE seq <= ? AND (event_time IS NULL OR event_time <= ?)
            ORDER BY seq
            """,
            (limit, at),
        ).fetchall()
        temporal = model.initial_state()
        for row in rows:
            # Time-filtered replay intentionally skips future-time events, so
            # hash-chain continuity is checked by the prefix pass above.
            temporal = model.reduce_event(temporal, self.row_to_event(row), verify=False)
        return temporal

    def recover(self) -> dict[str, Any]:
        integrity = self.conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ReplayIntegrityError("SQLite integrity check failed", {"result": integrity})
        state = self.replay_state()
        request_count = self.conn.execute("SELECT COUNT(*) FROM request_records").fetchone()[0]
        applied_count = self.conn.execute(
            "SELECT COUNT(*) FROM request_records WHERE status='applied'"
        ).fetchone()[0]
        rejected_count = self.conn.execute(
            "SELECT COUNT(*) FROM request_records WHERE status='rejected'"
        ).fetchone()[0]
        return {
            "ok": True,
            "replayed_events": state["head_seq"],
            "head_hash": state["head_hash"],
            "state_hash": model.state_hash(state),
            "requests": request_count,
            "applied": applied_count,
            "rejected": rejected_count,
            "integrity_check": integrity,
        }

    def create_snapshot(self, snapshot_id: str | None = None) -> dict[str, Any]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            state = self.replay_state()
            seq = int(state["head_seq"])
            digest = model.state_hash(state)
            sid = snapshot_id or f"snp-{seq:08d}-{short_hash(digest)}"
            existing = self.conn.execute(
                "SELECT * FROM snapshots WHERE snapshot_id = ?", (sid,)
            ).fetchone()
            if existing is not None:
                if existing["state_hash"] != digest or int(existing["seq"]) != seq:
                    raise EscrowError(
                        "snapshot_id already exists with different state",
                        {"snapshot_id": sid},
                    )
                result = {
                    "snapshot_id": sid,
                    "seq": seq,
                    "state_hash": digest,
                    "replayed": True,
                }
            else:
                self.conn.execute(
                    """
                    INSERT INTO snapshots(snapshot_id, seq, state_hash, state_json, created_at_request)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (sid, seq, digest, canonical_text(state), "manual"),
                )
                result = {
                    "snapshot_id": sid,
                    "seq": seq,
                    "state_hash": digest,
                    "replayed": False,
                }
            self.conn.execute("COMMIT")
            return result
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def resolve_snapshot_seq(self, snapshot: str | int | None) -> int | None:
        if snapshot is None:
            return None
        if isinstance(snapshot, int) or str(snapshot).isdigit():
            return int(snapshot)
        snapshot_text = str(snapshot)
        row = self.conn.execute(
            "SELECT seq, state_hash, state_json FROM snapshots WHERE snapshot_id = ?",
            (snapshot_text,),
        ).fetchone()
        if row is not None:
            state = self.replay_state(up_to_seq=int(row["seq"]))
            if model.state_hash(state) != row["state_hash"]:
                raise ReplayIntegrityError(
                    "stored snapshot does not match event replay",
                    {"snapshot": snapshot, "seq": row["seq"]},
                )
            return int(row["seq"])

        hash_row = self.conn.execute(
            "SELECT seq FROM snapshots WHERE state_hash = ?", (snapshot_text,)
        ).fetchone()
        if hash_row is not None:
            return int(hash_row["seq"])

        # A receipt's state_hash is also a known snapshot commitment.
        if len(snapshot_text) == 64:
            state = model.initial_state()
            for event_row in self._event_rows():
                state = model.reduce_event(state, self.row_to_event(event_row), verify=True)
                if model.state_hash(state) == snapshot_text:
                    return int(state["head_seq"])

        raise EscrowError("unknown snapshot", {"snapshot": snapshot})

    def list_request_records(
        self, snapshot_seq: int | None = None
    ) -> list[dict[str, Any]]:
        if snapshot_seq is None:
            rows = self.conn.execute(
                "SELECT request_id, request_hash, command, status, error_code, "
                "event_seq, head_seq_before, response_json "
                "FROM request_records ORDER BY rowid"
            ).fetchall()
        else:
            # Snapshot S contains the event S. A request processed while head=H
            # commits event H+1 if applied; it is visible to snapshots S>H.
            rows = self.conn.execute(
                "SELECT request_id, request_hash, command, status, error_code, "
                "event_seq, head_seq_before, response_json "
                "FROM request_records WHERE head_seq_before < ? ORDER BY rowid",
                (snapshot_seq,),
            ).fetchall()
        result = []
        for row in rows:
            item = {
                "request_id": row["request_id"],
                "request_hash": row["request_hash"],
                "command": row["command"],
                "status": row["status"],
                "error_code": row["error_code"],
                "event_seq": row["event_seq"],
                "head_seq_before": row["head_seq_before"],
                "response": json.loads(row["response_json"]),
            }
            result.append(item)
        return result

    def claim_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM events WHERE event_type='ClaimTaken'").fetchone()[0])

    def verify_receipt(self, receipt: dict[str, Any]) -> dict[str, Any]:
        seq = receipt.get("seq")
        if not isinstance(seq, int):
            raise ReplayIntegrityError("receipt has no integer seq")
        row = self.conn.execute("SELECT * FROM events WHERE seq = ?", (seq,)).fetchone()
        if row is None:
            raise ReplayIntegrityError("receipt sequence is absent from event log", {"seq": seq})
        event = self.row_to_event(row)
        prefix_state = self.replay_state(up_to_seq=seq)
        prefix_hash = model.state_hash(prefix_state)

        checks = {
            "event_hash_matches": event["event_hash"] == receipt.get("event_hash"),
            "prev_hash_matches": event["prev_hash"] == receipt.get("prev_hash"),
            "state_hash_matches": prefix_hash == receipt.get("state_hash"),
            "receipt_version_supported": receipt.get("receipt_version") == 1,
        }
        valid = all(checks.values())
        if not valid:
            raise ReplayIntegrityError("receipt replay verification failed", {"seq": seq, "checks": checks})
        return {
            "valid": True,
            "seq": seq,
            "event_hash": event["event_hash"],
            "replayed_state_hash": prefix_hash,
            "checks": checks,
        }
