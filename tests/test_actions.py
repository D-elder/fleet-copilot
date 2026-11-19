"""Proposal policy + approval/execution safety. Each test gets a fresh DB (these tests mutate state)."""
from __future__ import annotations

import json
import sqlite3

import pytest

from fleet_copilot import actions, audit, db
from fleet_copilot.repository import Repository
from fleet_copilot.session import Session

from .conftest import ACME, GLOBEX

DEVICE = "1LYSSFD074BB"   # Acme MacBook Pro; screen_lock failing in its latest snapshot, disk_encryption passing
ACTION = "open_remediation_ticket"
ARGS = {"device_id": DEVICE, "check_id": "screen_lock", "note": "Enforce screen lock."}


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.load_ndjson(c)
    db.seed_admins(c)
    return c


@pytest.fixture
def approver():
    return Session(ACME, "admin-acme-1", "approver")


def latest_id(conn, session=None, device=DEVICE):
    session = session or Session(ACME, "admin-acme-1", "approver")
    return Repository(conn, session).latest_snapshots()[device].snapshot_id


def outbox_count(conn):
    return conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]


def events(conn, name):
    return [r for r in audit.events(conn) if r["event"] == name]


def proposed(conn, session):
    r = actions.propose(conn, session, ACTION, ARGS, [latest_id(conn)], "screen_lock failing now")
    assert r.status == "pending", r.reasons
    return r


# --- policy: supported vs refused --------------------------------------------
def test_supported_ticket_is_created_pending_and_audited(conn, approver):
    r = proposed(conn, approver)
    assert r.proposal_id.startswith("P-") and not r.duplicate
    assert actions.get_proposal(conn, ACME, r.proposal_id)["status"] == "pending"
    assert outbox_count(conn) == 0
    assert len(events(conn, "proposal_created")) == 1


@pytest.mark.parametrize("label,make", [
    ("no evidence", lambda c, s: (ARGS, [])),
    ("check passes in latest snapshot", lambda c, s: ({**ARGS, "check_id": "disk_encryption"}, [latest_id(c)])),
    ("unknown check id", lambda c, s: ({**ARGS, "check_id": "firewall"}, [latest_id(c)])),
    ("unknown device", lambda c, s: ({**ARGS, "device_id": "NOPE"}, [latest_id(c)])),
    ("evidence is another device", lambda c, s: (ARGS, [latest_id(c, device="8NM23J95R5I6")])),
    ("evidence is not the latest snapshot", lambda c, s: (ARGS, [
        c.execute("SELECT snapshot_id FROM snapshots WHERE device_id=? ORDER BY collected_at LIMIT 1",
                  (DEVICE,)).fetchone()[0]])),
    ("evidence from another company", lambda c, s: (ARGS, [
        c.execute("SELECT snapshot_id FROM snapshots WHERE company_id=? LIMIT 1", (GLOBEX,)).fetchone()[0]])),
    ("invented snapshot id", lambda c, s: (ARGS, [f"{DEVICE}@2030-01-01T00:00:00Z"])),
])
def test_unsupported_ticket_is_refused_and_audited(conn, approver, label, make):
    args, evidence = make(conn, approver)
    r = actions.propose(conn, approver, ACTION, args, evidence)
    assert r.status == "refused" and r.reasons, label
    assert conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0
    assert len(events(conn, "proposal_refused")) == 1


def test_stale_device_cannot_support_a_ticket(conn, approver):
    # 8NM23J95R5I6 reports every day; make a device whose last report is days behind the as-of time.
    raw = json.loads(conn.execute("SELECT raw_json FROM snapshots WHERE device_id=? ORDER BY collected_at DESC LIMIT 1",
                                  (DEVICE,)).fetchone()[0])
    stale = {**raw, "device_id": "STALE-1", "collected_at": "2026-06-08T10:00:00Z"}
    db.load_records(conn, [stale])
    r = actions.propose(conn, approver, ACTION, {**ARGS, "device_id": "STALE-1"},
                        ["STALE-1@2026-06-08T10:00:00Z"])
    assert r.status == "refused" and any("stale" in x for x in r.reasons)


def test_identical_proposal_is_idempotent(conn, approver):
    a = proposed(conn, approver)
    b = actions.propose(conn, approver, ACTION, ARGS, [latest_id(conn)], "again")
    assert b.proposal_id == a.proposal_id and b.duplicate
    assert conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 1


# --- no execution without approval --------------------------------------------
def test_cannot_execute_without_approval(conn, approver):
    p = proposed(conn, approver)
    e = actions.execute(conn, approver, p.proposal_id)
    assert not e.ok and "not approved" in e.reasons[0]
    assert outbox_count(conn) == 0
    assert len(events(conn, "execution_blocked")) == 1


def test_viewer_cannot_approve(conn, approver):
    p = proposed(conn, approver)
    viewer = Session(ACME, "viewer-acme-1", "viewer")
    d = actions.decide(conn, viewer, p.proposal_id, p.payload_hash, "approve")
    assert not d.ok and "not an authorised approver" in d.reasons[0]
    assert actions.get_proposal(conn, ACME, p.proposal_id)["status"] == "pending"


