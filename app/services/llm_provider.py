"""
LLM Provider — instance-based ChatOpenAI wrapper with client caching.

Caches ChatOpenAI instances per (model, temperature) to reuse HTTP
connection pools across requests and avoid per-call client overhead.

Usage:
    from services.llm_provider import LLMProvider

    provider = LLMProvider(model="gpt-4.1-mini")
    llm = provider.get_llm()
"""
from __future__ import annotations

import os
from functools import lru_cache

from langchain_openai import ChatOpenAI

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")


@lru_cache(maxsize=10)
def _get_cached_llm(
    model: str,
    temperature: float,
    reasoning_effort: str | None,
) -> ChatOpenAI:
    """Return a shared ChatOpenAI instance for this (model, temperature, reasoning_effort) tuple."""
    kwargs = {
        "api_key": OPENAI_API_KEY,
        "model": model,
        "temperature": temperature,
        "max_retries": 3,
        "request_timeout": 120,
    }
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
    return ChatOpenAI(**kwargs)


class LLMProvider:
    """
    Instance-based LLM provider.

    Each instance wraps a single ChatOpenAI configured for a specific model.
    Instances with the same (model, temperature, reasoning_effort) share the
    underlying client.
    """

    def __init__(self, model="gpt-4.1-mini", temperature=0.2, reasoning_effort=None):
        self.llm = _get_cached_llm(model, temperature, reasoning_effort)

    def get_llm(self):
        return self.llm

    def invoke(self, messages):
        return self.llm.invoke(messages)

    def stream(self, messages):
        return self.llm.stream(messages)

    async def ainvoke(self, messages):
        return await self.llm.ainvoke(messages)
