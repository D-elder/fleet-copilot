"""Deterministic trend detection over each device's 30-day snapshot history.

Every finding carries the measurements behind it and the exact snapshot ids that support it, so an admin (and the
action policy) can verify it. The finding text and explanation are templates filled from those measurements; the
LLM never writes them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import config
from .models import Excluded, ResultRow, ToolResult, Window, make_result_id
from .repository import Repository, Snapshot, parse_ts

KINDS = ("battery_health", "memory_pressure", "storage", "compliance_drift")


@dataclass
class Finding:
    kind: str
    device_id: str
    status: str
    finding: str
    explanation: str
    metrics: dict[str, Any]
    evidence_snapshot_ids: list[str]
    latest_snapshot_id: str
    latest_collected_at: str
    series: list[list[Any]] = field(default_factory=list)


def _span_days(first: Snapshot, last: Snapshot) -> float:
    return (parse_ts(last.collected_at) - parse_ts(first.collected_at)).total_seconds() / 86400.0


def _day(ts: str) -> str:
    return ts[:10]


def _newest_first(ids: list[str]) -> list[str]:
    """Unique snapshot ids, most recent first (so "cite the first few" cites fresh evidence). Ids end in @timestamp."""
    return sorted(set(ids), key=lambda i: i.split("@", 1)[1], reverse=True)



# --- detectors ---------------------------------------------------------------
def _memory(device_id: str, snaps: list[Snapshot]) -> Finding | Excluded | None:
    pts = [(s, s.mem_used_pct) for s in snaps if s.mem_used_pct is not None]
    if len(pts) < config.MIN_READINGS or _span_days(pts[0][0], pts[-1][0]) < config.TREND_MIN_SPAN_DAYS:
        return Excluded(device_id=device_id, reason=f"insufficient memory telemetry ({len(pts)} readings)")
    thr = config.RAM_PRESSURE_USED_PCT
    over = [s for s, v in pts if v > thr]
    share = len(over) / len(pts)
    peak = max(v for _, v in pts)
    span = _span_days(pts[0][0], pts[-1][0])
    if share < config.RAM_INTERMITTENT_MIN_SHARE:
        return None
    if share < config.RAM_PRESSURE_MIN_SHARE:
        return Excluded(device_id=device_id, reason=(
            f"intermittent memory pressure: {len(over)} of {len(pts)} snapshots ({share:.0%}) above {thr:.0f}% used, "
            f"below the {config.RAM_PRESSURE_MIN_SHARE:.0%} bar for 'sustained'"))
    last = pts[-1][1]
    return Finding(
        kind="memory_pressure", device_id=device_id, status="sustained",
        finding=(f"Sustained memory pressure: {len(over)} of {len(pts)} snapshots ({share:.0%}) over {span:.0f} days "
                 f"show more than {thr:.0f}% of RAM in use (latest {last}%, peak {peak}%)."),
        explanation=("Memory use stays above the threshold for most of the window, so the pressure is persistent "
                     "rather than a one-off spike."),
        metrics={"snapshots_over_threshold": len(over), "snapshots_total": len(pts), "share": round(share, 2),
                 "threshold_pct": thr, "latest_used_pct": last, "peak_used_pct": peak, "span_days": round(span, 1)},
        evidence_snapshot_ids=_newest_first([s.snapshot_id for s in over]), latest_snapshot_id=snaps[-1].snapshot_id,
        latest_collected_at=snaps[-1].collected_at, series=[[s.collected_at, v] for s, v in pts])


def _storage(device_id: str, snaps: list[Snapshot]) -> Finding | Excluded | None:
    pts = [(s, s.disk_free_pct) for s in snaps if s.disk_free_pct is not None]
    if len(pts) < config.MIN_READINGS or _span_days(pts[0][0], pts[-1][0]) < config.TREND_MIN_SPAN_DAYS:
        return Excluded(device_id=device_id, reason=f"insufficient disk telemetry ({len(pts)} readings)")
    thr = config.STORAGE_LOW_FREE_PCT
    low = [s for s, v in pts if v < thr]
    share = len(low) / len(pts)
    first_v, last_v = pts[0][1], pts[-1][1]
    drop = first_v - last_v
    span = _span_days(pts[0][0], pts[-1][0])
    if last_v >= thr:
        return None
    if share < config.STORAGE_MIN_SHARE:
        return Excluded(device_id=device_id, reason=(
            f"recently low on storage: free space is {last_v}% now but only {len(low)} of {len(pts)} snapshots "
            f"({share:.0%}) are below {thr:.0f}% free, short of the {config.STORAGE_MIN_SHARE:.0%} bar for 'consistently low'"))
    declining = drop >= config.STORAGE_DECLINE_PP
    return Finding(
        kind="storage", device_id=device_id, status="low_and_declining" if declining else "consistently_low",
        finding=(f"Consistently low storage: {len(low)} of {len(pts)} snapshots ({share:.0%}) over {span:.0f} days have "
                 f"less than {thr:.0f}% free (latest {last_v}%, started at {first_v}%"
                 + (f", down {drop:.1f} points)." if declining else ").")),
        explanation=("Free space has stayed below the threshold for most of the window"
                     + (" and kept shrinking" if declining else "") + ", so the shortage is persistent rather than "
                     "a one-off dip."),
        metrics={"snapshots_below_threshold": len(low), "snapshots_total": len(pts), "share": round(share, 2),
                 "threshold_free_pct": thr, "latest_free_pct": last_v, "first_free_pct": first_v,
                 "decline_points": round(drop, 1), "span_days": round(span, 1)},
        evidence_snapshot_ids=_newest_first([s.snapshot_id for s in low]), latest_snapshot_id=snaps[-1].snapshot_id,
        latest_collected_at=snaps[-1].collected_at, series=[[s.collected_at, v] for s, v in pts])


def _battery(device_id: str, snaps: list[Snapshot]) -> Finding | Excluded | None:
    if not any(s.battery_present is not None for s in snaps):
        return Excluded(device_id=device_id, reason="no battery telemetry in any snapshot (not applicable)")
    rd = [s for s in snaps if s.battery_present == 1 and s.battery_full_charge]
    if len(rd) < config.BATTERY_MIN_READINGS or _span_days(rd[0], rd[-1]) < config.BATTERY_MIN_SPAN_DAYS:
        return Excluded(device_id=device_id, reason=f"insufficient battery readings ({len(rd)})")
    first, last = rd[0], rd[-1]
    drop = 100.0 * (first.battery_full_charge - last.battery_full_charge) / first.battery_full_charge
    cycles = last.battery_cycles or 0
    service = [s for s in rd if s.battery_condition == "Service Recommended"]
    at_risk = last.battery_condition == "Service Recommended" or (
        drop >= config.BATTERY_EOL_CAPACITY_DROP_PCT and cycles > config.BATTERY_EOL_MIN_CYCLES)
    if not at_risk:
        return None
    span = _span_days(first, last)
    since = f"; 'Service Recommended' since {_day(service[0].collected_at)}" if service else ""
    return Finding(
        kind="battery_health", device_id=device_id, status="end_of_life_risk",
        finding=(f"Battery approaching end of life: full-charge capacity fell {drop:.1f}% "
                 f"({first.battery_full_charge} to {last.battery_full_charge}) over {span:.0f} days at {cycles} "
                 f"cycles; latest condition '{last.battery_condition}'{since}."),
        explanation=("Full-charge capacity has fallen while the cycle count is high and/or the OS reports 'Service "
                     "Recommended'; the battery is a candidate for service or replacement."),
        metrics={"first_full_charge": first.battery_full_charge, "latest_full_charge": last.battery_full_charge,
                 "capacity_drop_pct": round(drop, 1), "cycle_count": cycles,
                 "latest_condition": last.battery_condition, "service_recommended_snapshots": len(service),
                 "readings": len(rd), "span_days": round(span, 1)},
        evidence_snapshot_ids=_newest_first([first.snapshot_id] + [s.snapshot_id for s in service]
                                              + [last.snapshot_id]),
        latest_snapshot_id=snaps[-1].snapshot_id, latest_collected_at=snaps[-1].collected_at,
        series=[[s.collected_at, s.battery_full_charge] for s in rd])


def _compliance(repo: Repository, device_id: str, snaps: list[Snapshot]) -> Finding | Excluded | None:
    per_check: dict[str, list[tuple[Snapshot, str, str]]] = {}
    for s in snaps:
        for c in repo.compliance(s.snapshot_id):
            per_check.setdefault(c["check_id"], []).append((s, c["status"], c["severity"]))
    drifted, evidence = [], []
    for check_id, seq in sorted(per_check.items()):
        if len(seq) < 2 or seq[-1][1] != "fail" or seq[0][1] != "pass":
            continue  # drift = was passing at the start of the window, failing now
        first_fail = next(i for i, (_, st, _) in enumerate(seq) if st == "fail")
        last_pass = seq[first_fail - 1][0] if first_fail else seq[0][0]
        fails = sum(1 for _, st, _ in seq if st == "fail")
        drifted.append({"check_id": check_id, "severity": seq[-1][2], "last_pass_at": last_pass.collected_at,
                        "first_fail_at": seq[first_fail][0].collected_at, "failing_snapshots": fails,
                        "snapshots_total": len(seq)})
        evidence += [last_pass.snapshot_id, seq[first_fail][0].snapshot_id, seq[-1][0].snapshot_id]
    if not drifted:
        return None
    parts = "; ".join(f"{d['check_id']} ({d['severity']}) passed until {_day(d['last_pass_at'])}, first failed "
                      f"{_day(d['first_fail_at'])}, failing in {d['failing_snapshots']} of {d['snapshots_total']} snapshots"
                      for d in drifted)
    return Finding(
        kind="compliance_drift", device_id=device_id, status="drifted",
        finding=f"Compliance drift: {parts}.",
        explanation=("The check was passing at the start of the window and is failing now, so the device moved from "
                     "compliant to non-compliant on this check. The telemetry does not record why."),
        metrics={"drifted_checks": drifted}, evidence_snapshot_ids=_newest_first(evidence),
        latest_snapshot_id=snaps[-1].snapshot_id, latest_collected_at=snaps[-1].collected_at)


# --- public API ----------------------------------------------------------------
def evaluate(repo: Repository, kind: str, device_id: str) -> Finding | Excluded | None:
    """Finding => actionable; Excluded => checked but not actionable (with why); None => nothing to report."""
    snaps = repo.history(device_id)
    if not snaps:
        return Excluded(device_id=device_id, reason="no telemetry")
    if repo.is_stale(snaps[-1]):
        return Excluded(device_id=device_id, reason=f"stale: last snapshot {snaps[-1].collected_at} is "
                                                    f"{repo.hours_behind(snaps[-1]):.0f}h before as-of {repo.as_of()}")
    if kind == "memory_pressure":
        return _memory(device_id, snaps)
    if kind == "storage":
        return _storage(device_id, snaps)
    if kind == "battery_health":
        return _battery(device_id, snaps)
    if kind == "compliance_drift":
        return _compliance(repo, device_id, snaps)
    raise ValueError(f"unknown trend kind {kind!r}; expected one of {KINDS}")


def analyze_trends(repo: Repository, kind: str, device_id: str | None = None) -> ToolResult:
    if kind not in KINDS:
        raise ValueError(f"unknown trend kind {kind!r}; expected one of {KINDS}")
    population = repo.device_ids()
    filters: dict[str, Any] = {"kind": kind, **({"device_id": device_id} if device_id else {})}
    notes: list[str] = []
    if device_id and device_id not in population:
        targets: list[str] = []
        notes.append(f"device {device_id!r} not found in this company")
        population = []
    else:
        targets = [device_id] if device_id else population

    rows: list[ResultRow] = []
    excluded: list[Excluded] = []
    for d in targets:
        out = evaluate(repo, kind, d)
        if isinstance(out, Excluded):
            excluded.append(out)
        elif isinstance(out, Finding):
            rows.append(ResultRow(
                device_id=d, snapshot_id=out.latest_snapshot_id, collected_at=out.latest_collected_at,
                evidence_snapshot_ids=out.evidence_snapshot_ids,
                values={"kind": out.kind, "status": out.status, "finding": out.finding,
                        "explanation": out.explanation, "metrics": out.metrics, "series": out.series}))
    latest = repo.latest_snapshots()
    starts = [repo.history(d)[0].collected_at for d in targets if repo.history(d)]
    notes.append("window = each device's full telemetry history; findings use the thresholds in config.py")
    return ToolResult(
        result_id=make_result_id("analyze_trends", repo.company_id, filters, repo.as_of()),
        tool="analyze_trends", company_id=repo.company_id, filters=filters,
        window=Window(start=min(starts) if starts else None, end=repo.as_of()), as_of=repo.as_of(),
        population_checked=sorted(targets) if targets else sorted(population), count=len(rows), rows=rows,
        excluded=excluded, notes=notes)
