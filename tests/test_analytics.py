"""Trend detectors against ground truth, plus the new read tools (fleet_overview, get_device) and tenant isolation."""
from __future__ import annotations

import pytest

from fleet_copilot import analytics, db
from fleet_copilot.repository import Repository
from fleet_copilot.session import Session

from .conftest import ACME, GLOBEX, INITECH, make_record

ALL = (ACME, GLOBEX, INITECH)

# Ground truth from profiling the raw NDJSON (see the plan); thresholds are documented in config.py.
BATTERY_EOL = {"7VP16KHV88LM", "8NM23J95R5I6", "M4XVHUV1MEPZ"}
RAM_SUSTAINED = {"M4XVHUV1MEPZ", "I3JLKILKQTAC", "JRZSGXVMKE6M"}
RAM_INTERMITTENT = {"CUXW0BQSV1B7"}            # 43% of snapshots over 85%: real, but below the 50% 'sustained' bar
STORAGE_LOW = {"8NM23J95R5I6", "MT7PJB7N5LRE", "E4V0SB5I0MYU", "JRZSGXVMKE6M", "DARFCP0BQM2G", "RLD7WV7J82SH"}
DRIFT_SCREEN_LOCK = {"1LYSSFD074BB", "8NM23J95R5I6", "DLT35MHGTH2D", "DSYPSES7SKBB", "MT7PJB7N5LRE",
                     "CUXW0BQSV1B7", "JRZSGXVMKE6M", "2MAN6T4DJD95", "SPKRRDF0L786", "XZYK11QTPWEC"}


def fleet(repo_for, kind):
    rows, excluded = {}, {}
    for c in ALL:
        res = analytics.analyze_trends(repo_for(c), kind)
        rows.update({r.device_id: r for r in res.rows})
        excluded.update({e.device_id: e.reason for e in res.excluded})
    return rows, excluded


def test_battery_end_of_life(repo_for):
    rows, excluded = fleet(repo_for, "battery_health")
    assert set(rows) == BATTERY_EOL
    for d in ("DLT35MHGTH2D", "I3JLKILKQTAC", "RLD7WV7J82SH"):          # Mac minis: not applicable, not "healthy"
        assert "not applicable" in excluded[d]
    r = rows["8NM23J95R5I6"]
    assert r.values["metrics"]["capacity_drop_pct"] == pytest.approx(4.2, abs=0.05)
    assert r.values["metrics"]["cycle_count"] > 900 and "Service Recommended" in r.values["finding"]


def test_memory_pressure_sustained_vs_intermittent(repo_for):
    rows, excluded = fleet(repo_for, "memory_pressure")
    assert set(rows) == RAM_SUSTAINED
    assert set(d for d, why in excluded.items() if why.startswith("intermittent")) == RAM_INTERMITTENT
    assert "43%" in excluded["CUXW0BQSV1B7"] and "below the 50% bar" in excluded["CUXW0BQSV1B7"]
    assert rows["M4XVHUV1MEPZ"].values["metrics"]["snapshots_over_threshold"] == 30


def test_consistently_low_storage(repo_for):
    rows, _ = fleet(repo_for, "storage")
    assert set(rows) == STORAGE_LOW
    assert rows["8NM23J95R5I6"].values["status"] == "low_and_declining"
    # MT7P was already low on day 1 (7.2% free) yet still lost 5.2 points: low AND declining, and below on every day
    m = rows["MT7PJB7N5LRE"].values
    assert m["status"] == "low_and_declining" and m["metrics"]["share"] == 1.0
    assert m["metrics"]["decline_points"] == pytest.approx(5.2, abs=0.1)


def test_compliance_drift(repo_for):
    rows, _ = fleet(repo_for, "compliance_drift")
    assert set(rows) == DRIFT_SCREEN_LOCK
    for r in rows.values():
        checks = r.values["metrics"]["drifted_checks"]
        assert [c["check_id"] for c in checks] == ["screen_lock"]          # os_up_to_date fails constantly: not drift
        assert checks[0]["last_pass_at"] < checks[0]["first_fail_at"]


