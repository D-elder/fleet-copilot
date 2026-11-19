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


def make_llm_invoke(tools: Sequence[Any]) -> LlmInvoke:
    from langchain_openai import ChatOpenAI

    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is not set. Put it in a gitignored .env file (see .env.example).")
    extra_body: dict[str, Any] = {}
    if config.PROVIDER:  # pin the upstream provider so repeated runs hit the same backend
        extra_body["provider"] = {"order": [config.PROVIDER], "allow_fallbacks": False}
    chat = ChatOpenAI(model=config.MODEL, base_url=config.OPENROUTER_BASE_URL, api_key=key,
                      temperature=config.TEMPERATURE, seed=config.SEED, max_retries=2, timeout=120,
                      extra_body=extra_body or None)
    return chat.bind_tools(list(tools)).invoke  # type: ignore[return-value]
