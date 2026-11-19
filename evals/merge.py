"""Combine several eval invocations into one report.

    uv run python -m evals.merge evals/reports/live-A.json evals/reports/live-B.json ... [--publish]

Why: a real-model run can be interrupted (provider credit limits, rate limits) or a test expectation can be fixed and
only the affected cases rerun. For each case the LAST run that completed without an infrastructure error wins; a case
that never completed stays an infrastructure error and is reported as unmeasured, never as passed.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from fleet_copilot import config

from . import run as eval_run


def merge(paths: list[Path]) -> dict:
    reps = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
    latest: dict[str, dict] = {}
    for rep in reps:
        for r in rep["runs"]:
            prev = latest.get(r["id"])
            if prev is None or r["error"] is None or prev["error"] is not None:
                latest[r["id"]] = r
    order = [c["id"] for c in eval_run.load_cases()]
    runs = [latest[i] for i in order if i in latest]
    meta = dict(reps[-1]["meta"])
    meta.update({"date": f"{reps[0]['meta']['date']} .. {reps[-1]['meta']['date']}", "repeats": 1, "cases": len(runs),
                 "merged_from": [p.name for p in paths]})
    return eval_run.build_report(runs, meta)


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    ap = argparse.ArgumentParser(prog="python -m evals.merge")
    ap.add_argument("reports", nargs="+", type=Path, help="report .json files, oldest first")
    ap.add_argument("--publish", action="store_true", help="copy to docs/evidence/eval-live-report.*")
    a = ap.parse_args(argv)
    rep = merge(a.reports)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    j, m = eval_run.REPORT_DIR / f"live-merged-{stamp}.json", eval_run.REPORT_DIR / f"live-merged-{stamp}.md"
    j.write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    m.write_text(eval_run.render_md(rep), encoding="utf-8")
    if a.publish:
        out = config.ROOT / "docs" / "evidence"
        out.mkdir(parents=True, exist_ok=True)
        shutil.copy(m, out / "eval-live-report.md")
        shutil.copy(j, out / "eval-live-report.json")
    s = rep["summary"]
    print(f"{s['passed']}/{s['runs']} cases passed ({s['pass_rate_pct']}%), unmeasured (infra errors): {s['infra_errors']}")
    for c, v in rep["by_category"].items():
        print(f"  {c:18} {v['passed']}/{v['runs']}")
    print(f"Report: {m}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
