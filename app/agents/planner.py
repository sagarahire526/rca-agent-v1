"""
Planner Agent — Multi-step parallel execution node for RCA.

Workflow:
  1. Fetch semantic context (KPIs, question bank, RCA scenarios).
  2. Use an LLM to decompose the RCA query into N focused investigation sub-queries.
  3. Execute each sub-query via the Traversal Agent concurrently.
  4. Accumulate all traversal results and pass them to the Response Agent.
"""
from __future__ import annotations

import asyncio
import json
import logging
import warnings
from datetime import date as _date
from typing import Any

from langchain_core.messages import SystemMessage, HumanMessage

from models.state import RCAState
from services.llm_provider import LLMProvider
from agents.traversal import atraversal_node
from services.semantic_service import get_semantic_service
from services.internal_scenarios import get_internal_scenarios_store
from prompts.planner_prompt import PLANNER_SYSTEM

_PLAN_TEMPLATE_THRESHOLD = 0.80
# Same bar applied to the semantic-search RCA scenarios (which carry no fetch-time
# floor) so the `scenario_match_found` flag uses one consistent threshold across both
# sources. See models/state.py:RCAState.scenario_match_found.
_SEMANTIC_RCA_STRONG_THRESHOLD = 0.80

logger = logging.getLogger(__name__)

_CYAN   = "\033[96m"
_GREEN  = "\033[92m"
_YELLOW = "\033[93m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"
_RESET  = "\033[0m"

_MAX_PARALLEL_STEPS = 10
_PLANNER_STEP_MAX_STEPS = 20
_STEP_TIMEOUT_SEC = 300


# ── Deterministic scenario-node bypass ───────────────────────────────────────

def _fetch_scenario_node_match(query: str, project_type: str, agent_type: str) -> dict | None:
    """Look up a matching entity_type='scenario' GRAPH node owned by ``agent_type``
    (embedding search sliced to that agent's scenario nodes, gated on the node's own
    scn_similarity_threshold). Returns {node_id, label, score, threshold} or None.
    Non-fatal on any error."""
    try:
        from services.schema_embedding_service import search_scenarios
        return search_scenarios(query, project_type=project_type, agent_type=agent_type)
    except Exception as e:
        logger.warning("Scenario-node match failed (non-fatal): %s", e)
        return None


def _fetch_scenario_param_schema(node_id: str) -> dict | None:
    """Fetch + parse a scenario node's `scn_param_schema` from Neo4j. Returns the parsed
    dict (expected to carry a `fields` list for schema-driven extraction), or None if
    absent/legacy/unparseable. Non-fatal on any error."""
    try:
        from tools.neo4j_tool import Neo4jTool
        out = Neo4jTool().run_cypher_safe(
            "MATCH (n:BKGNode {node_id: $nid}) RETURN coalesce(n.scn_param_schema, '') AS s",
            {"nid": node_id},
        )
        records = (out.get("records") or out.get("results") or []) if isinstance(out, dict) else []
        raw = (records[0].get("s") if records else "") or ""
        schema = json.loads(raw) if raw.strip() else None
        return schema if isinstance(schema, dict) else None
    except Exception as e:
        logger.warning("Scenario param-schema fetch failed (non-fatal): %s", e)
        return None


