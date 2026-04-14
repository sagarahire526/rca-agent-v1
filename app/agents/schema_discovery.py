"""
Schema Discovery node — Discovers the Neo4j knowledge graph schema
once at the start of each RCA investigation run.

Fetches the full unfiltered schema from Neo4j and appends the
PostgreSQL table list.
"""
from __future__ import annotations

import logging
from typing import Any

from models.state import RCAState
from tools.neo4j_tool import neo4j_tool
from tools.bkg_tool import BKGTool

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# PostgreSQL table list
# ─────────────────────────────────────────────



# ─────────────────────────────────────────────
# LangGraph node
# ─────────────────────────────────────────────

def discover_schema_node(state: RCAState) -> dict[str, Any]:
    """
    LangGraph node: Discover full KG schema (unfiltered).
    """
    try:
        schema = neo4j_tool.get_schema()
        full_schema = schema

        logger.info(f"Schema discovered: {len(full_schema)} chars (full, unfiltered)")
        
        return {
            "kg_schema": full_schema,
            "current_phase": "traversal",
            "messages": [{
                "agent": "schema_discovery",
                "content": f"Knowledge graph schema discovered ({len(full_schema)} chars, full)",
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
