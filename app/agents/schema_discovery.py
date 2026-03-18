"""
Schema Discovery node — Discovers the Neo4j knowledge graph schema
once at the start of each RCA investigation run.

The schema discovery includes all BKGNode instances by entity_type
and the complete node-to-node relationship map, so no separate table
list fetch is needed.
"""
from __future__ import annotations

import logging
from typing import Any

from models.state import RCAState
from tools.neo4j_tool import neo4j_tool

logger = logging.getLogger(__name__)


def discover_schema_node(state: RCAState) -> dict[str, Any]:
    """
    LangGraph node: Discover KG schema including all BKGNodes and relationships.
    """
    try:
        schema = neo4j_tool.get_schema()
        logger.info(f"Schema discovered: {len(schema)} chars")

        return {
            "kg_schema": schema,
            "current_phase": "traversal",
            "messages": [{
                "agent": "schema_discovery",
                "content": f"Knowledge graph schema discovered ({len(schema)} chars)",
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
