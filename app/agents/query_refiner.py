"""
Query Refiner Agent — Human-in-the-Loop node for RCA.

Analyses the user's raw query for completeness (geography/market present?).
If the query is under-specified, the node suspends the graph via LangGraph's
interrupt() mechanism and waits for the user to supply clarification.
Once the query is well-defined, it forwards the finalised query to the
Orchestrator Agent.

NOTE: project_type is supplied as an explicit input parameter (not extracted
from the query). The HITL phase only asks about geography/market.

Human-in-the-Loop flow:
  1. LLM evaluates the query.
  2. If complete → set refined_query and advance.
  3. If incomplete → `interrupt()` with clarification questions + assumptions.
  4. Caller resumes the graph with `Command(resume=<user_clarification_text>)`.
  5. Node merges the user's clarification with the original query → refined_query.
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
    """
    Parse the LLM's JSON output from the query refiner.
    Returns a safe default dict on any parse failure.
    """
    try:
        clean = content.strip()
        # Strip markdown fences if the LLM added them despite instructions
        if clean.startswith("```"):
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]
        # Also handle case where LLM wraps JSON in other text
        first_brace = clean.find("{")
        last_brace = clean.rfind("}")
        if first_brace != -1 and last_brace != -1:
            clean = clean[first_brace:last_brace + 1]
        return json.loads(clean.strip())
    except (json.JSONDecodeError, IndexError) as exc:
        logger.warning("Query refiner LLM returned non-JSON (error: %s); raw: %.200s", exc, content)
        return {
            "is_complete": True,
            "clarification_questions": [],
            "assumptions": [],
            "refined_query": "",
        }



def query_refiner_node(state: RCAState) -> dict[str, Any]:
    """
    LangGraph node: Query Refiner Agent (Human-in-the-Loop).

    Reads:  user_query, project_type (already set from API input)
    Writes: refined_query, current_phase, messages
    May interrupt the graph to ask clarifying questions about geography/market.
    """
    user_query = state["user_query"]
    project_type = state.get("project_type", "")

    print(f"\n{_BOLD}{'=' * 70}", flush=True)
    print(f"  QUERY REFINER — Evaluating query completeness", flush=True)
    print(f"{'=' * 70}{_RESET}\n", flush=True)
    print(f"  {_DIM}Query: {user_query}{_RESET}", flush=True)
    print(f"  {_GREEN}Project type (from input): {project_type}{_RESET}\n", flush=True)

    llm = LLMProvider(model="gpt-4o-mini").get_llm()

    response = llm.invoke([
        SystemMessage(content=QUERY_REFINER_SYSTEM),
        HumanMessage(content=user_query),
    ])

    raw_content = response.content
    logger.info("Query refiner raw LLM response: %s", raw_content[:500])
    print(f"  {_DIM}Raw LLM response: {raw_content[:300]}{_RESET}\n", flush=True)

    parsed = _parse_refiner_response(raw_content)
    is_complete: bool = parsed.get("is_complete", True)
    clarification_questions: list[str] = parsed.get("clarification_questions", [])
    assumptions: list[str] = parsed.get("assumptions", [])
    refined_query: str = parsed.get("refined_query", user_query) or user_query

    print(f"  {_DIM}Parsed: is_complete={is_complete}, "
          f"questions={len(clarification_questions)}{_RESET}", flush=True)

    if assumptions:
        print(f"  {_DIM}Assumptions: {' | '.join(assumptions)}{_RESET}", flush=True)

    if is_complete:
        print(f"  {_GREEN}OK Query is complete — proceeding to orchestrator.{_RESET}\n", flush=True)
        return {
            "refined_query": refined_query,
            "current_phase": "orchestration",
            "messages": [{
                "agent": "query_refiner",
                "content": f"Query accepted as complete. Refined: {refined_query}",
            }],
        }

    # ── Query is incomplete → ask the user for clarification (geography only) ──
    print(f"  {_YELLOW}Query needs clarification:{_RESET}", flush=True)
    for q in clarification_questions:
        print(f"     - {q}", flush=True)
    print(flush=True)

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

    # Suspend graph — caller must resume with Command(resume=<user_text>)
    user_clarification: str = interrupt(clarification_prompt)

    # ── Graph resumed with user's clarification ──
    if user_clarification and user_clarification.strip():
        refined_query = (
            f"{user_query} — Additional context: {user_clarification.strip()}"
        )
        print(f"  {_GREEN}OK Clarification received. Refined query:{_RESET}", flush=True)
        print(f"     {refined_query}\n", flush=True)
    else:
        refined_query = refined_query or user_query
        print(f"  {_DIM}No clarification provided — proceeding with assumptions.{_RESET}\n", flush=True)

    return {
        "refined_query": refined_query,
        "current_phase": "orchestration",
        "messages": [{
            "agent": "query_refiner",
            "content": f"Query refined after clarification. Final: {refined_query}",
        }],
    }
