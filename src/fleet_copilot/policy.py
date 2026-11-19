"""Evidence policy for proposed actions.

Run twice: when the proposal is made and again inside the execution transaction, so evidence that went stale
between proposal and approval is caught. Every rule is deterministic code; the LLM has no say in it.

Common principle: the evidence an action cites must (1) exist in this company, (2) belong to the target device,
(3) be fresh, and (4) show the SPECIFIC condition that action depends on. Sustained RAM pressure alone, for
example, never justifies an upgrade order: the catalog must also say the component is upgradeable on that model.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable

from . import catalog, config
from .analytics import Finding, evaluate
from .models import Excluded
from .repository import Repository, Snapshot, parse_ts


@dataclass(frozen=True)
class Decision:
    ok: bool
    reasons: tuple[str, ...] = ()


def _refuse(*reasons: str) -> Decision:
    return Decision(False, tuple(reasons))


# --- shared helpers ------------------------------------------------------------
def _resolve(repo: Repository, device_id: str, evidence: list[str]) -> tuple[list[Snapshot], list[str]]:
    snaps: list[Snapshot] = []
    reasons: list[str] = []
    for sid in evidence:
        snap = repo.get_snapshot(sid)  # company-scoped: a foreign id looks the same as a missing one
        if snap is None:
            reasons.append(f"evidence {sid!r} not found in this company")
        elif snap.device_id != device_id:
            reasons.append(f"evidence {sid!r} belongs to a different device than {device_id!r}")
        else:
            snaps.append(snap)
    return snaps, reasons


def _current_device(repo: Repository, device_id: str | None) -> tuple[Snapshot | None, list[str]]:
    latest = repo.latest_snapshots().get(device_id or "")
    if latest is None:
        return None, [f"device {device_id!r} not found in this company"]
    if repo.is_stale(latest):
        return None, [f"latest telemetry for {device_id!r} is stale ({repo.hours_behind(latest):.0f}h before as-of); "
                      f"cannot show the condition currently holds"]
    return latest, []


def _require_latest(repo: Repository, latest: Snapshot, evidence: list[str]) -> list[str]:
    if latest.snapshot_id not in evidence:
        return [f"evidence is not current: must cite the device's latest snapshot {latest.snapshot_id!r}"]
    if parse_ts(repo.as_of()) - parse_ts(latest.collected_at) > timedelta(days=config.EVIDENCE_MAX_AGE_DAYS):
        return ["evidence older than the maximum evidence age"]
    return []


def _trend_finding(repo: Repository, kind: str, device_id: str, label: str) -> tuple[Finding | None, list[str]]:
    out = evaluate(repo, kind, device_id)
    if isinstance(out, Finding):
        return out, []
    if isinstance(out, Excluded):
        return None, [f"no actionable {label} finding for {device_id!r}: {out.reason}"]
    return None, [f"no {label} finding for {device_id!r}: the telemetry does not show the condition"]


def _trend_evidence(repo: Repository, finding: Finding, snaps: list[Snapshot]) -> list[str]:
    reasons: list[str] = []
    if len(snaps) < config.MIN_TREND_EVIDENCE:
        reasons.append(f"cite at least {config.MIN_TREND_EVIDENCE} supporting snapshots from the trend result's "
                       f"evidence list (got {len(snaps)})")
    allowed = set(finding.evidence_snapshot_ids)
    stray = [s.snapshot_id for s in snaps if s.snapshot_id not in allowed]
    if stray:
        reasons.append(f"evidence does not support the finding (not among its supporting snapshots): {stray}")
    if snaps and parse_ts(repo.as_of()) - max(parse_ts(s.collected_at) for s in snaps) > timedelta(
            days=config.EVIDENCE_MAX_AGE_DAYS):
        reasons.append("newest cited evidence is older than the maximum evidence age")
    return reasons


# --- open_remediation_ticket ----------------------------------------------------
def check_remediation_ticket(repo: Repository, args: dict[str, Any], evidence: list[str]) -> Decision:
    """Supported only when the device's CURRENT (latest, non-stale) snapshot is cited and shows the check failing."""
    device_id, check_id = args.get("device_id"), args.get("check_id")
    if not evidence:
        return _refuse("no evidence cited: a remediation ticket must cite the snapshot showing the failing check")
    latest = repo.latest_snapshots().get(device_id or "")
    if latest is None:
        return _refuse(f"device {device_id!r} not found in this company")
    _, reasons = _resolve(repo, device_id, evidence)
    if reasons:
        return Decision(False, tuple(reasons))
    if latest.snapshot_id not in evidence:
        return _refuse(f"evidence is not current: must cite the device's latest snapshot {latest.snapshot_id!r}")
    if repo.is_stale(latest):
        return _refuse(f"latest telemetry for {device_id!r} is stale ({repo.hours_behind(latest):.0f}h before "
                       f"as-of); cannot show the check is currently failing")
    stale = _require_latest(repo, latest, evidence)
    if stale:
        return Decision(False, tuple(stale))
    row = next((c for c in repo.compliance(latest.snapshot_id) if c["check_id"] == check_id), None)
    if row is None:
        return _refuse(f"check {check_id!r} is not reported in snapshot {latest.snapshot_id!r}")
    if row["status"] != "fail":
        return _refuse(f"check {check_id!r} is {row['status']!r} (not failing) in the latest snapshot "
                       f"{latest.snapshot_id!r}")
    return Decision(True)


