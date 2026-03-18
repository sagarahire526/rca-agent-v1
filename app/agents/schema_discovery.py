"""
Schema Discovery node — Discovers the Neo4j knowledge graph schema
once at the start of each RCA investigation run.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from models.state import RCAState
from tools.neo4j_tool import neo4j_tool
from tools.bkg_tool import BKGTool

logger = logging.getLogger(__name__)


def _fetch_table_list() -> str:
    try:
        bkg = BKGTool()
        result = bkg.query({"mode": "schema"})
        tables = result.get("tables", [])
        if not tables:
            return ""
        lines = ["\n\n=== AVAILABLE POSTGRESQL TABLES ==="]
        lines.append("Use ONLY these table names with get_table_schema() and in SQL queries:\n")
        for t in tables:
            name = t.get("table_name", "")
            db = t.get("database_name", "")
            nodes = t.get("nodes", [])
            node_ids = ", ".join(n.get("node_id", "") for n in nodes)
            key_cols = ", ".join(
                n.get("key_column", "") for n in nodes if n.get("key_column")
            )
            detail = f"  nodes: {node_ids}" if node_ids else ""
            if db:
                detail += f"  database: {db}"
            if key_cols:
                detail += f"  key_columns: {key_cols}"
            lines.append(f"  - {name}{detail}")
        lines.append("\nDo NOT invent table names. If you need a table not listed here, the data does not exist.")
        return "\n".join(lines)
    except Exception as e:
        logger.warning("Failed to fetch table list: %s", e)
        return ""


def discover_schema_node(state: RCAState) -> dict[str, Any]:
    """
    LangGraph node: Discover KG schema + available PostgreSQL tables.
    """
    try:
        schema = neo4j_tool.get_schema()
        table_list = _fetch_table_list()
        full_schema = schema + table_list
        logger.info(f"Schema discovered: {len(full_schema)} chars (incl. table list)")

        return {
            "kg_schema": full_schema,
            "current_phase": "traversal",
            "messages": [{
                "agent": "schema_discovery",
                "content": f"Knowledge graph schema discovered ({len(full_schema)} chars)",
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
