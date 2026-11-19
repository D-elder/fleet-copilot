"""Loader, tenant isolation, and the data rules from the plan (same-row compliance match, OS family, staleness)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from fleet_copilot import db
from fleet_copilot.models import DeviceFilters, make_snapshot_id
from fleet_copilot.repository import Repository, form_factor, os_family, parse_os_version
from fleet_copilot.session import AccessDenied, Session

from .conftest import ACME, GLOBEX, INITECH, make_record


# --- loader -----------------------------------------------------------------
def test_loader_counts(real_conn):
    assert real_conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 750
    assert real_conn.execute("SELECT COUNT(DISTINCT device_id) FROM snapshots").fetchone()[0] == 25
    assert real_conn.execute("SELECT COUNT(DISTINCT company_id) FROM snapshots").fetchone()[0] == 3
    assert real_conn.execute("SELECT value FROM meta WHERE key='as_of'").fetchone()[0] == "2026-06-12T11:00:00Z"


def test_loader_is_idempotent(synth_conn):
    rec = make_record()
    assert db.load_records(synth_conn, [rec])["loaded"] == 1
    assert db.load_records(synth_conn, [rec]) == {"loaded": 0, "duplicates": 1}


def test_snapshot_id_is_deterministic():
    assert make_snapshot_id("D1", "2026-06-12T10:00:00Z") == "D1@2026-06-12T10:00:00Z"


# --- tenant isolation -------------------------------------------------------
def test_repository_sees_only_its_company(repo_for):
    assert len(repo_for(ACME).device_ids()) == 10
    assert len(repo_for(GLOBEX).device_ids()) == 8
    assert len(repo_for(INITECH).device_ids()) == 7
    assert set(repo_for(ACME).device_ids()).isdisjoint(repo_for(GLOBEX).device_ids())


def test_cross_company_snapshot_is_indistinguishable_from_missing(real_conn, repo_for):
    globex_sid = real_conn.execute(
        "SELECT snapshot_id FROM snapshots WHERE company_id=? LIMIT 1", (GLOBEX,)).fetchone()[0]
    acme = repo_for(ACME)
    assert acme.get_snapshot(globex_sid) is None
    assert acme.get_raw_snapshot(globex_sid) is None
    assert acme.compliance(globex_sid) == []
    assert acme.software(globex_sid) == []
    assert acme.get_snapshot("NOPE@2026-01-01T00:00:00Z") is None  # same answer as a real foreign id


def test_cross_company_employee_and_device_history(repo_for):
    acme = repo_for(ACME)
    assert not acme.employee_exists("emp-globex-1002")
    globex_device = repo_for(GLOBEX).device_ids()[0]
    assert acme.history(globex_device) == []


def test_session_requires_admin_of_that_company(real_conn):
    s = Session.for_admin(real_conn, "admin-acme-1", ACME)
    assert s.role == "approver"
    with pytest.raises(AccessDenied):
        Session.for_admin(real_conn, "admin-acme-1", GLOBEX)
    with pytest.raises(AccessDenied):
        Session.for_admin(real_conn, "ghost", ACME)


# --- OS family rule ---------------------------------------------------------
def test_version_parsing():
    assert parse_os_version("macos", "14.5") == (14, 5)
    assert parse_os_version("macos", "15") == (15,)
    assert parse_os_version("windows", "11 23H2") == (11, 23, 2)
    assert parse_os_version("windows", "10 22H2") < parse_os_version("windows", "11 22H2")
    for bad in [("macos", "Sonoma"), ("windows", "14.5"), ("linux", "1")]:
        with pytest.raises(ValueError):
            parse_os_version(*bad)


def test_os_family_and_form_factor():
    assert os_family("macOS", "darwin") == "macos" and os_family("Windows", "win32") == "windows"
    assert form_factor("MacBook Air") == "laptop" and form_factor("Dell XPS 15") == "laptop"
    assert form_factor("ThinkPad X1 Carbon") == "laptop" and form_factor("Mac mini") == "desktop"
    assert form_factor("Mystery Box") is None


def test_os_version_filter_requires_family():
    with pytest.raises(ValidationError):
        DeviceFilters(os_version_lt="15")


def test_older_than_macos_15_excludes_windows(repo_for):
    res = repo_for(ACME).query_devices(DeviceFilters(os_family="macos", os_version_lt="15"))
    # Acme macOS: 14.5 x3 (1LYSS, MT7P, XSU9) + 13.6 (7VP) are older; 15.3/15.4 are not.
    assert sorted(res.device_ids) == ["1LYSSFD074BB", "7VP16KHV88LM", "MT7PJB7N5LRE", "XSU9UUDGU6AC"]
    assert res.count == 4
    windows = {e.device_id for e in res.excluded if e.reason.startswith("not macos")}
    assert windows == {"8NM23J95R5I6", "9MXJVZ1I0DKA", "M4XVHUV1MEPZ"}  # Windows devices never silently counted


# --- compliance same-row match ---------------------------------------------
def test_high_severity_failures_is_a_grounded_zero(repo_for):
    for company in (ACME, GLOBEX, INITECH):
        repo = repo_for(company)
        res = repo.query_devices(DeviceFilters(form_factor="laptop", check_severity="high"))
        assert res.count == 0 and res.rows == []
        # a zero must still show what was checked
        assert res.population_checked == repo.device_ids()
        assert res.filters == {"form_factor": "laptop", "check_severity": "high"}
        assert res.as_of == "2026-06-12T11:00:00Z"


def test_severity_and_check_match_on_same_row(repo_for):
    acme = repo_for(ACME)
    # screen_lock fails and is medium severity -> match
    assert acme.query_devices(DeviceFilters(failing_check="screen_lock", check_severity="medium")).count > 0
    # screen_lock fails but is NOT high; disk_encryption is high but never fails. A per-column match would
    # wrongly return devices here; a same-row match returns none.
    assert acme.query_devices(DeviceFilters(failing_check="screen_lock", check_severity="high")).count == 0
    assert acme.query_devices(DeviceFilters(failing_check="disk_encryption", check_severity="medium")).count == 0


def test_same_row_match_with_synthetic_trap(synth_conn):
    # high-severity check PASSES while a medium check FAILS on the same device.
    db.load_records(synth_conn, [make_record()])
    repo = Repository(synth_conn, Session(ACME, "a", "approver"))
    assert repo.query_devices(DeviceFilters(check_severity="high")).count == 0
    assert repo.query_devices(DeviceFilters(check_severity="medium")).count == 1


def test_unknown_check_id_is_flagged_not_silently_empty(repo_for):
    res = repo_for(ACME).query_devices(DeviceFilters(failing_check="firewall"))
    assert res.count == 0
    assert any("does not exist" in n and "screen_lock" in n for n in res.notes)


# --- other filters ----------------------------------------------------------
def test_low_disk_at_acme(repo_for):
    res = repo_for(ACME).query_devices(DeviceFilters(disk_free_pct_lt=10))
    assert sorted(res.device_ids) == ["8NM23J95R5I6", "MT7PJB7N5LRE"]
    for row in res.rows:
        assert row.snapshot_id == f"{row.device_id}@{row.collected_at}"
        assert row.values["disk_free_pct"] < 10


def test_form_factor_filter_excludes_desktops(repo_for):
    res = repo_for(INITECH).query_devices(DeviceFilters(form_factor="laptop", mem_used_pct_gt=85))
    assert "I3JLKILKQTAC" not in res.device_ids  # Mac mini is a desktop even though its RAM is full


def test_result_id_is_deterministic(repo_for):
    f = DeviceFilters(disk_free_pct_lt=10)
    assert repo_for(ACME).query_devices(f).result_id == repo_for(ACME).query_devices(f).result_id
    assert repo_for(ACME).query_devices(f).result_id != repo_for(GLOBEX).query_devices(f).result_id


# --- staleness / missing data ----------------------------------------------
def test_stale_devices_are_excluded_with_reason(synth_conn):
    db.load_records(synth_conn, [
        make_record(device_id="FRESH", collected_at="2026-06-12T10:00:00Z"),
        make_record(device_id="STALE", collected_at="2026-06-07T10:00:00Z"),   # 5 days behind as-of
    ])
    repo = Repository(synth_conn, Session(ACME, "a", "approver"))
    res = repo.query_devices(DeviceFilters(failing_check="screen_lock"))
    assert res.device_ids == ["FRESH"]
    assert [(e.device_id, e.reason.split(":")[0]) for e in res.excluded] == [("STALE", "stale")]
    assert res.population_checked == ["FRESH", "STALE"]


def test_missing_disk_telemetry_is_unknown_not_low(synth_conn):
    db.load_records(synth_conn, [make_record(disk_volumes=[])])
    repo = Repository(synth_conn, Session(ACME, "a", "approver"))
    res = repo.query_devices(DeviceFilters(disk_free_pct_lt=10))
    assert res.count == 0
    assert [e.reason for e in res.excluded] == ["no disk telemetry in latest snapshot"]
