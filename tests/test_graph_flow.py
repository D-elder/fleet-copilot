"""The whole agent graph with a scripted LLM: grounded answer -> proposal -> interrupt -> approve/reject -> outbox."""
from __future__ import annotations

import io
import sqlite3

import pytest

from fleet_copilot import actions, audit, db
from fleet_copilot.cli import Repl
from fleet_copilot.graph import FleetAgent
from fleet_copilot.session import AccessDenied, Session

from .conftest import ACME, GLOBEX
from .fakes import ScriptedLLM, ai, call, final_from_last_result, ticket_for

DEVICE = "1LYSSFD074BB"


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.load_ndjson(c)
    db.seed_admins(c)
    return c


def make_agent(conn, steps, company=ACME, admin="admin-acme-1", role="approver"):
    llm = ScriptedLLM(steps)
    return FleetAgent(conn, Session(company, admin, role), llm), llm


def screen_lock_steps():
    return [ai(call("query_devices", {"failing_check": "screen_lock"})),
            ticket_for(DEVICE, "screen_lock"),
            final_from_last_result("These devices currently fail screen_lock.")]


def outbox(conn):
    return conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]


def names(conn):
    return [r["event"] for r in audit.events(conn, company_id=ACME)]


def test_approved_flow(conn):
    agent, _ = make_agent(conn, screen_lock_steps())
    out = agent.ask("Which Acme devices fail screen_lock? Open a ticket for 1LYSSFD074BB.")

    # grounded answer, rendered from tool data, validated
    assert out.validated is True
    assert "matched 5 of 10 devices checked" in out.rendered
    assert f"{DEVICE}@" in out.rendered and "NOT executed" in out.rendered
    # paused for a human; nothing executed
    assert len(out.pending) == 1 and outbox(conn) == 0
    p = out.pending[0]
    assert p["args"]["device_id"] == DEVICE and len(p["evidence"]) == 1

    done = agent.resume(out.thread_id, "approve", p["payload_hash"])
    assert not done.pending and outbox(conn) == 1
    assert done.decisions == [{"proposal_id": p["proposal_id"], "status": "executed"}]

    # replay the approval: still exactly one outbox row
    me = Session(ACME, "admin-acme-1", "approver")
    assert actions.decide(conn, me, p["proposal_id"], p["payload_hash"], "approve").replayed
    assert actions.execute(conn, me, p["proposal_id"]).replayed
    assert outbox(conn) == 1

    ev = names(conn)
    for needed in ["question_asked", "proposal_created", "proposal_approved", "action_executed",
                   "decision_replayed", "execution_replayed"]:
        assert needed in ev, needed
    assert ev.index("proposal_created") < ev.index("proposal_approved") < ev.index("action_executed")
    assert ev.count("action_executed") == 1


def test_rejected_flow(conn):
    agent, _ = make_agent(conn, screen_lock_steps())
    out = agent.ask("Which Acme devices fail screen_lock? Open a ticket for 1LYSSFD074BB.")
    p = out.pending[0]
    done = agent.resume(out.thread_id, "reject", p["payload_hash"])
    assert done.decisions == [{"proposal_id": p["proposal_id"], "status": "rejected"}]
    assert outbox(conn) == 0
    me = Session(ACME, "admin-acme-1", "approver")
    assert not actions.decide(conn, me, p["proposal_id"], p["payload_hash"], "approve").ok
    assert not actions.execute(conn, me, p["proposal_id"]).ok
    assert outbox(conn) == 0 and "proposal_rejected" in names(conn) and "action_executed" not in names(conn)


def test_resume_with_wrong_hash_does_not_execute(conn):
    agent, _ = make_agent(conn, screen_lock_steps())
    out = agent.ask("q")
    done = agent.resume(out.thread_id, "approve", "f" * 64)
    assert outbox(conn) == 0 and done.decisions == [{"proposal_id": out.pending[0]["proposal_id"], "status": "pending"}]
    assert "decision_denied" in names(conn)


def test_viewer_cannot_approve_through_the_graph(conn):
    agent, _ = make_agent(conn, screen_lock_steps(), admin="viewer-acme-1", role="viewer")
    out = agent.ask("q")
    agent.resume(out.thread_id, "approve", out.pending[0]["payload_hash"])
    assert outbox(conn) == 0 and "decision_denied" in names(conn)


