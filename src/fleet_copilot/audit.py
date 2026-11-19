"""Append-only audit log. Callers inside a transaction pass the same connection so the row commits atomically."""
from __future__ import annotations

import json
import sqlite3
from typing import Any


def record(conn: sqlite3.Connection, *, company_id: str | None, actor: str | None, event: str,
           details: dict[str, Any] | None = None) -> None:
    conn.execute(
        "INSERT INTO audit_log(company_id, actor, event, details_json) VALUES (?,?,?,?)",
        (company_id, actor, event, json.dumps(details or {}, sort_keys=True, default=str)),
    )


def events(conn: sqlite3.Connection, company_id: str | None = None, event: str | None = None) -> list[sqlite3.Row]:
    q, args = "SELECT * FROM audit_log WHERE 1=1", []
    if company_id is not None:
        q += " AND company_id=?"; args.append(company_id)
    if event is not None:
        q += " AND event=?"; args.append(event)
    return conn.execute(q + " ORDER BY id", args).fetchall()
