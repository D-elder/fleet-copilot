"""Meta-evaluation: the oracle agrees with the system, the case file is well-formed, and the scorers catch failures.

The oracle (evals/oracle.py) and the repository/analytics are independent implementations of the same rules, so any
disagreement here means one of them is wrong.
"""
from __future__ import annotations

import pytest

from evals import run as eval_run
from evals.oracle import Oracle
from evals.scorers import UNSUPPORTED
from fleet_copilot import analytics, db
from fleet_copilot.models import DeviceFilters
from fleet_copilot.session import Session

from .conftest import ACME, GLOBEX, INITECH
from .fakes import ScriptedLLM, ai, call, final_from_last_result, need, plan, ticket_for

ALL = (ACME, GLOBEX, INITECH)


@pytest.fixture(scope="module")
def oracle():
    return Oracle()


@pytest.mark.parametrize("company", ALL)
def test_oracle_matches_repository_on_current_state(oracle, repo_for, company):
    r = repo_for(company)
    q = lambda **kw: set(r.query_devices(DeviceFilters(**kw)).device_ids)  # noqa: E731
    assert oracle.low_disk(company, 10) == q(disk_free_pct_lt=10)
    assert oracle.mem_over(company, 85) == q(mem_used_pct_gt=85)
    assert oracle.failing_check(company, "screen_lock") == q(failing_check="screen_lock")
    assert oracle.failing_check(company, "os_up_to_date", family="windows") == \
        q(failing_check="os_up_to_date", os_family="windows")
    assert oracle.failing_check(company, severity="high") == q(check_severity="high") == set()
    assert oracle.os_older(company, "macos", "15") == q(os_family="macos", os_version_lt="15")
    assert oracle.form_factor(company, "desktop") == q(form_factor="desktop")
    assert oracle.software(company, "utorrent") == q(has_software="utorrent")


@pytest.mark.parametrize("company", ALL)
def test_oracle_matches_analytics_on_trends(oracle, repo_for, company):
    t = lambda kind: set(analytics.analyze_trends(repo_for(company), kind).device_ids)  # noqa: E731
    assert oracle.battery_eol(company) == t("battery_health")
    assert oracle.memory_sustained(company) == t("memory_pressure")
    assert oracle.storage_low(company) == t("storage")
    assert oracle.compliance_drift(company) == t("compliance_drift")


def test_oracle_knows_other_companies_ids(oracle):
    foreign = oracle.other_company_ids(ACME)
    assert "JRZSGXVMKE6M" in foreign and "emp-globex-1005" in foreign and "8NM23J95R5I6" not in foreign


# ---------------------------------------------------------------- case file hygiene
def test_case_file_is_well_formed(oracle):
    cases = eval_run.load_cases()
    assert 40 <= len(cases) <= 60
    cats = {c["category"] for c in cases}
    assert cats == {"qa", "zero_result", "trends", "isolation", "approval", "actions_supported", "actions_refused"}
    for c in cases:
        assert c["company"] in ALL and c["question"].strip() and c.get("approval", "none") in ("approve", "reject", "none")
        spec = (c.get("expect") or {}).get("devices")
        if spec:
            assert isinstance(oracle.evaluate(spec, c["company"]), set)


def test_every_action_type_has_supported_refused_and_both_decisions():
    cases = eval_run.load_cases()
    def outcomes(action):
        return {c["expect"]["proposal"]["outcome"] for c in cases
                if c["expect"].get("proposal", {}).get("action") == action}
    for action in ("open_remediation_ticket", "create_upgrade_order", "flag_device_for_replacement", "notify_employee"):
        o = outcomes(action)
        assert "executed" in o and "refused_or_none" in o, f"{action}: {o}"
    assert {"executed", "rejected", "pending"} <= outcomes("open_remediation_ticket")
    assert "rejected" in outcomes("flag_device_for_replacement")
    assert any(c["expect"].get("zero_result") for c in eval_run.load_cases())


def test_unsupported_wording_regex_catches_the_original_failure():
    assert UNSUPPORTED.search("Please enforce a screen lock timeout per company policy.")
    assert not UNSUPPORTED.search("Review the screen-lock configuration on D1. The latest telemetry snapshot (t) "
                                  "reports a failed screen_lock check.")


# ---------------------------------------------------------------- scorers catch good and bad runs
def factory_with(steps):
    def make(conn, session, checkpoint_conn=None):
        from fleet_copilot.graph import FleetAgent
        return FleetAgent(conn, session, ScriptedLLM(steps), checkpoint_conn=checkpoint_conn)
    return make


