from __future__ import annotations

import hashlib
import sqlite3
from typing import Any

from .db import Database
from .util import json_dumps, json_loads, new_id, utc_now


ZERO_HASH = "0" * 64


class Ledger:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _event_hash(
        *,
        project_id: str,
        event_type: str,
        actor: str,
        payload_json: str,
        parent_hash: str,
        created_at: str,
    ) -> str:
        envelope = json_dumps(
            {
                "project_id": project_id,
                "event_type": event_type,
                "actor": actor,
                "payload_json": payload_json,
                "parent_hash": parent_hash,
                "created_at": created_at,
            }
        )
        return hashlib.sha256(envelope.encode("utf-8")).hexdigest()

    def append(
        self,
        project_id: str,
        event_type: str,
        payload: dict[str, Any],
        actor: str = "system",
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        payload_json = json_dumps(payload)
        created_at = utc_now()
        event_id = new_id("evt")
        if connection is None:
            with self.database.connect() as own_connection:
                parent_hash, event_hash = self._insert(
                    own_connection,
                    event_id=event_id,
                    project_id=project_id,
                    event_type=event_type,
                    actor=actor,
                    payload_json=payload_json,
                    created_at=created_at,
                )
        else:
            parent_hash, event_hash = self._insert(
                connection,
                event_id=event_id,
                project_id=project_id,
                event_type=event_type,
                actor=actor,
                payload_json=payload_json,
                created_at=created_at,
            )
        return {
            "id": event_id,
            "project_id": project_id,
            "event_type": event_type,
            "actor": actor,
            "payload": payload,
            "parent_hash": parent_hash,
            "event_hash": event_hash,
            "created_at": created_at,
        }

    def _insert(
        self,
        connection: sqlite3.Connection,
        *,
        event_id: str,
        project_id: str,
        event_type: str,
        actor: str,
        payload_json: str,
        created_at: str,
    ) -> tuple[str, str]:
        previous = connection.execute(
            "SELECT event_hash FROM ledger_events WHERE project_id=? ORDER BY seq DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        parent_hash = previous["event_hash"] if previous else ZERO_HASH
        event_hash = self._event_hash(
            project_id=project_id,
            event_type=event_type,
            actor=actor,
            payload_json=payload_json,
            parent_hash=parent_hash,
            created_at=created_at,
        )
        connection.execute(
            """
            INSERT INTO ledger_events(id, project_id, event_type, actor, payload_json, parent_hash, event_hash, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                project_id,
                event_type,
                actor,
                payload_json,
                parent_hash,
                event_hash,
                created_at,
            ),
        )
        return parent_hash, event_hash

    def list(self, project_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM ledger_events WHERE project_id=? ORDER BY seq DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        return [
            {
                **dict(row),
                "payload": json_loads(row["payload_json"]),
            }
            for row in rows
        ]

    def verify(self, project_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM ledger_events WHERE project_id=? ORDER BY seq",
                (project_id,),
            ).fetchall()
        parent_hash = ZERO_HASH
        errors: list[dict[str, Any]] = []
        for row in rows:
            expected = self._event_hash(
                project_id=row["project_id"],
                event_type=row["event_type"],
                actor=row["actor"],
                payload_json=row["payload_json"],
                parent_hash=row["parent_hash"],
                created_at=row["created_at"],
            )
            if row["parent_hash"] != parent_hash:
                errors.append({"seq": row["seq"], "error": "parent_hash_mismatch"})
            if row["event_hash"] != expected:
                errors.append({"seq": row["seq"], "error": "event_hash_mismatch"})
            parent_hash = row["event_hash"]
        return {"valid": not errors, "event_count": len(rows), "head": parent_hash, "errors": errors}
