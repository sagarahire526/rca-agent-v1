"""
Query Refiner Agent — Human-in-the-Loop node for RCA.

Analyses the user's raw query for completeness (required scope params present?).
If the query is under-specified, the node suspends the graph via LangGraph's
interrupt() mechanism and waits for the user to supply clarification.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import SystemMessage, HumanMessage
from langgraph.types import interrupt

from models.state import RCAState
from services.llm_provider import LLMProvider
from prompts.query_refiner_prompt import QUERY_REFINER_SYSTEM

logger = logging.getLogger(__name__)

_CYAN  = "\033[96m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_BOLD  = "\033[1m"
_DIM   = "\033[2m"
_RESET = "\033[0m"


def _parse_refiner_response(content: str) -> dict:
    try:
        clean = content.strip()
        if clean.startswith("```"):
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]
        return json.loads(clean.strip())
    except (json.JSONDecodeError, IndexError):
        logger.warning("Query refiner LLM returned non-JSON; treating query as complete.")
        return {
            "is_complete": True,
            "clarification_questions": [],
            "assumptions": [],
            "refined_query": "",
        }


def query_refiner_node(state: RCAState) -> dict[str, Any]:
    """
    LangGraph node: Query Refiner Agent (Human-in-the-Loop).

    Reads:  user_query
    Writes: refined_query, current_phase, messages
    May interrupt the graph to ask clarifying questions.
    """
    user_query = state["user_query"]

    print(f"\n{_BOLD}{'=' * 70}")
    print(f"  QUERY REFINER — Evaluating query completeness")
    print(f"{'=' * 70}{_RESET}\n")
    print(f"  {_DIM}Query: {user_query}{_RESET}\n")

    provider = LLMProvider(model="gpt-4o-mini")
    llm = provider.get_llm()

    response = llm.invoke([
        SystemMessage(content=QUERY_REFINER_SYSTEM),
        HumanMessage(content=user_query),
    ])

    parsed = _parse_refiner_response(response.content)
    is_complete: bool = parsed.get("is_complete", True)
    clarification_questions: list[str] = parsed.get("clarification_questions", [])
    assumptions: list[str] = parsed.get("assumptions", [])
    refined_query: str = parsed.get("refined_query", user_query) or user_query

    if assumptions:
        print(f"  {_DIM}Assumptions: {' | '.join(assumptions)}{_RESET}")

    if is_complete:
        print(f"  {_GREEN}OK Query is complete — proceeding to orchestrator.{_RESET}\n")
        return {
            "refined_query": refined_query,
            "current_phase": "orchestration",
            "messages": [{
                "agent": "query_refiner",
                "content": f"Query accepted as complete. Refined: {refined_query}",
            }],
        }

    # ── Query is incomplete → ask the user for clarification ──
    print(f"  {_YELLOW}Query needs clarification:{_RESET}")
    for q in clarification_questions:
        print(f"     - {q}")
    print()

    clarification_prompt = {
        "type": "clarification_needed",
        "original_query": user_query,
        "questions": clarification_questions,
        "assumptions_if_skipped": assumptions,
        "message": (
            "Your query needs a bit more detail to run a precise RCA investigation. "
            "Please answer the questions below (or press Enter to accept assumptions):"
        ),
    }

    user_clarification: str = interrupt(clarification_prompt)

    if user_clarification and user_clarification.strip():
        refined_query = (
            f"{user_query} — Additional context: {user_clarification.strip()}"
        )
        print(f"  {_GREEN}OK Clarification received. Refined query:{_RESET}")
        print(f"     {refined_query}\n")
    else:
        refined_query = refined_query or user_query
        print(f"  {_DIM}No clarification provided — proceeding with assumptions.{_RESET}\n")

    return {
        "refined_query": refined_query,
        "current_phase": "orchestration",
        "messages": [{
            "agent": "query_refiner",
            "content": f"Query refined after clarification. Final: {refined_query}",
        }],
    }