def test_forged_session_cannot_approve(conn, approver):
    """An in-memory Session that claims approver rights is not enough: authority is re-read from the admins table."""
    p = proposed(conn, approver)
    forged = Session(ACME, "admin-globex-1", "approver")  # real admin, but of another company
    assert not actions.decide(conn, forged, p.proposal_id, p.payload_hash, "approve").ok
    ghost = Session(ACME, "ghost", "approver")
    assert not actions.decide(conn, ghost, p.proposal_id, p.payload_hash, "approve").ok
    assert outbox_count(conn) == 0


def test_other_company_cannot_see_or_decide(conn, approver):
    p = proposed(conn, approver)
    globex = Session(GLOBEX, "admin-globex-1", "approver")
    d = actions.decide(conn, globex, p.proposal_id, p.payload_hash, "approve")
    assert not d.ok and d.reasons == ["proposal not found"]  # same answer as a nonexistent id
    assert actions.execute(conn, globex, p.proposal_id).reasons == ["proposal not found"]
    assert actions.get_proposal(conn, GLOBEX, p.proposal_id) is None
    assert actions.list_proposals(conn, GLOBEX) == []


def test_hash_mismatch_is_denied(conn, approver):
    p = proposed(conn, approver)
    d = actions.decide(conn, approver, p.proposal_id, "0" * 64, "approve")
    assert not d.ok and "hash mismatch" in d.reasons[0]
    assert actions.get_proposal(conn, ACME, p.proposal_id)["status"] == "pending"


# --- approve -> execute -> replay ---------------------------------------------
def test_approve_execute_once_and_replay_is_idempotent(conn, approver):
    p = proposed(conn, approver)
    assert outbox_count(conn) == 0                                   # nothing before approval

    d = actions.decide(conn, approver, p.proposal_id, p.payload_hash, "approve")
    assert d.ok and d.status == "approved" and not d.replayed
    assert outbox_count(conn) == 0                                   # approval alone has no side effect
    e = actions.execute(conn, approver, p.proposal_id)
    assert e.ok and not e.replayed and outbox_count(conn) == 1

    d2 = actions.decide(conn, approver, p.proposal_id, p.payload_hash, "approve")   # replayed approval
    e2 = actions.execute(conn, approver, p.proposal_id)                              # replayed execution
    assert d2.ok and d2.replayed and e2.ok and e2.replayed and e2.outbox_id == e.outbox_id
    assert outbox_count(conn) == 1                                   # still exactly one

    assert len(events(conn, "action_executed")) == 1
    assert len(events(conn, "decision_replayed")) == 1 and len(events(conn, "execution_replayed")) == 1
    ob = actions.outbox_rows(conn, ACME)[0]
    assert json.loads(ob["payload_json"])["approved_by"] == "admin-acme-1"


def test_rejected_proposal_is_final(conn, approver):
    p = proposed(conn, approver)
    assert actions.decide(conn, approver, p.proposal_id, p.payload_hash, "reject").status == "rejected"
    late = actions.decide(conn, approver, p.proposal_id, p.payload_hash, "approve")
    assert not late.ok and "final" in late.reasons[0]
    assert not actions.execute(conn, approver, p.proposal_id).ok
    assert outbox_count(conn) == 0


# --- execution safety ---------------------------------------------------------
def test_execution_is_atomic(conn, approver):
    """If the outbox insert fails, the status change and the audit row roll back with it."""
    p = proposed(conn, approver)
    actions.decide(conn, approver, p.proposal_id, p.payload_hash, "approve")
    conn.execute("INSERT INTO outbox(proposal_id, company_id, action, payload_json) VALUES (?,?,?,?)",
                 (p.proposal_id, ACME, ACTION, "{}"))               # simulate a conflicting pre-existing entry
    with pytest.raises(sqlite3.IntegrityError):
        actions.execute(conn, approver, p.proposal_id)
    assert actions.get_proposal(conn, ACME, p.proposal_id)["status"] == "approved"   # CAS update rolled back
    assert events(conn, "action_executed") == []
    assert outbox_count(conn) == 1                                   # only the conflicting row we planted


def test_execution_rechecks_evidence_and_refuses_if_it_went_stale(conn, approver):
    """Approved against snapshot N; a newer snapshot shows the check passing -> execution must refuse."""
    p = proposed(conn, approver)
    actions.decide(conn, approver, p.proposal_id, p.payload_hash, "approve")
    raw = json.loads(conn.execute("SELECT raw_json FROM snapshots WHERE device_id=? ORDER BY collected_at DESC LIMIT 1",
                                  (DEVICE,)).fetchone()[0])
    raw["collected_at"] = "2026-06-12T12:00:00Z"
    for c in raw["compliance_results"]:
        c["status"] = "pass"
    db.load_records(conn, [raw])

    e = actions.execute(conn, approver, p.proposal_id)
    assert not e.ok and e.status == "failed" and any("not current" in r or "not failing" in r for r in e.reasons)
    assert outbox_count(conn) == 0
    assert actions.get_proposal(conn, ACME, p.proposal_id)["status"] == "failed"
    assert len(events(conn, "execution_refused_by_policy")) == 1
