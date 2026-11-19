"""Real-model evaluation suite.

    uv run python -m evals.run                      # all cases, 1 repeat, 4 workers
    uv run python -m evals.run --repeats 3          # measure stability
    uv run python -m evals.run --ids qa-low-disk-acme,act-ticket-approved
    uv run python -m evals.run --category trends
    uv run python -m evals.run --publish            # also copy the report to docs/evidence/

Needs OPENROUTER_API_KEY (in .env). Writes evals/reports/live-<timestamp>.{json,md}. Results are reported separately
from the offline pytest suite and always record the model, provider and sampling settings.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from fleet_copilot import actions, audit, config, db, llm as llm_mod
from fleet_copilot.session import Session

from .oracle import Oracle
from .scorers import Observed, Scored, score_case

CASES_PATH = Path(__file__).with_name("cases.yaml")
REPORT_DIR = Path(__file__).with_name("reports")


def load_cases(ids: list[str] | None = None, category: str | None = None) -> list[dict[str, Any]]:
    cases = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))["cases"]
    seen = set()
    for c in cases:
        assert c["id"] not in seen, f"duplicate case id {c['id']}"
        seen.add(c["id"])
    if ids:
        missing = set(ids) - seen
        if missing:
            raise SystemExit(f"unknown case ids: {sorted(missing)}")
        cases = [c for c in cases if c["id"] in ids]
    if category:
        cases = [c for c in cases if c["category"] == category]
    return cases


PRIMARY_TOOLS = {"query_devices", "analyze_trends"}


def _tool_of(state: dict[str, Any], claim: dict[str, Any]) -> str | None:
    return state.get("results", {}).get(claim["result_id"], {}).get("tool")


def observe(case: dict[str, Any], conn: sqlite3.Connection, outcome, approvals_given: int) -> Observed:
    company = case["company"]
    state = outcome.state
    final = state.get("final") or {}
    claims = final.get("claims", [])
    entries = [p for p in state.get("proposals", [])]
    db_props = actions.list_proposals(conn, company)
    outbox = actions.outbox_rows(conn, company)
    gen = []
    for p in db_props:
        gen.append(p["justification"])
        gen += [v for k, v in p["args"].items() if k in ("note", "reason", "message")]
    for o in outbox:
        pl = json.loads(o["payload_json"])
        gen += [pl.get("justification", "")] + [pl[k] for k in ("note", "reason", "message") if k in pl]
    orphans = conn.execute("SELECT COUNT(*) FROM outbox o LEFT JOIN approvals a ON a.proposal_id=o.proposal_id "
                           "WHERE a.proposal_id IS NULL OR a.decision!='approved'").fetchone()[0]
    return Observed(
        company=company, question=case["question"], approval_mode=case.get("approval", "none"),
        approvals_given=approvals_given, validated=outcome.validated, rendered=outcome.rendered,
        summary=final.get("summary", ""), claimed={d for c in claims for d in c["device_ids"]},
        claimed_primary={d for c in claims for d in c["device_ids"] if _tool_of(state, c) in PRIMARY_TOOLS},
        has_primary_claim=any(_tool_of(state, c) in PRIMARY_TOOLS for c in claims),
        tools=[t["tool"] for t in outcome.trace if t.get("tool")], proposal_entries=entries, db_proposals=db_props,
        outbox=outbox, audit_events=[r["event"] for r in audit.events(conn, company_id=company)],
        results=state.get("results", {}), orphan_outbox_rows=orphans, plan_recorded=bool(state.get("plan")),
        coverage_gaps=state.get("coverage_gaps", []), generated_text=[g for g in gen if g])


def run_one(case: dict[str, Any], repeat: int, oracle: Oracle, agent_factory=llm_mod.make_agent) -> dict[str, Any]:
    t0 = time.time()
    base = {"id": case["id"], "category": case["category"], "repeat": repeat, "question": case["question"]}
    try:
        conn = db.connect(":memory:")
        db.load_ndjson(conn)
        db.seed_admins(conn)
        company = case["company"]
        session = Session.for_admin(conn, f"admin-{company.split('-')[0]}-1", company)
        agent = agent_factory(conn, session, checkpoint_conn=sqlite3.connect(":memory:", check_same_thread=False))
        outcome = agent.ask(case["question"])
        mode, approvals = case.get("approval", "none"), 0
        while outcome.pending and mode in ("approve", "reject") and approvals < 5:   # the simulated human
            outcome = agent.resume(outcome.thread_id, mode, outcome.pending[0]["payload_hash"])
            approvals += 1 if mode == "approve" else 0
        obs = observe(case, conn, outcome, approvals)
        scored: Scored = score_case(case, obs, oracle)
        return {**base, "passed": scored.passed, "error": None, "seconds": round(time.time() - t0, 1),
                "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in scored.checks],
                "tp": scored.tp, "fp": scored.fp, "fn": scored.fn, "validated": obs.validated,
                "plan_recorded": obs.plan_recorded, "coverage_gaps": obs.coverage_gaps, "tools": obs.tools,
                "proposal_outcomes": [{"action": e.get("action"), "status": e["status"]} for e in obs.proposal_entries],
                "outbox_rows": len(obs.outbox), "answer": obs.rendered[:1800]}
    except Exception as e:  # noqa: BLE001 - an infrastructure error is a failed run, reported as such
        return {**base, "passed": False, "error": f"{type(e).__name__}: {str(e)[:300]}", "seconds": round(time.time() - t0, 1),
                "checks": [], "tp": 0, "fp": 0, "fn": 0, "validated": None, "plan_recorded": False,
                "coverage_gaps": [], "tools": [], "proposal_outcomes": [], "outbox_rows": 0, "answer": ""}


def _git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              cwd=config.ROOT, timeout=10).stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def build_report(runs: list[dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any]:
    ok_runs = [r for r in runs if r["error"] is None]
    by_cat: dict[str, list[dict]] = defaultdict(list)
    by_case: dict[str, list[dict]] = defaultdict(list)
    for r in runs:
        by_cat[r["category"]].append(r)
        by_case[r["id"]].append(r)
    tp, fp, fn = (sum(r[k] for r in runs) for k in ("tp", "fp", "fn"))
    pct = lambda a, b: round(100.0 * a / b, 1) if b else None  # noqa: E731
    return {
        "meta": meta,
        "summary": {
            "runs": len(runs), "passed": sum(r["passed"] for r in runs), "pass_rate_pct": pct(sum(r["passed"] for r in runs), len(runs)),
            "infra_errors": sum(r["error"] is not None for r in runs),
            "device_set_precision_pct": pct(tp, tp + fp), "device_set_recall_pct": pct(tp, tp + fn),
            "claim_validation_rate_pct": pct(sum(r["validated"] is True for r in ok_runs), len(ok_runs)),
            "plan_recorded_pct": pct(sum(r["plan_recorded"] for r in ok_runs), len(ok_runs)),
            "plan_coverage_gap_runs": sum(bool(r["coverage_gaps"]) for r in ok_runs),
            "avg_seconds": round(sum(r["seconds"] for r in runs) / len(runs), 1) if runs else None,
        },
        "by_category": {c: {"runs": len(v), "passed": sum(r["passed"] for r in v), "pass_rate_pct": pct(sum(r["passed"] for r in v), len(v))}
                        for c, v in sorted(by_cat.items())},
        "by_case": {i: {"runs": len(v), "passed": sum(r["passed"] for r in v), "stable": len({r["passed"] for r in v}) == 1}
                    for i, v in by_case.items()},
        "runs": runs,
    }


def render_md(rep: dict[str, Any]) -> str:
    m, s = rep["meta"], rep["summary"]
    L = [f"# Fleet Copilot - real-model evaluation report", "",
         "> Real-model results. Reported separately from the deterministic offline pytest suite.", "",
         "| | |", "|---|---|",
         f"| Date (UTC) | {m['date']} |", f"| Model | `{m['model']}` |", f"| Provider | `{m['provider']}` |",
         f"| Temperature / seed | {m['temperature']} / {m['seed']} |", f"| Repeats per case | {m['repeats']} |",
         f"| Cases | {m['cases']} |", f"| Git commit | `{m['git_commit']}` |", f"| Dataset sha256 | `{m['dataset_sha256']}` |",
         f"| Python | {m['python']} |",
         *([f"| Merged from | {', '.join(m['merged_from'])} (latest completed run per case) |"] if m.get("merged_from") else []),
         "",
         "## Summary", "", f"- **Pass rate: {s['passed']}/{s['runs']} runs ({s['pass_rate_pct']}%)**; infrastructure errors: {s['infra_errors']}",
         f"- Device-set precision {s['device_set_precision_pct']}% / recall {s['device_set_recall_pct']}% (cases that expect a device set)",
         f"- Answers passing claim validation: {s['claim_validation_rate_pct']}%",
         f"- Plan recorded: {s['plan_recorded_pct']}% of runs; runs with unresolved plan-coverage gaps: {s['plan_coverage_gap_runs']}",
         f"- Average wall time per run: {s['avg_seconds']}s", "",
         "## Pass rate by category", "", "| Category | Passed | Runs | Rate |", "|---|---|---|---|"]
    L += [f"| {c} | {v['passed']} | {v['runs']} | {v['pass_rate_pct']}% |" for c, v in rep["by_category"].items()]
    L += ["", "## Cases", "", "| Case | Category | Result | Failed checks |", "|---|---|---|---|"]
    by_case: dict[str, list[dict]] = defaultdict(list)
    for r in rep["runs"]:
        by_case[r["id"]].append(r)
    for cid, rs in by_case.items():
        p = sum(r["passed"] for r in rs)
        failed = sorted({c["name"] for r in rs for c in r["checks"] if not c["ok"]} | {r["error"][:60] for r in rs if r["error"]})
        flag = "PASS" if p == len(rs) else ("FAIL" if p == 0 else f"FLAKY {p}/{len(rs)}")
        if flag == "FAIL" and all(r["error"] for r in rs):
            flag = "NOT MEASURED (infrastructure error)"
        L.append(f"| {cid} | {rs[0]['category']} | {flag if len(rs) == 1 or flag != 'PASS' else f'PASS x{len(rs)}'} | {'; '.join(failed)} |")
    bad = [r for r in rep["runs"] if not r["passed"]]
    if bad:
        L += ["", "## Failure details", ""]
        for r in bad:
            L += [f"### {r['id']} (repeat {r['repeat']})", "", f"Question: {r['question']}", ""]
            if r["error"]:
                L += [f"Error: `{r['error']}`", ""]
            L += [f"- {'OK  ' if c['ok'] else 'FAIL'} {c['name']}" + (f" - {c['detail']}" if not c["ok"] and c["detail"] else "")
                  for c in r["checks"]]
            L += ["", f"Tools used: {r['tools']}; proposals: {r['proposal_outcomes']}; outbox rows: {r['outbox_rows']}", "",
                  "```text", r["answer"][:900], "```", ""]
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    ap = argparse.ArgumentParser(prog="python -m evals.run")
    ap.add_argument("--ids", help="comma-separated case ids")
    ap.add_argument("--category")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--publish", action="store_true", help="also copy the report to docs/evidence/")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args(argv)

    cases = load_cases(a.ids.split(",") if a.ids else None, a.category)
    if a.list:
        for c in cases:
            print(f"{c['id']:36} {c['category']:18} {c['company']}")
        return 0
    llm_mod._chat()  # fail fast, with a clear message, if the key is missing

    oracle = Oracle()
    jobs = [(c, r) for c in cases for r in range(1, a.repeats + 1)]
    print(f"Running {len(jobs)} runs ({len(cases)} cases x {a.repeats}) with {a.workers} workers; model={config.MODEL}",
          flush=True)
    runs: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        futs = {ex.submit(run_one, c, r, oracle): (c, r) for c, r in jobs}
        for n, f in enumerate(as_completed(futs), 1):
            res = f.result()
            runs.append(res)
            bad = [c["name"] for c in res["checks"] if not c["ok"]]
            print(f"[{n:>3}/{len(jobs)}] {'PASS' if res['passed'] else 'FAIL'} {res['id']} r{res['repeat']} "
                  f"({res['seconds']}s)" + (f"  ERROR {res['error']}" if res["error"] else (f"  -> {bad}" if bad else "")),
                  flush=True)
    order = {(c["id"], r): i for i, (c, r) in enumerate(jobs)}
    runs.sort(key=lambda r: order[(r["id"], r["repeat"])])

    meta = {"date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), **llm_mod.describe(),
            "repeats": a.repeats, "cases": len(cases), "git_commit": _git_commit(), "python": platform.python_version(),
            "dataset_sha256": hashlib.sha256(config.DATASET_PATH.read_bytes()).hexdigest()[:16]}
    meta["provider"] = meta["provider"]
    rep = build_report(runs, meta)
    REPORT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    jpath, mpath = REPORT_DIR / f"live-{stamp}.json", REPORT_DIR / f"live-{stamp}.md"
    jpath.write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    mpath.write_text(render_md(rep), encoding="utf-8")
    if a.publish:
        out = config.ROOT / "docs" / "evidence"
        out.mkdir(parents=True, exist_ok=True)
        shutil.copy(mpath, out / "eval-live-report.md")
        shutil.copy(jpath, out / "eval-live-report.json")
    s = rep["summary"]
    print(f"\n=== {s['passed']}/{s['runs']} runs passed ({s['pass_rate_pct']}%), infra errors {s['infra_errors']} ===")
    for c, v in rep["by_category"].items():
        print(f"  {c:18} {v['passed']}/{v['runs']}")
    print(f"\nReport: {mpath}")
    return 0 if s["passed"] == s["runs"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