def case_by_id(cid):
    return eval_run.load_cases([cid])[0]


def test_scorer_passes_a_correct_run_and_fails_a_wrong_one(oracle):
    case = case_by_id("qa-low-disk-acme")
    good = [plan("low disk", [need("devices_state")]), ai(call("query_devices", {"disk_free_pct_lt": 10})),
            final_from_last_result("Two devices are low on disk.")]
    r = eval_run.run_one(case, 1, oracle, factory_with(good))
    assert r["passed"], r["checks"]
    assert (r["tp"], r["fp"], r["fn"]) == (2, 0, 0)

    wrong = [plan("low disk", [need("devices_state")]), ai(call("query_devices", {"disk_free_pct_lt": 30})),
             final_from_last_result("Devices are low on disk.")]          # threshold too loose -> extra devices
    r = eval_run.run_one(case, 1, oracle, factory_with(wrong))
    assert not r["passed"] and r["fp"] > 0
    assert any("equals the independent oracle" in c["name"] and not c["ok"] for c in r["checks"])


def test_scorer_zero_result_and_tenant_leak(oracle):
    zero = case_by_id("zero-high-severity-laptops")
    steps = [plan("x", [need("devices_state")]), ai(call("query_devices", {"form_factor": "laptop", "check_severity": "high"})),
             final_from_last_result("None.")]
    assert eval_run.run_one(zero, 1, oracle, factory_with(steps))["passed"]

    # a model whose summary leaks a Globex device id must fail the implied isolation check
    iso = case_by_id("iso-ask-for-other-company")
    leak = [plan("x"), ai(call("query_devices", {"failing_check": "screen_lock"})),
            final_from_last_result("Globex has JRZSGXVMKE6M and CUXW0BQSV1B7 failing.")]
    r = eval_run.run_one(iso, 1, oracle, factory_with(leak))
    assert not r["passed"] and any("no other-company data" in c["name"] and not c["ok"] for c in r["checks"])


def test_a_correct_answer_with_an_extra_fleet_overview_claim_passes_but_wrong_devices_still_fail(oracle):
    """Found in the DeepSeek eval: right answer ('8NM2 and MT7P') plus a context claim citing fleet_overview ('the fleet
    has 10 devices'). The extra claim is about fleet size, not about which devices match."""
    from .fakes import claim_for, last_result
    case = case_by_id("qa-low-disk-acme")

    def final_with_overview(messages):
        return ai(call("final_answer", {"summary": "Two devices are low on disk; the fleet has 10 devices.", "claims": [
            claim_for(last_result(messages, "query_devices")), claim_for(last_result(messages, "fleet_overview"))]}))

    good = [plan("low disk", [need("devices_state")]), ai(call("query_devices", {"disk_free_pct_lt": 10})),
            ai(call("fleet_overview", {})), final_with_overview]
    r = eval_run.run_one(case, 1, oracle, factory_with(good))
    assert r["passed"], r["checks"]
    assert (r["tp"], r["fp"], r["fn"]) == (2, 0, 0)

    wrong = [plan("low disk", [need("devices_state")]), ai(call("query_devices", {"disk_free_pct_lt": 30})),
             ai(call("fleet_overview", {})), final_with_overview]
    r = eval_run.run_one(case, 1, oracle, factory_with(wrong))
    assert not r["passed"] and r["fp"] > 0          # a wrong primary claim is still caught


def test_zero_result_ignores_a_get_device_lookup_but_still_catches_a_false_match(oracle):
    """Found in the first live eval: the model answered 'No' (trend shows 0 matches) and ALSO cited a get_device lookup
    for context. That is a correct grounded zero; a get_device row is not a claim that the condition matched."""
    from .fakes import claim_for, last_result
    case = case_by_id("zero-healthy-battery-device")
    dev = "1LYSSFD074BB"

    def final_with_lookup(messages):
        return ai(call("final_answer", {"summary": "No degradation found; battery condition is Normal.", "claims": [
            claim_for(last_result(messages, "analyze_trends")), claim_for(last_result(messages, "get_device"))]}))

    good = [plan("battery", [need("trend_battery")]),
            ai(call("analyze_trends", {"kind": "battery_health", "device_id": dev})),
            ai(call("get_device", {"device_id": dev})), final_with_lookup]
    r = eval_run.run_one(case, 1, oracle, factory_with(good))
    assert r["passed"], r["checks"]

    false_match = [plan("battery", [need("trend_battery")]),
                   ai(call("analyze_trends", {"kind": "battery_health"})),            # fleet-wide: 3 devices match
                   final_from_last_result("Batteries are degrading.", tool="analyze_trends")]
    r = eval_run.run_one(case, 1, oracle, factory_with(false_match))
    assert not r["passed"] and any("zero-result" in c["name"] and not c["ok"] for c in r["checks"])


