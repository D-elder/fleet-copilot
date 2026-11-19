"""Company-scoped data access.

Every query in this module is parameterised by ``company_id`` taken from the Session. A device, employee or
snapshot id that belongs to another company is indistinguishable from one that does not exist.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from . import config
from .models import DeviceFilters, Excluded, ResultRow, ToolResult, Window, make_result_id
from .session import Session


# --- pure helpers (unit-tested directly) ------------------------------------
def parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


def os_family(os_name: str | None, platform: str | None = None) -> str | None:
    n = (os_name or "").lower()
    if "mac" in n or platform == "darwin":
        return "macos"
    if "windows" in n or platform == "win32":
        return "windows"
    return None


def parse_os_version(family: str, version: str) -> tuple[int, ...]:
    """macOS '14.5' -> (14, 5). Windows '11 23H2' -> (11, 23, 2). Raises ValueError on anything else."""
    v = version.strip()
    if family == "macos":
        if not re.fullmatch(r"\d+(\.\d+){0,2}", v):
            raise ValueError(f"unparseable macOS version {version!r}")
        return tuple(int(p) for p in v.split("."))
    if family == "windows":
        m = re.fullmatch(r"(\d+)\s+(\d{2})H([12])", v, flags=re.I)
        if not m:
            raise ValueError(f"unparseable Windows version {version!r} (expected e.g. '11 23H2')")
        return tuple(int(g) for g in m.groups())
    raise ValueError(f"unknown OS family {family!r}")


def form_factor(model_name: str | None) -> str | None:
    n = (model_name or "").lower()
    for prefix, ff in config.FORM_FACTOR_BY_MODEL_PREFIX.items():
        if n.startswith(prefix):
            return ff
    return None


def pct(num: float | None, den: float | None) -> float | None:
    if num is None or not den:
        return None
    return round(100.0 * num / den, 1)


# --- snapshot value object --------------------------------------------------
@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    company_id: str
    device_id: str
    employee_id: str | None
    collected_at: str
    os_platform: str | None
    os_name: str | None
    os_version: str | None
    model_name: str | None
    model_identifier: str | None
    mem_total_bytes: int | None
    mem_used_bytes: int | None
    disk_size_bytes: int | None
    disk_available_bytes: int | None
    battery_present: int | None
    battery_condition: str | None
    battery_cycles: int | None
    battery_full_charge: int | None

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "Snapshot":
        return cls(**{f: r[f] for f in cls.__dataclass_fields__})

    @property
    def disk_free_pct(self) -> float | None:
        return pct(self.disk_available_bytes, self.disk_size_bytes)

    @property
    def mem_used_pct(self) -> float | None:
        return pct(self.mem_used_bytes, self.mem_total_bytes)

    @property
    def family(self) -> str | None:
        return os_family(self.os_name, self.os_platform)


_SNAP_COLS = ", ".join(Snapshot.__dataclass_fields__)


class Repository:
    def __init__(self, conn: sqlite3.Connection, session: Session):
        self.conn = conn
        self.session = session
        self.company_id = session.company_id

    # --- basics -------------------------------------------------------------
    def as_of(self) -> str:
        row = self.conn.execute("SELECT value FROM meta WHERE key='as_of'").fetchone()
        if row is None:
            raise RuntimeError("dataset not loaded: meta.as_of missing")
        return row["value"]

    def device_ids(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT device_id FROM snapshots WHERE company_id=? ORDER BY device_id", (self.company_id,))
        return [r["device_id"] for r in rows]

    def latest_snapshots(self) -> dict[str, Snapshot]:
        rows = self.conn.execute(
            f"""SELECT {_SNAP_COLS} FROM snapshots s
                WHERE company_id=? AND collected_at=(
                  SELECT MAX(collected_at) FROM snapshots WHERE company_id=s.company_id AND device_id=s.device_id)
                ORDER BY device_id""", (self.company_id,))
        return {r["device_id"]: Snapshot.from_row(r) for r in rows}

    def get_snapshot(self, snapshot_id: str) -> Snapshot | None:
        row = self.conn.execute(
            f"SELECT {_SNAP_COLS} FROM snapshots WHERE snapshot_id=? AND company_id=?",
            (snapshot_id, self.company_id)).fetchone()
        return Snapshot.from_row(row) if row else None

    def get_raw_snapshot(self, snapshot_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT raw_json FROM snapshots WHERE snapshot_id=? AND company_id=?",
            (snapshot_id, self.company_id)).fetchone()
        return row["raw_json"] if row else None

    def history(self, device_id: str, since: str | None = None) -> list[Snapshot]:
        q = f"SELECT {_SNAP_COLS} FROM snapshots WHERE company_id=? AND device_id=?"
        args: list[Any] = [self.company_id, device_id]
        if since:
            q += " AND collected_at>=?"; args.append(since)
        return [Snapshot.from_row(r) for r in self.conn.execute(q + " ORDER BY collected_at", args)]

    def compliance(self, snapshot_id: str) -> list[dict[str, str]]:
        rows = self.conn.execute(
            """SELECT c.check_id, c.status, c.severity FROM compliance c
               JOIN snapshots s ON s.snapshot_id=c.snapshot_id
               WHERE c.snapshot_id=? AND s.company_id=? ORDER BY c.check_id""",
            (snapshot_id, self.company_id))
        return [dict(r) for r in rows]

    def software(self, snapshot_id: str) -> list[str]:
        rows = self.conn.execute(
            """SELECT w.name FROM software w JOIN snapshots s ON s.snapshot_id=w.snapshot_id
               WHERE w.snapshot_id=? AND s.company_id=? ORDER BY w.name""",
            (snapshot_id, self.company_id))
        return [r["name"] for r in rows]

    def employee_exists(self, employee_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM snapshots WHERE company_id=? AND employee_id=? LIMIT 1",
            (self.company_id, employee_id)).fetchone() is not None

    def known_check_ids(self) -> list[str]:
        rows = self.conn.execute(
            """SELECT DISTINCT c.check_id FROM compliance c JOIN snapshots s ON s.snapshot_id=c.snapshot_id
               WHERE s.company_id=? ORDER BY 1""", (self.company_id,))
        return [r["check_id"] for r in rows]

    # --- staleness ----------------------------------------------------------
    def hours_behind(self, snap: Snapshot) -> float:
        return (parse_ts(self.as_of()) - parse_ts(snap.collected_at)).total_seconds() / 3600.0

    def is_stale(self, snap: Snapshot) -> bool:
        return self.hours_behind(snap) > config.STALE_AFTER_HOURS

    # --- query_devices ------------------------------------------------------
    def query_devices(self, f: DeviceFilters) -> ToolResult:
        latest = self.latest_snapshots()
        population = sorted(latest)
        rows: list[ResultRow] = []
        excluded: list[Excluded] = []
        notes: list[str] = []

        threshold = parse_os_version(f.os_family, f.os_version_lt) if f.os_version_lt else None

        if f.failing_check and f.failing_check not in self.known_check_ids():
            notes.append(f"check_id {f.failing_check!r} does not exist in this company's telemetry; "
                         f"known checks: {', '.join(self.known_check_ids())}")

        for device_id in population:
            s = latest[device_id]
            if self.is_stale(s):
                excluded.append(Excluded(device_id=device_id,
                                         reason=f"stale: last snapshot {s.collected_at} is "
                                                f"{self.hours_behind(s):.0f}h before as-of {self.as_of()}"))
                continue

            reason = self._mismatch_reason(s, f, threshold)
            if reason is not None:
                if reason.startswith("not ") or reason.startswith("no "):
                    excluded.append(Excluded(device_id=device_id, reason=reason))
                continue

            comp = self.compliance(s.snapshot_id)
            rows.append(ResultRow(
                device_id=device_id, snapshot_id=s.snapshot_id, collected_at=s.collected_at,
                values={
                    "employee_id": s.employee_id, "model": s.model_name, "form_factor": form_factor(s.model_name),
                    "os": f"{s.os_name} {s.os_version}", "disk_free_pct": s.disk_free_pct,
                    "mem_used_pct": s.mem_used_pct,
                    "failing_checks": [{"check_id": c["check_id"], "severity": c["severity"]}
                                       for c in comp if c["status"] == "fail"],
                }))

        filters = f.model_dump(exclude_none=True)
        starts = [s.collected_at for s in latest.values()]
        return ToolResult(
            result_id=make_result_id("query_devices", self.company_id, filters, self.as_of()),
            tool="query_devices", company_id=self.company_id, filters=filters,
            window=Window(start=min(starts) if starts else None, end=self.as_of()), as_of=self.as_of(),
            population_checked=population, count=len(rows), rows=rows, excluded=excluded, notes=notes)

    def _mismatch_reason(self, s: Snapshot, f: DeviceFilters, threshold: tuple[int, ...] | None) -> str | None:
        """None => device matches. Reasons starting 'not '/'no ' are reported as exclusions (unknown / n.a.);
        other reasons are ordinary non-matches."""
        if f.form_factor and form_factor(s.model_name) != f.form_factor:
            return "filtered: form factor"
        if f.model_name_contains and f.model_name_contains.lower() not in (s.model_name or "").lower():
            return "filtered: model"
        if f.employee_id and s.employee_id != f.employee_id:
            return "filtered: employee"
        if f.os_family and s.family != f.os_family:
            return f"not {f.os_family}: device runs {s.os_name} {s.os_version}"
        if threshold is not None:
            try:
                if parse_os_version(f.os_family, s.os_version or "") >= threshold:
                    return "filtered: os version"
            except ValueError:
                return f"no comparable OS version: {s.os_version!r}"
        if f.disk_free_pct_lt is not None:
            if s.disk_free_pct is None:
                return "no disk telemetry in latest snapshot"
            if not s.disk_free_pct < f.disk_free_pct_lt:
                return "filtered: disk"
        if f.mem_used_pct_gt is not None:
            if s.mem_used_pct is None:
                return "no memory telemetry in latest snapshot"
            if not s.mem_used_pct > f.mem_used_pct_gt:
                return "filtered: memory"
        if f.has_software:
            if not any(f.has_software.lower() in n.lower() for n in self.software(s.snapshot_id)):
                return "filtered: software"
        if f.failing_check or f.check_severity:
            # Failure, check_id and severity are all matched on the SAME compliance row.
            if not any(c["status"] == "fail"
                       and (not f.failing_check or c["check_id"] == f.failing_check)
                       and (not f.check_severity or c["severity"] == f.check_severity)
                       for c in self.compliance(s.snapshot_id)):
                return "filtered: compliance"
        return None
