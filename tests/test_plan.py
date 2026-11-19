"""Planning is tied to execution: planned evidence must be gathered (or the plan revised) before an answer stands."""
from __future__ import annotations

import pytest

from fleet_copilot import actions, db
from fleet_copilot.coverage import uncovered
from fleet_copilot.graph import FleetAgent
from fleet_copilot.models import ToolResult, Window

from .fakes import ScriptedLLM, ai, call, final_from_last_result, need, plan
from .helpers import sess, trend_ids


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.load_ndjson(c)
    db.seed_admins(c)
    return c


def agent_for(conn, steps, company="acme-001"):
    return FleetAgent(conn, sess(company), ScriptedLLM(steps))


def result(tool, **filters):
    return ToolResult(result_id=f"R-{tool}", tool=tool, company_id="acme-001", filters=filters,
                      window=Window(), as_of="x", population_checked=[], count=0, rows=[])


def test_coverage_mapping():
    rs = {"a": result("query_devices"), "b": result("analyze_trends", kind="memory_pressure"),
          "c": result("get_device", device_id="D1"), "d": result("analyze_trends", kind="storage", device_id="D2")}
    needs = [need("devices_state"), need("trend_memory"), need("trend_memory", "ANY"), need("device_detail", "D1"),
             need("device_detail", "D9"), need("trend_storage", "D2"), need("trend_storage", "D3"),
             need("trend_battery"), need("fleet_overview")]
    assert uncovered(needs, rs) == ["device_detail(D9)", "trend_storage(D3)", "trend_battery", "fleet_overview"]


def test_uncovered_plan_item_sends_the_agent_back_to_fetch_it(conn):
    a = agent_for(conn, [
        plan("memory trends", [need("trend_memory")]),
        ai(call("query_devices", {"failing_check": "screen_lock"})),
        final_from_last_result("premature"),                                  # trend_memory never fetched
        ai(call("analyze_trends", {"kind": "memory_pressure"})),
        final_from_last_result("Sustained memory pressure on one device.", tool="analyze_trends"),
    ])
    out = a.ask("Which devices are constrained by RAM?")
    cov = [t for t in out.trace if t.get("node") == "coverage"]
    assert cov[0]["gaps"] == ["trend_memory"] and cov[0]["retry"] is True
    assert cov[-1]["gaps"] == [] and out.validated is True
    assert "memory_pressure: sustained" in out.rendered and "M4XVHUV1MEPZ" in out.rendered
    assert "planned evidence was not gathered" not in out.rendered


def test_plan_can_be_revised_once_and_then_coverage_passes(conn):
    a = agent_for(conn, [
        plan("memory trends", [need("trend_memory")]),
        ai(call("query_devices", {"failing_check": "screen_lock"})),
        final_from_last_result("premature"),
        ai(call("revise_plan", {"reason": "the question only needs current state", "evidence_needed": []})),
        final_from_last_result("Five devices fail screen_lock."),
    ])
    out = a.ask("q")
    assert out.validated is True and out.state["plan"]["revision_reason"].startswith("the question")
    assert any(t.get("node") == "plan" and t.get("revised") for t in out.trace)
    assert [t["gaps"] for t in out.trace if t.get("node") == "coverage"] == [["trend_memory"], []]


def test_second_revision_is_refused(conn):
    a = agent_for(conn, [
        plan("p", [need("trend_memory")]),
        ai(call("revise_plan", {"reason": "r1", "evidence_needed": [{"kind": "trend_storage"}]})),
        ai(call("revise_plan", {"reason": "r2", "evidence_needed": []})),
        ai(call("query_devices", {})),
        ai(call("analyze_trends", {"kind": "storage"})),
        final_from_last_result("x", tool="analyze_trends"),
    ])
    out = a.ask("q")
    assert out.state["plan_revisions"] == 1 and out.state["plan"]["revision_reason"] == "r1"
    errs = [m.content for m in out.state["messages"] if getattr(m, "name", "") == "revise_plan"]
    assert errs[-1] == "ERROR: the plan may be revised only once"


def test_gap_after_the_retry_budget_is_disclosed_not_hidden(conn):
    a = agent_for(conn, [
        plan("memory trends", [need("trend_memory")]),
        ai(call("query_devices", {"failing_check": "screen_lock"})),
        final_from_last_result("first try"),
        final_from_last_result("second try ignores the gap"),
    ])
    out = a.ask("q")
    assert out.validated is True                                           # claims are still verified...
    assert "planned evidence was not gathered: trend_memory" in out.rendered   # ...and the gap is shown
    assert out.state["coverage_gaps"] == ["trend_memory"]


def test_a_model_that_returns_no_plan_is_flagged_not_fatal(conn):
    a = agent_for(conn, [
        ai(call("query_devices", {})),                                    # not submit_plan
        ai(call("query_devices", {"failing_check": "screen_lock"})),
        final_from_last_result("ok"),
    ])
    out = a.ask("q")
    assert out.state["plan"] is None and out.validated is True
    assert any(t.get("node") == "plan" and "error" in t for t in out.trace)


def test_invalid_plan_arguments_are_flagged(conn):
    a = agent_for(conn, [
        ai(call("submit_plan", {"intent": "x", "evidence_needed": [{"kind": "vibes"}]})),
        ai(call("query_devices", {"failing_check": "screen_lock"})),
        final_from_last_result("ok"),
    ])
    out = a.ask("q")
    assert out.state["plan"] is None and out.validated is True


def test_trend_to_upgrade_order_flow_through_the_graph(conn):
    """analyze_trends -> create_upgrade_order (evidence from the trend row) -> approve -> one outbox row."""
    ids = trend_ids(conn, "acme-001", "memory_pressure", "M4XVHUV1MEPZ")
    a = agent_for(conn, [
        plan("RAM pressure + upgrade", [need("trend_memory")], proposes=True),
        ai(call("analyze_trends", {"kind": "memory_pressure"})),
        ai(call("create_upgrade_order", {"device_id": "M4XVHUV1MEPZ", "component": "ram", "spec": "32GB",
                                         "evidence": ids})),
        final_from_last_result("One device has sustained memory pressure.", tool="analyze_trends"),
    ])
    out = a.ask("Which Acme devices are RAM constrained? Order a RAM upgrade for the worst one.")
    assert out.validated is True and len(out.pending) == 1
    p = out.pending[0]
    assert p["action"] == "create_upgrade_order" and "sustained memory pressure" in p["justification"].lower()
    done = a.resume(out.thread_id, "approve", p["payload_hash"])
    assert done.decisions[0]["status"] == "executed"
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
    assert actions.outbox_rows(conn, "acme-001")[0]["action"] == "create_upgrade_order"


def test_refused_upgrade_is_reported_not_retried(conn):
    ids = trend_ids(conn, "initech-003", "memory_pressure", "I3JLKILKQTAC")
    a = agent_for(conn, [
        plan("RAM upgrade", [need("trend_memory")], proposes=True),
        ai(call("analyze_trends", {"kind": "memory_pressure"})),
        ai(call("create_upgrade_order", {"device_id": "I3JLKILKQTAC", "component": "ram", "spec": "16GB",
                                         "evidence": ids})),
        final_from_last_result("The upgrade was refused.", tool="analyze_trends"),
    ], company="initech-003")
    out = a.ask("Order a RAM upgrade for the Mac mini")
    assert not out.pending and "REFUSED" in out.rendered and "not upgradeable" in out.rendered
    assert conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0
