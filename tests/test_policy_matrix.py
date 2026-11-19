"""Every action type: a supported case and several refused cases. Fresh DB per test (proposals mutate state)."""
from __future__ import annotations

import json

import pytest

from fleet_copilot import actions, audit, db
from fleet_copilot.tools import ToolBox

from .helpers import latest_id, sess, supported_cases, trend_ids


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.load_ndjson(c)
    db.seed_admins(c)
    return c


def propose(conn, company, action, args, evidence):
    return actions.propose(conn, sess(company), action, args, evidence)


def refused_events(conn):
    return [e["event"] for e in audit.events(conn)].count("proposal_refused")


def refused(conn, company, action, args, evidence, *fragments):
    before = refused_events(conn)
    r = propose(conn, company, action, args, evidence)
    assert r.status == "refused", f"expected refusal, got {r.status}"
    text = " | ".join(r.reasons)
    for f in fragments:
        assert f in text, f"{f!r} not in refusal reasons: {text}"
    assert conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0
    assert refused_events(conn) == before + 1   # each refusal is audited exactly once
    return r


# ------------------------------------------------------------------ supported
@pytest.mark.parametrize("name", ["ticket", "upgrade_ram", "upgrade_storage", "replace_battery",
                                  "replace_ram_soldered", "replace_storage_soldered", "notify"])
def test_supported_case_is_accepted_pending_and_text_is_generated(conn, name):
    company, action, args, evidence = supported_cases(conn)[name]
    r = propose(conn, company, action, args, evidence)
    assert r.status == "pending", r.reasons
    p = actions.get_proposal(conn, company, r.proposal_id)
    assert p["status"] == "pending" and p["justification"]
    # the app, not the model, wrote every free-text field
    gen = {"open_remediation_ticket": "note", "flag_device_for_replacement": "reason",
           "notify_employee": "message"}.get(action)
    if gen:
        assert p["args"][gen]
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


@pytest.mark.parametrize("name", ["upgrade_ram", "replace_battery", "notify"])
def test_supported_case_executes_after_approval(conn, name):
    company, action, args, evidence = supported_cases(conn)[name]
    s = sess(company)
    r = propose(conn, company, action, args, evidence)
    assert actions.decide(conn, s, r.proposal_id, r.payload_hash, "approve").ok
    assert actions.execute(conn, s, r.proposal_id).ok
    ob = actions.outbox_rows(conn, company)
    assert len(ob) == 1 and json.loads(ob[0]["payload_json"])["type"] == action


# ------------------------------------------------------------- upgrade refusals
def test_upgrade_refused_when_ram_is_soldered_even_under_sustained_pressure(conn):
    ev = trend_ids(conn, "initech-003", "memory_pressure", "I3JLKILKQTAC")        # pressure is real...
    refused(conn, "initech-003", "create_upgrade_order",
            {"device_id": "I3JLKILKQTAC", "component": "ram", "spec": "16GB"}, ev,
            "not upgradeable", "flag_device_for_replacement")                      # ...but not fixable by an upgrade


def test_upgrade_refused_for_thinkpad_ram_but_ssd_is_fine(conn):
    ev = trend_ids(conn, "globex-002", "memory_pressure", "JRZSGXVMKE6M")
    refused(conn, "globex-002", "create_upgrade_order",
            {"device_id": "JRZSGXVMKE6M", "component": "ram", "spec": "32GB"}, ev, "not upgradeable")


def test_upgrade_refused_for_spec_not_in_catalog(conn):
    ev = trend_ids(conn, "acme-001", "memory_pressure", "M4XVHUV1MEPZ")
    refused(conn, "acme-001", "create_upgrade_order",
            {"device_id": "M4XVHUV1MEPZ", "component": "ram", "spec": "48GB"}, ev, "not a supported RAM option", "32GB")
    refused(conn, "acme-001", "create_upgrade_order",
            {"device_id": "M4XVHUV1MEPZ", "component": "ram", "spec": "lots"}, ev, "not a supported RAM option")


