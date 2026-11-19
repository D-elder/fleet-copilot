"""A scripted stand-in for the LLM so the whole graph (including interrupt/resume) runs offline and deterministically.

The same ScriptedLLM serves the plan call and the agent calls, so a script always starts with a plan step.
"""
from __future__ import annotations

import itertools
import json
from typing import Any, Callable, Sequence

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

_ids = itertools.count(1)


def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "args": args, "id": f"call_{next(_ids)}", "type": "tool_call"}


def ai(*calls: dict[str, Any]) -> AIMessage:
    return AIMessage(content="", tool_calls=list(calls))


def plan(intent: str = "answer the question", needs: list[dict[str, Any]] | None = None, proposes: bool = False,
         steps: list[dict[str, str]] | None = None) -> AIMessage:
    return ai(call("submit_plan", {"intent": intent, "steps": steps or [], "evidence_needed": needs or [],
                                   "proposes_action": proposes}))


def need(kind: str, device_id: str | None = None) -> dict[str, Any]:
    return {"kind": kind, **({"device_id": device_id} if device_id else {})}


def last_result(messages: Sequence[BaseMessage], tool: str | None = "query_devices") -> dict[str, Any]:
    for m in reversed(messages):
        if isinstance(m, ToolMessage) and (tool is None or m.name == tool) and not m.content.startswith(
                ("ERROR", "REJECTED", "Plan coverage", "accepted", "plan ")) and m.name not in (
                    "final_answer", "revise_plan") and m.content.startswith("{") and '"result_id"' in m.content:
            return json.loads(m.content)
    raise AssertionError(f"no {tool} result in the conversation yet")


def claim_for(result: dict[str, Any], **over: Any) -> dict[str, Any]:
    c = {"result_id": result["result_id"], "device_ids": [r["device_id"] for r in result["rows"]],
         "count": result["count"], "snapshot_ids": [r["snapshot_id"] for r in result["rows"]], "subset": False}
    c.update(over)
    return c


def final_from_last_result(summary: str, tool: str | None = "query_devices",
                           **claim_overrides: Any) -> Callable[[Sequence[BaseMessage]], AIMessage]:
    """A well-behaved model: claims exactly what the last result says (optionally corrupted)."""
    def step(messages: Sequence[BaseMessage]) -> AIMessage:
        r = last_result(messages, tool)
        return ai(call("final_answer", {"summary": summary, "claims": [claim_for(r, **claim_overrides)]}))
    return step


def ticket_for(device_id: str, check_id: str) -> Callable[[Sequence[BaseMessage]], AIMessage]:
    def step(messages: Sequence[BaseMessage]) -> AIMessage:
        r = last_result(messages)
        row = next(x for x in r["rows"] if x["device_id"] == device_id)
        return ai(call("open_remediation_ticket", {"device_id": device_id, "check_id": check_id,
                                                   "evidence": [row["snapshot_id"]]}))
    return step


class ScriptedLLM:
    def __init__(self, steps: Sequence[Any]):
        self.steps = list(steps)
        self.seen: list[list[BaseMessage]] = []

    def __call__(self, messages: Sequence[BaseMessage]) -> AIMessage:
        self.seen.append(list(messages))
        if not self.steps:
            raise AssertionError("ScriptedLLM ran out of steps (agent looped more than expected)")
        step = self.steps.pop(0)
        return step(messages) if callable(step) else step
