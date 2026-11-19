from __future__ import annotations

from fleet_copilot import analytics
from fleet_copilot.repository import Repository
from fleet_copilot.session import Session


def sess(company: str, role: str = "approver") -> Session:
    short = company.split("-")[0]
    return Session(company, f"admin-{short}-1" if role == "approver" else f"viewer-{short}-1", role)


def repo_of(conn, company: str) -> Repository:
    return Repository(conn, sess(company))


def latest_id(conn, company: str, device: str) -> str:
    return repo_of(conn, company).latest_snapshots()[device].snapshot_id


def trend_ids(conn, company: str, kind: str, device: str, n: int = 3) -> list[str]:
    f = analytics.evaluate(repo_of(conn, company), kind, device)
    assert isinstance(f, analytics.Finding), f"expected a finding for {device}/{kind}, got {f}"
    return f.evidence_snapshot_ids[:n]


# Supported proposals: (company, action, identifier args, evidence builder)
def supported_cases(conn):
    return {
        "ticket": ("acme-001", "open_remediation_ticket",
                   {"device_id": "1LYSSFD074BB", "check_id": "screen_lock"},
                   [latest_id(conn, "acme-001", "1LYSSFD074BB")]),
        "upgrade_ram": ("acme-001", "create_upgrade_order",
                        {"device_id": "M4XVHUV1MEPZ", "component": "ram", "spec": "32GB"},
                        trend_ids(conn, "acme-001", "memory_pressure", "M4XVHUV1MEPZ")),
        "upgrade_storage": ("acme-001", "create_upgrade_order",
                            {"device_id": "8NM23J95R5I6", "component": "storage", "spec": "2TB"},
                            trend_ids(conn, "acme-001", "storage", "8NM23J95R5I6")),
        "replace_battery": ("acme-001", "flag_device_for_replacement",
                            {"device_id": "7VP16KHV88LM", "reason_code": "battery_eol"},
                            trend_ids(conn, "acme-001", "battery_health", "7VP16KHV88LM")),
        "replace_ram_soldered": ("initech-003", "flag_device_for_replacement",
                                 {"device_id": "I3JLKILKQTAC", "reason_code": "ram_capacity_unupgradeable"},
                                 trend_ids(conn, "initech-003", "memory_pressure", "I3JLKILKQTAC")),
        "replace_storage_soldered": ("acme-001", "flag_device_for_replacement",
                                     {"device_id": "MT7PJB7N5LRE", "reason_code": "storage_capacity_unupgradeable"},
                                     trend_ids(conn, "acme-001", "storage", "MT7PJB7N5LRE")),
        "notify": ("acme-001", "notify_employee",
                   {"employee_id": "emp-acme-1005", "device_id": "8NM23J95R5I6", "topic": "compliance_failure",
                    "check_id": "screen_lock"},
                   [latest_id(conn, "acme-001", "8NM23J95R5I6")]),
    }