def _run_scenario_bypass(scenario_match: dict, query: str, project_type: str = "") -> dict | None:
    """Deterministically execute a matched scenario node — NO planner LLM, NO traversal
    LLM. Extracts scope params (constrained), runs the scenario's orchestrator via the
    sandbox `run_scenario` helper, and returns a planner_step_results-shaped payload.
    Returns None to fall through to the normal LLM planning path on any failure.

    `project_type` is the user's explicit selection; it is resolved to the scenario's
    `smp_name` scope deterministically (not LLM-extracted)."""
    try:
        import time as _time
        from services.scenario_params import extract_scenario_params, extract_params_by_schema
        from tools.python_sandbox import PythonSandbox

        scn_id = scenario_match["node_id"]

        # If the node declares a `fields` schema, use the GENERIC schema-driven extractor
        # (no per-scenario few-shots); otherwise fall back to the legacy extractor.
        schema = _fetch_scenario_param_schema(scn_id)
        if isinstance(schema, dict) and schema.get("fields"):
            params = extract_params_by_schema(query, schema, project_type=project_type)
        else:
            params = extract_scenario_params(query, project_type=project_type)

        print(
            f"  {_CYAN}Scenario params{_RESET} "
            f"{_DIM}(node={scn_id}){_RESET}\n{json.dumps(params, indent=2)}",
            flush=True,
        )
        logger.info("Scenario params (%s): %s", scn_id, json.dumps(params))

        # Pass params as JSON data (no code injection) into the sandbox call.
        payload = json.dumps({
            "scenario_id": scn_id,
            "filter": params["filter"],
            "group_by": params["group_by"],
        })
        code = (
            "import json as _json\n"
            f"_p = _json.loads({payload!r})\n"
            "result = run_scenario(_p['scenario_id'], _p['filter'], _p['group_by'])"
        )

        t0 = _time.perf_counter()
        out = PythonSandbox().execute(code, timeout_seconds=120)
        dt = (_time.perf_counter() - t0) * 1000.0

        if out.get("status") != "success":
            logger.warning("Scenario bypass execution error: %s", out.get("error"))
            return None

        result = out.get("result") or {}
        preds = result.get("predictions", []) if isinstance(result, dict) else []

        step = {
            "traversal_findings": (
                f"Executed approved scenario '{scenario_match.get('label', scn_id)}' "
                f"deterministically. Resolved scope: {json.dumps(params.get('resolved', {}))}."
            ),
            "traversal_tool_calls": [{
                "tool_name": "run_scenario",
                "tool_input": {
                    "scenario_id": scn_id,
                    "filter": params["filter"],
                    "group_by": params["group_by"],
                },
                "tool_output": result,
                "status": "success",
                "execution_time_ms": round(dt, 1),
            }],
            "traversal_steps_taken": 1,
            "scenario_full_result": result,
            "scenario_label": scenario_match.get("label", "scenario"),
            "scenario_resolved": params.get("resolved", {}),
        }
        return {"step_result": step, "resolved_params": params, "row_count": len(preds)}
    except Exception as e:
        logger.warning("Scenario bypass failed (falling back to planner): %s", e)
        return None


def _parse_planner_response(content: str) -> tuple[str, list[str]]:
    try:
        clean = content.strip()
        if clean.startswith("```"):
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]
        data = json.loads(clean.strip())
        rationale = data.get("planning_rationale", "")
        steps = data.get("steps", [])
        if not steps or not isinstance(steps, list):
            raise ValueError("No steps found in planner response")
        return rationale, [str(s) for s in steps if str(s).strip()]
    except (json.JSONDecodeError, ValueError, IndexError):
        logger.warning("Planner LLM returned non-JSON or empty steps; using single-step fallback.")
        return "Single-step fallback due to parse error.", []


async def _run_traversal_step_async(
    step_query: str,
    base_state: RCAState,
    step_idx: int,
    max_steps: int = _PLANNER_STEP_MAX_STEPS,
) -> dict:
    warnings.filterwarnings("ignore", message=".*pandas only supports SQLAlchemy.*")
    step_state: RCAState = {
        **base_state,
        "user_query": step_query,
        "max_traversal_steps": max_steps,
    }
    try:
        return await atraversal_node(step_state)
    except Exception as e:
        logger.error("Traversal step %d failed for query '%s': %s", step_idx + 1, step_query[:80], e)
        return {
            "traversal_findings": f"Step failed: {e}",
            "traversal_tool_calls": [],
            "traversal_steps_taken": 0,
            "errors": [f"Traversal step error: {e}"],
        }


async def _gather_traversals(steps: list[str], state: RCAState) -> list:
    tasks = [
        asyncio.wait_for(
            _run_traversal_step_async(step, state, idx),
            timeout=float(_STEP_TIMEOUT_SEC),
        )
        for idx, step in enumerate(steps)
    ]
    return await asyncio.gather(*tasks, return_exceptions=True)