def test_upgrade_refused_without_sustained_pressure(conn):
    # Dell XPS 15 (RAM is upgradeable) but memory sits at ~69% -> compatible hardware alone is not enough
    refused(conn, "globex-002", "create_upgrade_order",
            {"device_id": "E4V0SB5I0MYU", "component": "ram", "spec": "32GB"},
            [latest_id(conn, "globex-002", "E4V0SB5I0MYU")] * 3, "no RAM pressure finding")


def test_upgrade_refused_with_too_little_or_wrong_evidence(conn):
    ev = trend_ids(conn, "acme-001", "memory_pressure", "M4XVHUV1MEPZ", n=3)
    refused(conn, "acme-001", "create_upgrade_order",
            {"device_id": "M4XVHUV1MEPZ", "component": "ram", "spec": "32GB"}, ev[:1], "at least 3")
    refused(conn, "acme-001", "create_upgrade_order",
            {"device_id": "M4XVHUV1MEPZ", "component": "ram", "spec": "32GB"}, [], "no evidence cited")


def test_upgrade_refused_when_cited_snapshots_do_not_show_the_condition(conn):
    # 8NM23J95R5I6 only dropped below 10% free mid-window; its EARLY snapshots do not support a storage order
    early = [r[0] for r in conn.execute(
        "SELECT snapshot_id FROM snapshots WHERE device_id='8NM23J95R5I6' ORDER BY collected_at LIMIT 3")]
    refused(conn, "acme-001", "create_upgrade_order",
            {"device_id": "8NM23J95R5I6", "component": "storage", "spec": "2TB"}, early,
            "does not support the finding")


def test_upgrade_refused_for_foreign_or_other_device_evidence(conn):
    foreign = trend_ids(conn, "globex-002", "storage", "E4V0SB5I0MYU")
    refused(conn, "acme-001", "create_upgrade_order",
            {"device_id": "8NM23J95R5I6", "component": "storage", "spec": "2TB"}, foreign, "not found in this company")
    other = trend_ids(conn, "acme-001", "storage", "MT7PJB7N5LRE")
    refused(conn, "acme-001", "create_upgrade_order",
            {"device_id": "8NM23J95R5I6", "component": "storage", "spec": "2TB"}, other, "different device")


def test_upgrade_refused_for_unknown_device_and_foreign_device(conn):
    refused(conn, "acme-001", "create_upgrade_order", {"device_id": "NOPE", "component": "ram", "spec": "32GB"},
            ["NOPE@2026-06-12T00:00:00Z"], "not found in this company")
    refused(conn, "acme-001", "create_upgrade_order",   # M4XV is Acme's; Globex's JRZS is invisible to Acme
            {"device_id": "JRZSGXVMKE6M", "component": "ram", "spec": "32GB"}, ["x"], "not found in this company")


# ------------------------------------------------------------ replacement refusals
def test_replacement_refused_for_healthy_battery(conn):
    refused(conn, "acme-001", "flag_device_for_replacement",
            {"device_id": "1LYSSFD074BB", "reason_code": "battery_eol"},
            [latest_id(conn, "acme-001", "1LYSSFD074BB")] * 3, "no battery end-of-life finding")


def test_replacement_refused_when_there_is_no_battery_to_assess(conn):
    refused(conn, "acme-001", "flag_device_for_replacement",
            {"device_id": "DLT35MHGTH2D", "reason_code": "battery_eol"},      # Mac mini: no battery at all
            [latest_id(conn, "acme-001", "DLT35MHGTH2D")] * 3, "not applicable")


def test_replacement_refused_when_the_component_is_upgradeable(conn):
    ev = trend_ids(conn, "acme-001", "memory_pressure", "M4XVHUV1MEPZ")
    refused(conn, "acme-001", "flag_device_for_replacement",
            {"device_id": "M4XVHUV1MEPZ", "reason_code": "ram_capacity_unupgradeable"}, ev,
            "IS upgradeable", "create_upgrade_order")


