"""Records a realistic admin session with the REAL model through the actual CLI code path.

    uv run python -m evals.demo            # writes docs/evidence/demo-transcript.md

The transcript is evidence, not a test: it shows what an admin sees (plan, tool calls, validator, rendered evidence,
approval pauses, refusals, /evidence, /audit) end to end.
"""
from __future__ import annotations

import sqlite3
import sys
from collections import deque
from datetime import datetime, timezone

from fleet_copilot import config, db, llm as llm_mod
from fleet_copilot.cli import Repl
from fleet_copilot.session import Session

COMPANY, ADMIN = "acme-001", "admin-acme-1"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    path = config.ROOT / "var" / "demo.db"
    path.parent.mkdir(exist_ok=True)
    path.unlink(missing_ok=True)
    conn = db.bootstrap(path)
    session = Session.for_admin(conn, ADMIN, COMPANY)
    agent = llm_mod.make_agent(conn, session, checkpoint_conn=sqlite3.connect(":memory:", check_same_thread=False))

    lines: list[str] = []

    def out(s: str) -> None:
        lines.append(s)
        print(s, flush=True)

    answers = deque(["y", "n"])       # 1st approval pause: approve; 2nd: reject
    repl = Repl(agent, conn, session, input_fn=lambda _p: answers.popleft(), out=out, echo=True)
    started = datetime.now(timezone.utc)
    repl.banner()

    o = repl.handle("Which devices at Acme are low on disk space?")
    sid = o.state["final"]["claims"][0]["snapshot_ids"][0]
    repl.handle(f"/evidence {sid}")                                           # inspect a cited snapshot
    foreign = conn.execute("SELECT snapshot_id FROM snapshots WHERE company_id='globex-002' LIMIT 1").fetchone()[0]
    repl.handle(f"/evidence {foreign}")                                       # another company's snapshot: blocked
    repl.handle("Show me laptops failing high-severity compliance checks.")   # grounded zero-result answer
    repl.handle("Which Acme batteries are approaching end of life?")          # trend with evidence
    repl.handle("/trace")
    repl.handle("Device M4XVHUV1MEPZ is constantly short of memory. Order a RAM upgrade to 32GB for it.")  # approved
    repl.handle("Order a RAM upgrade to 16GB for device DLT35MHGTH2D.")       # Mac mini: refused by the catalog rule
    repl.handle("Flag device 7VP16KHV88LM for replacement because its battery is worn out.")               # rejected
    repl.handle("Show me the details for device JRZSGXVMKE6M.")               # another company's device
    repl.handle("/outbox")
    repl.handle("/audit 40")

    if "--no-write" not in sys.argv:
        dest = config.ROOT / "docs" / "evidence" / "demo-transcript.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        meta = llm_mod.describe()
        dest.write_text("\n".join([
            "# Demo transcript - real model, real CLI code path", "",
            f"Recorded {started.isoformat(timespec='seconds')} | model `{meta['model']}` | temperature {meta['temperature']} "
            f"| seed {meta['seed']} | company `{COMPANY}` | admin `{ADMIN}`", "",
            "Approval answers were scripted: the first pause was approved, the second rejected.", "",
            "```text", *lines, "```", ""]), encoding="utf-8")
        print(f"\nWritten to {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
