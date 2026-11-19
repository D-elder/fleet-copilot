"""SQLite schema + NDJSON loader."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

import yaml

from . import config
from .models import make_snapshot_id

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS snapshots(
  snapshot_id TEXT PRIMARY KEY,
  company_id  TEXT NOT NULL,
  device_id   TEXT NOT NULL,
  employee_id TEXT,
  collected_at TEXT NOT NULL,
  os_platform TEXT, os_name TEXT, os_version TEXT,
  model_name TEXT, model_identifier TEXT, processor TEXT,
  mem_total_bytes INTEGER, mem_used_bytes INTEGER,
  disk_size_bytes INTEGER, disk_available_bytes INTEGER,
  battery_present INTEGER,            -- NULL = no battery data in this snapshot (unknown)
  battery_condition TEXT, battery_cycles INTEGER,
  battery_full_charge INTEGER, battery_percentage INTEGER,
  raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snap_dev ON snapshots(company_id, device_id, collected_at);

CREATE TABLE IF NOT EXISTS compliance(
  snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
  check_id TEXT NOT NULL, status TEXT NOT NULL, severity TEXT NOT NULL,
  PRIMARY KEY(snapshot_id, check_id)
);

CREATE TABLE IF NOT EXISTS software(
  snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
  name TEXT NOT NULL, version TEXT, publisher TEXT
);
CREATE INDEX IF NOT EXISTS idx_sw_snap ON software(snapshot_id);

CREATE TABLE IF NOT EXISTS admins(
  admin_id TEXT PRIMARY KEY,
  company_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('viewer','approver'))
);

CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  company_id TEXT,
  actor TEXT,
  event TEXT NOT NULL,
  details_json TEXT NOT NULL DEFAULT '{}'
);
"""


def connect(path: str | Path = config.DB_PATH) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)  # autocommit; explicit BEGIN where needed
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def _disk_totals(volumes: list[dict[str, Any]] | None) -> tuple[int | None, int | None]:
    if not volumes:
        return None, None
    return (sum(v.get("size_bytes", 0) for v in volumes), sum(v.get("available_bytes", 0) for v in volumes))


def insert_record(conn: sqlite3.Connection, rec: dict[str, Any]) -> bool:
    """Insert one raw telemetry record. Returns False if it was a duplicate snapshot."""
    sid = make_snapshot_id(rec["device_id"], rec["collected_at"])
    os_ = rec.get("os") or {}
    ident = rec.get("device_identity") or {}
    mem = rec.get("memory") or {}
    bat = rec.get("battery")
    disk_size, disk_avail = _disk_totals(rec.get("disk_volumes"))
    cur = conn.execute(
        """INSERT OR IGNORE INTO snapshots VALUES
           (?,?,?,?,?, ?,?,?, ?,?,?, ?,?, ?,?, ?,?,?,?,?, ?)""",
        (
            sid, rec["company_id"], rec["device_id"], rec.get("employee_id"), rec["collected_at"],
            os_.get("platform"), os_.get("product_name"), os_.get("product_version"),
            ident.get("model_name"), ident.get("model_identifier"), ident.get("processor"),
            mem.get("total_memory_bytes") or mem.get("ram_bytes"), mem.get("used_memory_bytes"),
            disk_size, disk_avail,
            None if bat is None else int(bool(bat.get("battery_present", True))),
            (bat or {}).get("condition"), (bat or {}).get("cycle_count"),
            (bat or {}).get("full_charge_capacity"), (bat or {}).get("percentage"),
            json.dumps(rec, sort_keys=True),
        ),
    )
    if cur.rowcount == 0:
        return False
    for c in rec.get("compliance_results") or []:
        conn.execute("INSERT INTO compliance VALUES (?,?,?,?)", (sid, c["check_id"], c["status"], c["severity"]))
    for s in rec.get("installed_software") or []:
        conn.execute("INSERT INTO software VALUES (?,?,?,?)", (sid, s["name"], s.get("version"), s.get("publisher")))
    return True


def load_records(conn: sqlite3.Connection, records: Iterable[dict[str, Any]]) -> dict[str, int]:
    loaded = dup = 0
    conn.execute("BEGIN")
    try:
        for rec in records:
            if insert_record(conn, rec):
                loaded += 1
            else:
                dup += 1
        last = conn.execute("SELECT MAX(collected_at) FROM snapshots").fetchone()[0]
        if last:
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('as_of', ?)", (last,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"loaded": loaded, "duplicates": dup}


def load_ndjson(conn: sqlite3.Connection, path: str | Path = config.DATASET_PATH) -> dict[str, int]:
    records, bad = [], 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                bad += 1
    stats = load_records(conn, records)
    stats["malformed_lines"] = bad
    return stats


def seed_admins(conn: sqlite3.Connection, path: str | Path = config.ADMINS_PATH) -> int:
    admins = yaml.safe_load(Path(path).read_text(encoding="utf-8"))["admins"]
    for a in admins:
        conn.execute("INSERT OR REPLACE INTO admins VALUES (?,?,?)", (a["admin_id"], a["company_id"], a["role"]))
    return len(admins)


def bootstrap(path: str | Path = config.DB_PATH, dataset: str | Path = config.DATASET_PATH) -> sqlite3.Connection:
    """Open the DB, loading the dataset and admins on first use."""
    conn = connect(path)
    if conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0:
        load_ndjson(conn, dataset)
    seed_admins(conn)
    return conn
