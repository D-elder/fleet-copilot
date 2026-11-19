"""End-to-end proof of one complete flow, driven through the real CLI code path.

  grounded answer -> remediation ticket proposed (backed by a CURRENT failing check) -> pause for approval
  -> outbox empty before approval -> exactly one entry after approval -> still one after replaying the approval.

    uv run python -m evals.smoke            # real model via OpenRouter (needs OPENROUTER_API_KEY in .env)
    uv run python -m evals.smoke --fake     # scripted model, offline rehearsal (not evidence of model behaviour)

Expected results are computed from the raw NDJSON, independently of the repository code under test.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fleet_copilot import actions, audit, config, db, llm as llm_mod
from fleet_copilot.cli import Repl
from fleet_copilot.graph import FleetAgent
from fleet_copilot.session import Session
from fleet_copilot.tools import ToolBox

COMPANY, ADMIN, TARGET, CHECK = "acme-001", "admin-acme-1", "1LYSSFD074BB", "screen_lock"
QUESTION = (f"Which devices at Acme are currently failing the {CHECK} compliance check? "
            f"Then open a remediation ticket for {CHECK} on device {TARGET}.")


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def expected_from_raw(company: str, check: str) -> dict[str, str]:
    """device_id -> latest snapshot_id, for non-stale devices whose latest snapshot has `check` failing."""
    recs = [json.loads(line) for line in open(config.DATASET_PATH, encoding="utf-8") if line.strip()]
    as_of = max(_ts(r["collected_at"]) for r in recs)
    latest: dict[str, dict] = {}
    for r in recs:
        if r["company_id"] == company and (r["device_id"] not in latest
                                           or r["collected_at"] > latest[r["device_id"]]["collected_at"]):
            latest[r["device_id"]] = r
    return {d: f"{d}@{r['collected_at']}" for d, r in latest.items()
            if as_of - _ts(r["collected_at"]) <= timedelta(hours=config.STALE_AFTER_HOURS)
            and any(c["check_id"] == check and c["status"] == "fail" for c in r["compliance_results"])}


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    ap = argparse.ArgumentParser()
    ap.add_argument("--fake", action="store_true", help="use a scripted model (offline rehearsal)")
    ap.add_argument("--out", help="markdown evidence file (default: docs/evidence/slice-live.md for live runs)")
    args = ap.parse_args(argv)
    out_path = Path(args.out) if args.out else (None if args.fake else config.ROOT / "docs" / "evidence" / "slice-live.md")

    db_path = config.ROOT / "var" / "smoke.db"
    db_path.parent.mkdir(exist_ok=True)
    db_path.unlink(missing_ok=True)
    conn = db.bootstrap(db_path)
    session = Session.for_admin(conn, ADMIN, COMPANY)

    if args.fake:
        from tests.fakes import ScriptedLLM, ai, call, final_from_last_result, ticket_for
        model = ScriptedLLM([ai(call("query_devices", {"failing_check": CHECK})), ticket_for(TARGET, CHECK),
                             final_from_last_result("Devices currently failing screen_lock are listed below.")])
        meta = {"mode": "FAKE (scripted model - rehearsal only)"}
    else:
        model = llm_mod.make_llm_invoke(ToolBox(conn, session).specs())
        meta = {"mode": "LIVE", **llm_mod.describe()}
    agent = FleetAgent(conn, session, model, checkpoint_conn=sqlite3.connect(":memory:", check_same_thread=False))

    transcript: list[str] = []

    def out(s: str) -> None:
        transcript.append(s)
        print(s, flush=True)

    def outbox_n() -> int:
        return conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    at_pause: dict[str, object] = {}

    def approval_input(prompt: str) -> str:
        # Runs at the exact moment the graph is paused waiting for a human: nothing may have executed yet.
        at_pause["outbox"] = outbox_n()
        at_pause["statuses"] = [p["status"] for p in actions.list_proposals(conn, COMPANY)]
        return "y"

    repl = Repl(agent, conn, session, input_fn=approval_input, out=out, echo=True)
    started = datetime.now(timezone.utc)
    repl.banner()
    outcome = repl.handle(QUESTION)
    assert outcome is not None

    after_approval = outbox_n()
    props = actions.list_proposals(conn, COMPANY)
    pid = props[0]["proposal_id"] if props else "P-none"
    repl.handle("/proposals")
    repl.handle(f"/approve {pid}")          # replay #1 (idempotent)
    after_replay_1 = outbox_n()
    repl.handle(f"/approve {pid}")          # replay #2
    after_replay_2 = outbox_n()
    repl.handle("/outbox")
    repl.handle("/audit 60")

    # ---------------------------------------------------------------- verdicts
    expected = expected_from_raw(COMPANY, CHECK)
    state = outcome.state
    claims = (state.get("final") or {}).get("claims", [])
    claimed = {d for c in claims for d in c["device_ids"]}
    cited = {s for c in claims for s in c["snapshot_ids"]}
    prop = actions.get_proposal(conn, COMPANY, pid) or {}
    ev = [r["event"] for r in audit.events(conn, company_id=COMPANY)]

    checks: list[tuple[str, bool, str]] = [
        ("Precondition: target device is currently failing the check in the raw data", TARGET in expected,
         f"{len(expected)} devices fail {CHECK} now: {sorted(expected)}"),
        ("Answer passed claim validation (claims match tool results)", state.get("validated") is True,
         f"validated={state.get('validated')}"),
        ("Answer's device set equals the independently computed set", claimed == set(expected),
         f"claimed={sorted(claimed)}"),
        ("Cited snapshots are exactly each device's latest snapshot", cited == set(expected.values()),
         f"cited {len(cited)} snapshot ids"),
        ("Ticket proposed for the target/check with the current failing snapshot as evidence",
         prop.get("args", {}).get("device_id") == TARGET and prop.get("args", {}).get("check_id") == CHECK
         and expected.get(TARGET) in prop.get("evidence", []), f"proposal={pid} evidence={prop.get('evidence')}"),
        ("Graph paused for approval with the proposal still pending", at_pause.get("statuses") == ["pending"],
         f"statuses at pause={at_pause.get('statuses')}"),
        ("Outbox EMPTY before approval", at_pause.get("outbox") == 0, f"outbox rows at pause={at_pause.get('outbox')}"),
        ("Outbox has EXACTLY ONE entry after approval", after_approval == 1 and prop.get("status") == "executed",
         f"outbox rows={after_approval}, status={prop.get('status')}"),
        ("Still exactly one after replaying the approval (x2)", after_replay_1 == 1 and after_replay_2 == 1,
         f"after replay 1={after_replay_1}, replay 2={after_replay_2}"),
        ("Audit: one action_executed; replays recorded as replays", ev.count("action_executed") == 1
         and ev.count("decision_replayed") == 2 and ev.count("execution_replayed") == 2,
         f"action_executed={ev.count('action_executed')} decision_replayed={ev.count('decision_replayed')} "
         f"execution_replayed={ev.count('execution_replayed')}"),
        ("Audit order: created < approved < executed", "proposal_created" in ev and "action_executed" in ev
         and ev.index("proposal_created") < ev.index("proposal_approved") < ev.index("action_executed"), ""),
    ]
    ok = all(c[1] for c in checks)
    print("\n=== VERDICT: " + ("ALL CHECKS PASSED" if ok else "FAILED") + " ===")
    for name, passed, detail in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))

    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rows = audit.events(conn, company_id=COMPANY)
        md = [f"# Thursday slice - end-to-end evidence ({'PASS' if ok else 'FAIL'})", "",
              f"Run started: {started.isoformat(timespec='seconds')}  ",
              *[f"{k}: `{v}`  " for k, v in {**meta, "company": COMPANY, "admin": ADMIN,
                                              "data as-of": agent.repo.as_of()}.items()],
              "", "## Checks", "", "| Result | Check | Detail |", "|---|---|---|",
              *[f"| {'PASS' if p else 'FAIL'} | {n} | {d.replace('|', '/')} |" for n, p, d in checks],
              "", "## CLI transcript", "", "```text", *transcript, "```", "",
              "## Audit records (audit_log, this company)", "", "| id | ts | actor | event | details |",
              "|---|---|---|---|---|",
              *[f"| {r['id']} | {r['ts']} | {r['actor']} | {r['event']} | `{r['details_json'].replace('|', '/')}` |"
                for r in rows],
              "", "## Proposal and outbox rows", "", "```text",
              *[json.dumps({k: v for k, v in p.items()}, sort_keys=True, default=str) for p in props],
              *[json.dumps(o, sort_keys=True) for o in actions.outbox_rows(conn, COMPANY)], "```", ""]
        out_path.write_text("\n".join(md), encoding="utf-8")
        print(f"\nEvidence written to {out_path}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