def test_replacement_refused_for_bad_reason_or_thin_evidence(conn):
    ev = trend_ids(conn, "acme-001", "battery_health", "7VP16KHV88LM")
    refused(conn, "acme-001", "flag_device_for_replacement",
            {"device_id": "7VP16KHV88LM", "reason_code": "looks_old"}, ev, "unsupported reason_code")
    refused(conn, "acme-001", "flag_device_for_replacement",
            {"device_id": "7VP16KHV88LM", "reason_code": "battery_eol"}, ev[:1], "at least 3")


# ------------------------------------------------------------------ notify refusals
def test_notify_refused_for_device_not_assigned_to_employee(conn):
    refused(conn, "acme-001", "notify_employee",
            {"employee_id": "emp-acme-1000", "device_id": "M4XVHUV1MEPZ", "topic": "memory_pressure"},
            [latest_id(conn, "acme-001", "M4XVHUV1MEPZ")], "not assigned to employee")


def test_notify_refused_for_employee_of_another_company(conn):
    refused(conn, "acme-001", "notify_employee",
            {"employee_id": "emp-globex-1005", "device_id": "JRZSGXVMKE6M", "topic": "memory_pressure"},
            [latest_id(conn, "globex-002", "JRZSGXVMKE6M")], "not found in this company")


def test_notify_refused_when_condition_does_not_hold(conn):
    dev = "1LYSSFD074BB"
    emp = "emp-acme-1001"
    ev = [latest_id(conn, "acme-001", dev)]
    refused(conn, "acme-001", "notify_employee",
            {"employee_id": emp, "device_id": dev, "topic": "low_storage"}, ev, "not below 10%")
    refused(conn, "acme-001", "notify_employee",
            {"employee_id": emp, "device_id": dev, "topic": "compliance_failure", "check_id": "disk_encryption"}, ev,
            "not failing")
    refused(conn, "acme-001", "notify_employee",
            {"employee_id": emp, "device_id": dev, "topic": "battery_service"}, ev, "not 'Service Recommended'")


def test_notify_refused_without_current_evidence(conn):
    old = conn.execute("SELECT snapshot_id FROM snapshots WHERE device_id='8NM23J95R5I6' "
                       "ORDER BY collected_at LIMIT 1").fetchone()[0]
    refused(conn, "acme-001", "notify_employee",
            {"employee_id": "emp-acme-1005", "device_id": "8NM23J95R5I6", "topic": "compliance_failure",
             "check_id": "screen_lock"}, [old], "not current")
    refused(conn, "acme-001", "notify_employee",
            {"employee_id": "emp-acme-1005", "device_id": "8NM23J95R5I6", "topic": "compliance_failure",
             "check_id": "screen_lock"}, [], "no evidence cited")


# ------------------------------------------------------- tool layer: no model prose
@pytest.mark.parametrize("tool,args", [
    ("open_remediation_ticket", {"device_id": "1LYSSFD074BB", "check_id": "screen_lock", "evidence": ["x"],
                                 "note": "Enforce screen lock per company policy."}),
    ("open_remediation_ticket", {"device_id": "1LYSSFD074BB", "check_id": "screen_lock", "evidence": ["x"],
                                 "justification": "per company policy"}),
    ("flag_device_for_replacement", {"device_id": "7VP16KHV88LM", "reason_code": "battery_eol", "evidence": ["x"],
                                     "reason": "the battery is dangerous"}),
    ("notify_employee", {"employee_id": "emp-acme-1005", "device_id": "8NM23J95R5I6", "topic": "low_storage",
                         "evidence": ["x"], "message": "Fix it by Friday or face consequences."}),
    ("create_upgrade_order", {"device_id": "M4XVHUV1MEPZ", "component": "ram", "spec": "32GB", "evidence": ["x"],
                              "company_id": "globex-002"}),
])
def test_action_tools_reject_model_written_prose_and_company_ids(conn, tool, args):
    out = ToolBox(conn, sess("acme-001")).dispatch(tool, args)
    assert out.content.startswith("ERROR: invalid arguments") and out.proposal is None
    assert "application generates all free text" in out.content
    assert conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0
