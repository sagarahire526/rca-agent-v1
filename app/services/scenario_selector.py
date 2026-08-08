"""
Scenario selector — LLM re-ranker over embedding-recall candidates.

Stage 2 of scenario matching (ported from the simulation agent).
`schema_embedding_service.search_scenario_candidates` returns every scenario owned by
this agent above a loose recall floor; this asks a light LLM (gpt-5-mini, low reasoning
effort) to pick the single best-matching scenario by meaning, or return None so the
planner handles the query normally.

Scores are not shown to the LLM. Any failure (LLM error, unparseable output, or an
id outside the candidate set) returns None — the safe fallback to the general planner.
"""
from __future__ import annotations

import json
import logging

from langchain_core.messages import SystemMessage, HumanMessage

from services.llm_provider import LLMProvider
from prompts.scenario_select_prompt import (
    SCENARIO_SELECT_SYSTEM,
    build_scenario_select_user,
)

logger = logging.getLogger(__name__)

_NONE_TOKENS = {"", "none", "null", "no", "n/a", "nan"}


def _parse_choice(content: str) -> tuple[str | None, str]:
    """Parse {"node_id": ..., "reason": ...}; return (node_id or None, reason)."""
    clean = (content or "").strip()
    if clean.startswith("```"):
        parts = clean.split("```")
        clean = parts[1] if len(parts) > 1 else clean
        if clean.startswith("json"):
            clean = clean[4:]
        clean = clean.strip()
    try:
        data = json.loads(clean)
    except (json.JSONDecodeError, ValueError):
        return None, ""
    if not isinstance(data, dict):
        return None, ""
    reason = str(data.get("reason") or "").strip()
    nid = data.get("node_id")
    if nid is None:
        return None, reason
    nid = str(nid).strip()
    return (None if nid.lower() in _NONE_TOKENS else nid), reason


def select_scenario(query: str, candidates: list[dict]) -> dict | None:
    """Return the chosen candidate dict, or None to fall through to the planner.

    `candidates` come from search_scenario_candidates. The LLM sees every field
    except the score and returns one node_id or 'none'. The picked candidate is
    returned with the model's one-line `selection_reason` attached for logging.
    """
    if not candidates:
        return None

    by_id = {c["node_id"]: c for c in candidates}
    # Similarity decides only WHICH scenarios are shown, never how they are presented:
    # the score is not rendered into the prompt, and the recall ranking is re-ordered by
    # node_id so it leaks no positional hint either. node_id order is query-independent,
    # so the same candidate set always renders identically (repeatable selection).
    shown = sorted(candidates, key=lambda c: str(c["node_id"]))
    try:
        llm = LLMProvider(model="gpt-5-mini", temperature=0.0, reasoning_effort="low").get_llm()
        resp = llm.invoke([
            SystemMessage(content=SCENARIO_SELECT_SYSTEM),
            HumanMessage(content=build_scenario_select_user(query, shown)),
        ])
        choice, reason = _parse_choice(resp.content)
    except Exception as e:  # noqa: BLE001
        logger.warning("Scenario LLM selection failed (falling back to planner): %s", e)
        return None

    if choice is None:
        logger.info(
            "Scenario selector: LLM returned none for %d candidate(s). %s",
            len(candidates), reason,
        )
        print(
            "  \033[2mScenario selector: no candidate answers this query — "
            "continuing with normal planning.\033[0m",
            flush=True,
        )
        return None
    if choice not in by_id:
        logger.warning("Scenario selector: LLM returned out-of-set node_id %r; treating as none.", choice)
        return None

    logger.info(
        "Scenario selector: LLM picked %s from %d candidate(s). %s",
        choice, len(candidates), reason,
    )
    return {**by_id[choice], "selection_reason": reason}