# --- create_upgrade_order ---------------------------------------------------------
def check_upgrade_order(repo: Repository, args: dict[str, Any], evidence: list[str]) -> Decision:
    device_id, component, spec = args.get("device_id"), args.get("component"), str(args.get("spec", ""))
    if not evidence:
        return _refuse("no evidence cited: an upgrade order must cite snapshots showing sustained pressure")
    latest, reasons = _current_device(repo, device_id)
    if latest is None:
        return Decision(False, tuple(reasons))
    if component not in ("ram", "storage"):
        return _refuse(f"unsupported component {component!r}; expected 'ram' or 'storage'")

    # 1. the catalog must say this component can be upgraded on this exact model, and to this spec
    entry = catalog.entry(latest.model_identifier)
    if entry is None:
        return _refuse(f"no hardware catalog entry for model {latest.model_identifier!r}: cannot confirm that an "
                       f"upgrade is compatible")
    comp = entry.component(component)
    label = component.upper() if component == "ram" else "storage"
    if not comp.upgradeable:
        return _refuse(f"{label} is not upgradeable on {entry.name} ({latest.model_identifier}); sustained pressure "
                       f"cannot be fixed by an upgrade order - consider flag_device_for_replacement")
    want = catalog.parse_size_gb(spec)
    if want is None or want not in comp.options_gb:
        opts = ", ".join(catalog.fmt_gb(g) for g in comp.options_gb)
        return _refuse(f"spec {spec!r} is not a supported {label} option for {entry.name}; options: {opts}")
    current = round((latest.mem_total_bytes if component == "ram" else latest.disk_size_bytes or 0) / 2**30)
    if want <= current:
        return _refuse(f"spec {spec!r} is not larger than the current {label} ({catalog.fmt_gb(current)})")

    # 2. the telemetry must show sustained pressure on that component, and the cited snapshots must be its evidence
    kind = "memory_pressure" if component == "ram" else "storage"
    finding, why = _trend_finding(repo, kind, device_id, f"{label} pressure")
    if finding is None:
        return Decision(False, tuple(why))
    snaps, bad = _resolve(repo, device_id, evidence)
    reasons = bad or _trend_evidence(repo, finding, snaps)
    return Decision(False, tuple(reasons)) if reasons else Decision(True)


# --- flag_device_for_replacement ----------------------------------------------------
REPLACEMENT_REASONS = ("battery_eol", "ram_capacity_unupgradeable", "storage_capacity_unupgradeable")