def test_findings_are_traceable(real_conn, repo_for):
    for kind in analytics.KINDS:
        for c in ALL:
            repo = repo_for(c)
            for row in analytics.analyze_trends(repo, kind).rows:
                assert row.evidence_snapshot_ids, (kind, row.device_id)
                for sid in row.evidence_snapshot_ids:
                    s = repo.get_snapshot(sid)
                    assert s is not None and s.device_id == row.device_id
                assert row.values["finding"] and row.values["explanation"]


def test_trends_are_tenant_scoped(repo_for):
    acme = analytics.analyze_trends(repo_for(ACME), "storage")
    assert set(acme.device_ids) == {"8NM23J95R5I6", "MT7PJB7N5LRE"}
    assert set(acme.population_checked) == set(repo_for(ACME).device_ids())
    foreign = analytics.analyze_trends(repo_for(ACME), "storage", device_id="E4V0SB5I0MYU")   # a Globex device
    ghost = analytics.analyze_trends(repo_for(ACME), "storage", device_id="NOPE")
    assert foreign.count == ghost.count == 0 and foreign.population_checked == ghost.population_checked == []
    assert [n.replace("E4V0SB5I0MYU", "X") for n in foreign.notes] == [n.replace("NOPE", "X") for n in ghost.notes]


def test_single_device_trend(repo_for):
    res = analytics.analyze_trends(repo_for(ACME), "battery_health", device_id="7VP16KHV88LM")
    assert res.device_ids == ["7VP16KHV88LM"] and res.population_checked == ["7VP16KHV88LM"]


def test_unknown_kind_is_rejected(repo_for):
    with pytest.raises(ValueError):
        analytics.analyze_trends(repo_for(ACME), "vibes")


def test_stale_and_thin_telemetry_are_not_findings(synth_conn):
    from datetime import datetime, timedelta, timezone

    end = datetime(2026, 6, 12, 10, tzinfo=timezone.utc)

    def daily(device_id: str, last: datetime, days: int):
        mem = {"total_memory_bytes": 100, "used_memory_bytes": 99}
        return [make_record(device_id=device_id, memory=mem,
                            collected_at=(last - timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%SZ"))
                for i in range(days)]

    recs = (daily("FRESH", end, 20)                                    # 20 days, all over threshold, current
            + daily("STALE", end - timedelta(days=10), 20)             # same data but stopped reporting 10 days ago
            + daily("THIN", end, 3))                                   # current but only 3 readings
    db.load_records(synth_conn, recs)
    res = analytics.analyze_trends(Repository(synth_conn, Session(ACME, "a", "approver")), "memory_pressure")
    why = {e.device_id: e.reason for e in res.excluded}
    assert res.device_ids == ["FRESH"]
    assert why["STALE"].startswith("stale") and why["THIN"].startswith("insufficient")


# --- fleet_overview / get_device ---------------------------------------------------------------
def test_fleet_overview_counts(repo_for):
    res = repo_for(ACME).fleet_overview()
    assert res.tool == "fleet_overview" and res.count == 10 and res.summary["devices_in_company"] == 10
    assert res.summary["by_form_factor"] == {"laptop": 9, "desktop": 1}
    assert res.summary["devices_failing_each_check"]["screen_lock"] == 5
    assert res.summary["by_model"]["MacBook Pro"] == 6


def test_get_device_and_foreign_device_look_the_same_as_missing(repo_for):
    acme = repo_for(ACME)
    d = acme.get_device("8NM23J95R5I6")
    assert d.count == 1 and d.rows[0].values["model"] == "ThinkPad X1 Carbon" and d.rows[0].values["history"]["snapshots"] == 30
    foreign, ghost = acme.get_device("JRZSGXVMKE6M"), acme.get_device("NOPE")
    assert foreign.count == ghost.count == 0 and foreign.rows == ghost.rows == []
    assert foreign.notes[0].replace("JRZSGXVMKE6M", "X") == ghost.notes[0].replace("NOPE", "X")
