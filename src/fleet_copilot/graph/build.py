"""Explicit agent loop (LangGraph).

    START -> agent <-> tools -> validate -> respond -> approval(interrupt) -> END
                         ^____________|  (one retry with the validator's diff)

* agent     : one LLM call (tools bound; no execute tool exists).
* tools     : dispatches tool calls through the session-bound ToolBox, records ToolResults / proposals in state.
* validate  : deterministic claim validation of final_answer against this turn's ToolResults.
* respond   : renders the answer from tool data (code, not the model).
* approval  : LangGraph interrupt() pauses for a human; the decision is applied by application code (actions.py).
"""
import operator
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable, Sequence, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, interrupt

from .. import actions, audit, config
from ..models import Answer, ToolResult
from ..prompts import SYSTEM_PROMPT
from ..repository import Repository
from ..session import AccessDenied, Session
from ..tools import ToolBox
from ..validation import render_answer, render_fallback, validate_answer

RECURSION_LIMIT = 80


def _merge(a: dict | None, b: dict | None) -> dict:
    return {**(a or {}), **(b or {})}


class State(TypedDict, total=False):
    company_id: str
    messages: Annotated[list[AnyMessage], add_messages]
    results: Annotated[dict, _merge]           # result_id -> ToolResult dump (this turn)
    proposals: Annotated[list, operator.add]   # ProposalResult dumps (+ the raw request) created this turn
    final: dict | None
    final_call_id: str | None
    validated: bool | None                     # True/False = settled, None = retrying
    validation_errors: list
    retries: int
    iterations: int
    rendered: str | None
    decisions: list
    trace: Annotated[list, operator.add]


@dataclass
class TurnOutcome:
    thread_id: str
    rendered: str
    pending: list[dict[str, Any]] = field(default_factory=list)   # proposals awaiting a human decision
    decisions: list[dict[str, Any]] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)
    validated: bool | None = None
    state: dict[str, Any] = field(default_factory=dict)


