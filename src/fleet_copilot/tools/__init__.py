"""Tool layer. Tools are bound to a Session: none of the argument schemas has a company_id, and all use
``extra='forbid'`` so the model cannot smuggle one in. Action tools accept identifiers and evidence ids ONLY; the
application generates every piece of prose (see notes.py). There is deliberately NO execute tool."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, ValidationError

from .. import actions, analytics, audit
from ..models import (Answer, DeviceFilters, PlanArgs, ProposalResult, RevisePlanArgs, ToolResult, TrendKind)
from ..repository import Repository
from ..session import Session


class EmptyArgs(BaseModel):
    model_config = {"extra": "forbid"}


class GetDeviceArgs(BaseModel):
    model_config = {"extra": "forbid"}
    device_id: str


class AnalyzeTrendsArgs(BaseModel):
    model_config = {"extra": "forbid"}
    kind: TrendKind
    device_id: str | None = Field(default=None, description="omit to analyse every device in the company")


EVIDENCE_DESC = "snapshot_ids copied from tool results"


class TicketArgs(BaseModel):
    model_config = {"extra": "forbid"}
    device_id: str
    check_id: str = Field(description="compliance check that is failing now, e.g. screen_lock")
    evidence: list[str] = Field(default_factory=list,
                                description="the device's LATEST snapshot_id from a query_devices/get_device result")


class UpgradeArgs(BaseModel):
    model_config = {"extra": "forbid"}
    device_id: str
    component: Literal["ram", "storage"]
    spec: str = Field(description="target size, e.g. '32GB' or '2TB'")
    evidence: list[str] = Field(default_factory=list,
                                description="at least 3 snapshot_ids from the evidence list of the matching "
                                            "analyze_trends row (memory_pressure for ram, storage for storage)")


class ReplaceArgs(BaseModel):
    model_config = {"extra": "forbid"}
    device_id: str
    reason_code: Literal["battery_eol", "ram_capacity_unupgradeable", "storage_capacity_unupgradeable"]
    evidence: list[str] = Field(default_factory=list,
                                description="at least 3 snapshot_ids from the evidence list of the matching "
                                            "analyze_trends row (battery_health / memory_pressure / storage)")


class NotifyArgs(BaseModel):
    model_config = {"extra": "forbid"}
    employee_id: str
    device_id: str = Field(description="the employee's device the message is about")
    topic: Literal["compliance_failure", "low_storage", "battery_service", "memory_pressure"]
    check_id: str | None = Field(default=None, description="required when topic is compliance_failure")
    evidence: list[str] = Field(default_factory=list, description="the device's LATEST snapshot_id")


ACTION_MODELS: dict[str, type[BaseModel]] = {
    "open_remediation_ticket": TicketArgs,
    "create_upgrade_order": UpgradeArgs,
    "flag_device_for_replacement": ReplaceArgs,
    "notify_employee": NotifyArgs,
}
ACTION_TOOLS = tuple(ACTION_MODELS)

NO_PROSE = (" Supply identifiers and evidence ids only: the application generates all text (notes, reasons, messages) "
            "from verified telemetry.")

DESCRIPTIONS = {
    "query_devices": ("Query the CURRENT state (each device's latest snapshot) of this company's fleet. Filters are "
                      "ANDed. Returns result_id, matched rows with snapshot_ids, the devices checked, and devices "
                      "excluded with reasons. failing_check + check_severity are matched on the same compliance "
                      "check. os_version_lt requires os_family."),
    "fleet_overview": "Counts of this company's devices by model, OS, form factor and failing check (current state).",
    "get_device": "Details and 30-day history summary for ONE device of this company.",
    "analyze_trends": ("Detect trends over each device's 30-day history. kind: battery_health (end-of-life risk), "
                       "memory_pressure (sustained RAM use), storage (consistently low free space), "
                       "compliance_drift (checks that were passing and now fail). Each row has the finding, "
                       "its measurements and explanation, and evidence_snapshot_ids you can cite in claims or "
                       "actions. Devices that are only intermittently affected are listed under 'excluded'."),
    "open_remediation_ticket": ("PROPOSE a remediation ticket for a compliance check that is failing NOW. Nothing is "
                                "created until a human approves." + NO_PROSE),
    "create_upgrade_order": ("PROPOSE a RAM or storage upgrade for a device with sustained pressure on that "
                             "component. Refused if the hardware catalog says the component cannot be upgraded on "
                             "that model, or the spec is not a catalog option. Nothing is ordered until a human "
                             "approves." + NO_PROSE),
    "flag_device_for_replacement": ("PROPOSE flagging a device for replacement: battery end-of-life, or sustained "
                                    "RAM/storage pressure on a model whose component cannot be upgraded. Nothing "
                                    "happens until a human approves." + NO_PROSE),
    "notify_employee": ("PROPOSE a notification to the employee who is assigned the device. The condition must hold "
                        "in the device's latest snapshot. Nothing is sent until a human approves." + NO_PROSE),
    "revise_plan": ("Replace the evidence_needed list of your plan (allowed once) when an item turns out to be "
                    "unnecessary or unobtainable. Give the reason."),
    "final_answer": ("Finish the turn. Provide a short summary and one claim per tool result you rely on, listing the "
                     "exact device_ids, count and snapshot_ids from that result (for trends, snapshot_ids may come "
                     "from the row's evidence_snapshot_ids). For a question with no matches, give a claim with "
                     "empty device_ids, count 0 and the result_id that showed zero."),
    "submit_plan": "Record your plan before using other tools.",
}


def _noop(**_: Any) -> None:  # schemas only; dispatch happens in ToolBox.dispatch
    return None


def _spec(name: str, schema: type[BaseModel]) -> StructuredTool:
    return StructuredTool.from_function(_noop, name=name, description=DESCRIPTIONS[name], args_schema=schema)


def plan_spec() -> StructuredTool:
    return _spec("submit_plan", PlanArgs)


@dataclass
class Dispatch:
    content: str
    result: ToolResult | None = None
    proposal: ProposalResult | None = None
    final: Answer | None = None
    revision: RevisePlanArgs | None = None
    trace: dict[str, Any] | None = None


_DEVICE_ID = re.compile(r"\b(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{12}\b")
_EMPLOYEE_ID = re.compile(r"\bemp-[a-z0-9]+-\d+\b")


class ToolBox:
    def __init__(self, conn: sqlite3.Connection, session: Session):
        self.conn, self.session = conn, session
        self.repo = Repository(conn, session)
        self.named_devices: set[str] = set()
        self.named_employees: set[str] = set()

    def set_request(self, question: str) -> None:
        """Remember which devices/employees the admin NAMED in this question (target guard, see _target_mismatch)."""
        self.named_devices = set(_DEVICE_ID.findall(question))
        self.named_employees = set(_EMPLOYEE_ID.findall(question))

    def _target_mismatch(self, args: BaseModel) -> str | None:
        """If the admin named targets, an action may only target those. The model may not substitute its own: a
        request that conflicts with the data (e.g. the named employee does not own the device) must be reported
        back to the admin, not silently redirected."""
        device, employee = getattr(args, "device_id", None), getattr(args, "employee_id", None)
        if self.named_devices and device and device not in self.named_devices:
            return (f"target mismatch: the admin named device(s) {', '.join(sorted(self.named_devices))} but this "
                    f"proposal targets {device!r}. Do not substitute a different target: report the conflict to the "
                    f"admin and let them decide")
        if self.named_employees and employee and employee not in self.named_employees:
            return (f"target mismatch: the admin named employee(s) {', '.join(sorted(self.named_employees))} but this "
                    f"proposal targets {employee!r}. Do not substitute a different target: report the conflict to the "
                    f"admin and let them decide")
        return None

    def specs(self) -> list[StructuredTool]:
        return [_spec("query_devices", DeviceFilters), _spec("fleet_overview", EmptyArgs),
                _spec("get_device", GetDeviceArgs), _spec("analyze_trends", AnalyzeTrendsArgs),
                *[_spec(n, m) for n, m in ACTION_MODELS.items()],
                _spec("revise_plan", RevisePlanArgs), _spec("final_answer", Answer)]

    def dispatch(self, name: str, args: dict[str, Any]) -> Dispatch:
        try:
            if name == "query_devices":
                return self._result(name, args, self.repo.query_devices(DeviceFilters(**args)))
            if name == "fleet_overview":
                EmptyArgs(**args)
                return self._result(name, args, self.repo.fleet_overview())
            if name == "get_device":
                return self._result(name, args, self.repo.get_device(GetDeviceArgs(**args).device_id))
            if name == "analyze_trends":
                a = AnalyzeTrendsArgs(**args)
                return self._result(name, args, analytics.analyze_trends(self.repo, a.kind, a.device_id))
            if name in ACTION_MODELS:
                a = ACTION_MODELS[name](**args)
                payload = a.model_dump(exclude={"evidence"}, exclude_none=True)
                mismatch = self._target_mismatch(a)
                if mismatch:
                    audit.record(self.conn, company_id=self.session.company_id, actor=self.session.admin_id,
                                 event="proposal_refused",
                                 details={"action": name, "args": payload, "evidence": list(a.evidence),
                                          "reasons": [mismatch], "guard": "target_mismatch"})
                    prop = ProposalResult(status="refused", reasons=[mismatch])
                    return Dispatch(content=prop.model_dump_json(), proposal=prop,
                                    trace={"tool": name, "args": args, "proposal_id": None, "status": "refused",
                                           "reasons": [mismatch]})
                prop = actions.propose(self.conn, self.session, name, payload, list(a.evidence))
                return Dispatch(content=prop.model_dump_json(), proposal=prop,
                                trace={"tool": name, "args": args, "proposal_id": prop.proposal_id,
                                       "status": prop.status, "reasons": prop.reasons})
            if name == "revise_plan":
                return Dispatch(content="plan revised", revision=RevisePlanArgs(**args),
                                trace={"tool": name, "args": args})
            if name == "final_answer":
                return Dispatch(content="received", final=Answer(**args), trace={"tool": name})
            return Dispatch(content=f"ERROR: unknown tool {name!r}", trace={"tool": name, "error": "unknown tool"})
        except ValidationError as e:
            msg = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
            if any(x["type"] == "extra_forbidden" for x in e.errors()):
                msg += ("  (unknown arguments are not accepted: tools take only the documented fields; the "
                        "application generates all free text and fixes the company itself)")
            return Dispatch(content=f"ERROR: invalid arguments for {name}: {msg}",
                            trace={"tool": name, "args": args, "error": msg})
        except ValueError as e:  # e.g. unparseable version, os_version_lt without os_family
            return Dispatch(content=f"ERROR: {e}", trace={"tool": name, "args": args, "error": str(e)})

    @staticmethod
    def _result(name: str, args: dict[str, Any], result: ToolResult) -> Dispatch:
        return Dispatch(content=result.model_dump_json(), result=result,
                        trace={"tool": name, "args": args, "result_id": result.result_id, "count": result.count})
