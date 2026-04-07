"""
Schema Discovery node — Discovers the Neo4j knowledge graph schema
once at the start of each RCA investigation run.

Uses an LLM pre-filter to select the most relevant BKG nodes,
producing a two-tier schema that reduces prompt token waste.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import SystemMessage, HumanMessage

from models.state import RCAState
from tools.neo4j_tool import neo4j_tool
from tools.bkg_tool import BKGTool
from services.llm_provider import LLMProvider

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# LLM-powered node selection
# ─────────────────────────────────────────────

_NODE_SELECTION_PROMPT = """\
You are a knowledge-graph expert. Given a user query and a list of BKG nodes, \
select the 10 most relevant node_ids that an agent would need to answer the query.

Consider semantic meaning, not just keyword overlap. For example:
- "delivery" may relate to "completion" or "handover" nodes
- "towers" may relate to "sites" or "cell_sites" nodes
- "GC run rate" involves both the GC entity and KPI/metric nodes

Return ONLY a JSON array of node_id strings. Example: ["node_1", "node_2", ...]
Do not include any other text or explanation."""


def _select_relevant_nodes(query: str, all_nodes: list[dict[str, str]]) -> set[str] | None:
    """
    Use a fast LLM to pick the ~10 most relevant node_ids for the query.
    Returns a set of node_ids, or None on failure (triggers full-schema fallback).
    """
    if not all_nodes:
        return None

    # Build compact node list (~3 tokens per node)
    node_lines = []
    for n in all_nodes:
        label = n.get("label") or n["node_id"]
        et = n.get("entity_type", "")
        defn = n.get("definition", "")
        compact = f"{n['node_id']} | {et} | {label}"
        if defn:
            compact += f" | {defn[:80]}"
        node_lines.append(compact)

    node_list_str = "\n".join(node_lines)

    try:
        provider = LLMProvider(model="gpt-4o-mini", temperature=0.0)
        llm = provider.get_llm()

        response = llm.invoke([
            SystemMessage(content=_NODE_SELECTION_PROMPT),
            HumanMessage(content=f"User query: {query}\n\nAvailable nodes:\n{node_list_str}"),
        ])

        raw = response.content.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        selected = json.loads(raw)

        if not isinstance(selected, list) or len(selected) < 3:
            logger.warning("LLM returned too few nodes (%s), falling back to full schema", selected)
            return None

        # Validate node_ids exist
        valid_ids = {n["node_id"] for n in all_nodes}
        result = {nid for nid in selected if nid in valid_ids}

        if len(result) < 3:
            logger.warning("Only %d valid node_ids after filtering, falling back", len(result))
            return None

        logger.info("LLM selected %d relevant nodes for query", len(result))
        return result

    except Exception as e:
        logger.warning("LLM node selection failed (%s), falling back to full schema", e)
        return None


# ─────────────────────────────────────────────
# PostgreSQL table list
# ─────────────────────────────────────────────

def _fetch_table_list() -> str:
    """Fetch all available PostgreSQL tables from the KG and format as a prompt section."""
    try:
        bkg = BKGTool()
        result = bkg.query({"mode": "schema"})
        tables = result.get("tables", [])
        if not tables:
            return ""

        lines = ["\n\n=== AVAILABLE POSTGRESQL TABLES ==="]
        lines.append("Use ONLY these table names in SQL queries (use get_node on mapped nodes for full details):\n")

        for t in tables:
            name = t.get("table_name", "")
            db = t.get("database_name", "")
            nodes = t.get("nodes", [])
            node_ids = ", ".join(n.get("node_id", "") for n in nodes)
            key_cols = ", ".join(
                filter(None, set(n.get("key_column", "") for n in nodes))
            )

            detail = f"  nodes: {node_ids}" if node_ids else ""
            if key_cols:
                detail += f"  key_column(s): {key_cols}"
            if db:
                detail += f"  db: {db}"
            lines.append(f"  - {name}{detail}")

        lines.append("\nDo NOT invent table names. If you need a table not listed here, the data does not exist.")
        return "\n".join(lines)
    except Exception as e:
        logger.warning("Failed to fetch table list: %s", e)
        return ""


# ─────────────────────────────────────────────
# LangGraph node
# ─────────────────────────────────────────────

def discover_schema_node(state: RCAState) -> dict[str, Any]:
    """
    LangGraph node: Discover KG schema with LLM-powered pre-filtering.
    Falls back to full unfiltered schema if LLM selection fails.
    """
    try:
        # Use refined_query if available, otherwise user_query
        query = state.get("refined_query") or state.get("user_query", "")

        # Step 1: Fetch all nodes (lightweight)
        all_nodes = neo4j_tool.get_all_nodes()

        # Step 2: LLM selects relevant nodes
        relevant_ids = _select_relevant_nodes(query, all_nodes) if query else None

        # Step 3: Build schema with two-tier filtering
        schema = neo4j_tool.get_schema(relevant_ids=relevant_ids)
        table_list = _fetch_table_list()
        full_schema = schema + table_list

        filter_msg = f", filtered to {len(relevant_ids)} relevant nodes" if relevant_ids else ", full (unfiltered)"
        logger.info(f"Schema discovered: {len(full_schema)} chars{filter_msg}")
        
        return {
            "kg_schema": full_schema,
            "current_phase": "traversal",
            "messages": [{
                "agent": "schema_discovery",
                "content": f"Knowledge graph schema discovered ({len(full_schema)} chars{filter_msg})",
            }],
        }
    except Exception as e:
        logger.error(f"Schema discovery failed: {e}")
        return {
            "kg_schema": f"Schema discovery failed: {e}. Write generic Cypher queries.",
            "current_phase": "traversal",
            "errors": [f"Schema discovery error: {e}"],
            "messages": [{
                "agent": "schema_discovery",
                "content": f"Schema discovery failed: {e}",
            }],
        }
