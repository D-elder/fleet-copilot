"""Application-generated text for proposals.

Every free-text field an approver (or a downstream system, or an employee) sees is rendered HERE from facts the
application has verified against the database. The LLM supplies only identifiers and evidence ids, never prose, so
it cannot add unsupported claims (e.g. invented policies, deadlines or causes).
"""
from __future__ import annotations

from typing import Any

from . import catalog, config
from .analytics import Finding, evaluate
from .repository import Repository

CHECK_LABELS = {
    "screen_lock": "screen-lock",
    "os_up_to_date": "operating-system update",
    "disk_encryption": "disk-encryption",
}


def check_label(check_id: str) -> str:
    return CHECK_LABELS.get(check_id, check_id.replace("_", " "))


def ticket_note(device_id: str, check_id: str, collected_at: str) -> str:
    return (f"Review the {check_label(check_id)} configuration on {device_id}. "
            f"The latest telemetry snapshot ({collected_at}) reports a failed {check_id} check.")


def generate(action: str, repo: Repository, args: dict[str, Any], evidence: list[str]) -> tuple[dict[str, Any], str]:
    """Return (args including generated fields, justification). Call only after policy has passed."""
    out = dict(args)
    if action == "open_remediation_ticket":
        latest = repo.latest_snapshots()[args["device_id"]]
        out["note"] = ticket_note(args["device_id"], args["check_id"], latest.collected_at)
        return out, (f"{args['check_id']} is reported as failed in the device's latest snapshot "
                     f"{latest.snapshot_id}.")

    if action == "create_upgrade_order":
        kind = "memory_pressure" if args["component"] == "ram" else "storage"
        f = evaluate(repo, kind, args["device_id"])
        assert isinstance(f, Finding)
        return out, (f"{f.finding} The hardware catalog lists {args['component'].upper()} as upgradeable on this "
                     f"model; ordering {args['spec']}.")

    if action == "flag_device_for_replacement":
        code = args["reason_code"]
        kind = {"battery_eol": "battery_health", "ram_capacity_unupgradeable": "memory_pressure",
                "storage_capacity_unupgradeable": "storage"}[code]
        f = evaluate(repo, kind, args["device_id"])
        assert isinstance(f, Finding)
        tail = {"battery_eol": "",
                "ram_capacity_unupgradeable": " The hardware catalog lists RAM as not upgradeable on this model.",
                "storage_capacity_unupgradeable": " The hardware catalog lists storage as not upgradeable on this "
                                                  "model."}[code]
        out["reason"] = f.finding + tail
        return out, out["reason"]

    if action == "notify_employee":
        s = repo.latest_snapshots()[args["device_id"]]
        topic = args["topic"]
        what = {
            "compliance_failure": f"the {check_label(args.get('check_id', ''))} check ({args.get('check_id')}) is failing",
            "low_storage": f"free disk space is {s.disk_free_pct}%",
            "battery_service": f"the battery condition is '{s.battery_condition}'",
            "memory_pressure": f"memory use is {s.mem_used_pct}%",
        }[topic]
        out["message"] = (f"IT fleet telemetry for your {s.model_name} ({args['device_id']}), collected "
                          f"{s.collected_at}, shows that {what}. Please contact IT support if you need help.")
        return out, f"Latest snapshot {s.snapshot_id} shows that {what}."

    return out, ""
