"""Evidence policy for proposed actions.

Run twice: when the proposal is made and again inside the execution transaction, so evidence that went stale
between proposal and approval is caught. Every rule is deterministic code; the LLM has no say in it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable

from . import config
from .repository import Repository, parse_ts


@dataclass(frozen=True)
class Decision:
    ok: bool
    reasons: tuple[str, ...] = ()


def _refuse(*reasons: str) -> Decision:
    return Decision(False, tuple(reasons))


def check_remediation_ticket(repo: Repository, args: dict[str, Any], evidence: list[str]) -> Decision:
    """Supported only when the device's CURRENT (latest, non-stale) snapshot is cited and shows the check failing."""
    device_id, check_id = args.get("device_id"), args.get("check_id")
    if not evidence:
        return _refuse("no evidence cited: a remediation ticket must cite the snapshot showing the failing check")

    latest = repo.latest_snapshots().get(device_id)
    if latest is None:
        return _refuse(f"device {device_id!r} not found in this company")

    reasons: list[str] = []
    for sid in evidence:
        snap = repo.get_snapshot(sid)  # company-scoped: a foreign id looks the same as a missing one
        if snap is None:
            reasons.append(f"evidence {sid!r} not found in this company")
        elif snap.device_id != device_id:
            reasons.append(f"evidence {sid!r} belongs to a different device than {device_id!r}")
    if reasons:
        return Decision(False, tuple(reasons))

    if latest.snapshot_id not in evidence:
        return _refuse(f"evidence is not current: must cite the device's latest snapshot {latest.snapshot_id!r}")

    if repo.is_stale(latest):
        return _refuse(f"latest telemetry for {device_id!r} is stale ({repo.hours_behind(latest):.0f}h before "
                       f"as-of); cannot show the check is currently failing")
    if parse_ts(repo.as_of()) - parse_ts(latest.collected_at) > timedelta(days=config.EVIDENCE_MAX_AGE_DAYS):
        return _refuse("evidence older than the maximum evidence age")

    row = next((c for c in repo.compliance(latest.snapshot_id) if c["check_id"] == check_id), None)
    if row is None:
        return _refuse(f"check {check_id!r} is not reported in snapshot {latest.snapshot_id!r}")
    if row["status"] != "fail":
        return _refuse(f"check {check_id!r} is {row['status']!r} (not failing) in the latest snapshot "
                       f"{latest.snapshot_id!r}")
    return Decision(True)


POLICIES: dict[str, Callable[[Repository, dict[str, Any], list[str]], Decision]] = {
    "open_remediation_ticket": check_remediation_ticket,
}


def check(repo: Repository, action: str, args: dict[str, Any], evidence: list[str]) -> Decision:
    fn = POLICIES.get(action)
    if fn is None:
        return _refuse(f"unsupported action {action!r}")
    return fn(repo, args, evidence)