def check_replacement(repo: Repository, args: dict[str, Any], evidence: list[str]) -> Decision:
    device_id, code = args.get("device_id"), args.get("reason_code")
    if code not in REPLACEMENT_REASONS:
        return _refuse(f"unsupported reason_code {code!r}; expected one of {', '.join(REPLACEMENT_REASONS)}")
    if not evidence:
        return _refuse("no evidence cited: a replacement flag must cite the snapshots that show the condition")
    latest, reasons = _current_device(repo, device_id)
    if latest is None:
        return Decision(False, tuple(reasons))

    if code == "battery_eol":
        kind, label = "battery_health", "battery end-of-life"
    else:
        component = "ram" if code.startswith("ram") else "storage"
        kind, label = ("memory_pressure" if component == "ram" else "storage"), f"{component} pressure"
        entry = catalog.entry(latest.model_identifier)
        if entry is None:
            return _refuse(f"no hardware catalog entry for model {latest.model_identifier!r}: cannot tell whether "
                           f"{component} is upgradeable")
        if entry.component(component).upgradeable:
            return _refuse(f"{component} IS upgradeable on {entry.name}; replacement is not justified - propose "
                           f"create_upgrade_order instead")
    finding, why = _trend_finding(repo, kind, device_id, label)
    if finding is None:
        return Decision(False, tuple(why))
    snaps, bad = _resolve(repo, device_id, evidence)
    reasons = bad or _trend_evidence(repo, finding, snaps)
    return Decision(False, tuple(reasons)) if reasons else Decision(True)


# --- notify_employee --------------------------------------------------------------
NOTIFY_TOPICS = ("compliance_failure", "low_storage", "battery_service", "memory_pressure")


def check_notify(repo: Repository, args: dict[str, Any], evidence: list[str]) -> Decision:
    employee_id, device_id, topic = args.get("employee_id"), args.get("device_id"), args.get("topic")
    if topic not in NOTIFY_TOPICS:
        return _refuse(f"unsupported topic {topic!r}; expected one of {', '.join(NOTIFY_TOPICS)}")
    if not repo.employee_exists(employee_id or ""):
        return _refuse(f"employee {employee_id!r} not found in this company")
    if not evidence:
        return _refuse("no evidence cited: a notification must cite the latest snapshot showing the condition")
    latest, reasons = _current_device(repo, device_id)
    if latest is None:
        return Decision(False, tuple(reasons))
    if latest.employee_id != employee_id:
        return _refuse(f"device {device_id!r} is not assigned to employee {employee_id!r}")
    _, bad = _resolve(repo, device_id, evidence)
    if bad:
        return Decision(False, tuple(bad))
    cur = _require_latest(repo, latest, evidence)
    if cur:
        return Decision(False, tuple(cur))

    if topic == "compliance_failure":
        row = next((c for c in repo.compliance(latest.snapshot_id) if c["check_id"] == args.get("check_id")), None)
        if row is None or row["status"] != "fail":
            return _refuse(f"check {args.get('check_id')!r} is not failing in the latest snapshot "
                           f"{latest.snapshot_id!r}")
    elif topic == "low_storage":
        if latest.disk_free_pct is None or not latest.disk_free_pct < config.STORAGE_LOW_FREE_PCT:
            return _refuse(f"free disk space is {latest.disk_free_pct}% in the latest snapshot, not below "
                           f"{config.STORAGE_LOW_FREE_PCT:.0f}%")
    elif topic == "battery_service":
        if latest.battery_condition != "Service Recommended":
            return _refuse(f"battery condition is {latest.battery_condition!r} in the latest snapshot, not "
                           f"'Service Recommended'")
    elif topic == "memory_pressure":
        if latest.mem_used_pct is None or not latest.mem_used_pct > config.RAM_PRESSURE_USED_PCT:
            return _refuse(f"memory use is {latest.mem_used_pct}% in the latest snapshot, not above "
                           f"{config.RAM_PRESSURE_USED_PCT:.0f}%")
    return Decision(True)


POLICIES: dict[str, Callable[[Repository, dict[str, Any], list[str]], Decision]] = {
    "open_remediation_ticket": check_remediation_ticket,
    "create_upgrade_order": check_upgrade_order,
    "flag_device_for_replacement": check_replacement,
    "notify_employee": check_notify,
}


def check(repo: Repository, action: str, args: dict[str, Any], evidence: list[str]) -> Decision:
    fn = POLICIES.get(action)
    if fn is None:
        return _refuse(f"unsupported action {action!r}")
    return fn(repo, args, evidence)
