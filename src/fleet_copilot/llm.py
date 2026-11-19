"""LLM factory (OpenRouter via the OpenAI-compatible API). Settings come from config / .env."""
from __future__ import annotations

import os
from typing import Any, Callable, Sequence

from langchain_core.messages import AIMessage, BaseMessage

from . import config

LlmInvoke = Callable[[Sequence[BaseMessage]], AIMessage]


def describe() -> dict[str, Any]:
    return {"model": config.MODEL, "provider": config.PROVIDER or "(openrouter default routing)",
            "temperature": config.TEMPERATURE, "seed": config.SEED, "base_url": config.OPENROUTER_BASE_URL}


def _chat():
    from langchain_openai import ChatOpenAI

    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is not set. Put it in a gitignored .env file (see .env.example).")
    extra_body: dict[str, Any] = {}
    if config.PROVIDER:  # pin the upstream provider so repeated runs hit the same backend
        extra_body["provider"] = {"order": [config.PROVIDER], "allow_fallbacks": False}
    return ChatOpenAI(model=config.MODEL, base_url=config.OPENROUTER_BASE_URL, api_key=key,
                      temperature=config.TEMPERATURE, seed=config.SEED, max_retries=2, timeout=120,
                      extra_body=extra_body or None)


def make_llm_invoke(tools: Sequence[Any]) -> LlmInvoke:
    return _chat().bind_tools(list(tools)).invoke  # type: ignore[return-value]


def make_plan_invoke() -> LlmInvoke:
    """Planning call: submit_plan is the ONLY tool offered, and PLAN_INSTRUCTION tells the model to call it.

    tool_choice is deliberately not forced: the configured model (verified live) rejects forced tool choice with
    "tool_choice: type 'tool' and 'any' are not supported for this model". If a model ever skips the call, the
    plan node records "no plan" in the trace and the turn proceeds without coverage enforcement.
    """
    from .tools import plan_spec

    return _chat().bind_tools([plan_spec()]).invoke  # type: ignore[return-value]


def make_agent(conn, session, checkpoint_conn=None):
    """Production wiring: a FleetAgent using the configured OpenRouter model for planning and acting."""
    from .graph import FleetAgent
    from .tools import ToolBox

    return FleetAgent(conn, session, make_llm_invoke(ToolBox(conn, session).specs()),
                      checkpoint_conn=checkpoint_conn, plan_invoke=make_plan_invoke())