def planner_node(state: RCAState) -> dict[str, Any]:
    """
    LangGraph node: Planner Agent for RCA.

    Reads:  refined_query, max_traversal_steps
    Writes: planner_steps, planner_step_results,
            rca_scenario_guidance, current_phase, messages
    """
    refined_query = state.get("refined_query") or state["user_query"]

    print(f"\n{_BOLD}{'=' * 70}")
    print(f"  PLANNER AGENT — Decomposing RCA query into investigation steps")
    print(f"{'=' * 70}{_RESET}\n")
    print(f"  {_DIM}Query: {refined_query}{_RESET}\n")

    # ── Step 0: Deterministic scenario-node bypass (fast path) ──
    # If an entity_type='scenario' GRAPH node owned by this agent (scn_agent_type ==
    # state.agent_type) matches >= its own threshold, run it deterministically — no
    # planner LLM, no traversal LLM. The scenario's orchestrator calls its contributing
    # nodes with fixed group_by/filters, so grouping/filtering/computation are fully
    # repeatable (the consistency path).
    # Match on the RAW user query, not the refined one: a scenario's canonical question
    # is written in the user's own phrasing, and the refiner's normalization can drift a
    # verbatim query below the similarity threshold. Param extraction below still uses
    # refined_query (entity names normalized, which the filters need to be case-correct).
    agent_type = state.get("agent_type") or "rca"
    scenario_match_query = state.get("user_query") or refined_query
    scenario_node = _fetch_scenario_node_match(
        scenario_match_query, state.get("project_type", ""), agent_type
    )
    if scenario_node:
        bypass = _run_scenario_bypass(scenario_node, refined_query, state.get("project_type", ""))
        if bypass is not None:
            print(
                f"  {_GREEN}Deterministic scenario bypass: '{scenario_node.get('label')}' "
                f"(sim {scenario_node.get('score')}) — skipping LLM planner + traversal{_RESET}"
            )
            return {
                "planning_rationale": f"Deterministic scenario bypass: {scenario_node.get('label')}",
                "planner_steps": [f"Scenario: {scenario_node.get('label')}"],
                "planner_step_results": [bypass["step_result"]],
                "rca_scenario_guidance": "",
                "planner_semantic_context": "",
                "semantic_context_data": {},
                "scenario_match_found": True,
                "current_phase": "response",
                "messages": [{
                    "agent": "planner",
                    "content": (
                        f"Deterministic scenario '{scenario_node.get('label')}' executed "
                        f"({bypass['row_count']} rows); LLM planning + traversal bypassed. "
                        f"Resolved scope: {bypass['resolved_params']['resolved']}."
                    ),
                }],
            }

    # ── Step 1: Fetch semantic context ──
    semantic_context = ""
    rca_guidance = ""
    context_data: dict[str, list[dict]] = {}
    sim_strong = 0  # count of RCA scenario hits at >= 0.80 (no fetch-time floor)
    try:
        semantic = get_semantic_service()
        gcl_query = (
            f"{refined_query} for NAS"
            if state.get("project_type") == "NAS"
            else refined_query
        )
        context_data = semantic.get_all_context(gcl_query)

        sim_rows = context_data.get("rca", []) or []
        sim_strong = sum(
            1 for r in sim_rows
            if (r.get("similarity_score") or 0) >= _SEMANTIC_RCA_STRONG_THRESHOLD
        )

        total_hits = sum(len(v) for v in context_data.values())
        if total_hits:
            semantic_context = semantic.format_traversal_context(context_data)
            rca_guidance = semantic.format_rca_guidance(context_data)
            kpi_hits = len(context_data.get("kpi", []))
            qb_hits  = len(context_data.get("question_bank", []))
            sim_hits = len(context_data.get("rca", []))
            kw_hits  = len(context_data.get("keywords", []))
            print(
                f"  {_GREEN}Semantic context: "
                f"{kpi_hits} KPI, {qb_hits} Q&A, {sim_hits} scenario(s), {kw_hits} keyword(s){_RESET}"
            )
        else:
            print(f"  {_DIM}No semantic context (API may be unreachable).{_RESET}")
    except Exception as e:
        logger.warning("Semantic search in planner failed (non-fatal): %s", e)

    # ── Step 1b: Look up curated plan template (override path) ──
    # Search with BOTH raw user_query and refined_query, keep the best match.
    # The refiner paraphrases (expanding abbreviations, swapping verbs, appending
    # skip-default scope) which can shift the embedding off a stored scenario
    # that was authored to mirror raw user phrasing.
    matched_plan_template = ""
    matched_template_meta: dict[str, Any] = {}
    try:
        store = get_internal_scenarios_store()
        raw_query = state["user_query"]

        query_forms: list[tuple[str, str]] = []
        seen: set[str] = set()
        for label, q in (("raw", raw_query), ("refined", refined_query)):
            if q and q not in seen:
                query_forms.append((label, q))
                seen.add(q)

        best: dict[str, Any] | None = None
        best_form: str = ""
        for label, q in query_forms:
            hits = store.search(q, threshold=_PLAN_TEMPLATE_THRESHOLD, top_k=1)
            if hits and (best is None or hits[0]["similarity_score"] > best["similarity_score"]):
                best = hits[0]
                best_form = label

        if best is not None:
            m = best
            steps_block = "\n".join(
                f"  {i + 1}. {s}" for i, s in enumerate(m["steps"])
            )
            matched_plan_template = (
                f"## Curated Plan Template (high-confidence match — "
                f"similarity {m['similarity_score'] * 100:.1f}%)\n"
                f"This is a pre-vetted plan for a near-identical question. "
                f"Use these steps as the spine of your plan; only adapt filters "
                f"(market, region, vendor/GC, dates, time window) to the user's "
                f"actual ask. Do not invent new steps, drop steps, or reorder "
                f"steps unless filter adaptation strictly requires it.\n\n"
                f"**Matched scenario tag:** {m['tag']}\n"
                f"**Matched question:** {m['question']}\n\n"
                f"**Pre-vetted steps:**\n{steps_block}"
            )
            matched_template_meta = {
                "id": m["id"],
                "tag": m["tag"],
                "similarity_score": m["similarity_score"],
            }
            print(
                f"  {_GREEN}Curated plan template hit: {m['tag']} "
                f"({m['similarity_score'] * 100:.1f}%, matched on {best_form} query){_RESET}"
            )
            logger.info(
                "Internal scenario fetched: tag='%s' id=%s similarity=%.3f form=%s",
                m["tag"], m["id"], m["similarity_score"], best_form,
            )
        else:
            print(
                f"  {_DIM}No curated plan template match (threshold "
                f"{_PLAN_TEMPLATE_THRESHOLD * 100:.0f}%, tried "
                f"{len(query_forms)} query form(s)).{_RESET}"
            )
            logger.info(
                "No internal scenario match (threshold=%.2f) for raw=%.80s | refined=%.80s",
                _PLAN_TEMPLATE_THRESHOLD, raw_query, refined_query,
            )
    except Exception as e:
        logger.warning("Internal scenario lookup failed (non-fatal): %s", e)

    # ── Scenario coverage flag ──
    # OR semantics: any source at >= 0.80 counts as "grounded in an approved scenario".
    # The flag is informational — the planner/traversal/response pipeline runs either
    # way. The UI uses it (via the SSE event and the history endpoint) to render a
    # "no matching scenario" notice on a miss.
    scenario_match_found = bool(sim_strong) or (matched_template_meta != {})
    print(
        f"  {_DIM}Scenario match flag: {scenario_match_found} "
        f"(sim_strong={sim_strong}, curated={'yes' if matched_template_meta else 'no'}){_RESET}"
    )

    # ── Step 2: LLM creates the investigation plan ──
    provider = LLMProvider(model="gpt-5", reasoning_effort="medium")
    llm = provider.get_llm()

    safe_semantic = semantic_context.replace("{", "{{").replace("}", "}}")
    safe_template = matched_plan_template.replace("{", "{{").replace("}", "}}")

    planning_prompt = PLANNER_SYSTEM.format(
        semantic_context=safe_semantic,
        matched_plan_template=safe_template,
        today_date=_date.today().isoformat(),
    )

    llm_response = llm.invoke([
        SystemMessage(content=planning_prompt),
        HumanMessage(content=refined_query),
    ])

    rationale, steps = _parse_planner_response(llm_response.content)

    if not steps:
        steps = [f"Sub-query 1: {refined_query}"]

    steps = steps[:_MAX_PARALLEL_STEPS]

    print(f"\n  {_BOLD}Investigation Plan ({len(steps)} steps):{_RESET}")
    if rationale:
        print(f"  {_YELLOW}Intent:{_RESET} {rationale}\n")
    for i, step in enumerate(steps, 1):
        display = step
        if ": " in step:
            display = step.split(": ", 1)[1]
        print(f"  {_CYAN}  Step {i}:{_RESET} {display}")
    print()

    # ── Step 3: Execute each step concurrently ──
    print(f"  {_BOLD}Executing {len(steps)} traversal(s) in parallel...{_RESET}\n")

    gathered = asyncio.run(_gather_traversals(steps, state))

    step_results: list[dict] = []
    for idx, result in enumerate(gathered):
        if isinstance(result, (asyncio.TimeoutError, TimeoutError)):
            logger.warning("Step %d timed out after %ds", idx + 1, _STEP_TIMEOUT_SEC)
            step_results.append({
                "traversal_findings": f"Step timed out after {_STEP_TIMEOUT_SEC}s",
                "traversal_tool_calls": [],
                "traversal_steps_taken": 0,
                "errors": [f"Step {idx + 1} timed out"],
            })
        elif isinstance(result, Exception):
            logger.error("Unexpected error in step %d: %s", idx + 1, result)
            step_results.append({
                "traversal_findings": f"Unexpected error: {result}",
                "traversal_tool_calls": [],
                "traversal_steps_taken": 0,
            })
        else:
            step_results.append(result)

    total_tool_calls = sum(
        r.get("traversal_steps_taken", 0) for r in step_results
    )
    print(f"\n  {_GREEN}All steps complete — {total_tool_calls} total tool calls{_RESET}\n")

    logger.info(
        "Planner completed: %d steps, %d total tool calls",
        len(steps), total_tool_calls,
    )

    return {
        "planning_rationale": rationale,
        "planner_steps": steps,
        "planner_step_results": step_results,
        "rca_scenario_guidance": rca_guidance,
        "planner_semantic_context": semantic_context,
        "semantic_context_data": context_data,
        "matched_plan_template": matched_template_meta,
        "scenario_match_found": scenario_match_found,
        "current_phase": "response",
        "messages": [{
            "agent": "planner",
            "content": (
                f"Investigation plan complete: {len(steps)} steps executed in parallel, "
                f"{total_tool_calls} total traversal tool calls."
            ),
        }],
    }
