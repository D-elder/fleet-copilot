"""Target guard: an action may only target the devices/employees the admin NAMED.

Found by the real-model eval (DeepSeek): asked to notify emp-acme-1000 about M4XVHUV1MEPZ, the model discovered the
device belongs to emp-acme-1002 and quietly proposed notifying THEM instead. The proposal was policy-valid and
disclosed, but it was not what the admin asked for.
"""
from __future__ import annotations

import pytest

from fleet_copilot import audit, db
from fleet_copilot.graph import FleetAgent
from fleet_copilot.tools import ToolBox

from .fakes import ScriptedLLM, ai, call, final_from_last_result, need, plan
from .helpers import latest_id, sess


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.load_ndjson(c)
    db.seed_admins(c)
    return c


def notify_args(conn, employee="emp-acme-1002"):
    return {"employee_id": employee, "device_id": "M4XVHUV1MEPZ", "topic": "memory_pressure",
            "evidence": [latest_id(conn, "acme-001", "M4XVHUV1MEPZ")]}


def test_redirecting_a_notification_to_a_different_employee_is_refused(conn):
    tb = ToolBox(conn, sess("acme-001"))
    tb.set_request("Notify employee emp-acme-1000 that their device M4XVHUV1MEPZ is running out of memory.")
    out = tb.dispatch("notify_employee", notify_args(conn))              # policy-valid, but not who the admin named
    assert out.proposal.status == "refused" and "target mismatch" in out.proposal.reasons[0]
    assert "emp-acme-1000" in out.proposal.reasons[0] and "emp-acme-1002" in out.proposal.reasons[0]
    assert conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0
    ev = [r for r in audit.events(conn, event="proposal_refused")]
    assert len(ev) == 1 and '"guard": "target_mismatch"' in ev[0]["details_json"]


def test_the_named_employee_is_still_subject_to_the_normal_policy(conn):
    tb = ToolBox(conn, sess("acme-001"))
    tb.set_request("Notify employee emp-acme-1000 that their device M4XVHUV1MEPZ is running out of memory.")
    out = tb.dispatch("notify_employee", notify_args(conn, "emp-acme-1000"))   # guard passes, policy refuses
    assert out.proposal.status == "refused" and "not assigned to employee" in out.proposal.reasons[0]


def test_redirecting_to_a_different_device_is_refused(conn):
    tb = ToolBox(conn, sess("acme-001"))
    tb.set_request("Open a screen_lock ticket for device 1LYSSFD074BB.")
    out = tb.dispatch("open_remediation_ticket", {"device_id": "8NM23J95R5I6", "check_id": "screen_lock",
                                                  "evidence": [latest_id(conn, "acme-001", "8NM23J95R5I6")]})
    assert out.proposal.status == "refused" and "target mismatch" in out.proposal.reasons[0]


def test_a_foreign_device_named_by_the_admin_cannot_be_swapped_for_an_own_device(conn):
    tb = ToolBox(conn, sess("acme-001"))
    tb.set_request("Open a screen_lock remediation ticket for device JRZSGXVMKE6M.")     # a Globex device
    out = tb.dispatch("open_remediation_ticket", {"device_id": "1LYSSFD074BB", "check_id": "screen_lock",
                                                  "evidence": [latest_id(conn, "acme-001", "1LYSSFD074BB")]})
    assert out.proposal.status == "refused" and "target mismatch" in out.proposal.reasons[0]


def test_no_named_targets_means_no_constraint(conn):
    """'Notify the employee who uses device X' names a device only; deriving the employee from data is legitimate."""
    tb = ToolBox(conn, sess("acme-001"))
    tb.set_request("Notify the employee who uses device M4XVHUV1MEPZ about its memory use.")
    out = tb.dispatch("notify_employee", notify_args(conn))
    assert out.proposal.status == "pending"
    tb2 = ToolBox(conn, sess("acme-001"))
    tb2.set_request("Which devices are low on disk space? Open tickets for the worst ones.")   # nothing named
    assert tb2.named_devices == set() and tb2.named_employees == set()


def test_ordinary_words_are_not_mistaken_for_device_ids():
    tb = ToolBox(db.connect(":memory:"), sess("acme-001"))
    tb.set_request("Please reconfigure the Installation of CONFIGURATION settings and ORGANIZATION wide ABCDEFGHIJKL")
    assert tb.named_devices == set()           # 12-char tokens need both letters and digits


def test_the_guard_applies_through_the_graph_and_is_reported_to_the_user(conn):
    steps = [plan("notify", [need("device_detail", "M4XVHUV1MEPZ")], True),
             ai(call("get_device", {"device_id": "M4XVHUV1MEPZ"})),
             ai(call("notify_employee", notify_args(conn))),                # the model redirects to emp-acme-1002
             final_from_last_result("The device belongs to emp-acme-1002, not emp-acme-1000.", tool="get_device")]
    agent = FleetAgent(conn, sess("acme-001"), ScriptedLLM(steps))
    out = agent.ask("Notify employee emp-acme-1000 that their device M4XVHUV1MEPZ is running out of memory.")
    assert not out.pending and "REFUSED" in out.rendered and "target mismatch" in out.rendered
    assert conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
