from __future__ import annotations

import copy

import pytest

from fleet_copilot import db
from fleet_copilot.repository import Repository
from fleet_copilot.session import Session

ACME, GLOBEX, INITECH = "acme-001", "globex-002", "initech-003"


@pytest.fixture(scope="session")
def real_conn():
    conn = db.connect(":memory:")
    stats = db.load_ndjson(conn)
    assert stats["malformed_lines"] == 0
    db.seed_admins(conn)
    return conn


@pytest.fixture
def repo_for(real_conn):
    def _make(company_id: str, role: str = "approver") -> Repository:
        return Repository(real_conn, Session(company_id=company_id, admin_id=f"admin-{company_id}", role=role))
    return _make


BASE_RECORD = {
    "device_id": "DEV-X", "company_id": ACME, "employee_id": "emp-acme-9000",
    "collected_at": "2026-06-12T10:00:00Z", "agent_version": "42.0.0",
    "os": {"platform": "darwin", "product_name": "macOS", "product_version": "14.5"},
    "device_identity": {"model_name": "MacBook Pro", "model_identifier": "Mac15,7"},
    "memory": {"total_memory_bytes": 16 * 2**30, "used_memory_bytes": 8 * 2**30},
    "disk_volumes": [{"size_bytes": 1000, "available_bytes": 500}],
    "battery": {"battery_present": True, "condition": "Normal", "cycle_count": 100, "full_charge_capacity": 5000},
    "installed_software": [],
    "compliance_results": [
        {"check_id": "disk_encryption", "status": "pass", "severity": "high"},
        {"check_id": "screen_lock", "status": "fail", "severity": "medium"},
    ],
}


def make_record(**overrides):
    rec = copy.deepcopy(BASE_RECORD)
    for k, v in overrides.items():
        rec[k] = v
    return rec


@pytest.fixture
def synth_conn():
    """Fresh empty DB for hand-built edge cases."""
    conn = db.connect(":memory:")
    db.seed_admins(conn)
    return conn
