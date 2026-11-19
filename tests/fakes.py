"""A scripted stand-in for the LLM so the whole graph (including interrupt/resume) runs offline and deterministically."""
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


def last_result(messages: Sequence[BaseMessage], tool: str = "query_devices") -> dict[str, Any]:
    for m in reversed(messages):
        if isinstance(m, ToolMessage) and m.name == tool and not m.content.startswith("ERROR"):
            return json.loads(m.content)
    raise AssertionError(f"no {tool} result in the conversation yet")


def claim_for(result: dict[str, Any], **over: Any) -> dict[str, Any]:
    c = {"result_id": result["result_id"], "device_ids": [r["device_id"] for r in result["rows"]],
         "count": result["count"], "snapshot_ids": [r["snapshot_id"] for r in result["rows"]], "subset": False}
    c.update(over)
    return c


def final_from_last_result(summary: str, **claim_overrides: Any) -> Callable[[Sequence[BaseMessage]], AIMessage]:
    """A well-behaved model: claims exactly what the last query_devices result says (optionally corrupted)."""
    def step(messages: Sequence[BaseMessage]) -> AIMessage:
        r = last_result(messages)
        return ai(call("final_answer", {"summary": summary, "claims": [claim_for(r, **claim_overrides)]}))
    return step


def ticket_for(device_id: str, check_id: str) -> Callable[[Sequence[BaseMessage]], AIMessage]:
    def step(messages: Sequence[BaseMessage]) -> AIMessage:
        r = last_result(messages)
        row = next(x for x in r["rows"] if x["device_id"] == device_id)
        return ai(call("open_remediation_ticket", {
            "device_id": device_id, "check_id": check_id, "note": "Enforce screen lock policy.",
            "evidence": [row["snapshot_id"]], "justification": f"{check_id} is failing in the latest snapshot."}))
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