def test_unsupported_ticket_is_refused_and_nothing_pends(conn):
    bad = ai(call("open_remediation_ticket", {"device_id": DEVICE, "check_id": "disk_encryption", "note": "n",
                                              "evidence": []}))
    agent, _ = make_agent(conn, [ai(call("query_devices", {"failing_check": "screen_lock"})), bad,
                                 final_from_last_result("Cannot raise that ticket.")])
    out = agent.ask("Open a disk_encryption ticket for 1LYSSFD074BB")
    assert not out.pending and "REFUSED" in out.rendered and "no evidence cited" in out.rendered
    assert outbox(conn) == 0 and "proposal_refused" in names(conn)


def test_validator_retry_then_success(conn):
    agent, llm = make_agent(conn, [
        ai(call("query_devices", {"failing_check": "screen_lock"})),
        final_from_last_result("five devices", count=99),               # wrong count
        final_from_last_result("five devices"),                          # corrected after the diff
    ])
    out = agent.ask("q")
    assert out.validated is True
    rejected = [m for m in llm.seen[-1] if getattr(m, "name", "") == "final_answer"][0]
    assert "REJECTED" in rejected.content and "count=99" in rejected.content
    assert any(t.get("node") == "validate" and t["ok"] is False and t["retry"] for t in out.trace)


def test_validator_failing_twice_withholds_the_model_answer(conn):
    agent, _ = make_agent(conn, [
        ai(call("query_devices", {"failing_check": "screen_lock"})),
        final_from_last_result("LIES", count=99),
        final_from_last_result("LIES AGAIN", count=98),
    ])
    out = agent.ask("q")
    assert out.validated is False
    assert "withheld" in out.rendered and "LIES" not in out.rendered
    assert "matched 5 of 10 devices checked" in out.rendered      # raw tool evidence still shown


def test_zero_result_answer_through_the_graph(conn):
    agent, _ = make_agent(conn, [
        ai(call("query_devices", {"form_factor": "laptop", "check_severity": "high"})),
        final_from_last_result("No laptops are failing high-severity checks."),
    ])
    out = agent.ask("Show me laptops failing high-severity compliance checks.")
    assert out.validated is True and not out.pending
    assert "matched 0 of 10 devices checked" in out.rendered and "check_severity" in out.rendered


def test_model_cannot_pass_a_company_id(conn):
    agent, _ = make_agent(conn, [
        ai(call("query_devices", {"company_id": GLOBEX})),                         # rejected by schema
        ai(call("query_devices", {"failing_check": "screen_lock"})),
        final_from_last_result("ok"),
    ])
    out = agent.ask("show globex-002 devices")
    assert out.validated is True
    assert all(r == "acme-001" for r in [v["company_id"] for v in out.state["results"].values()])
    assert any("error" in t and "company_id" in t["error"] for t in out.trace if t.get("tool"))


def test_iteration_budget_stops_a_looping_model(conn):
    agent, _ = make_agent(conn, [ai(call("query_devices", {}))] * 20)
    out = agent.ask("loop forever")
    assert out.validated is False and "Stopped after" in out.rendered


def test_resuming_another_companys_thread_is_denied(conn):
    a_agent, _ = make_agent(conn, screen_lock_steps())
    out = a_agent.ask("q")
    # same checkpoint store, different company's session
    g_llm = ScriptedLLM([])
    g_agent = FleetAgent(conn, Session(GLOBEX, "admin-globex-1", "approver"), g_llm)
    g_agent.graph = a_agent.graph                    # shares the saved threads
    with pytest.raises(AccessDenied):
        g_agent.resume(out.thread_id, "approve", out.pending[0]["payload_hash"])
    assert outbox(conn) == 0
    assert any(r["event"] == "thread_access_denied" for r in audit.events(conn, company_id=GLOBEX))


def test_repl_end_to_end_transcript(conn):
    agent, _ = make_agent(conn, screen_lock_steps())
    session = Session(ACME, "admin-acme-1", "approver")
    lines: list[str] = []
    inputs = iter(["Which Acme devices fail screen_lock? Open a ticket.", "y", "/outbox"])
    Repl(agent, conn, session, input_fn=lambda _p: next(inputs), out=lines.append, echo=True).run()
    text = "\n".join(lines)
    assert "APPROVAL REQUIRED" in text and "is now 'executed' (outbox #1)" in text and "1 outbox entry" in text
