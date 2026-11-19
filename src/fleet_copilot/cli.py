"""CLI for IT admins: ask questions, inspect evidence, approve or reject proposals.

The Repl class takes injectable input/output functions so the exact same code path is driven by tests and by the
end-to-end evidence script.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Any, Callable

from . import actions, audit, config, db
from .graph import FleetAgent, TurnOutcome
from .repository import Repository
from .session import AccessDenied, Session

HELP = """Commands:
  <question>            ask the copilot (answers are validated against tool results)
  /evidence <snap_id>   show the raw telemetry snapshot behind a citation
  /proposals            list this company's proposals and their status
  /approve <proposal>   approve (and execute) a pending proposal; safe to repeat
  /reject <proposal>    reject a pending proposal
  /outbox               show executed actions (simulated outbox)
  /audit [n]            show the last n audit records (default 15)
  /quit
"""


class Repl:
    def __init__(self, agent: FleetAgent, conn: sqlite3.Connection, session: Session,
                 input_fn: Callable[[str], str] = input, out: Callable[[str], None] = print, echo: bool = False):
        """echo=True prints the typed input too (for scripted runs, where a terminal would have shown it)."""
        self.agent, self.conn, self.session = agent, conn, session
        self.input_fn, self.out, self.echo = input_fn, out, echo
        self.repo = Repository(conn, session)

    # -- main loop -------------------------------------------------------------
    def banner(self) -> None:
        self.out(f"Fleet Copilot | company={self.session.company_id} admin={self.session.admin_id} "
                 f"role={self.session.role} | data as of {self.repo.as_of()}\nType /help for commands.")

    def run(self) -> None:
        self.banner()
        while True:
            try:
                line = self.input_fn("fleet> ").strip()
            except (EOFError, StopIteration):
                break
            if not line:
                continue
            if line in ("/quit", "/exit"):
                break
            self.handle(line)

    def handle(self, line: str) -> TurnOutcome | None:
        """Run one line of input. Returns the final TurnOutcome for a question, None for a /command."""
        if self.echo:
            self.out(f"fleet> {line}")
        if line.startswith("/"):
            cmd, _, arg = line.partition(" ")
            fn = {"/help": lambda a: self.out(HELP), "/evidence": self.evidence, "/proposals": self.proposals,
                  "/approve": lambda a: self.decide(a, "approve"), "/reject": lambda a: self.decide(a, "reject"),
                  "/outbox": self.outbox, "/audit": self.audit}.get(cmd)
            if fn is None:
                self.out(f"Unknown command {cmd}. Try /help.")
            else:
                fn(arg.strip())
            return None
        return self.ask(line)

    # -- questions + approval ----------------------------------------------------
    def ask(self, question: str) -> TurnOutcome:
        outcome = self.agent.ask(question)
        self._show_trace(outcome)
        self.out("\n" + outcome.rendered)
        while outcome.pending:
            p = outcome.pending[0]
            self.out("\n--- APPROVAL REQUIRED " + "-" * 40)
            self.out(f"proposal : {p['proposal_id']}  ({p['action']})")
            self.out(f"args     : {json.dumps(p['args'], sort_keys=True)}")
            self.out(f"evidence : {', '.join(p['evidence'])}")
            self.out(f"why      : {p['justification'] or '(none given)'}")
            self.out(f"hash     : {p['payload_hash'][:16]}...  (your decision is bound to this exact payload)")
            answer = self.input_fn(f"Approve {p['proposal_id']}? [y/N] ").strip().lower()
            if self.echo:
                self.out(f"Approve {p['proposal_id']}? [y/N] {answer}")
            decision = "approve" if answer in ("y", "yes") else "reject"
            outcome = self.agent.resume(outcome.thread_id, decision, p["payload_hash"])
            self._show_decision(p["proposal_id"])
        return outcome

    def _show_trace(self, outcome: TurnOutcome) -> None:
        self.out("[trace]")
        for t in outcome.trace:
            if t.get("tool") == "query_devices":
                self.out(f"  tool query_devices {json.dumps(t['args'], sort_keys=True)} -> {t.get('result_id')} "
                         f"(count={t.get('count')})")
            elif t.get("tool") == "open_remediation_ticket":
                self.out(f"  tool open_remediation_ticket {t.get('args', {}).get('device_id')}/"
                         f"{t.get('args', {}).get('check_id')} -> {t.get('proposal_id')} status={t.get('status')}")
            elif t.get("node") == "validate":
                self.out("  validator: " + ("claims verified against tool results" if t["ok"]
                                            else f"REJECTED ({len(t['errors'])} issue(s)); retry={t['retry']}"))

    def _show_decision(self, proposal_id: str) -> None:
        p = actions.get_proposal(self.conn, self.session.company_id, proposal_id)
        ob = self.conn.execute("SELECT outbox_id FROM outbox WHERE proposal_id=?", (proposal_id,)).fetchone()
        extra = f" (outbox #{ob['outbox_id']})" if ob else ""
        self.out(f"Result: proposal {proposal_id} is now '{p['status'] if p else '?'}'{extra}")

    # -- commands ----------------------------------------------------------------
    def evidence(self, snapshot_id: str) -> None:
        raw = self.repo.get_raw_snapshot(snapshot_id)
        if raw is None:
            audit.record(self.conn, company_id=self.session.company_id, actor=self.session.admin_id,
                         event="evidence_not_found", details={"snapshot_id": snapshot_id})
            self.out(f"No snapshot {snapshot_id!r} in company {self.session.company_id}.")
            return
        self.out(json.dumps(json.loads(raw), indent=2))

    def proposals(self, _: str = "") -> None:
        rows = actions.list_proposals(self.conn, self.session.company_id)
        if not rows:
            self.out("No proposals.")
        for p in rows:
            self.out(f"{p['proposal_id']}  {p['status']:9}  {p['action']}  {json.dumps(p['args'], sort_keys=True)}  "
                     f"hash={p['payload_hash'][:16]}...")

    def decide(self, proposal_id: str, decision: str) -> None:
        p = actions.get_proposal(self.conn, self.session.company_id, proposal_id)
        if p is None:
            self.out(f"No proposal {proposal_id!r} in company {self.session.company_id}.")
            audit.record(self.conn, company_id=self.session.company_id, actor=self.session.admin_id,
                         event="decision_denied", details={"proposal_id": proposal_id, "reason": "proposal not found"})
            return
        # The hash comes from the proposal the human listed/reviewed (see /proposals).
        d = actions.decide(self.conn, self.session, proposal_id, p["payload_hash"], decision)
        if not d.ok:
            self.out("Denied: " + "; ".join(d.reasons))
            return
        if decision == "approve":
            e = actions.execute(self.conn, self.session, proposal_id)
            tag = " (replay: already executed, nothing new written)" if e.replayed else ""
            self.out(("Executed" if e.ok else "Not executed: " + "; ".join(e.reasons)) + tag
                     + (f" outbox #{e.outbox_id}" if e.outbox_id else ""))
        else:
            self.out(f"Proposal {proposal_id} is {d.status}" + (" (replay)" if d.replayed else ""))

    def outbox(self, _: str = "") -> None:
        rows = actions.outbox_rows(self.conn, self.session.company_id)
        self.out(f"{len(rows)} outbox entr{'y' if len(rows) == 1 else 'ies'}")
        for r in rows:
            self.out(f"  #{r['outbox_id']} {r['proposal_id']} {r['action']} {r['payload_json']}")

    def audit(self, arg: str = "") -> None:
        n = int(arg) if arg.isdigit() else 15
        rows = audit.events(self.conn, company_id=self.session.company_id)[-n:]
        for r in rows:
            self.out(f"  {r['id']:>3} {r['ts']} {r['actor']} {r['event']} {r['details_json']}")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
    ap = argparse.ArgumentParser(prog="fleet-copilot")
    ap.add_argument("--company", required=True, help="company id, e.g. acme-001")
    ap.add_argument("--admin", required=True, help="admin id, e.g. admin-acme-1")
    ap.add_argument("--db", default=str(config.DB_PATH))
    args = ap.parse_args(argv)

    conn = db.bootstrap(args.db)
    try:
        session = Session.for_admin(conn, args.admin, args.company)
    except AccessDenied as e:
        print(f"Access denied: {e}", file=sys.stderr)
        return 2
    from .llm import make_llm_invoke
    from .tools import ToolBox

    llm = make_llm_invoke(ToolBox(conn, session).specs())
    cp = sqlite3.connect(str(config.ROOT / "var" / "checkpoints.db"), check_same_thread=False)
    Repl(FleetAgent(conn, session, llm, checkpoint_conn=cp), conn, session).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
