"""Claim validation and deterministic rendering.

The LLM proposes an Answer {summary, claims}. This module checks every claim against the ToolResults produced in
the same turn, then renders devices, counts, citations and the 'what was checked' block FROM THE TOOL DATA, so the
model's prose can never change what the admin sees as evidence.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .models import Answer, ToolResult


@dataclass
class Report:
    ok: bool
    errors: list[str] = field(default_factory=list)


def validate_answer(answer: Answer, results: dict[str, ToolResult], company_id: str,
                    company_device_ids: list[str]) -> Report:
    errors: list[str] = []
    claimed_devices: set[str] = set()

    for i, c in enumerate(answer.claims):
        tag = f"claim[{i}] ({c.result_id})"
        r = results.get(c.result_id)
        if r is None:
            errors.append(f"{tag}: result_id was not returned by any tool call in this turn")
            continue
        if r.company_id != company_id:
            errors.append(f"{tag}: result belongs to a different company")
            continue
        claimed_devices.update(c.device_ids)

        if len(set(c.device_ids)) != len(c.device_ids):
            errors.append(f"{tag}: device_ids contains duplicates")
        if c.count != len(set(c.device_ids)):
            errors.append(f"{tag}: count={c.count} but {len(set(c.device_ids))} device_ids listed")

        have, want = set(c.device_ids), set(r.device_ids)
        if c.subset:
            extra = sorted(have - want)
            if extra:
                errors.append(f"{tag}: devices not in the result: {extra}")
        else:
            if c.count != r.count:
                errors.append(f"{tag}: count={c.count} but the tool result has count={r.count}")
            if have != want:
                errors.append(f"{tag}: device_ids differ from the result; missing={sorted(want - have)}, "
                              f"extra={sorted(have - want)}")

        by_snapshot = {row.snapshot_id: row.device_id for row in r.rows}
        for sid in c.snapshot_ids:
            if sid not in by_snapshot:
                errors.append(f"{tag}: snapshot_id {sid!r} is not in the result")
            elif by_snapshot[sid] not in have:
                errors.append(f"{tag}: snapshot_id {sid!r} is for a device the claim does not list")
        cited_devices = {by_snapshot[s] for s in c.snapshot_ids if s in by_snapshot}
        uncited = sorted(have - cited_devices)
        if uncited:
            errors.append(f"{tag}: no snapshot_id cited for devices {uncited}")

    for dev in company_device_ids:
        if dev in answer.summary and dev not in claimed_devices:
            errors.append(f"summary mentions device {dev!r} that is not in any claim")

    return Report(ok=not errors, errors=errors)


# --- rendering (plain ASCII so it survives any Windows console code page) -------
def _fmt_values(v: dict[str, Any]) -> str:
    parts = [str(v.get("model", "?")), str(v.get("os", "?"))]
    if v.get("disk_free_pct") is not None:
        parts.append(f"disk free {v['disk_free_pct']}%")
    if v.get("mem_used_pct") is not None:
        parts.append(f"mem used {v['mem_used_pct']}%")
    fails = v.get("failing_checks") or []
    parts.append("failing: " + (", ".join(f"{f['check_id']}({f['severity']})" for f in fails) or "none"))
    return " | ".join(parts)


def render_result(r: ToolResult, only_devices: set[str] | None = None) -> str:
    rows = [row for row in r.rows if only_devices is None or row.device_id in only_devices]
    lines = [
        f"Evidence {r.result_id} ({r.tool})",
        f"  company: {r.company_id} | as of: {r.as_of} | window: {r.window.start} .. {r.window.end}",
        f"  filters: {json.dumps(r.filters, sort_keys=True) if r.filters else '(none)'}",
        f"  matched {len(rows)} of {len(r.population_checked)} devices checked",
    ]
    for row in rows:
        lines.append(f"    - {row.device_id}  [{row.snapshot_id}]  {_fmt_values(row.values)}")
    if r.excluded:
        lines.append(f"  excluded ({len(r.excluded)}):")
        lines += [f"    - {e.device_id}: {e.reason}" for e in r.excluded]
    lines += [f"  note: {n}" for n in r.notes]
    return "\n".join(lines)


def render_answer(answer: Answer, results: dict[str, ToolResult]) -> str:
    blocks = [answer.summary.strip()]
    seen: set[str] = set()
    for c in answer.claims:
        r = results[c.result_id]
        only = set(c.device_ids) if c.subset else None
        key = f"{c.result_id}|{sorted(only) if only else ''}"
        if key not in seen:
            seen.add(key)
            blocks.append(render_result(r, only))
    return "\n\n".join(blocks)


def render_fallback(results: dict[str, ToolResult], errors: list[str]) -> str:
    head = ("WARNING: the model's answer could not be verified against the tool results, so it is withheld.\n"
            "Validation errors:\n" + "\n".join(f"  - {e}" for e in errors))
    return "\n\n".join([head] + [render_result(r) for r in results.values()])
