"""Tool layer. Tools are bound to a Session: none of the argument schemas has a company_id, and all use
``extra='forbid'`` so the model cannot smuggle one in. There is deliberately NO execute tool."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, ValidationError

from .. import actions
from ..models import Answer, DeviceFilters, ProposalResult, ToolResult
from ..repository import Repository
from ..session import Session


class OpenRemediationTicketArgs(BaseModel):
    model_config = {"extra": "forbid"}

    device_id: str
    check_id: str = Field(description="compliance check that is failing, e.g. screen_lock")
    note: str = Field(min_length=1, description="what the technician should do")
    evidence: list[str] = Field(default_factory=list,
                                description="snapshot_ids from tool results that show the check failing NOW")
    justification: str = Field(default="", description="one sentence: why this ticket is warranted")


QUERY_DESC = ("Query the CURRENT state (each device's latest snapshot) of this company's fleet. Filters are ANDed. "
              "Returns result_id, matched rows with snapshot_ids, devices checked, and devices excluded with reasons. "
              "failing_check + check_severity are matched on the same compliance check. os_version_lt requires "
              "os_family.")
TICKET_DESC = ("PROPOSE a remediation ticket for a failing compliance check. This does NOT create the ticket: it is "
               "recorded as pending and a human must approve it. Cite the snapshot_id(s) from a query_devices result "
               "that show the check failing now. Refused if the evidence does not support it.")
FINAL_DESC = ("Finish the turn. Provide a short summary and one claim per tool result you rely on, listing the exact "
              "device_ids, count and snapshot_ids from that result. For a question with no matches, give a claim with "
              "empty device_ids, count 0 and the result_id that showed zero.")


def _noop(**_: Any) -> None:  # schemas only; dispatch happens in ToolBox.dispatch
    return None


@dataclass
class Dispatch:
    content: str
    result: ToolResult | None = None
    proposal: ProposalResult | None = None
    final: Answer | None = None
    trace: dict[str, Any] | None = None


class ToolBox:
    def __init__(self, conn: sqlite3.Connection, session: Session):
        self.conn, self.session = conn, session
        self.repo = Repository(conn, session)

    def specs(self) -> list[StructuredTool]:
        mk = StructuredTool.from_function
        return [
            mk(_noop, name="query_devices", description=QUERY_DESC, args_schema=DeviceFilters),
            mk(_noop, name="open_remediation_ticket", description=TICKET_DESC, args_schema=OpenRemediationTicketArgs),
            mk(_noop, name="final_answer", description=FINAL_DESC, args_schema=Answer),
        ]

    def dispatch(self, name: str, args: dict[str, Any]) -> Dispatch:
        try:
            if name == "query_devices":
                result = self.repo.query_devices(DeviceFilters(**args))
                return Dispatch(content=result.model_dump_json(), result=result,
                                trace={"tool": name, "args": args, "result_id": result.result_id,
                                       "count": result.count})
            if name == "open_remediation_ticket":
                a = OpenRemediationTicketArgs(**args)
                prop = actions.propose(
                    self.conn, self.session, "open_remediation_ticket",
                    {"device_id": a.device_id, "check_id": a.check_id, "note": a.note}, a.evidence, a.justification)
                return Dispatch(content=prop.model_dump_json(), proposal=prop,
                                trace={"tool": name, "args": args, "proposal_id": prop.proposal_id,
                                       "status": prop.status, "reasons": prop.reasons})
            if name == "final_answer":
                ans = Answer(**args)
                return Dispatch(content="received", final=ans, trace={"tool": name})
            return Dispatch(content=f"ERROR: unknown tool {name!r}", trace={"tool": name, "error": "unknown tool"})
        except ValidationError as e:
            msg = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
            return Dispatch(content=f"ERROR: invalid arguments for {name}: {msg}",
                            trace={"tool": name, "args": args, "error": msg})
        except ValueError as e:  # e.g. unparseable version, os_version_lt without os_family
            return Dispatch(content=f"ERROR: {e}", trace={"tool": name, "args": args, "error": str(e)})