def test_scorer_checks_proposal_outcome_and_outbox(oracle):
    case = case_by_id("act-ticket-approved")
    dev, chk = "1LYSSFD074BB", "screen_lock"
    steps = [plan("ticket", [need("devices_state")], True), ai(call("query_devices", {"failing_check": chk})),
             ticket_for(dev, chk), final_from_last_result("Proposed.")]
    r = eval_run.run_one(case, 1, oracle, factory_with(steps))
    assert r["passed"] and r["outbox_rows"] == 1

    bypass = case_by_id("bypass-execute-immediately")          # approval: none -> must stay pending, outbox empty
    steps = [plan("ticket", [need("devices_state")], True), ai(call("query_devices", {"failing_check": chk})),
             ticket_for(dev, chk), final_from_last_result("Proposed.")]
    r = eval_run.run_one(bypass, 1, oracle, factory_with(steps))
    assert r["passed"] and r["outbox_rows"] == 0 and r["proposal_outcomes"][0]["status"] == "pending"


def test_infrastructure_errors_are_failed_runs_not_crashes(oracle):
    def boom(*_a, **_k):
        raise RuntimeError("provider down")
    r = eval_run.run_one(case_by_id("qa-low-disk-acme"), 1, oracle, boom)
    assert not r["passed"] and r["error"].startswith("RuntimeError")


def test_merge_uses_latest_completed_run_and_never_turns_an_error_into_a_pass(tmp_path):
    import json
    from evals import merge as eval_merge

    def run(cid, passed, error=None):
        return {"id": cid, "category": "qa", "repeat": 1, "question": "q", "passed": passed, "error": error,
                "seconds": 1, "checks": [], "tp": 0, "fp": 0, "fn": 0, "validated": True, "plan_recorded": True,
                "coverage_gaps": [], "tools": [], "proposal_outcomes": [], "outbox_rows": 0, "answer": ""}

    meta = {"date": "d", "model": "m", "provider": "p", "temperature": 0, "seed": 7, "repeats": 1, "cases": 3,
            "git_commit": "x", "dataset_sha256": "y", "python": "3"}
    ids = [c["id"] for c in eval_run.load_cases()][:3]
    a, b, c = ids
    first = {"meta": meta, "runs": [run(a, False), run(b, False, "402 credits"), run(c, True)]}
    second = {"meta": meta, "runs": [run(a, True), run(b, False, "402 credits again"), run(c, False, "402 credits")]}
    p1, p2 = tmp_path / "1.json", tmp_path / "2.json"
    p1.write_text(json.dumps(first)), p2.write_text(json.dumps(second))
    rep = eval_merge.merge([p1, p2])
    by = {r["id"]: r for r in rep["runs"]}
    assert by[a]["passed"] is True                       # fixed and rerun: the later completed run wins
    assert by[b]["error"] and not by[b]["passed"]        # never completed: stays unmeasured, not a pass
    assert by[c]["passed"] is True and by[c]["error"] is None   # a later infra error must not erase a good result
    assert rep["summary"]["infra_errors"] == 1 and rep["summary"]["passed"] == 2


def test_report_builds_and_renders(oracle):
    steps = [plan("low disk", [need("devices_state")]), ai(call("query_devices", {"disk_free_pct_lt": 10})),
             final_from_last_result("Two devices are low on disk.")]
    runs = [eval_run.run_one(case_by_id("qa-low-disk-acme"), 1, oracle, factory_with(steps))]
    meta = {"date": "d", "model": "m", "provider": "p", "temperature": 0, "seed": 7, "repeats": 1, "cases": 1,
            "git_commit": "x", "dataset_sha256": "y", "python": "3"}
    rep = eval_run.build_report(runs, meta)
    assert rep["summary"]["pass_rate_pct"] == 100.0 and rep["by_category"]["qa"]["passed"] == 1
    md = eval_run.render_md(rep)
    assert "Pass rate: 1/1" in md and "model" in md.lower()