class FleetAgent:
    def __init__(self, conn: sqlite3.Connection, session: Session,
                 llm_invoke: Callable[[Sequence[BaseMessage]], AIMessage],
                 checkpoint_conn: sqlite3.Connection | None = None):
        self.conn, self.session, self.llm_invoke = conn, session, llm_invoke
        self.repo = Repository(conn, session)
        self.toolbox = ToolBox(conn, session)
        cp = checkpoint_conn or sqlite3.connect(":memory:", check_same_thread=False)
        self.graph = self._build(SqliteSaver(cp))

    # ------------------------------------------------------------------ graph
    def _build(self, saver: SqliteSaver):
        session, conn, toolbox = self.session, self.conn, self.toolbox

        def agent(state: State) -> dict:
            ai = self.llm_invoke(state["messages"])
            calls = [tc["name"] for tc in (ai.tool_calls or [])]
            return {"messages": [ai], "iterations": state.get("iterations", 0) + 1,
                    "trace": [{"node": "agent", "iteration": state.get("iterations", 0) + 1, "tool_calls": calls}]}

        def tools(state: State) -> dict:
            ai = state["messages"][-1]
            if not getattr(ai, "tool_calls", None):
                return {"messages": [HumanMessage("You must call tools or finish by calling final_answer.")],
                        "final": None, "trace": [{"node": "tools", "note": "no tool call; nudged"}]}
            msgs: list[BaseMessage] = []
            results: dict[str, dict] = {}
            proposals: list[dict] = []
            trace: list[dict] = []
            final: dict | None = None
            final_call_id: str | None = None
            for tc in sorted(ai.tool_calls, key=lambda c: c["name"] == "final_answer"):  # final_answer last
                out = toolbox.dispatch(tc["name"], tc["args"])
                if out.trace:
                    trace.append(out.trace)
                if out.final is not None:
                    if final is None:
                        final, final_call_id = out.final.model_dump(), tc["id"]
                    else:
                        msgs.append(ToolMessage("ERROR: final_answer may be called only once", tool_call_id=tc["id"],
                                                name=tc["name"]))
                    continue
                msgs.append(ToolMessage(content=out.content, tool_call_id=tc["id"], name=tc["name"]))
                if out.result is not None:
                    results[out.result.result_id] = out.result.model_dump()
                if out.proposal is not None:
                    proposals.append({**out.proposal.model_dump(), "request": tc["args"]})
            return {"messages": msgs, "results": results, "proposals": proposals, "final": final,
                    "final_call_id": final_call_id, "trace": trace}

        def after_tools(state: State) -> str:
            if state.get("final"):
                return "validate"
            return "give_up" if state.get("iterations", 0) >= config.MAX_AGENT_ITERATIONS else "agent"

        def validate(state: State) -> dict:
            answer = Answer(**state["final"])
            results = {k: ToolResult(**v) for k, v in state.get("results", {}).items()}
            rep = validate_answer(answer, results, session.company_id, self.repo.device_ids())
            call_id = state["final_call_id"]
            if rep.ok:
                return {"messages": [ToolMessage("accepted", tool_call_id=call_id, name="final_answer")],
                        "validated": True, "validation_errors": [],
                        "trace": [{"node": "validate", "ok": True}]}
            can_retry = state.get("retries", 0) < 1 and state.get("iterations", 0) < config.MAX_AGENT_ITERATIONS
            body = "REJECTED by the validator. Fix these and call final_answer again:\n" + "\n".join(rep.errors)
            return {"messages": [ToolMessage(body, tool_call_id=call_id, name="final_answer")],
                    "validated": None if can_retry else False, "validation_errors": rep.errors,
                    "retries": state.get("retries", 0) + (1 if can_retry else 0),
                    "final": None if can_retry else state["final"],
                    "trace": [{"node": "validate", "ok": False, "errors": rep.errors, "retry": can_retry}]}

        def after_validate(state: State) -> str:
            return "agent" if state.get("validated") is None else "respond"

        def respond(state: State) -> dict:
            results = {k: ToolResult(**v) for k, v in state.get("results", {}).items()}
            if state.get("validated"):
                text = render_answer(Answer(**state["final"]), results)
            else:
                text = render_fallback(results, state.get("validation_errors", []))
            prop_lines = []
            for p in state.get("proposals", []):
                req = p.get("request", {})
                label = f"{req.get('device_id')} / {req.get('check_id')}"
                if p["status"] == "refused":
                    prop_lines.append(f"  - REFUSED ({label}): " + "; ".join(p.get("reasons", [])))
                elif p.get("duplicate"):
                    prop_lines.append(f"  - {p['proposal_id']} ({label}) already exists, status={p['status']}")
                else:
                    prop_lines.append(f"  - {p['proposal_id']} open_remediation_ticket ({label}) "
                                      f"status={p['status']} - NOT executed")
            if prop_lines:
                text += "\n\nProposed actions:\n" + "\n".join(prop_lines)
            return {"rendered": text}

        def after_respond(state: State) -> str:
            return "approval" if any(p["status"] == "pending" and not p.get("duplicate")
                                     for p in state.get("proposals", [])) else END

        def give_up(state: State) -> dict:
            results = {k: ToolResult(**v) for k, v in state.get("results", {}).items()}
            text = (f"Stopped after {state.get('iterations', 0)} steps without a verified answer.\n\n"
                    + "\n\n".join(render_fallback(results, ["iteration budget exhausted"]).split("\n\n")[1:]))
            return {"rendered": text, "validated": False}

        def approval(state: State) -> dict:
            if state["company_id"] != session.company_id:  # belt and braces; resume is also scope-checked
                raise AccessDenied("thread belongs to a different company")
            for p in state.get("proposals", []):
                if p["status"] != "pending" or p.get("duplicate"):
                    continue
                cur = actions.get_proposal(conn, session.company_id, p["proposal_id"])
                if cur is None or cur["status"] != "pending":
                    continue  # already decided (this node re-runs from the top after each resume)
                human = interrupt({"proposal_id": cur["proposal_id"], "action": cur["action"], "args": cur["args"],
                                   "evidence": cur["evidence"], "justification": cur["justification"],
                                   "payload_hash": cur["payload_hash"]})
                if human.get("decision") == "approve":
                    d = actions.decide(conn, session, cur["proposal_id"], human.get("payload_hash", ""), "approve")
                    if d.ok:
                        actions.execute(conn, session, cur["proposal_id"])
                else:
                    actions.decide(conn, session, cur["proposal_id"], human.get("payload_hash", ""), "reject")
            decisions = []
            for p in state.get("proposals", []):
                cur = actions.get_proposal(conn, session.company_id, p["proposal_id"]) if p.get("proposal_id") else None
                if cur:
                    decisions.append({"proposal_id": cur["proposal_id"], "status": cur["status"]})
            return {"decisions": decisions, "trace": [{"node": "approval", "decisions": decisions}]}

        g = StateGraph(State)
        for name, fn in [("agent", agent), ("tools", tools), ("validate", validate), ("respond", respond),
                         ("give_up", give_up), ("approval", approval)]:
            g.add_node(name, fn)
        g.add_edge(START, "agent")
        g.add_edge("agent", "tools")
        g.add_conditional_edges("tools", after_tools, {"validate": "validate", "give_up": "give_up", "agent": "agent"})
        g.add_conditional_edges("validate", after_validate, {"agent": "agent", "respond": "respond"})
        g.add_conditional_edges("respond", after_respond, {"approval": "approval", END: END})
        g.add_edge("give_up", END)
        g.add_edge("approval", END)
        return g.compile(checkpointer=saver)

    # -------------------------------------------------------------------- api
    def _cfg(self, thread_id: str) -> dict:
        return {"configurable": {"thread_id": thread_id}, "recursion_limit": RECURSION_LIMIT}

    def _assert_scope(self, thread_id: str) -> None:
        values = self.graph.get_state(self._cfg(thread_id)).values
        if values and values.get("company_id") != self.session.company_id:
            audit.record(conn=self.conn, company_id=self.session.company_id, actor=self.session.admin_id,
                         event="thread_access_denied", details={"thread_id": thread_id})
            raise AccessDenied("this conversation thread does not belong to your company")

    def ask(self, question: str, thread_id: str | None = None) -> TurnOutcome:
        thread_id = thread_id or f"{self.session.company_id}:{uuid.uuid4().hex[:8]}"
        self._assert_scope(thread_id)
        audit.record(self.conn, company_id=self.session.company_id, actor=self.session.admin_id,
                     event="question_asked", details={"thread_id": thread_id, "question": question})
        init: State = {
            "company_id": self.session.company_id,
            "messages": [SystemMessage(SYSTEM_PROMPT.format(company_id=self.session.company_id,
                                                           as_of=self.repo.as_of())), HumanMessage(question)],
            "results": {}, "proposals": [], "final": None, "retries": 0, "iterations": 0, "trace": [],
        }
        self.graph.invoke(init, self._cfg(thread_id))
        return self._outcome(thread_id)

    def resume(self, thread_id: str, decision: str, payload_hash: str) -> TurnOutcome:
        self._assert_scope(thread_id)
        if not self.graph.get_state(self._cfg(thread_id)).next:
            raise ValueError("nothing is waiting for approval on this thread")
        self.graph.invoke(Command(resume={"decision": decision, "payload_hash": payload_hash}),
                          self._cfg(thread_id))
        return self._outcome(thread_id)

    def _outcome(self, thread_id: str) -> TurnOutcome:
        snap = self.graph.get_state(self._cfg(thread_id))
        v = dict(snap.values)
        pending = [i.value for t in snap.tasks for i in t.interrupts]
        return TurnOutcome(thread_id=thread_id, rendered=v.get("rendered") or "", pending=pending,
                           decisions=v.get("decisions") or [], trace=v.get("trace") or [],
                           validated=v.get("validated"), state=v)
